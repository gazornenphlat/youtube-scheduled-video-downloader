"""
Edit config.yaml as text, so the owner's comments, order and layout survive.

Only the lines for the changed settings are touched. Every save:
  * checks the new file still parses as YAML and that each value reads back
    as what was asked for,
  * keeps a timestamped copy in config_backups\\ (the last 20 are kept),
  * replaces the file atomically.
"""
import datetime as dt
import os
import re
from pathlib import Path

import yaml

# (path, label, type, help, options)
#   type: int | float | bool | time_range | ymd
SCHEMA = [
    ("Downloads", [
        ("download.workers", "Downloads at the same time", "int",
         "3 to 5 is best. More can trigger YouTube's limits.", {"min": 1, "max": 8}),
        ("download.max_fails", "Give up on a video after", "int",
         "Failed runs before a video is moved to Failed.", {"min": 1, "max": 20}),
        ("download.min_free_gb", "Keep at least this many GB free", "float",
         "Downloads stop when the drive has less free. 0 turns the check off.", {"min": 0, "max": 10000}),
        ("download.min_date", "Skip videos uploaded before", "ymd",
         "Date as YYYYMMDD. Leave blank for no limit.", {}),
        ("download.skip_live", "Skip live streams until they end", "bool", "", {}),
        ("download.prefer_h264", "Prefer H.264 video", "bool",
         "Plays on more devices. Files can be a little larger.", {}),
        ("download.date_in_filename", "Put the upload date in filenames", "bool",
         "Only for new downloads. Example: 20260101 - Channel - Title.mp4", {}),
        ("download.separate_archives", "Keep audio and video records apart", "bool",
         "A video you already have can still be downloaded as WAV.", {}),
    ]),
    ("Checking channels", [
        ("tray.quiet_hours", "Quiet hours", "time_range",
         "No checks or downloads in this time, e.g. 23:00-07:00. Blank = off.", {}),
        ("tray.interval_minutes", "Minutes between checks (smart schedule off)", "int",
         "Only used when the smart schedule is turned off.", {"min": 5, "max": 1440}),
        ("tray.smart_schedule.max_gap_hours", "Check every channel at least every (hours)", "int",
         "168 = once a week.", {"min": 1, "max": 720}),
        ("tray.smart_schedule.active_minutes", "Check every (minutes) during an upload window", "int",
         "", {"min": 5, "max": 120}),
    ]),
]

DEFAULTS = {
    "download.workers": 3, "download.max_fails": 3, "download.min_free_gb": 5,
    "download.min_date": "", "download.skip_live": True, "download.prefer_h264": False,
    "download.date_in_filename": False, "download.separate_archives": False,
    "tray.quiet_hours": "", "tray.interval_minutes": 120,
    "tray.smart_schedule.max_gap_hours": 168, "tray.smart_schedule.active_minutes": 30,
}
FIELDS = {path: (label, typ, help_, opts)
          for _, items in SCHEMA for path, label, typ, help_, opts in items}


class SettingsError(ValueError):
    pass


# ----------------------------------------------------------------- reading
def read_values(config_path):
    """Current value of every setting (defaults where the file doesn't say)."""
    try:
        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    out = {}
    for path in FIELDS:
        node, found = data, True
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                found = False
                break
        out[path] = node if found else DEFAULTS[path]
    return out


