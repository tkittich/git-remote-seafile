"""AA27 (fable H2.5): a dropped connection is a ``SeafileAPIError``, so every handler sees it.

The helper's ``except SeafileAPIError`` blocks (``cmd_list``'s retry, the ref-write cleanup, the
pack download) missed the raw ``requests`` exception a refused or reset connection raised.  The
session wraps it once; the error stays a ``requests.RequestException`` too.
"""

from __future__ import annotations

import socket
import threading
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


class TestOnlyAReadIsSentAgain(unittest.TestCase):
    """fable2-B2, C2: a request that may have reached the server is not re-sent unless it only reads."""

    def setUp(self):
        patcher = patch("urllib3.util.retry.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hits: list[str] = []
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        self.addCleanup(listener.close)
        threading.Thread(target=self._serve, args=(listener,), daemon=True).start()
        self.client = SeafileClient(server_url=f"http://127.0.0.1:{listener.getsockname()[1]}", token="t")
        self.addCleanup(self.client.close)

    def _serve(self, listener):
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    head = conn.recv(4096)
                except OSError:
                    continue
                self.hits.append(head.split(b" ", 1)[0].decode())   # read the request, answer nothing

    def test_a_delete_the_server_never_answered_is_sent_once(self):
        with self.assertRaises(SeafileAPIError):
            self.client.session.delete(f"{self.client.server_url}/api2/x/", timeout=2)
        self.assertEqual(self.hits, ["DELETE"])

    def test_a_post_the_server_never_answered_is_sent_once(self):
        with self.assertRaises(SeafileAPIError):
            self.client.session.post(f"{self.client.server_url}/api2/x/", timeout=2)
        self.assertEqual(self.hits, ["POST"])

    def test_a_get_the_server_never_answered_is_sent_again(self):
        with self.assertRaises(SeafileAPIError):
            self.client.session.get(f"{self.client.server_url}/api2/x/", timeout=2)
        self.assertGreater(len(self.hits), 1, self.hits)

    def test_an_ssl_error_is_not_retried_for_any_method(self):
        # "other" errors (an SSL failure after the send) were retried for every method.
        retry = self.client.session.get_adapter("https://x/").max_retries
        self.assertEqual(retry.other, 0)


class TestTheCauseIsKept(unittest.TestCase):
    def test_a_refused_connection_names_why(self):
        with patch("urllib3.util.retry.time.sleep"):
            client = SeafileClient(server_url=f"http://127.0.0.1:{_closed_port()}", token="t")
            self.addCleanup(client.close)
            with self.assertRaises(SeafileAPIError) as cm:
                client.session.get(f"{client.server_url}/api2/ping/?token=link-token", timeout=2)
        self.assertRegex(str(cm.exception), r"Caused by \w+Error")
        self.assertNotIn("link-token", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
