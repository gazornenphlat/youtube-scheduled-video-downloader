"""Waiting-list reading, removing and the cross-process list lock."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401  (sets sys.path)
import flexget_channels as fc
import listlock


class WaitingListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lists = self.tmp / "lists"
        self.lists.mkdir()
        (self.lists / "Music.txt").write_text(
            "# comment\nhttps://youtu.be/AAA\nhttps://youtu.be/BBB wav\n; note\n"
            "https://youtu.be/CCC\nhttps://youtu.be/AAA\n", encoding="utf-8")
        (self.lists / "News.txt").write_text("https://youtu.be/DDD\nhttps://youtu.be/EEE \n",
                                             encoding="utf-8")

    def test_read_skips_comments_and_duplicates(self):
        rows = fc.read_waiting(self.lists)
        urls = [(p.stem, u, f) for p, u, f in rows]
        self.assertEqual(len(rows), 5)                       # AAA is counted once in Music
        self.assertIn(("Music", "https://youtu.be/BBB", "wav"), urls)
        self.assertNotIn("#", "".join(u for _, u, _ in urls))

    def test_remove_keeps_other_lines_byte_for_byte(self):
        n = fc.remove_waiting([(self.lists / "Music.txt", "https://youtu.be/AAA"),
                               (self.lists / "News.txt", "https://youtu.be/EEE")])
        self.assertEqual(n, 3)
        self.assertEqual((self.lists / "Music.txt").read_text(encoding="utf-8"),
                         "# comment\nhttps://youtu.be/BBB wav\n; note\nhttps://youtu.be/CCC\n")
        self.assertEqual((self.lists / "News.txt").read_text(encoding="utf-8"),
                         "https://youtu.be/DDD\n")
        self.assertEqual(list(self.lists.glob("*.tmp")), [])

    def test_remove_unknown_url_changes_nothing(self):
        before = (self.lists / "Music.txt").read_bytes()
        self.assertEqual(fc.remove_waiting([(self.lists / "Music.txt", "https://nope")]), 0)
        self.assertEqual((self.lists / "Music.txt").read_bytes(), before)


class ListLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.list = self.tmp / "Music.txt"
        self.list.write_text("x\n")

    def test_lock_file_is_removed_after_use(self):
        with listlock.list_lock(self.list):
            self.assertTrue((self.tmp / "Music.lock").exists())
        self.assertFalse((self.tmp / "Music.lock").exists())

    def test_stale_lock_is_taken_over(self):
        lock = self.tmp / "Music.lock"
        lock.write_text("1234")
        old = time.time() - listlock.STALE_SECONDS - 5
        os.utime(lock, (old, old))
        with listlock.list_lock(self.list):
            pass
        self.assertFalse(lock.exists())

    def test_busy_lock_times_out(self):
        (self.tmp / "Music.lock").write_text("someone else")
        saved = listlock.WAIT_SECONDS
        listlock.WAIT_SECONDS = 0.3
        try:
            with self.assertRaises(TimeoutError):
                with listlock.list_lock(self.list):
                    pass
        finally:
            listlock.WAIT_SECONDS = saved


if __name__ == "__main__":
    unittest.main()