# ----------------------------------------------------------------- checking
def check(path, value):
    """Returns the cleaned value, or raises SettingsError with a plain message."""
    label, typ, _, opts = FIELDS[path]
    if typ == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise SettingsError(f"{label}: choose on or off")
    if typ in ("int", "float"):
        try:
            v = int(value) if typ == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{label}: enter a number")
        if "min" in opts and v < opts["min"] or "max" in opts and v > opts["max"]:
            raise SettingsError(f"{label}: must be between {opts.get('min')} and {opts.get('max')}")
        return v
    text = "" if value is None else str(value).strip()
    if typ == "ymd" and text and not re.fullmatch(r"\d{8}", text):
        raise SettingsError(f"{label}: use YYYYMMDD, for example 20260101")
    if typ == "time_range" and text and not re.fullmatch(
            r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", text):
        raise SettingsError(f"{label}: use HH:MM-HH:MM, for example 23:00-07:00")
    return text


def literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# ----------------------------------------------------------------- text edits
_KEY_RE = r"^(?P<ind>[ \t]*){key}[ \t]*:(?P<rest>[^\r\n]*?)(?P<eol>\r?\n)?$"


def _is_content(line):
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _indent(line):
    return len(line) - len(line.lstrip(" \t"))


def _find(lines, start, end, indent, key):
    pat = re.compile(_KEY_RE.format(key=re.escape(key)))
    for i in range(start, end):
        if _is_content(lines[i]) and _indent(lines[i]) == indent:
            m = pat.match(lines[i])
            if m:
                return i
    return None


def _block_end(lines, header, indent):
    j = header + 1
    while j < len(lines):
        if _is_content(lines[j]) and _indent(lines[j]) <= indent:
            break
        j += 1
    return j


def _child_indent(lines, header, end, parent_indent):
    for i in range(header + 1, end):
        if _is_content(lines[i]):
            return _indent(lines[i]) if _indent(lines[i]) > parent_indent else parent_indent + 2
    return parent_indent + 2


def set_value(text, path, value):
    """Returns new file text with one setting changed. Other lines are untouched."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    parts = path.split(".")
    start, end, indent = 0, len(lines), 0
    for depth, part in enumerate(parts[:-1]):
        header = _find(lines, start, end, indent, part)
        if header is None:                      # section missing: add it at the end
            tail = "".join(f"{'  ' * d}{p}:{eol}" for d, p in enumerate(parts[depth:-1]))
            tail += "  " * (len(parts) - 1) + f"{parts[-1]}: {literal(value)}{eol}"
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += eol               # only now add the missing final newline
            return "".join(lines) + tail
        start, end = header + 1, _block_end(lines, header, indent)
        indent = _child_indent(lines, header, end, indent)
    key = parts[-1]
    found = _find(lines, start, end, indent, key)
    if found is not None:
        m = re.match(_KEY_RE.format(key=re.escape(key)), lines[found])
        rest = m.group("rest")
        comment = ""
        if "#" in rest:                        # keep the user's comment and spacing before it
            before = rest[:rest.index("#")]
            spacing = before[len(before.rstrip()):] or " "
            comment = spacing + rest[rest.index("#"):].rstrip()
        eol_now = m.group("eol") or eol
        lines[found] = f"{m.group('ind')}{key}: {literal(value)}{comment}{eol_now}"
    else:
        last = end - 1
        while last > start and not _is_content(lines[last]):
            last -= 1
        pos = last + 1
        if pos > 0 and not lines[pos - 1].endswith(("\n", "\r")):
            lines[pos - 1] += eol
        lines.insert(pos, f"{' ' * indent}{key}: {literal(value)}{eol}")
    return "".join(lines)


# ----------------------------------------------------------------- saving
def save(config_path, changes, backup_dir=None, keep=20):
    """changes: {path: raw value from the form}. Returns the list of saved paths."""
    config_path = Path(config_path)
    clean = {}
    errors = []
    for path, raw in changes.items():
        if path not in FIELDS:
            errors.append(f"Unknown setting: {path}")
            continue
        try:
            clean[path] = check(path, raw)
        except SettingsError as e:
            errors.append(str(e))
    if errors:
        raise SettingsError("; ".join(errors))
    if not clean:
        return []

    raw_text = config_path.read_bytes().decode("utf-8") if config_path.exists() else ""
    current = read_values(config_path) if raw_text else {}
    clean = {p: v for p, v in clean.items() if current.get(p) != v or p not in current}
    text = raw_text
    for path, value in clean.items():
        text = set_value(text, path, value)
    if text == raw_text:
        return []

    # Check before writing: valid YAML, and every value reads back as set
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise SettingsError(f"Not saved - the change would break the file: {e}")
    for path, value in clean.items():
        node = data
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node != value:
            raise SettingsError(f"Not saved - {path} did not read back correctly.")

    # Backup, then atomic replace
    if config_path.exists():
        bdir = Path(backup_dir) if backup_dir else config_path.parent / "config_backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        (bdir / f"config-{stamp}.yaml").write_bytes(raw_text.encode("utf-8"))
        for old in sorted(bdir.glob("config-*.yaml"))[:-keep]:
            old.unlink(missing_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, config_path)
    return sorted(clean)


def describe(config_path):
    """Groups of settings with their current values, for the web page."""
    values = read_values(config_path)
    groups = []
    for title, items in SCHEMA:
        rows = []
        for path, label, typ, help_, opts in items:
            value = values[path]
            rows.append({"path": path, "label": label, "type": typ, "help": help_,
                         "value": "" if value is None else value, "min": opts.get("min"), "max": opts.get("max")})
        groups.append({"title": title, "items": rows})
    return groups
