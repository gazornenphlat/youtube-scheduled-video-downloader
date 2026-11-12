"""Settings page: text-preserving edits to config.yaml, checks and the web API."""
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

import yaml

from tests import helpers  # noqa: F401
import config_edit as ce
from tests.test_web_api import free_port

SAMPLE = (
    "download:\r\n  workers: 3   # how many at once\r\n  max_fails: 3\r\n\r\n"
    "# tray section\r\ntray:\r\n  interval_minutes: 120\r\n  smart_schedule:\r\n"
    "    max_gap_hours: 72  # weekly max\r\n    active_minutes: 30"
)


class ConfigEditTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg = self.dir / "config.yaml"
        self.cfg.write_bytes(SAMPLE.encode("utf-8"))

    def test_edit_keeps_comments_and_crlf(self):
        ce.save(self.cfg, {"download.workers": "5", "tray.smart_schedule.max_gap_hours": "168"})
        new = self.cfg.read_bytes()
        self.assertEqual(new.count(b"\n"), new.count(b"\r\n"))
        self.assertIn(b"workers: 5   # how many at once", new)
        self.assertIn(b"max_gap_hours: 168  # weekly max", new)
        data = yaml.safe_load(new)
        self.assertEqual(data["download"]["workers"], 5)
        self.assertEqual(data["tray"]["smart_schedule"]["max_gap_hours"], 168)

    def test_same_values_leave_file_byte_identical(self):
        ce.save(self.cfg, {"download.workers": "3"})
        self.assertEqual(self.cfg.read_bytes(), SAMPLE.encode("utf-8"))

    def test_adds_missing_setting_and_section(self):
        ce.save(self.cfg, {"tray.quiet_hours": "23:00-07:00", "download.skip_live": "false"})
        data = yaml.safe_load(self.cfg.read_bytes())
        self.assertEqual(data["tray"]["quiet_hours"], "23:00-07:00")
        self.assertIs(data["download"]["skip_live"], False)

    def test_bad_values_refused_and_file_untouched(self):
        for bad in ({"download.workers": "99"}, {"tray.quiet_hours": "late"},
                    {"download.min_date": "2026-01-01"}, {"nope.x": "1"}):
            with self.assertRaises(ce.SettingsError):
                ce.save(self.cfg, bad)
        self.assertEqual(self.cfg.read_bytes(), SAMPLE.encode("utf-8"))

    def test_backup_made_before_change(self):
        ce.save(self.cfg, {"download.workers": "4"})
        backups = list((self.dir / "config_backups").glob("config-*.yaml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), SAMPLE.encode("utf-8"))

    def test_describe_gives_current_values(self):
        groups = ce.describe(self.cfg)
        values = {it["path"]: it["value"] for g in groups for it in g["items"]}
        self.assertEqual(values["download.workers"], 3)
        self.assertEqual(values["tray.smart_schedule.max_gap_hours"], 72)
        self.assertEqual(values["tray.quiet_hours"], "")


class SettingsApiTests(unittest.TestCase):
    def get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Host": f"127.0.0.1:{self.port}"})
        return json.load(urllib.request.urlopen(req, timeout=10))

    def post(self, path, body, header=True):
        h = {"Host": f"127.0.0.1:{self.port}", "Origin": self.base, "Content-Type": "application/json"}
        if header:
            h["X-YTDL"] = "1"
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        return json.load(urllib.request.urlopen(req, timeout=10))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.yaml"
        self.cfg.write_bytes(SAMPLE.encode("utf-8"))
        self.lists = self.tmp / "lists"; self.lists.mkdir()
        self.state = self.tmp / "state"; self.state.mkdir()
        self.port = free_port()
        import web_server

        class Runner:
            link_opts = {}
            config_file = self.cfg
        self.srv, _ = web_server.start(Runner(), {"host": "127.0.0.1", "port": self.port},
                                       self.state, self.lists, self.tmp / "tray.log",
                                       log=lambda *a: None)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_get_and_save_settings(self):
        groups = self.get("/api/settings")["groups"]
        self.assertTrue(any(it["path"] == "download.workers" for g in groups for it in g["items"]))
        res = self.post("/api/settings", {"values": {"download.workers": "5"}})
        self.assertEqual(res["saved"], ["download.workers"])
        self.assertIn(b"workers: 5", self.cfg.read_bytes())

    def test_bad_save_returns_error(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/settings", {"values": {"download.workers": "99"}})
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(self.cfg.read_bytes(), SAMPLE.encode("utf-8"))

    def test_save_needs_header(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.post("/api/settings", {"values": {"download.workers": "4"}}, header=False)


if __name__ == "__main__":
    unittest.main()
