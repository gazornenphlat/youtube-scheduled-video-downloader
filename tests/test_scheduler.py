"""Smart schedule rules: upload windows at any hour, learning channels, weekly safety net."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401
import ytdl_schedule as ys

BASE = dt.datetime(2026, 11, 8)


def at(h, m, days=0):
    return (BASE + dt.timedelta(days=days, hours=h, minutes=m)).astimezone().timestamp()


def stamps(times, days):
    out = []
    for d in range(days):
        for h, m in times:
            t = (BASE - dt.timedelta(days=d + 1)).replace(hour=h, minute=m)
            out.append(t.astimezone().isoformat())
    return sorted(out)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yml").write_text(
            "tasks:\n"
            "  NightOwl:\n    rss:\n      url: https://www.youtube.com/feeds/videos.xml?channel_id=UCa\n"
            "  Quiet:\n    rss:\n      url: https://www.youtube.com/feeds/videos.xml?channel_id=UCb\n"
            "  Other:\n    rss:\n      url: https://example.com/feed.xml\n", encoding="utf-8")
        self.s = ys.Scheduler(self.tmp / "config.yml", self.tmp, {}, log=lambda *a: None)
        for n, c in self.s.flexget_tasks().items():
            self.s._task(n)["cid"] = c
        self.s._task("NightOwl")["uploads"] = stamps([(23, 30)], 20)
        self.s._task("Quiet")["uploads"] = stamps([(9, 0)], 3)     # too little history
        self.s._task("Other")["uploads"] = []

    def check(self, name, ts):
        """Due check with a recent last check, so only the window rules apply."""
        self.s._task(name)["last_checked"] = ts - 3 * 3600
        return self.s.is_due(name, ts)[0]

    def test_pattern_window_at_night_and_across_midnight(self):
        self.assertTrue(self.check("NightOwl", at(23, 45)))
        self.assertTrue(self.check("NightOwl", at(2, 0, 1)))
        self.assertFalse(self.check("NightOwl", at(12, 0)))
        self.assertFalse(self.check("NightOwl", at(5, 30, 1)))

    def test_learning_and_non_youtube_wait_for_the_weekly_check(self):
        self.s._task("Quiet")["last_checked"] = at(12, 0)
        self.s._task("Other")["last_checked"] = at(12, 0)
        self.assertFalse(self.s.is_due("Quiet", at(9, 10, 3))[0])
        self.assertFalse(self.s.is_due("Other", at(9, 10, 3))[0])
        self.assertFalse(self.s.is_due("Quiet", at(12, 0, 6))[0])
        self.assertTrue(self.s.is_due("Quiet", at(12, 0, 7))[0])
        self.assertTrue(self.s.is_due("Other", at(12, 0, 7))[0])

    def test_next_due_finds_the_weekly_check(self):
        for n in self.s.state["tasks"]:
            self.s._task(n)["last_checked"] = at(12, 0)
        self.s._task("Quiet")["last_checked"] = at(12, 0)
        ts, _ = self.s.next_due(at(12, 0))
        self.assertLess(ts, at(12, 0, 7) + 3600)

    def test_busy_channel_uses_its_usual_times(self):
        self.s._task("Busy")["cid"] = "UCc"
        self.s._task("Busy")["uploads"] = stamps([(7, 0), (13, 0), (19, 0), (22, 0)], 10)
        self.assertEqual(self.s.pattern("Busy")[0], "pattern")
        self.assertTrue(self.check("Busy", at(13, 5)))
        self.assertFalse(self.check("Busy", at(11, 0)))


if __name__ == "__main__":
    unittest.main()
