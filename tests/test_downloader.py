"""Downloader settings: archives, filename default, H.264 and the command it builds."""
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from tests import helpers  # noqa: F401
import ytdl


def make_config(extra_download=""):
    tmp = Path(tempfile.mkdtemp())
    cfg = tmp / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  base_output: '{tmp / 'out'}'\n"
        f"  state_dir: '{tmp / 'state'}'\n"
        f"  lists_dir: '{tmp / 'lists'}'\n"
        "download:\n" + (extra_download or "  workers: 3\n"), encoding="utf-8")
    return ytdl.Config(cfg), tmp


class DownloaderSettingsTests(unittest.TestCase):
    def test_default_filename_has_no_date(self):
        cfg, _ = make_config()
        self.assertEqual(cfg.filename, "%(channel)s - %(title)s.%(ext)s")

    def test_date_in_filename(self):
        cfg, _ = make_config("  date_in_filename: true\n")
        self.assertTrue(cfg.filename.startswith("%(upload_date)s"))

    def test_shared_archive_by_default(self):
        cfg, _ = make_config()
        self.assertEqual(cfg.archive_for("video"), cfg.archive_for("wav"))

    def test_separate_archives(self):
        cfg, _ = make_config("  separate_archives: true\n")
        self.assertNotEqual(cfg.archive_for("video"), cfg.archive_for("wav"))
        self.assertEqual(cfg.archive_for("video"), cfg.archive)

    def test_h264_option_only_for_video(self):
        cfg, _ = make_config("  prefer_h264: true\n")
        ytdl.CFG = cfg
        s = cfg.for_list("music")
        video = ytdl.build_cmd("https://youtu.be/A", dict(s, format="video"), Namespace(mindate=None))
        audio = ytdl.build_cmd("https://youtu.be/A", dict(s, format="wav"), Namespace(mindate=None))
        self.assertIn("vcodec:h264", video)
        self.assertNotIn("vcodec:h264", audio)

    def test_free_space_check_never_blocks_when_unknown(self):
        self.assertEqual(ytdl.free_gb(Path("/no/such/folder/here")), float("inf"))


if __name__ == "__main__":
    unittest.main()
