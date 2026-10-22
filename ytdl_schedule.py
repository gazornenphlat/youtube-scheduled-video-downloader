"""
Smart scheduler - learns when each channel usually uploads and only asks
FlexGet to check a channel around those times.

How it learns:
  Each channel's YouTube RSS feed lists its ~15 latest videos with publish
  times. Those are refreshed once a day and kept (up to 60 per channel), so
  the history grows over time.

When a channel is due:
  * Never checked, or not checked for max_gap_hours        -> due (safety net)
  * Too little history, or it uploads very often           -> every interval_minutes
  * Otherwise: inside a window that starts a little before one of its usual
    upload times of day and lasts window_hours            -> every active_minutes
    (if it only ever uploads on certain weekdays, the window only opens on those days)
"""
import datetime as dt
import json
import re
import statistics
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ytdl-scheduler"
NS = {"a": "http://www.w3.org/2005/Atom"}
CID_RE = re.compile(r"channel_id=([0-9A-Za-z_-]+)")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class Scheduler:
    def __init__(self, flexget_config: Path, state_dir: Path, opts: dict, log=print):
        self.flexget_config = Path(flexget_config)
        self.state_file = Path(state_dir) / "schedule.json"
        self.report_file = Path(state_dir) / "schedule_report.txt"
        self.log = log
        self.interval = float(opts.get("interval_minutes", 120)) * 60
        self.active = float(opts.get("active_minutes", 30)) * 60
        self.window = float(opts.get("window_hours", 3)) * 3600
        self.lead = float(opts.get("lead_minutes", 15)) * 60
        self.max_gap = float(opts.get("max_gap_hours", 24)) * 3600
        self.refresh_every = float(opts.get("refresh_hours", 24)) * 3600
        self.min_uploads = int(opts.get("min_uploads", 4))
        self.busy_gap = float(opts.get("busy_channel_hours", 8)) * 3600
        self.state = self._load()

    # ---------- state ----------
    def _load(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {"tasks": {}, "last_refresh": 0}

    def save(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def _task(self, name):
        return self.state["tasks"].setdefault(name, {"uploads": [], "last_checked": 0})

    # ---------- FlexGet tasks ----------
    def flexget_tasks(self):
        """{task name: channel_id or None} from the FlexGet config."""
        path = self.flexget_config
        if path.is_dir():
            path = path / "config.yml"
        cfg = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        out = {}
        for name, task in (cfg.get("tasks") or {}).items():
            cid = None
            rss = (task or {}).get("rss") if isinstance(task, dict) else None
            url = rss.get("url") if isinstance(rss, dict) else rss
            if isinstance(url, str):
                m = CID_RE.search(url)
                cid = m.group(1) if m else None
            out[str(name)] = cid
        return out

    # ---------- learning ----------
    def refresh(self, force=False):
        """Re-read the FlexGet config and update upload history from RSS feeds."""
        tasks = self.flexget_tasks()
        # forget tasks that were deleted from FlexGet
        for gone in set(self.state["tasks"]) - set(tasks):
            del self.state["tasks"][gone]

        stale = force or time_now() - self.state.get("last_refresh", 0) >= self.refresh_every
        todo = [(n, c) for n, c in tasks.items() if c and
                (stale or not self._task(n)["uploads"] or self._task(n).get("cid") != c)]
        for n, c in tasks.items():
            self._task(n)["cid"] = c
        if not todo:
            self.save()
            return 0

        self.log(f"Scheduler: reading upload history for {len(todo)} channel(s)")

        def fetch(item):
            name, cid = item
            try:
                req = urllib.request.Request(FEED_URL.format(cid), headers={"User-Agent": UA})
                root = ET.fromstring(urllib.request.urlopen(req, timeout=20).read())
                return name, [e.findtext("a:published", "", NS) for e in root.findall("a:entry", NS)]
            except Exception:
                return name, None

        ok = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            for name, stamps in pool.map(fetch, todo):
                if stamps is None:
                    continue
                t = self._task(name)
                merged = sorted(set(t["uploads"]) | {s for s in stamps if s})
                t["uploads"] = merged[-60:]
                ok += 1
        if stale:
            self.state["last_refresh"] = time_now()
        self.save()
        self.write_report()
        self.log(f"Scheduler: updated {ok}/{len(todo)} channel(s)")
        return ok

    # ---------- patterns ----------
    def pattern(self, name):
        """Returns (kind, minutes_of_day list, weekdays set or None). Cached."""
        cache = self.__dict__.setdefault("_pcache", {})
        key = (name, len(self._task(name)["uploads"]),
               (self._task(name)["uploads"] or [""])[-1])
        if key not in cache:
            cache[key] = self._pattern(name)
        return cache[key]

    def _pattern(self, name):
        ups = [parse_ts(s) for s in self._task(name)["uploads"]]
        ups = sorted(u for u in ups if u)
        if len(ups) < self.min_uploads:
            return "unknown", [], None
        gaps = [(b - a).total_seconds() for a, b in zip(ups, ups[1:])]
        if gaps and statistics.median(gaps) < self.busy_gap:
            return "busy", [], None
        recent = ups[-30:]
        minutes = sorted({u.hour * 60 + u.minute for u in recent})
        days = {u.weekday() for u in recent}
        weekdays = days if len(recent) >= 8 and len(days) <= 4 else None
        return "pattern", minutes, weekdays

    def in_window(self, name, when: dt.datetime):
        kind, minutes, weekdays = self.pattern(name)
        if kind != "pattern":
            return False
        now_min = when.hour * 60 + when.minute
        lead, win = self.lead / 60, self.window / 60
        for m in minutes:
            # minutes since (upload time - lead), wrapping past midnight
            delta = (now_min - (m - lead)) % 1440
            if delta <= lead + win:
                start_day = (when - dt.timedelta(minutes=delta)).weekday()
                if weekdays is None or start_day in weekdays:
                    return True
        return False

    def is_due(self, name, now_ts):
        t = self._task(name)
        since = now_ts - t.get("last_checked", 0)
        if since >= self.max_gap:
            return True, "safety check"
        if not t.get("cid"):
            return since >= self.interval, "not YouTube"
        kind, _, _ = self.pattern(name)
        if kind in ("unknown", "busy"):
            return since >= self.interval, kind
        if self.in_window(name, dt.datetime.fromtimestamp(now_ts)):
            return since >= self.active, "upload window"
        return False, "outside window"

    def due(self, now_ts=None):
        now_ts = now_ts or time_now()
        names = self.flexget_tasks()
        for n, cid in names.items():
            self._task(n)["cid"] = cid
        return [n for n in names if self.is_due(n, now_ts)[0]]

    def mark_checked(self, names, now_ts=None):
        now_ts = now_ts or time_now()
        for n in names:
            self._task(n)["last_checked"] = now_ts
        self.save()

    def next_due(self, now_ts=None, horizon_hours=24, step_minutes=5):
        """Earliest time (timestamp) any task becomes due, and how many."""
        now_ts = now_ts or time_now()
        names = list(self.state["tasks"])
        for i in range(1, int(horizon_hours * 60 / step_minutes) + 1):
            ts = now_ts + i * step_minutes * 60
            n = sum(1 for name in names if self.is_due(name, ts)[0])
            if n:
                return ts, n
        return now_ts + horizon_hours * 3600, 0

    def describe(self, name, now_ts=None):
        """Plain-language summary of one channel's schedule (for the web UI)."""
        now_ts = now_ts or time_now()
        kind, minutes, weekdays = self.pattern(name)
        t = self._task(name)
        due, reason = self.is_due(name, now_ts)
        if kind == "pattern":
            times = ", ".join(f"{m // 60:02d}:{m % 60:02d}" for m in cluster(minutes))
            days = " ".join(DAYS[d] for d in sorted(weekdays)) if weekdays else "Any day"
            summary = f"{days} · around {times}"
        elif kind == "busy":
            summary = f"Uploads very often · every {self.interval / 60:g} min"
        elif not t.get("cid"):
            summary = f"Not a YouTube feed · every {self.interval / 60:g} min"
        else:
            summary = f"Learning · every {self.interval / 60:g} min"
        return {"kind": kind, "summary": summary, "uploads": len(t["uploads"]),
                "last_checked": t.get("last_checked") or None,
                "last_upload": (t["uploads"] or [None])[-1],
                "due": due, "reason": reason,
                "in_window": kind == "pattern" and self.in_window(
                    name, dt.datetime.fromtimestamp(now_ts))}

    # ---------- report ----------
    def write_report(self):
        lines = [f"Channel upload schedule (learned {dt.datetime.now():%Y-%m-%d %H:%M})",
                 "Times are local. Checked from a little before each time until "
                 f"{self.window / 3600:g}h after.", ""]
        for name in sorted(self.state["tasks"], key=str.lower):
            kind, minutes, weekdays = self.pattern(name)
            n = len(self._task(name)["uploads"])
            if kind == "pattern":
                times = ", ".join(f"{m // 60:02d}:{m % 60:02d}" for m in cluster(minutes))
                days = " ".join(DAYS[d] for d in sorted(weekdays)) if weekdays else "any day"
                desc = f"{days} around {times}"
            elif kind == "busy":
                desc = f"uploads very often - checked every {self.interval / 60:g} min"
            else:
                desc = f"not enough history yet - checked every {self.interval / 60:g} min"
            lines.append(f"{name:<28} {desc}   ({n} uploads seen)")
        try:
            self.report_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass


# ---------- helpers ----------
def time_now():
    return dt.datetime.now().timestamp()


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def cluster(minutes, gap=45):
    """Collapse nearby times (for the report) - returns one time per cluster."""
    out = []
    for m in sorted(minutes):
        if not out or m - out[-1][-1] > gap:
            out.append([m])
        else:
            out[-1].append(m)
    return [c[0] for c in out]
