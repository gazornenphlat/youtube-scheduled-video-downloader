"""Web API: waiting list, title fallback and the Test a link checks (no network)."""
import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from tests import helpers  # noqa: F401
import web_server


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lists = self.tmp / "lists"; self.lists.mkdir()
        self.state = self.tmp / "state"; self.state.mkdir()
        (self.lists / "Music.txt").write_text("https://youtu.be/AAA\nhttps://youtu.be/BBB\n")
        self.port = free_port()

        class Runner:
            link_opts = {}
        self.srv, _ = web_server.start(Runner(), {"host": "127.0.0.1", "port": self.port},
                                       self.state, self.lists, self.tmp / "tray.log",
                                       log=lambda *a: None)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path):
        req = urllib.request.Request(self.base + path,
                                     headers={"Host": f"127.0.0.1:{self.port}"})
        return json.load(urllib.request.urlopen(req, timeout=10))

    def post(self, path, body, header=True):
        h = {"Host": f"127.0.0.1:{self.port}", "Origin": self.base,
             "Content-Type": "application/json"}
        if header:
            h["X-YTDL"] = "1"
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        return json.load(urllib.request.urlopen(req, timeout=10))

    def test_waiting_list_and_remove(self):
        rows = self.get("/api/waiting")["rows"]
        self.assertEqual([r["url"] for r in rows], ["https://youtu.be/AAA", "https://youtu.be/BBB"])
        res = self.post("/api/waiting/remove",
                        {"items": [{"list": "../state/x", "url": "https://youtu.be/AAA"},
                                   {"list": "Music", "url": "https://youtu.be/AAA"}]})
        self.assertEqual(res["removed"], 1)
        self.assertEqual((self.lists / "Music.txt").read_text(), "https://youtu.be/BBB\n")

    def test_post_needs_the_ytdl_header(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.post("/api/waiting/remove", {"items": []}, header=False)

    def test_test_link_rejects_non_links(self):
        res = self.post("/api/test-link", {"url": "not a link"})
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
