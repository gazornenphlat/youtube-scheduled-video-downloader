"""
Web interface for ytdl - a small JSON API plus a single-page front end
(web/index.html). Runs inside the tray app on a background thread using only
the Python standard library.

Security:
  * Listens on 127.0.0.1 by default (this PC only).
  * Requests whose Host header isn't this server are refused (blocks DNS rebinding).
  * Anything that changes something needs the X-YTDL header and a same-origin
    request, so other websites can't trigger actions through your browser.
  * host: 0.0.0.0 makes it reachable from your network; a password is optional.
"""
import base64
import datetime as dt
import hmac
import json
import os
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import flexget_channels as fc

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"


class WebApp:
    """Glue between HTTP requests and the running tray app (runner)."""

    def __init__(self, runner, state_dir: Path, lists_dir: Path, logfile: Path, log=print):
        self.runner = runner
        self.state_dir = Path(state_dir)
        self.lists_dir = Path(lists_dir)
        self.logfile = Path(logfile)
        self.log = log
        self.lock = threading.Lock()      # serialise FlexGet config edits
        # Video titles for the Waiting page, looked up in the background
        self.titles_file = self.state_dir / "titles.json"
        self.titles = fc.load_titles(self.titles_file)
        self.title_lock = threading.Lock()
        self.title_queue = []             # urls waiting for a title
        self.title_retry = {}             # url -> time to try again (after a failure)
        self.title_busy = False           # is the lookup thread running?

    # ---------- helpers ----------
    def cfg(self):
        path, lists_dir = fc.load_settings()
        return fc.FlexgetConfig(path, lists_dir)

    def read_json(self, name):
        try:
            return json.loads((self.state_dir / name).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def queue(self):
        out = {}
        try:
            for f in sorted(self.lists_dir.glob("*.txt"), key=lambda p: p.stem.lower()):
                n = sum(1 for l in f.read_text(encoding="utf-8", errors="ignore").splitlines()
                        if l.strip() and not l.lstrip().startswith(("#", ";")))
                out[f.stem] = n
        except OSError:
            pass
        return out

    def failed_rows(self):
        f = self.state_dir / "failed.txt"
        rows = []
        try:
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    rows.append({"when": parts[0], "list": parts[1], "url": parts[2].strip()})
        except OSError:
            pass
        return rows

    def write_failed(self, rows):
        f = self.state_dir / "failed.txt"
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text("".join(f"{r['when']}\t{r['list']}\t{r['url']}\n" for r in rows),
                       encoding="utf-8")
        os.replace(tmp, f)

    # ---------- API ----------
    def status(self):
        r = self.runner
        q = self.queue()
        last = self.read_json("last_run.json")
        return {
            "status": r.status,
            "state": "paused" if r.paused.is_set() else getattr(r, "icon_state", "idle"),
            "paused": r.paused.is_set(),
            "cookie_problem": getattr(r, "cookie_problem", False),
            "next_run": getattr(r, "next_run", None),
            "next_count": getattr(r, "next_count", None),
            "last_check": getattr(r, "last_flexget", None),
            "interval_minutes": getattr(r, "interval_minutes", 120),
            "smart": bool(getattr(r, "sched", None)),
            "queue": q,
            "queue_total": sum(q.values()),
            "failed": len(self.failed_rows()),
            "last_run": last,
            "now": dt.datetime.now().timestamp(),
        }

    def channels(self):
        cfg = self.cfg()
        sched = getattr(self.runner, "sched", None)
        lists_dir = cfg.lists_dir.as_posix().replace("\\", "/").lower()
        out = []
        for c in sorted(cfg.channels, key=lambda c: c.name.lower()):
            problems = []
            if c.list_path and Path(c.list_path).parent.as_posix().lower() != lists_dir:
                problems.append("List is outside the lists folder - ytdl won't download it")
            if c.channel_id and len(cfg.find_id(c.channel_id)) > 1:
                problems.append("Duplicate channel")
            if not c.channel_id:
                problems.append("No channel ID")
            item = {"name": c.name, "list": c.list_name, "id": c.channel_id,
                    "wav": c.wav, "list_path": c.list_path, "problems": problems}
            if sched:
                try:
                    item["schedule"] = sched.describe(c.name)
                except Exception:
                    pass
            out.append(item)
        return {"channels": out, "lists": cfg.known_lists(), "config": str(cfg.path)}

    def lookup(self, q):
        info = fc.lookup_channel(q)
        cfg = self.cfg()
        info["duplicates"] = [{"name": d.name, "list": d.list_name}
                              for d in cfg.find_id(info["id"])]
        base = fc.suggest_task_name(info["name"] or info.get("handle", "").lstrip("@"))
        cand, n, names = base, 2, cfg.names()
        while cand.lower() in names:
            cand, n = f"{base}{n}", n + 1
        info["suggested_name"] = cand
        return info

    def add_channel(self, body):
        name, cid = body.get("name", "").strip(), body.get("id", "").strip()
        lst, wav = body.get("list", "").strip(), bool(body.get("wav"))
        err = fc.validate(name, lst)
        if not err and not fc.CHANNEL_ID_RE.fullmatch(cid):
            err = "That doesn't look like a channel ID."
        if err:
            raise ValueError(err)
        with self.lock:
            cfg = self.cfg()
            new_list = lst.lower() not in {l.lower() for l in cfg.known_lists()}
            cfg.add(name, cid, lst, wav)
            if new_list:
                fc.touch_list(cfg.lists_dir, lst)
        self.log(f"Web: added channel {name} -> {lst}{' (wav)' if wav else ''}")
        return {"ok": True, "new_list": new_list}

    def update_channel(self, old, body):
        name, cid = body.get("name", "").strip(), body.get("id", "").strip()
        lst, wav = body.get("list", "").strip(), bool(body.get("wav"))
        err = fc.validate(name, lst)
        if not err and not fc.CHANNEL_ID_RE.fullmatch(cid):
            err = "The channel ID should start with UC and be 24 characters long."
        if err:
            raise ValueError(err)
        with self.lock:
            cfg = self.cfg()
            new_list = lst.lower() not in {l.lower() for l in cfg.known_lists()}
            cfg.update(old, name, cid, lst, wav)
            if new_list:
                fc.touch_list(cfg.lists_dir, lst)
        self.log(f"Web: edited channel {old}")
        return {"ok": True}

    def delete_channels(self, body):
        names = set(body.get("names") or [])
        if not names:
            raise ValueError("Nothing selected.")
        with self.lock:
            self.cfg().delete(names)
        self.log(f"Web: deleted {len(names)} channel(s): {', '.join(sorted(names))}")
        return {"ok": True, "deleted": len(names)}

    def schedule(self):
        sched = getattr(self.runner, "sched", None)
        if not sched:
            return {"enabled": False, "channels": []}
        rows = []
        for name in sorted(sched.state["tasks"], key=str.lower):
            d = sched.describe(name)
            d["name"] = name
            rows.append(d)
        return {"enabled": True, "channels": rows,
                "next_run": getattr(self.runner, "next_run", None),
                "next_count": getattr(self.runner, "next_count", None)}

    def retry_failed(self, body):
        urls = set(body.get("urls") or [])
        rows = self.failed_rows()
        keep, moved = [], 0
        for r in rows:
            if r["url"] in urls:
                lf = self.lists_dir / f"{r['list']}.txt"
                with open(lf, "a", encoding="utf-8") as f:
                    f.write(r["url"] + "\n")
                moved += 1
            else:
                keep.append(r)
        self.write_failed(keep)
        self.log(f"Web: re-queued {moved} failed URL(s)")
        return {"ok": True, "requeued": moved}

    def waiting_rows(self):
        items = fc.read_waiting(self.lists_dir)
        self.queue_titles([u for _, u, _ in items])
        with self.title_lock:
            rows = [{"list": Path(p).stem, "url": u, "format": f, "title": self.titles.get(u)}
                    for p, u, f in items]
            pending = self.title_busy
        return {"rows": rows, "pending": pending}

    def queue_titles(self, urls):
        """Queue titles that aren't known yet and start the lookup thread if idle."""
        now = time.time()
        with self.title_lock:
            for u in urls:
                if u in self.titles or u in self.title_queue or self.title_retry.get(u, 0) > now:
                    continue
                self.title_queue.append(u)
            if self.title_queue and not self.title_busy:
                self.title_busy = True
                threading.Thread(target=self._title_worker, daemon=True, name="titles").start()

    def _title_worker(self):
        while True:
            with self.title_lock:
                if not self.title_queue:
                    self.title_busy = False
                    break
                url = self.title_queue.pop(0)
            title = fc.fetch_title(url)
            with self.title_lock:
                if title:
                    self.titles[url] = title
                else:
                    self.title_retry[url] = time.time() + 600   # try again in 10 minutes
                if not self.title_queue:
                    fc.save_titles(self.titles_file, self.titles)

    def remove_waiting(self, body):
        # Only lists that exist in the lists folder are accepted (no path tricks).
        names = {f.stem: f for f in self.lists_dir.glob("*.txt")} if self.lists_dir.is_dir() else {}
        pairs = []
        for it in body.get("items") or []:
            lst, url = str(it.get("list", "")), str(it.get("url", "")).strip()
            if lst in names and url:
                pairs.append((names[lst], url))
        return {"ok": True, "removed": fc.remove_waiting(pairs)}

    def remove_failed(self, body):
        urls = set(body.get("urls") or [])
        rows = self.failed_rows()
        keep = [r for r in rows if r["url"] not in urls]
        self.write_failed(keep)
        return {"ok": True, "removed": len(rows) - len(keep)}

    def log_tail(self, lines=300):
        try:
            with open(self.logfile, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 400_000))
                text = f.read().decode("utf-8", "replace")
            return {"lines": text.splitlines()[-lines:]}
        except OSError as e:
            return {"lines": [f"(can't read log: {e})"]}


