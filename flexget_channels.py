"""
Shared channel logic used by the Channel Manager window (channels.py) and the
web interface (web_server.py):

  * FlexgetConfig - read/add/edit/delete channel tasks in the FlexGet config,
    editing it as text so formatting is preserved (with automatic backups)
  * lookup_channel - turn any YouTube link / @handle / ID into channel info
"""
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import yaml

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.yaml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
CHANNEL_ID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def ensure_config(path):
    """First run: create config.yaml from config.example.yaml if it exists."""
    path = Path(path)
    example = path.with_name("config.example.yaml")
    if not path.exists() and example.exists():
        shutil.copy(example, path)
    return path


# ======================================================================
#  Settings
# ======================================================================
def load_settings():
    cfg = yaml.safe_load(ensure_config(CONFIG_FILE).read_text(encoding="utf-8")) or {}
    p = cfg.get("paths") or {}
    flexget = Path(p.get("flexget_config") or Path.home() / "flexget" / "config.yml")
    if flexget.is_dir():
        # A folder was given - look for the usual FlexGet file names inside it
        for name in ("config.yml", "config.yaml"):
            if (flexget / name).is_file():
                flexget = flexget / name
                break
        else:
            raise FileNotFoundError(
                f"'{flexget}' is a folder with no config.yml in it.\n"
                "Set paths: flexget_config in config.yaml to the full path of the file.")
    if not flexget.is_file():
        raise FileNotFoundError(
            f"FlexGet config not found at {flexget}. "
            "Set paths: flexget_config in config.yaml to your FlexGet config.yml.")
    state_dir = Path(p.get("state_dir") or HERE / "data")
    lists_dir = p.get("lists_dir") or state_dir / "lists"
    return flexget, Path(lists_dir)


# ======================================================================
#  FlexGet config - text based editing
# ======================================================================
TASK_RE = re.compile(r"^  ([^\s#][^:]*):\s*$")
RSS_RE = re.compile(r"channel_id=([0-9A-Za-z_-]+)")
EXEC_RE = re.compile(r'>>\s*"([^"]+)"')
# "echo {{url}} >>" or "echo {{url}} wav >>"
ECHO_RE = re.compile(r"(\{\{\s*url\s*\}\})(?:[ \t]+([A-Za-z0-9]+))?[ \t]*>>")


class Channel:
    def __init__(self, name, channel_id, list_path, start, end, wav=False):
        self.name = name
        self.channel_id = channel_id
        self.wav = wav
        self.list_path = list_path      # as written in the file (forward slashes)
        self.start, self.end = start, end   # line range [start, end)

    @property
    def list_name(self):
        return Path(self.list_path).stem if self.list_path else ""


