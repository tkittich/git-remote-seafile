"""fable-C1 (review 68e81e5): a server's ``Retry-After`` is slept for at most a few seconds.

urllib3 honours the header on 429, 500 and 503 with no bound the client sets; one ``list_dir``
against ``Retry-After: 4`` took 12 s and a ``delete_entry`` 24 s, on the thread that renews the push
lease.  The cap is patched small here; the point is that 600 s is not slept.
"""

from __future__ import annotations

import io
import threading
import time
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
        began = time.monotonic()
        with patch.object(client_module, "RETRY_AFTER_CAP_SECONDS", 0.2), \
                patch.object(client_module, "_RETRY_AFTER_NOTED", False), \
                patch("sys.stderr", err):
            with self.assertRaises(SeafileAPIError):
                client.list_dir("rid", "/repo/refs/heads")
            with self.assertRaises(SeafileAPIError):
                client.list_dir("rid", "/repo/refs/heads")
        self.assertLess(time.monotonic() - began, 20.0, "the 600 s Retry-After was slept through")
        self.assertGreaterEqual(_Throttle.hits, 4, "the request was not retried")
        self.assertEqual(err.getvalue().count("asked this client to slow down"), 1, err.getvalue())


if __name__ == "__main__":
    unittest.main()
