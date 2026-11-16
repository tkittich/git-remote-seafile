"""fable-C1 (review dfd2269): a ``.netrc`` entry for the Seafile host must not change the auth.

``requests`` reads ``.netrc`` when a request has no auth object of its own, and then replaces the
``Authorization: Token`` header with Basic auth built from it: every API call answered 401 ("refresh
your token") for a good token, and the netrc password was sent on the file session that is meant to
carry no credential at all.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from git_remote_seafile.client import SeafileClient


class _Recorder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[str | None] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"[]")


class TestNetrcDoesNotReplaceTheToken(unittest.TestCase):
    def test_the_token_header_and_no_netrc_password_go_out(self):
        _Recorder.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as td:
            netrc = Path(td) / "netrc"
            netrc.write_text("machine 127.0.0.1 login someone password netrc-secret\n", encoding="utf-8")
            if os.name != "nt":
                netrc.chmod(0o600)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with patch.dict(os.environ, {"NETRC": str(netrc)}):
                client = SeafileClient(server_url=base, token="the-token")
                self.addCleanup(client.close)
                client.session.get(f"{base}/api2/ping/")
                client._file_session.get(f"{base}/file")
        self.assertEqual(_Recorder.seen[0], "Token the-token")
        self.assertIsNone(_Recorder.seen[1], "the file session carries no credential")


if __name__ == "__main__":
    unittest.main()