class FlexgetConfig:
    def __init__(self, path: Path, lists_dir: Path):
        self.path = path
        self.lists_dir = lists_dir
        self.load()

    # ---------- read ----------
    def load(self):
        raw = self.path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
            self.encoding = "utf-8"
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
            self.encoding = "cp1252"
        self.newline = "\r\n" if b"\r\n" in raw else "\n"
        self.final_newline = raw.endswith(b"\n")
        self.lines = text.splitlines()
        self.channels = self._parse()

    def _parse(self):
        out = []
        in_tasks = False
        starts = []
        for i, line in enumerate(self.lines):
            if re.match(r"^tasks:\s*$", line):
                in_tasks = True
                continue
            if in_tasks and line and not line.startswith(" ") and not line.startswith("#"):
                starts.append((i, None))      # end of tasks section
                in_tasks = False
                continue
            m = TASK_RE.match(line) if in_tasks else None
            if m:
                starts.append((i, m.group(1).strip()))
        starts.append((len(self.lines), None))

        for (s, name), (e, _) in zip(starts, starts[1:]):
            if name is None:
                continue
            block = "\n".join(self.lines[s:e])
            cid = RSS_RE.search(block)
            lst = EXEC_RE.search(block)
            echo = ECHO_RE.search(block)
            wav = bool(echo and (echo.group(2) or "").lower() == "wav")
            out.append(Channel(name, cid.group(1) if cid else "",
                               lst.group(1) if lst else "", s, e, wav))
        return out

    def names(self):
        return {c.name.lower() for c in self.channels}

    def find_id(self, channel_id):
        return [c for c in self.channels if c.channel_id == channel_id]

    def known_lists(self):
        """List names from the config and the lists folder, deduped ignoring case."""
        seen = {}
        for c in self.channels:
            if c.list_name:
                seen.setdefault(c.list_name.lower(), c.list_name)
        try:
            for f in self.lists_dir.glob("*.txt"):
                seen.setdefault(f.stem.lower(), f.stem)
        except OSError:
            pass
        return sorted(seen.values(), key=str.lower)

    def list_path_for(self, list_name):
        """Path for a list, reusing the exact spelling already in the file if any."""
        for c in self.channels:
            if c.list_name.lower() == list_name.lower() and c.list_path:
                if Path(c.list_path).parent.as_posix().lower() == \
                        self.lists_dir.as_posix().replace("\\", "/").lower():
                    return c.list_path
        return f"{self.lists_dir.as_posix()}/{list_name}.txt".replace("\\", "/")

    # ---------- build ----------
    def block(self, name, channel_id, list_name, wav=False):
        fmt = " wav" if wav else ""
        return [
            f"  {name}:",
            "    rss:",
            f"      url: {FEED_URL.format(channel_id)}",
            "    exec:",
            f'      - echo {{{{url}}}}{fmt} >> "{self.list_path_for(list_name)}"',
            "    accept_all: yes",
        ]

    # ---------- write ----------
    def _save(self, new_lines):
        text = self.newline.join(new_lines) + (self.newline if self.final_newline else "")
        # Make sure the result is still valid YAML before touching the real file
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict) or "tasks" not in parsed:
            raise ValueError("Result is not a valid FlexGet config - nothing was saved.")

        backups = self.path.parent / "config_backups"
        backups.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(self.path, backups / f"{self.path.stem}-{stamp}{self.path.suffix}")
        for old in sorted(backups.glob(f"{self.path.stem}-*"))[:-20]:
            try:
                old.unlink()
            except OSError:
                pass

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(text.encode(self.encoding))
        os.replace(tmp, self.path)
        self.load()

    def add(self, name, channel_id, list_name, wav=False):
        self.load()
        if name.lower() in self.names():
            raise ValueError(f"A task named '{name}' already exists.")
        lines = list(self.lines)
        # insert after the last task (end of the tasks section)
        insert_at = self.channels[-1].end if self.channels else len(lines)
        while insert_at > 0 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        if not any(re.match(r"^tasks:\s*$", l) for l in lines):
            lines += ["tasks:"]
            insert_at = len(lines)
        lines[insert_at:insert_at] = self.block(name, channel_id, list_name, wav)
        self._save(lines)

    def update(self, old_name, name, channel_id, list_name, wav=None):
        self.load()
        ch = next((c for c in self.channels if c.name == old_name), None)
        if not ch:
            raise ValueError(f"'{old_name}' is no longer in the config.")
        if name.lower() != old_name.lower() and name.lower() in self.names():
            raise ValueError(f"A task named '{name}' already exists.")
        lines = list(self.lines)
        block = lines[ch.start:ch.end]
        trailing = []
        while block and block[-1].strip() == "":
            trailing.insert(0, block.pop())
        # Edit in place so any extra settings in the task are kept
        block[0] = f"  {name}:"
        block = [RSS_RE.sub(f"channel_id={channel_id}", l) for l in block]
        new_path = self.list_path_for(list_name)
        block = [EXEC_RE.sub(f'>> "{new_path}"', l) for l in block]
        if wav is not None:
            repl = r"\1 wav >>" if wav else r"\1 >>"
            block = [ECHO_RE.sub(repl, l) for l in block]
        lines[ch.start:ch.end] = block + trailing
        self._save(lines)

    def delete(self, names):
        self.load()
        targets = sorted((c for c in self.channels if c.name in names),
                         key=lambda c: c.start, reverse=True)
        lines = list(self.lines)
        for c in targets:
            end = c.end
            # keep blank lines that separate other sections
            while end > c.start and lines[end - 1].strip() == "":
                end -= 1
            del lines[c.start:end]
        self._save(lines)


# ======================================================================
#  YouTube lookup
# ======================================================================
def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+1; SOCS=CAI",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def normalize_url(text):
    text = text.strip()
    if CHANNEL_ID_RE.fullmatch(text):
        return f"https://www.youtube.com/channel/{text}"
    if text.startswith("@"):
        return f"https://www.youtube.com/{text}"
    if not text.startswith("http"):
        text = "https://" + text
    return text


