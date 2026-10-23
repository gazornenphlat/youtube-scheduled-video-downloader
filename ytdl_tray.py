"""
FlexGet + ytdl system tray runner

Loop:
  1. update yt-dlp (once a day)
  2. run FlexGet (builds the lists)
  3. run ytdl.py (downloads them)
  4. notify with the results
  5. wait until FLEXGET_INTERVAL minutes have passed since FlexGet last started
  6. repeat

If YouTube cookies expire, it pauses, turns the icon yellow and notifies you.
Export a fresh cookies.txt, then click Resume.

Setup:
    pip install pystray pillow
Run hidden (no console window):
    pythonw ytdl_tray.py
"""
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def fatal(msg):
    """Show an error even when running hidden with pythonw, then exit."""
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "ytdl tray error", 0x10)
    print(msg)
    if sys.stdin and sys.stdin.isatty():
        input("Press Enter to exit...")
    sys.exit(1)


try:
    import pystray
    import yaml
    from PIL import Image, ImageDraw
except ImportError as e:
    fatal(f"Missing library: {e.name}\n\nOpen a command prompt and run:\n"
          f"    pip install pystray pillow pyyaml")

# ===== Settings (from config.yaml) =====
HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.yaml"
PYTHON = sys.executable.replace("pythonw.exe", "python.exe")

try:
    _cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
except FileNotFoundError:
    fatal(f"Config not found:\n{CONFIG_FILE}\n\nPut config.yaml next to ytdl_tray.py.")
except yaml.YAMLError as e:
    fatal(f"config.yaml has a mistake:\n\n{e}")

_tray = _cfg.get("tray") or {}
_paths = _cfg.get("paths") or {}
FLEXGET_CMD = _tray.get("flexget_cmd", ["flexget", "execute"])
UPDATE_CMD = [PYTHON if c == "python" else c
              for c in _tray.get("update_cmd", ["yt-dlp", "-U"])]
FLEXGET_INTERVAL = float(_tray.get("interval_minutes", 120))
LOG_MAX_MB = float(_tray.get("log_max_mb", 5))
LOG_BACKUPS = int(_tray.get("log_backups", 3))
STATE_DIR = Path(_paths.get("state_dir", r"T:\sync\Movies\yt-dlp"))
LOGFILE = STATE_DIR / "tray.log"
YTDL_CMD = [PYTHON, str(HERE / "ytdl.py"), "--config", str(CONFIG_FILE)]
LISTS_DIR = Path(_paths.get("lists_dir") or STATE_DIR / "lists")
FLEXGET_CONFIG = Path(_paths.get("flexget_config") or Path.home() / "flexget" / "config.yml")
_sched_cfg = _tray.get("smart_schedule", True)
SMART = _sched_cfg is not False
SCHED_OPTS = dict(_sched_cfg) if isinstance(_sched_cfg, dict) else {}
SCHED_OPTS.setdefault("interval_minutes", FLEXGET_INTERVAL)
TASKS_PER_CALL = 40     # keep the FlexGet command line short
WEB_OPTS = _cfg.get("web") or {}
WEB_ENABLED = WEB_OPTS.get("enabled", True) is not False
# ====================

SUMMARY_FILE = STATE_DIR / "last_run.json"     # written by ytdl.py
TRAY_STATE = STATE_DIR / "tray_state.json"     # remembers last yt-dlp update
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

logger = logging.getLogger("ytdl_tray")
logger.setLevel(logging.INFO)
try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _h = RotatingFileHandler(LOGFILE, maxBytes=int(LOG_MAX_MB * 1024 * 1024),
                             backupCount=LOG_BACKUPS, encoding="utf-8")
except Exception as e:
    fatal(f"Can't write the log file:\n{LOGFILE}\n\n{e}\n\n"
          f"Check state_dir in config.yaml.")
_h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(_h)
log = logger.info


# ---------- single instance ----------
_mutex = None


def already_running() -> bool:
    global _mutex
    if os.name != "nt":
        return False
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = k32.CreateMutexW(None, False, "Local\\ytdl_tray_single_instance")
    return ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS


def message_box(text, title="ytdl"):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


# ---------- tray icon ----------
COLORS = {"idle": (60, 140, 220), "busy": (220, 60, 50), "warn": (230, 180, 30)}


def make_icon(state: str):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=COLORS[state])
    d.polygon([(25, 18), (25, 46), (47, 32)], fill="white")  # play triangle
    return img


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


