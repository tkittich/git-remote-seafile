"""fable-C1 (review 68e81e5): a server's ``Retry-After`` is slept for at most a few seconds.

urllib3 honours the header on 429, 500 and 503 with no bound the client sets; one ``list_dir``
against ``Retry-After: 4`` took 12 s and a ``delete_entry`` 24 s, on the thread that renews the push
lease.  The cap is patched small here; the point is that 600 s is not slept.
"""

from __future__ import annotations

import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from git_remote_seafile import client as client_module
from git_remote_seafile.client import SeafileAPIError, SeafileClient


class _Throttle(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits += 1
        body = b'{"detail":"Request was throttled."}'
        self.send_response(429)
        self.send_header("Retry-After", "600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestRetryAfterIsCapped(unittest.TestCase):
    def test_a_long_retry_after_is_not_slept_through_and_is_said_once(self):
        _Throttle.hits = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Throttle)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = SeafileClient(server_url=f"http://127.0.0.1:{server.server_address[1]}", token="t")
        err = io.StringIO()
        slept: list[float] = []
        # Recorded, not slept: with the cap gone this test used to sleep 600 s a retry (AA36).
        with patch.object(client_module, "RETRY_AFTER_CAP_SECONDS", 0.2), \
                patch.object(client_module, "_RETRY_AFTER_NOTED", False), \
                patch("urllib3.util.retry.time.sleep", side_effect=slept.append), \
                patch("sys.stderr", err):
            with self.assertRaises(SeafileAPIError):
                client.list_dir("rid", "/repo/refs/heads")
            with self.assertRaises(SeafileAPIError):
                client.list_dir("rid", "/repo/refs/heads")
        self.assertTrue(slept, "no retry was waited for")
        self.assertLess(max(slept), 5.0, f"the 600 s Retry-After was slept through: {slept}")
        self.assertGreaterEqual(_Throttle.hits, 4, "the request was not retried")
        self.assertEqual(err.getvalue().count("asked this client to slow down"), 1, err.getvalue())


class _DeleteThrottle(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    answers: list[int] = []
    hits = 0

    def log_message(self, *args):
        pass

    def do_DELETE(self):
        type(self).hits += 1
        status = type(self).answers.pop(0) if type(self).answers else 200
        body = b"{}"
        self.send_response(status)
        if status == 429:
            self.send_header("Retry-After", "nan")      # whatever the server says is bounded
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestADeleteIsSentAgainAfterAThrottle(unittest.TestCase):
    """fable-E1 (AA44): the session never re-sends a DELETE, so the first 429 of a payload
    delete ended the run as "the server refused"; a 429 is the limiter refusing before it acted."""

    def _client(self, answers):
        _DeleteThrottle.answers = list(answers)
        _DeleteThrottle.hits = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _DeleteThrottle)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = SeafileClient(server_url=f"http://127.0.0.1:{server.server_address[1]}", token="t")
        self.addCleanup(client.close)
        return client

    def test_a_429_is_waited_out_and_the_delete_goes_through(self):
        client = self._client([429, 429, 200])
        slept: list[float] = []
        with patch.object(client_module.time, "sleep", side_effect=slept.append):
            self.assertTrue(client.delete_entry("rid", "/r/objects/ab/cd"))
        self.assertEqual(_DeleteThrottle.hits, 3)
        self.assertEqual(len(slept), 2)
        self.assertTrue(all(0.0 <= s <= client_module.RETRY_AFTER_CAP_SECONDS for s in slept), slept)

    def test_a_server_that_keeps_throttling_ends_as_a_refusal_after_four_tries_per_endpoint(self):
        client = self._client([429] * 20)
        with patch.object(client_module.time, "sleep"):
            self.assertFalse(client.delete_entry("rid", "/r/objects/ab/cd"))
        self.assertEqual(_DeleteThrottle.hits, 8)      # /file/ then /dir/, each 1 + 3 tries


if __name__ == "__main__":
    unittest.main()
