"""
yt-dlp Smart Downloader - Parallel Multi-List Edition
All settings live in config.yaml (next to this script).

Usage:
    python ytdl.py                       # settings from config.yaml
    python ytdl.py -j 6                  # override simultaneous downloads
    python ytdl.py --config other.yaml   # use a different config
    python ytdl.py --update              # run yt-dlp -U first

Exit codes: 0 = all ok, 1 = some failures, 2 = config error, 3 = cookies expired
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing library: run  pip install pyyaml")

import listlock

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.yaml"

AUDIO_FORMATS = {"wav", "mp3", "flac", "m4a", "opus", "aac", "vorbis", "alac"}

# Output that means YouTube wants fresh cookies
COOKIE_ERRORS = (
    "sign in to confirm",
    "cookies are no longer valid",
    "confirm you're not a bot",
    "confirm you’re not a bot",
)

# yt-dlp prints this when --match-filter rejects a video (live / upcoming)
FILTER_SKIP = "does not pass filter"

# ffmpeg chatter that floods the log (mostly from live/HLS streams)
NOISE = ("timestamp discontinuity", "frame=", "Opening '", "Skip ('#EXT",
         "Error in the pull function", "IO error: End of file")

# Make yt-dlp (a Python program) write UTF-8 instead of the Windows codepage
UTF8_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# Our own output: UTF-8, and never crash on a character that can't be printed
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print_lock = threading.Lock()
list_lock = threading.Lock()
state_lock = threading.Lock()
cookies_bad = threading.Event()
space_low = threading.Event()     # set when the download drive is nearly full


# ---------- config ----------
class Config:
    def __init__(self, path: Path):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        p = raw.get("paths", {})
        d = raw.get("download", {})
        self.base_output = Path(p.get("base_output") or HERE / "downloads")
        self.state_dir = Path(p.get("state_dir") or HERE / "data")
        self.lists_dir = Path(p.get("lists_dir") or self.state_dir / "lists")
        self.cookies = Path(p["cookies"]) if p.get("cookies") else None
        self.node = p.get("node")

        self.workers = int(d.get("workers", 3))
        self.fragments = int(d.get("fragments", 10))
        self.min_date = str(d.get("min_date") or "")
        self.sleep_min = int(d.get("sleep_min", 5))
        self.sleep_max = int(d.get("sleep_max", 15))
        self.max_fails = int(d.get("max_fails", 3))
        self.keep_urls = bool(d.get("keep_urls", False))
        self.skip_live = bool(d.get("skip_live", True))
        self.date_in_filename = bool(d.get("date_in_filename", False))
        default_name = ("%(upload_date)s - %(channel)s - %(title)s.%(ext)s"
                        if self.date_in_filename else "%(channel)s - %(title)s.%(ext)s")
        self.filename = d.get("filename", default_name)
        self.min_free_gb = float(d.get("min_free_gb", 5))          # 0 = don't check
        self.separate_archives = bool(d.get("separate_archives", False))
        self.prefer_h264 = bool(d.get("prefer_h264", False))

        self.defaults = raw.get("defaults") or {}
        self.lists = {str(k).lower(): (v or {}) for k, v in (raw.get("lists") or {}).items()}

        self.archive = self.state_dir / "downloaded.txt"
        self.audio_archive = self.state_dir / "downloaded_audio.txt"
        self.fails_file = self.state_dir / "failures.json"
        self.failed_list = self.state_dir / "failed.txt"
        self.summary_file = self.state_dir / "last_run.json"

    def archive_for(self, fmt: str) -> Path:
        """Which 'already downloaded' record to use. Optionally audio and video
        are kept apart, so a video you already have can still be fetched as WAV."""
        if self.separate_archives and fmt != "video":
            return self.audio_archive
        return self.archive

    def for_list(self, name: str) -> dict:
        """Merged settings for one list: defaults <- per-list."""
        s = {"format": "video", "output": None, "keep": self.keep_urls,
             "filename": self.filename, "min_date": self.min_date,
             "skip_live": self.skip_live}
        s.update(self.defaults)
        s.update(self.lists.get(name.lower(), {}))
        s["format"] = str(s["format"]).lower()
        if s["format"] != "video" and s["format"] not in AUDIO_FORMATS:
            raise ValueError(f"list '{name}': unknown format '{s['format']}' "
                             f"(use video or one of {', '.join(sorted(AUDIO_FORMATS))})")
        s["output"] = Path(s["output"]) if s["output"] else self.base_output / name
        s["min_date"] = str(s["min_date"] or "")
        return s


CFG: Config = None  # set in main()


# ---------- helpers ----------
def log(tag, msg):
    with print_lock:
        try:
            print(f"[{tag}] {msg}", flush=True)
        except Exception:
            # Never let an odd character in a title crash a download
            print(f"[{tag}] {msg}".encode("ascii", "replace").decode(), flush=True)


def read_urls(listfile: Path):
    """Returns [(url, format_or_None)]. A line may be 'URL' or 'URL wav'."""
    urls, seen = [], set()
    for line in listfile.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if not parts or parts[0].startswith(("#", ";")):
            continue
        url = parts[0]
        fmt = parts[1].lower() if len(parts) > 1 else None
        if fmt and fmt != "video" and fmt not in AUDIO_FORMATS:
            fmt = None
        if url not in seen:
            seen.add(url)
            urls.append((url, fmt))
    return urls


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8"):
    """Write text to path atomically (tmp file + replace), so a crash mid-write
    can't leave a half-written/truncated file behind."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def remove_from_list(listfile: Path, url: str):
    # list_lock: threads in this process. list_lock(): other programs (web UI, Channel Manager)
    with list_lock, listlock.list_lock(listfile):
        lines = listfile.read_text(encoding="utf-8", errors="ignore").splitlines()
        kept = [l for l in lines if (l.split() or [""])[0] != url]
        atomic_write_text(listfile, "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def load_fails():
    try:
        return json.loads(CFG.fails_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_result(listfile: Path, url: str, ok: bool) -> bool:
    """Update failure count. Returns True if we've now given up on this URL."""
    with state_lock:
        fails = load_fails()
        if ok:
            changed = fails.pop(url, None) is not None
            gave_up = False
        else:
            n = fails.get(url, 0) + 1
            gave_up = n >= CFG.max_fails
            if gave_up:
                fails.pop(url, None)
                with open(CFG.failed_list, "a", encoding="utf-8") as f:
                    f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M}\t{listfile.stem}\t{url}\n")
            else:
                fails[url] = n
            changed = True
        if changed:
            atomic_write_text(CFG.fails_file, json.dumps(fails, indent=1), encoding="utf-8")
    return gave_up


def free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return float("inf")       # can't tell - don't block downloads


def build_cmd(url, s: dict, args):
    cmd = ["yt-dlp", url]
    if s["format"] == "video":
        cmd += ["-f", "bestvideo+bestaudio/best",
                "--remux-video", "mp4", "--merge-output-format", "mp4"]
        if CFG.prefer_h264:
            cmd += ["-S", "vcodec:h264"]      # prefer H.264 among the best formats
    else:
        cmd += ["-f", "bestaudio/best", "-x", "--audio-format", s["format"],
                "--audio-quality", "0"]
    cmd += [
        "-o", str(s["output"] / s["filename"]),
        "--download-archive", str(CFG.archive_for(s["format"])),
        "--no-overwrites", "--continue",
        "--concurrent-fragments", str(CFG.fragments),
        "--extractor-retries", "5", "--retry-sleep", "extractor:3",
        "--sleep-interval", str(CFG.sleep_min),
        "--max-sleep-interval", str(CFG.sleep_max),
        "--newline", "--no-colors",
    ]
    if s["skip_live"]:
        # Skip streams that are live, scheduled, or just ended and still processing
        cmd += ["--match-filter",
                "!is_live & live_status !=? is_upcoming & live_status !=? post_live"]
    if CFG.node:
        cmd += ["--js-runtimes", f"node:{CFG.node}"]
    if CFG.cookies and CFG.cookies.exists():
        cmd += ["--cookies", str(CFG.cookies)]
    min_date = args.mindate if args.mindate is not None else s["min_date"]
    if min_date:
        cmd += ["--dateafter", min_date]
    return cmd


def run_job(listfile: Path, url, s: dict, args):
    """Returns one of: ok, failed, gave_up, cookies, skipped."""
    tag = f"{listfile.stem} | {url[-30:]}"
    if cookies_bad.is_set() or space_low.is_set():
        return "skipped"
    if CFG.min_free_gb > 0 and free_gb(s["output"]) < CFG.min_free_gb:
        space_low.set()
        log(tag, f"Not enough free space on {s['output']} "
                 f"(need {CFG.min_free_gb:g} GB) - stopping downloads")
        return "skipped"

    log(tag, f"starting ({s['format']})")
    proc = subprocess.Popen(
        build_cmd(url, s, args),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=UTF8_ENV,
    )
    cookie_hit = False
    live_hit = False
    for line in proc.stdout:
        line = line.rstrip()
        if any(p in line.lower() for p in COOKIE_ERRORS):
            cookie_hit = True
        if FILTER_SKIP in line:
            live_hit = True
        # Hide progress and ffmpeg spam; keep everything else
        if line.startswith("[download]") and "%" in line and "100%" not in line:
            continue
        if any(n in line for n in NOISE):
            continue
        if line:
            log(tag, line)
    proc.wait()

    if proc.returncode == 0 and live_hit:
        # Live or upcoming - not downloaded, not archived. Try again next run.
        log(tag, "live/upcoming stream - skipped, left in list for next run")
        return "live"

    if proc.returncode == 0:
        record_result(listfile, url, ok=True)
        if s["keep"]:
            log(tag, "done")
        else:
            remove_from_list(listfile, url)
            log(tag, "done - removed from list")
        return "ok"

    if cookie_hit:
        cookies_bad.set()
        log(tag, "COOKIES EXPIRED - stopping remaining downloads")
        return "cookies"

    if record_result(listfile, url, ok=False):
        remove_from_list(listfile, url)
        log(tag, f"FAILED {CFG.max_fails} times - moved to {CFG.failed_list.name}")
        return "gave_up"

    log(tag, f"FAILED (exit {proc.returncode}) - will retry next run")
    return "failed"


def write_summary(counts, gave_up_urls):
    data = dict(counts, gave_up_urls=gave_up_urls,
                cookies_expired=cookies_bad.is_set(),
                low_space=space_low.is_set(),
                finished=f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    try:
        atomic_write_text(CFG.summary_file, json.dumps(data, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"Couldn't write summary: {e}")


def main():
    global CFG
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("-j", "--jobs", type=int, help="simultaneous downloads (overrides config)")
    p.add_argument("--mindate", help="YYYYMMDD, overrides config for all lists ('' = none)")
    p.add_argument("--update", action="store_true", help="run yt-dlp -U first")
    args = p.parse_args()

    from flexget_channels import ensure_config
    ensure_config(args.config)
    try:
        CFG = Config(args.config)
    except FileNotFoundError:
        print(f"Config not found: {args.config}")
        return 2
    except yaml.YAMLError as e:
        print(f"Config file has a mistake:\n{e}")
        return 2

    if args.update:
        subprocess.run(["yt-dlp", "-U"])

    workers = args.jobs or CFG.workers
    CFG.base_output.mkdir(parents=True, exist_ok=True)
    CFG.lists_dir.mkdir(parents=True, exist_ok=True)
    CFG.state_dir.mkdir(parents=True, exist_ok=True)

    counts = {"ok": 0, "failed": 0, "gave_up": 0, "cookies": 0, "skipped": 0, "live": 0}
    gave_up_urls = []

    lists = sorted(CFG.lists_dir.glob("*.txt"))
    jobs = []
    for lf in lists:
        try:
            s = CFG.for_list(lf.stem)
        except ValueError as e:
            print(f"Config error: {e}")
            return 2
        urls = read_urls(lf)
        if urls:
            s["output"].mkdir(parents=True, exist_ok=True)
            n_override = sum(1 for _, f in urls if f and f != s["format"])
            extra = f" ({n_override} as other formats)" if n_override else ""
            print(f"  {lf.stem}: {len(urls)} URL(s) -> {s['format']}{extra} in {s['output']}")
        for url, fmt in urls:
            js = dict(s, format=fmt) if fmt else s   # per-URL format from the list line
            jobs.append((lf, url, js))

    if not jobs:
        print(f"Nothing to download in: {CFG.lists_dir}")
        write_summary(counts, gave_up_urls)
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to exit...")
        return 0

    print(f"\n{len(jobs)} URL(s) from {len(lists)} list(s). Running {workers} at a time.\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_job, *j, args): j[1] for j in jobs}
        for f in as_completed(futures):
            result = f.result()
            counts[result] += 1
            if result == "gave_up":
                gave_up_urls.append(futures[f])

    write_summary(counts, gave_up_urls)

    print("=" * 42)
    print(f"Done. {counts['ok']} ok, {counts['failed']} failed, "
          f"{counts['gave_up']} given up, {counts['live']} live (later), "
          f"{counts['skipped']} skipped.")
    if cookies_bad.is_set():
        print("COOKIES EXPIRED - export a fresh cookies.txt and run again.")
    if space_low.is_set():
        print("LOW DISK SPACE - free up space, then run again. Remaining downloads were skipped.")
    print("=" * 42)

    if cookies_bad.is_set():
        return 3
    return 1 if counts["failed"] or counts["gave_up"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