def lookup_channel(text):
    """Returns dict(id, name, handle, avatar, videos[list of titles])."""
    url = normalize_url(text)
    cid = None
    m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", url)
    if m:
        cid = m.group(1)

    page = ""
    try:
        page = fetch(url)
    except Exception as e:
        if not cid:
            cid = lookup_with_ytdlp(url)
            if not cid:
                raise RuntimeError(f"Couldn't open that page: {e}")

    if not cid and page:
        for pat in (r'<meta itemprop="identifier" content="(UC[0-9A-Za-z_-]{22})"',
                    r'"externalId":"(UC[0-9A-Za-z_-]{22})"',
                    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"',
                    r'"browseId":"(UC[0-9A-Za-z_-]{22})"',
                    r'"channelId":"(UC[0-9A-Za-z_-]{22})"'):
            m = re.search(pat, page)
            if m:
                cid = m.group(1)
                break
    if not cid:
        cid = lookup_with_ytdlp(url)
    if not cid:
        raise RuntimeError("Couldn't find a channel ID on that page.")

    info = {"id": cid, "name": "", "handle": "", "avatar": "", "videos": []}
    if page:
        m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
        if m and "/watch" not in url:
            info["name"] = html.unescape(m.group(1))
        m = re.search(r'<meta property="og:image" content="([^"]*)"', page)
        if m and "/watch" not in url:
            info["avatar"] = html.unescape(m.group(1))
        m = re.search(r'"canonicalBaseUrl":"/(@[^"]+)"', page)
        if m:
            info["handle"] = m.group(1)

    # The RSS feed is what FlexGet will actually read - check it works
    try:
        feed = ET.fromstring(fetch(FEED_URL.format(cid)))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        title = feed.find("a:title", ns)
        if title is not None and title.text:
            info["name"] = info["name"] or title.text
        info["videos"] = [e.findtext("a:title", "", ns) for e in feed.findall("a:entry", ns)][:5]
        info["feed_ok"] = True
    except Exception:
        info["feed_ok"] = False
    return info


def lookup_with_ytdlp(url):
    try:
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--playlist-items", "1",
             "--print", "channel_id", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        m = CHANNEL_ID_RE.search(out.stdout)
        return m.group(0) if m else None
    except Exception:
        return None


def suggest_task_name(name):
    s = re.sub(r"[^\w]", "", name.replace(" ", ""))
    return s or "NewChannel"



def validate(task, lst):
    if not task:
        return "Enter a task name."
    if not re.fullmatch(r"[\w\-]+", task):
        return "Task names can only contain letters, numbers, - and _."
    if not lst:
        return "Choose or type a list name."
    if re.search(r'[\\/:*?"<>|]', lst):
        return 'List names can\'t contain  \\ / : * ? " < > |'
    return None


def touch_list(lists_dir: Path, name):
    try:
        lists_dir.mkdir(parents=True, exist_ok=True)
        (lists_dir / f"{name}.txt").touch(exist_ok=True)
    except OSError:
        pass




# ----------------------------------------------------------------------
#  Waiting list: URLs in lists/*.txt that ytdl hasn't downloaded yet
# ----------------------------------------------------------------------
def read_waiting(lists_dir: Path):
    """Returns [(list_path, url, format_or_'')], one row per URL per list.
    A line is 'URL' or 'URL wav'; lines starting with # or ; are ignored."""
    items = []
    if not lists_dir.is_dir():
        return items
    for f in sorted(lists_dir.glob("*.txt"), key=lambda p: p.name.lower()):
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        seen = set()
        for line in lines:
            parts = line.split()
            if not parts or parts[0].startswith(("#", ";")):
                continue
            url = parts[0]
            if url in seen:
                continue
            seen.add(url)
            fmt = parts[1].lower() if len(parts) > 1 else ""
            items.append((f, url, fmt))
    return items


def load_state_dir():
    """The state_dir from config.yaml (default: data folder next to the scripts)."""
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    return Path((cfg.get("paths") or {}).get("state_dir") or HERE / "data")


def load_titles(path):
    """Cached video titles: {url: title}."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_titles(path, titles):
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def fetch_title(url, timeout=10):
    """Video title from YouTube's public oEmbed endpoint. None if it can't be
    found (offline, private, removed, or not a single video)."""
    api = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
    try:
        data = json.loads(fetch(api, timeout=timeout))
        title = str(data.get("title") or "").strip()
        return title or None
    except Exception:
        return None


def remove_waiting(pairs):
    """Remove the given (list_path, url) URLs from their list files.
    Every line for that URL in the file is removed. Each file is rewritten
    atomically (tmp file + replace). Returns the number of lines removed."""
    by_file = {}
    for path, url in pairs:
        by_file.setdefault(Path(path), set()).add(url)
    removed = 0
    for path, urls in by_file.items():
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        kept = []
        for line in lines:
            parts = line.split()
            if parts and not parts[0].startswith(("#", ";")) and parts[0] in urls:
                removed += 1
                continue
            kept.append(line)
        if len(kept) == len(lines):
            continue
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, path)
    return removed
