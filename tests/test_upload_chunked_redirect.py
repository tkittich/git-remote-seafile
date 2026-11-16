"""gemini-4-hydrogen-2-C3 (review dfd2269): a chunk POST is never re-sent where a 3xx points.

``_xfer`` forbids it for every other upload; ``_upload_chunked`` posted on the session directly,
so ``requests`` followed a 302 with a GET and the chunk was reported as sent if the listing then
showed the right size.
"""

from __future__ import annotations

import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from git_remote_seafile.client import SeafileAPIError, SeafileClient


class _Redirecting(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[tuple[str, str]] = []

    def log_message(self, *args):
        pass

    def _answer(self, status: int, headers: dict[str, str]) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).seen.append((self.command, self.path))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._answer(302, {"Location": "/elsewhere"})

    def do_GET(self):
        self._answer(200, {})


class TestAChunkIsNotSentToARedirect(unittest.TestCase):
    def test_a_302_on_a_chunk_post_is_a_failed_upload_and_nothing_follows_it(self):
        _Redirecting.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Redirecting)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        client = SeafileClient(server_url=base, token="t")
        self.addCleanup(client.close)
        data = b"x" * 64
        with self.assertRaises(SeafileAPIError) as ctx:
            client._upload_chunked("rid", "/d", "f.bin", io.BytesIO(data), len(data), True,
                                   f"{base}/upload?t=1")
        self.assertIn("HTTP 302", str(ctx.exception))
        self.assertEqual([c for c, _ in _Redirecting.seen if c == "GET"], [], _Redirecting.seen)


if __name__ == "__main__":
    unittest.main()
