"""AA27 (fable H2.5): a dropped connection is a ``SeafileAPIError``, so every handler sees it.

The helper's ``except SeafileAPIError`` blocks (``cmd_list``'s retry, the ref-write cleanup, the
pack download) missed the raw ``requests`` exception a refused or reset connection raised.  The
session wraps it once; the error stays a ``requests.RequestException`` too.
"""

from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

import requests

from git_remote_seafile.client import SeafileAPIError, SeafileClient, SeafileConnectionError


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestAConnectionFailureIsAnApiError(unittest.TestCase):
    def setUp(self):
        patcher = patch("urllib3.util.retry.time.sleep")  # the retry backoff, not under test
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = SeafileClient(server_url=f"http://127.0.0.1:{_closed_port()}", token="secret-token")
        self.addCleanup(self.client.close)

    def test_a_refused_connection_is_a_seafile_api_error_and_a_request_exception(self):
        with self.assertRaises(SeafileAPIError) as cm:
            self.client.list_dir("rid", "/repo/refs/heads")
        self.assertIsInstance(cm.exception, SeafileConnectionError)
        self.assertIsInstance(cm.exception, requests.RequestException)

    def test_the_message_names_the_call_and_never_the_token(self):
        with self.assertRaises(SeafileAPIError) as cm:
            self.client.session.get(f"{self.client.server_url}/api2/ping/?token=link-token", timeout=2)
        text = str(cm.exception)
        self.assertIn("during GET /api2/ping/", text)
        self.assertNotIn("link-token", text)
        self.assertNotIn("secret-token", text)


if __name__ == "__main__":
    unittest.main()
