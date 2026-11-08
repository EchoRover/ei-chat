"""End-to-end tests against a real server on a free port. Stdlib only:
    python3 -m unittest discover tests
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_server(data_dir, **env):
    os.environ["EI_CHAT_DATA"] = data_dir
    os.environ["EI_CHAT_QUIET"] = "1"
    os.environ.pop("EI_CHAT_TOKEN", None)
    os.environ.pop("EI_CHAT_TARGET", None)
    os.environ.update(env)
    import server
    return importlib.reload(server)


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = load_server(self.tmp)
        self.s.MESSAGES = self.s._read_json(self.s.STORE, [])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.s.Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.api = f"{self.base}/api/{self.s.TOKEN}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def req(self, path, body=None, key=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.api + path, data=data)
        r.add_header("Content-Type", "application/json")
        if key:
            r.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_token_is_random_and_persisted(self):
        self.assertTrue(len(self.s.TOKEN) >= 8)
        with open(os.path.join(self.tmp, "token")) as f:
            self.assertEqual(f.read().strip(), self.s.TOKEN)

    def test_page_needs_token(self):
        with urllib.request.urlopen(f"{self.base}/chat/{self.s.TOKEN}") as r:
            page = r.read().decode()
        self.assertIn(f"/api/{self.s.TOKEN}", page)
        self.assertNotIn("__TOKEN__", page)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"{self.base}/chat/wrong")
        self.assertEqual(cm.exception.code, 404)

    def test_send_and_read(self):
        code, m = self.req("/send", {"name": "ann", "text": "hi"})
        self.assertEqual((code, m["id"], m["verified"]), (200, 1, False))
        self.req("/send", {"name": "bob", "text": "yo"})
        code, msgs = self.req("/messages?since=1")
        self.assertEqual([x["text"] for x in msgs], ["yo"])

    def test_rejects_bad_input(self):
        self.assertEqual(self.req("/send", {"name": "ann", "text": "  "})[0], 400)
        self.assertEqual(self.req("/send", {"name": "", "text": "x"})[0], 400)
        self.assertEqual(self.req("/send", {"name": "System", "text": "x"})[0], 403)
        self.assertEqual(self.req("/messages?since=abc")[0], 400)

    def test_keyed_name_needs_its_key(self):
        self.s._write_json(self.s.KEYS_FILE, {"sekrit": "Alice"})
        code, body = self.req("/send", {"name": "Alice", "text": "fake"})
        self.assertEqual(code, 403)
        self.assertIn("key", body["error"])
        code, m = self.req("/send", {"name": "whatever", "text": "real"}, key="sekrit")
        self.assertEqual((code, m["name"], m["verified"]), (200, "Alice", True))
        self.assertEqual(self.req("/send", {"name": "x", "text": "y"}, key="nope")[0], 403)

    def test_long_poll_returns_on_new_message(self):
        got = {}

        def waiter():
            t0 = time.time()
            got["msgs"] = self.req("/messages?since=0&wait=10")[1]
            got["secs"] = time.time() - t0

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.3)
        self.req("/send", {"name": "ann", "text": "ping"})
        t.join(5)
        self.assertEqual([m["text"] for m in got["msgs"]], ["ping"])
        self.assertLess(got["secs"], 5)

    def test_ids_continue_after_restart(self):
        self.req("/send", {"name": "ann", "text": "one"})
        self.req("/send", {"name": "ann", "text": "two"})
        self.s.MESSAGES = self.s._read_json(self.s.STORE, [])
        code, m = self.req("/send", {"name": "ann", "text": "three"})
        self.assertEqual(m["id"], 3)

    def test_presence(self):
        self.req("/heartbeat", {"name": "ann"})
        _, people = self.req("/participants")
        self.assertEqual([(p["name"], p["online"]) for p in people], [("ann", True)])

    def test_key_cli_replaces_old_key(self):
        self.s.cmd_key("Alice")
        self.s.cmd_key("Alice")
        self.assertEqual(list(self.s.load_keys().values()), ["Alice"])
        self.s.cmd_revoke("Alice")
        self.assertEqual(self.s.load_keys(), {})


if __name__ == "__main__":
    unittest.main()