def make_handler(app: WebApp, host: str, port: int, password: str):
    allowed_hosts = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
    lan = host not in ("127.0.0.1", "localhost", "::1")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ytdl-web"

        def log_message(self, fmt, *args):     # keep the tray log quiet
            pass

        # ---------- plumbing ----------
        def send_json(self, obj, code=200):
            data = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def guard(self, changing=False):
            hosthdr = (self.headers.get("Host") or "").lower()
            if not lan and hosthdr not in allowed_hosts:
                self.send_json({"error": "Bad host"}, 403)
                return False
            if password:
                auth = self.headers.get("Authorization", "")
                ok = False
                if auth.startswith("Basic "):
                    try:
                        _, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
                        ok = hmac.compare_digest(pw, password)
                    except Exception:
                        ok = False
                if not ok:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="ytdl"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
            if changing:
                origin = self.headers.get("Origin")
                if self.headers.get("X-YTDL") != "1" or (
                        origin and urlparse(origin).netloc.lower() != hosthdr):
                    self.send_json({"error": "Blocked cross-site request"}, 403)
                    return False
            return True

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("Request too large")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def handle_api(self, method):
            url = urlparse(self.path)
            path, q = url.path, parse_qs(url.query)
            r = app.runner
            try:
                if method == "GET":
                    if path == "/api/status":
                        return self.send_json(app.status())
                    if path == "/api/channels":
                        return self.send_json(app.channels())
                    if path == "/api/lookup":
                        return self.send_json(app.lookup((q.get("q") or [""])[0]))
                    if path == "/api/schedule":
                        return self.send_json(app.schedule())
                    if path == "/api/failed":
                        return self.send_json({"rows": app.failed_rows()})
                    if path == "/api/waiting":
                        return self.send_json(app.waiting_rows())
                    if path == "/api/log":
                        n = int((q.get("lines") or ["300"])[0])
                        return self.send_json(app.log_tail(min(max(n, 10), 3000)))
                elif method == "POST":
                    b = self.body()
                    if path == "/api/run-now":
                        r.api_run_now()
                        return self.send_json({"ok": True})
                    if path == "/api/pause":
                        r.api_pause()
                        return self.send_json({"ok": True})
                    if path == "/api/resume":
                        r.api_resume()
                        return self.send_json({"ok": True})
                    if path == "/api/channels":
                        return self.send_json(app.add_channel(b))
                    if path == "/api/channels/delete":
                        return self.send_json(app.delete_channels(b))
                    if path == "/api/failed/retry":
                        return self.send_json(app.retry_failed(b))
                    if path == "/api/failed/remove":
                        return self.send_json(app.remove_failed(b))
                    if path == "/api/waiting/remove":
                        return self.send_json(app.remove_waiting(b))
                elif method == "PUT":
                    m = re.fullmatch(r"/api/channels/(.+)", path)
                    if m:
                        return self.send_json(app.update_channel(unquote(m.group(1)), self.body()))
                self.send_json({"error": "Not found"}, 404)
            except (ValueError, FileNotFoundError, RuntimeError) as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                app.log("Web API error:\n" + traceback.format_exc())
                self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        # ---------- verbs ----------
        def do_GET(self):
            if not self.guard():
                return
            path = urlparse(self.path).path
            if path.startswith("/api/"):
                return self.handle_api("GET")
            if path in ("/", "/index.html"):
                f = WEB_DIR / "index.html"
                try:
                    data = f.read_bytes()
                except OSError:
                    return self.send_json({"error": f"Missing {f}"}, 500)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_json({"error": "Not found"}, 404)

        def do_POST(self):
            if self.guard(changing=True):
                self.handle_api("POST")

        def do_PUT(self):
            if self.guard(changing=True):
                self.handle_api("PUT")

    return Handler


def start(runner, opts: dict, state_dir, lists_dir, logfile, log=print):
    """Start the web server on a daemon thread. Returns (server, url) or (None, error)."""
    host = str(opts.get("host") or "127.0.0.1")
    port = int(opts.get("port") or 8765)
    password = str(opts.get("password") or "")
    if host not in ("127.0.0.1", "localhost", "::1") and not password:
        log("Web interface is open to your whole network with no password")
    app = WebApp(runner, state_dir, lists_dir, logfile, log)
    try:
        srv = ThreadingHTTPServer((host, port), make_handler(app, host, port, password))
    except OSError as e:
        return None, f"Couldn't start web interface on port {port}: {e}"
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    return srv, f"http://{shown}:{port}/"