class Runner:
    def __init__(self):
        self.status = "Starting"
        self.last_flexget = None
        self.cookie_problem = False
        self.paused = threading.Event()
        self.wake = threading.Event()   # set to skip the wait ("Run now")
        self.stopping = threading.Event()
        self.proc = None
        self.icon = None
        self.force_all = True     # first run (and "Run now") checks every channel
        self.icon_state = "idle"
        self.next_run = None
        self.next_count = None
        self.interval_minutes = FLEXGET_INTERVAL
        self.web_url = None
        self.sched = None
        if SMART:
            try:
                from ytdl_schedule import Scheduler
                self.sched = Scheduler(FLEXGET_CONFIG, STATE_DIR, SCHED_OPTS, log)
                self.sched.flexget_tasks()          # fail now if the config can't be read
            except Exception as e:
                log(f"Smart schedule off - {e}")
                self.sched = None

    # ---------- helpers ----------
    def lists_have_urls(self):
        try:
            for f in LISTS_DIR.glob("*.txt"):
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.strip() and not line.lstrip().startswith(("#", ";")):
                        return True
        except OSError:
            return True     # can't tell - let ytdl decide
        return False

    def run_flexget(self):
        """Runs FlexGet for the channels that are due. Returns how many were checked."""
        if not self.sched:
            self.set_status("Running FlexGet (all channels)", "busy")
            self.run(FLEXGET_CMD, "flexget")
            return -1
        try:
            if self.sched.refresh():
                self.set_status("Learned channel schedules", "busy")
        except Exception as e:
            log(f"Scheduler refresh failed: {e}")
        try:
            names = list(self.sched.flexget_tasks())
            due = names if self.force_all else self.sched.due()
        except Exception as e:
            log(f"Scheduler error, checking everything: {e}")
            names, due = None, None
        self.force_all = False
        if due is None or (names and len(due) == len(names)):
            self.set_status("Running FlexGet (all channels)", "busy")
            self.run(FLEXGET_CMD, "flexget")
        elif due:
            self.set_status(f"Running FlexGet ({len(due)} channel(s) due)", "busy")
            log("Due: " + ", ".join(due))
            for i in range(0, len(due), TASKS_PER_CALL):
                if self.stopping.is_set():
                    break
                self.run(FLEXGET_CMD + ["--tasks"] + due[i:i + TASKS_PER_CALL], "flexget")
        else:
            log("No channels due")
            return 0
        if due:
            self.sched.mark_checked(due)
        return len(due) if due else -1
    def set_status(self, text, state=None):
        self.status = text
        if state:
            self.icon_state = state
        log(text)
        if self.icon:
            self.icon.title = f"ytdl: {text}"[:127]
            if state:
                self.icon.icon = make_icon(state)
            self.icon.update_menu()

    def notify(self, msg, title="ytdl"):
        log(f"NOTIFY: {title} - {msg}")
        try:
            self.icon.notify(msg, title)
        except Exception:
            pass

    def run(self, cmd, name):
        log(f"--- {name}: {' '.join(cmd)}")
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=HERE, creationflags=NO_WINDOW,
                text=True, encoding="utf-8", errors="replace",
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
            )
        except FileNotFoundError:
            log(f"--- {name}: command not found: {cmd[0]}")
            self.notify(f"Couldn't find {cmd[0]}", f"{name} failed")
            return -1
        for line in self.proc.stdout:
            line = line.rstrip()
            if line:
                log(f"  {line}")
        code = self.proc.wait()
        self.proc = None
        log(f"--- {name} exited with {code}")
        return code

    def maybe_update_ytdlp(self):
        state = load_json(TRAY_STATE)
        today = dt.date.today().isoformat()
        if state.get("last_update") == today:
            return
        self.set_status("Updating yt-dlp", "busy")
        self.run(UPDATE_CMD, "yt-dlp update")
        state["last_update"] = today
        tmp = TRAY_STATE.with_suffix(TRAY_STATE.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, TRAY_STATE)

    def report(self, code):
        s = load_json(SUMMARY_FILE)
        if code == 3 or s.get("cookies_expired"):
            self.cookie_problem = True
            self.paused.set()
            self.set_status("PAUSED - cookies expired", "warn")
            self.notify("Export a fresh cookies.txt, then right-click the tray icon "
                        "and choose Resume.", "YouTube cookies expired")
            return
        ok, failed, gave_up = s.get("ok", 0), s.get("failed", 0), s.get("gave_up", 0)
        if ok or failed or gave_up:
            parts = [f"{ok} downloaded"]
            if failed:
                parts.append(f"{failed} failed (will retry)")
            if gave_up:
                parts.append(f"{gave_up} given up - see failed.txt")
            self.notify(", ".join(parts), "Downloads finished")

    # ---------- main loop ----------
    def loop(self):
        while not self.stopping.is_set():
            while self.paused.is_set() and not self.stopping.is_set():
                time.sleep(1)
            if self.stopping.is_set():
                break

            self.maybe_update_ytdlp()
            if self.stopping.is_set():
                break

            self.last_flexget = time.time()
            self.run_flexget()
            if self.stopping.is_set():
                break

            if self.lists_have_urls():
                self.set_status("Downloading (ytdl)", "busy")
                code = self.run(YTDL_CMD, "ytdl")
                if self.stopping.is_set():
                    break
                self.report(code)
                if self.paused.is_set():
                    continue    # cookies problem - wait for Resume at the top

            self.wake.clear()
            if self.sched:
                next_run, n = self.sched.next_due()
                self.next_count = n
                self.set_status(f"Idle - next check {dt.datetime.fromtimestamp(next_run):%a %H:%M}"
                                f" ({n} channel{'s' if n != 1 else ''})", "idle")
            else:
                next_run = self.last_flexget + FLEXGET_INTERVAL * 60
                self.next_count = None
                self.set_status(f"Idle - next FlexGet at "
                                f"{dt.datetime.fromtimestamp(next_run):%H:%M}", "idle")
            self.next_run = next_run
            while time.time() < next_run and not self.stopping.is_set():
                if self.wake.wait(timeout=5):
                    log("Run now requested")
                    break

    # ---------- menu actions ----------
    def run_now(self, icon, item):
        self.force_all = True
        self.cookie_problem = False
        self.paused.clear()
        self.wake.set()

    def toggle_pause(self, icon, item):
        if self.paused.is_set():
            self.cookie_problem = False
            self.paused.clear()
            self.set_status("Resumed", "idle")
        else:
            self.paused.set()
            self.set_status("Paused (current step will finish first)", "idle")

    # ---------- used by the web interface ----------
    def api_run_now(self):
        self.run_now(None, None)

    def api_pause(self):
        if not self.paused.is_set():
            self.toggle_pause(None, None)

    def api_resume(self):
        if self.paused.is_set():
            self.toggle_pause(None, None)

    def open_web(self, icon, item):
        if self.web_url:
            import webbrowser
            webbrowser.open(self.web_url)
        else:
            self.notify("The web interface isn't running - see tray.log", "ytdl")

    def open_channels(self, icon, item):
        script = HERE / "channels.py"
        if not script.exists():
            self.notify(f"channels.py not found in {HERE}", "Can't open Channel Manager")
            return
        log("Opening Channel Manager")

        def launch():
            try:
                p = subprocess.Popen(
                    [PYTHON, str(script)], cwd=HERE, creationflags=NO_WINDOW,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace")
                err = p.communicate()[1]
                if p.returncode:
                    log(f"Channel Manager exited with {p.returncode}:\n{err}")
                    last = (err.strip().splitlines() or ["unknown error"])[-1]
                    self.notify(f"{last[:200]}\nDetails in tray.log",
                                "Channel Manager crashed")
            except Exception as e:
                log(f"Couldn't start Channel Manager: {e}")
                self.notify(str(e), "Can't open Channel Manager")

        threading.Thread(target=launch, daemon=True).start()

    def open_log(self, icon, item):
        if os.name == "nt":
            os.startfile(LOGFILE)

    def open_schedule(self, icon, item):
        f = STATE_DIR / "schedule_report.txt"
        if not f.exists():
            if self.sched:
                self.sched.write_report()
            else:
                f.write_text("Smart schedule is off.\n", encoding="utf-8")
        if os.name == "nt":
            os.startfile(f)

    def open_failed(self, icon, item):
        f = STATE_DIR / "failed.txt"
        if not f.exists():
            f.write_text("", encoding="utf-8")
        if os.name == "nt":
            os.startfile(f)

    def quit(self, icon, item):
        self.stopping.set()
        self.wake.set()
        if self.proc and self.proc.poll() is None:
            log("Quit: stopping running process")
            self.proc.terminate()
        log("Exiting")
        icon.stop()


def main():
    if already_running():
        message_box("ytdl tray is already running - check your system tray.")
        return

    log("=" * 40)
    log("Tray app started")
    r = Runner()
    if WEB_ENABLED:
        import web_server
        srv, msg = web_server.start(r, WEB_OPTS, STATE_DIR, LISTS_DIR, LOGFILE, log)
        if srv:
            r.web_url = msg
            log(f"Web interface: {msg}")
        else:
            log(f"Web interface off - {msg}")
    menu = pystray.Menu(
        pystray.MenuItem(lambda item: r.status, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open web interface", r.open_web, default=bool(r.web_url)),
        pystray.MenuItem("Manage channels", r.open_channels, default=not r.web_url),
        pystray.MenuItem("Run now", r.run_now),
        pystray.MenuItem(lambda item: "Resume" if r.paused.is_set() else "Pause",
                         r.toggle_pause),
        pystray.MenuItem("Open log", r.open_log),
        pystray.MenuItem("Open failed list", r.open_failed),
        pystray.MenuItem("View channel schedule", r.open_schedule),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", r.quit),
    )
    r.icon = pystray.Icon("ytdl", make_icon("idle"), "ytdl", menu)
    threading.Thread(target=r.loop, daemon=True).start()
    r.icon.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        err = traceback.format_exc()
        crash = HERE / "tray_crash.txt"
        try:
            crash.write_text(err, encoding="utf-8")
        except Exception:
            pass
        message_box(f"ytdl tray crashed:\n\n{err}\n\nSaved to {crash}", "ytdl tray error")
