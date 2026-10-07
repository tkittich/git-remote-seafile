"""test_client.py - Comprehensive unit tests for SeafileClient."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.client import SeafileAPIError, SeafileAuthError, SeafileClient


class TestClientCredentials(unittest.TestCase):
    """Test credential discovery mechanisms."""

    def test_load_credentials_from_env(self):
        with patch.dict("os.environ", {"SEAFILE_SERVER": "https://env.example.com", "SEAFILE_TOKEN": "env-token-123"}):
            client = SeafileClient()
            self.assertEqual(client.server_url, "https://env.example.com")
            self.assertEqual(client.token, "env-token-123")
            self.assertEqual(client.session.headers.get("Authorization"), "Token env-token-123")

    def test_load_credentials_from_json(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            cfg_file = fake_home / ".git-seafile.json"
            cfg_file.write_text(json.dumps({"server": "https://json.example.com", "token": "json-tok"}), encoding="utf-8")

            with patch.dict("os.environ", {}, clear=True), patch("pathlib.Path.home", return_value=fake_home):
                client = SeafileClient()
                self.assertEqual(client.server_url, "https://json.example.com")
                self.assertEqual(client.token, "json-tok")

    def test_load_credentials_from_accounts_db(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            ccnet_dir = fake_home / "ccnet"
            ccnet_dir.mkdir()
            db_path = ccnet_dir / "accounts.db"

            con = sqlite3.connect(str(db_path))
            con.execute("CREATE TABLE Accounts (url TEXT, token TEXT, lastVisited INTEGER)")
            con.execute("INSERT INTO Accounts VALUES ('https://db.example.com', 'db-tok-456', 12345)")
            con.commit()
            con.close()

            with patch.dict("os.environ", {}, clear=True), patch("pathlib.Path.home", return_value=fake_home):
                client = SeafileClient()
                self.assertEqual(client.server_url, "https://db.example.com")
                self.assertEqual(client.token, "db-tok-456")

    def test_load_credentials_missing_raises_auth_error(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            with patch.dict("os.environ", {}, clear=True), patch("pathlib.Path.home", return_value=fake_home):
                with self.assertRaises(SeafileAuthError):
                    SeafileClient()

    def test_server_url_is_available_without_a_token_when_not_required(self):
        """`desktop-url` only needs to know which server to name.

        It used to build a fully credentialed client, so it failed with
        "Could not find Seafile credentials" for anyone who has the desktop
        client installed but no reachable token -- even though that command
        never makes an authenticated request.
        """
        with patch.dict("os.environ", {"SEAFILE_SERVER": "https://server-only.example.com"}, clear=True):
            client = SeafileClient(require_credentials=False)
            self.assertEqual(client.server_url, "https://server-only.example.com")
            self.assertIsNone(client.token)
            self.assertNotIn("Authorization", client.session.headers)

    def test_requiring_credentials_remains_the_default(self):
        # A server URL alone must NOT be enough for the normal code paths: the
        # helper needs a token to talk to the API.
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"SEAFILE_SERVER": "https://server-only.example.com"}, clear=True):
                with patch("pathlib.Path.home", return_value=Path(td)):
                    with self.assertRaises(SeafileAuthError):
                        SeafileClient()

    def test_a_server_is_still_required_even_when_credentials_are_not(self):
        # Dropping the token requirement must not turn into "no configuration
        # needed at all" -- without a server there is nothing to point at.
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {}, clear=True), patch("pathlib.Path.home", return_value=Path(td)):
                with self.assertRaises(SeafileAuthError) as ctx:
                    SeafileClient(require_credentials=False)
                self.assertIn("server", str(ctx.exception).lower())

    def test_no_credential_search_when_a_server_is_given_and_not_required(self):
        # Passing a server explicitly and not requiring a token must not go
        # looking through the environment or the desktop client's databases.
        with patch.dict("os.environ", {"SEAFILE_TOKEN": "should-not-be-used"}, clear=True):
            client = SeafileClient(server_url="https://explicit.example.com", require_credentials=False)
            self.assertEqual(client.server_url, "https://explicit.example.com")
            self.assertIsNone(client.token)


class TestClientAPI(unittest.TestCase):
    """Test Seafile REST API methods with mocked requests."""

    def setUp(self):
        self.client = SeafileClient(server_url="https://seafile.example.com", token="test-tok")

    def test_adjust_url_leaves_a_foreign_host_alone_by_default(self):
        """A clustered Seafile serves files from its own host -- trust it.

        Forcing every returned link onto the API host 404s there, which is what
        the three reviews flagged.  The rewrite is now opt-in.
        """
        raw_url = "https://storage-node.example.net:8082/seafhttp/files/abc123/file.pack?token=xyz"
        self.assertFalse(self.client.force_file_host)
        self.assertEqual(self.client._adjust_url(raw_url), raw_url)

    def test_adjust_url_still_completes_a_relative_link(self):
        """Making a relative link absolute is completion, not host rewriting.

        This half must keep working regardless of the opt-in: a bare path is
        not something `requests` can fetch on its own.
        """
        adjusted = self.client._adjust_url("/seafhttp/files/abc123/file.pack?token=xyz")
        self.assertEqual(
            adjusted,
            "https://seafile.example.com/seafhttp/files/abc123/file.pack?token=xyz",
        )

    def test_adjust_url_rewrites_the_host_when_forced(self):
        """The escape hatch for a reverse proxy that reports an internal host."""
        self.client.force_file_host = True
        raw_url = "http://internal-docker-host:8082/seafhttp/files/abc123/file.pack?token=xyz"
        self.assertEqual(
            self.client._adjust_url(raw_url),
            "https://seafile.example.com/seafhttp/files/abc123/file.pack?token=xyz",
        )

    def test_force_file_host_is_off_unless_asked_for(self):
        from git_remote_seafile.client import _force_file_host_enabled

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "git_remote_seafile.client.get_git_config_bool", return_value=False
            ):
                self.assertFalse(_force_file_host_enabled())

        # env opt-in
        for val in ("1", "true", "yes", "on", "TRUE"):
            with patch.dict("os.environ", {"SEAFILE_FORCE_FILE_HOST": val}):
                self.assertTrue(_force_file_host_enabled())

        # env opt-out beats a config that says yes
        with patch.dict("os.environ", {"SEAFILE_FORCE_FILE_HOST": "off"}):
            with patch(
                "git_remote_seafile.client.get_git_config_bool", return_value=True
            ):
                self.assertFalse(_force_file_host_enabled())

        # git config
        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "git_remote_seafile.client.get_git_config_bool", return_value=True
            ):
                self.assertTrue(_force_file_host_enabled())

    def test_get_repo_id_cached(self):
        self.client._repos_cache["cached_repo"] = "repo-id-123"
        self.assertEqual(self.client.get_repo_id("cached_repo"), "repo-id-123")

    def test_get_repo_id_fetch_success(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {"id": "id-1", "name": "Documents"},
            {"id": "id-2", "name": "code"},
        ]
        self.client.session.get = MagicMock(return_value=mock_resp)

        self.assertEqual(self.client.get_repo_id("code"), "id-2")
        self.assertEqual(self.client.get_repo_id("id-1"), "id-1")
        self.assertIn("code", self.client._repos_cache)

    def test_get_repo_id_errors(self):
        # HTTP error
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=500, text="Internal Error"))
        with self.assertRaises(SeafileAPIError):
            self.client.get_repo_id("myrepo")

        # Not found
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [{"id": "id-1", "name": "Documents"}]
        self.client.session.get = MagicMock(return_value=mock_resp)
        with self.assertRaises(SeafileAPIError):
            self.client.get_repo_id("nonexistent")

    def test_list_dir(self):
        # 404 returns empty list
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=404))
        self.assertEqual(self.client.list_dir("repo1", "/nonexistent"), [])

        # 200 returns items
        items = [{"type": "file", "name": "main"}]
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=200, json=lambda: items))
        self.assertEqual(self.client.list_dir("repo1", "/refs/heads"), items)

        # 500 raises error
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=500, text="Server Error"))
        with self.assertRaises(SeafileAPIError):
            self.client.list_dir("repo1", "/some/dir")

    def test_get_file_bytes_and_text(self):
        """The payload transfer must go through the session, not bare requests.

        Two legs are routed by URL so they stay distinct: the API call that
        returns the download link, and the transfer of the bytes it points at.
        A bare `requests.get` bypassed the session -- and with it connection
        reuse, the `verify`/proxy settings and the retry adapter.
        """
        link = '"https://seafile.example.com/seafhttp/files/123/file.txt"'
        transfer = {}

        def route(url, **kwargs):
            if "/api2/repos/" in url:
                return MagicMock(status_code=200, text=link)
            return transfer["resp"]

        transfer["resp"] = MagicMock(status_code=200, content=b"Hello World\n")
        self.client.session.get = MagicMock(side_effect=route)

        with patch("requests.get") as bare_get:
            self.assertEqual(
                self.client.get_file_bytes("repo1", "/file.txt"), b"Hello World\n"
            )
            self.assertEqual(self.client.get_file_text("repo1", "/file.txt"), "Hello World")
            bare_get.assert_not_called()

        # 404 on the payload
        transfer["resp"] = MagicMock(status_code=404)
        self.assertIsNone(self.client.get_file_bytes("repo1", "/file.txt"))

        # 500 on the payload
        transfer["resp"] = MagicMock(status_code=500)
        with self.assertRaises(SeafileAPIError):
            self.client.get_file_bytes("repo1", "/file.txt")

        # 404 on the download link
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=404))
        self.assertIsNone(self.client.get_file_bytes("repo1", "/missing.txt"))
        self.assertIsNone(self.client.get_file_text("repo1", "/missing.txt"))

        # 500 on the download link
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=500))
        with self.assertRaises(SeafileAPIError):
            self.client.get_file_bytes("repo1", "/bad.txt")

    def test_upload_file(self):
        # Mock dir_exists so mkdir_p doesn't post
        self.client.dir_exists = MagicMock(return_value=True)

        link = '"https://seafile.example.com/seafhttp/upload/xyz"'
        self.client.session.get = MagicMock(
            return_value=MagicMock(status_code=200, text=link)
        )

        with patch("requests.post") as bare_post:
            # Upload post success
            self.client.session.post = MagicMock(return_value=MagicMock(status_code=200))
            self.assertTrue(self.client.upload_file("repo1", "/seafile", "test.txt", b"content"))
            bare_post.assert_not_called()

            # Post upload failure
            self.client.session.post = MagicMock(
                return_value=MagicMock(status_code=500, text="Upload failed")
            )
            with self.assertRaises(SeafileAPIError):
                self.client.upload_file("repo1", "/seafile", "test.txt", b"content")

        # Upload link failure
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=403, text="Forbidden"))
        with self.assertRaises(SeafileAPIError):
            self.client.upload_file("repo1", "/seafile", "test.txt", b"content")


class TestTransferSession(unittest.TestCase):
    """File transfers must reuse the session -- but not its credentials (#7).

    Reusing the session buys connection reuse, the `verify`/proxy settings and
    the retry adapter.  It must not also send the account token to whatever host
    a returned link names: download and upload links carry their own short-lived
    token, and a clustered deployment serves them from a different host.
    """

    def setUp(self):
        self.client = SeafileClient(
            server_url="https://seafile.example.com", token="test-tok"
        )

    def test_a_same_host_link_uses_the_api_session(self):
        url = "https://seafile.example.com/seafhttp/files/abc/file.pack?token=xyz"
        self.assertIs(self.client._transfer_session(url), self.client.session)
        self.assertEqual(
            self.client.session.headers.get("Authorization"), "Token test-tok"
        )

    def test_a_foreign_host_link_uses_a_credential_free_session(self):
        url = "https://storage-node.example.net:8082/seafhttp/files/abc/file.pack?token=xyz"
        session = self.client._transfer_session(url)
        self.assertIsNot(session, self.client.session)
        self.assertIsNone(
            session.headers.get("Authorization"),
            "the account token must not follow a link to another host",
        )

    def test_a_transfer_to_a_foreign_host_carries_no_token(self):
        """End to end: the header actually sent must not be the account token."""
        link = '"https://storage-node.example.net:8082/seafhttp/files/abc/file.pack"'
        self.client.force_file_host = False

        self.client.session.get = MagicMock(
            return_value=MagicMock(status_code=200, text=link)
        )
        captured = {}

        def fake_get(url, **kwargs):
            captured["headers"] = kwargs.get("headers")
            captured["url"] = url
            return MagicMock(status_code=200, content=b"PACK")

        # Only the foreign session is exercised for the transfer.
        self.client._file_session.get = MagicMock(side_effect=fake_get)

        self.assertEqual(self.client.get_file_bytes("repo1", "/a.pack"), b"PACK")
        self.assertEqual(captured["url"], link.strip('"'))
        sent = captured["headers"] or {}
        self.assertNotIn("Authorization", sent)
        # ...and the foreign session has none in its defaults either, which is
        # what `requests` would merge in.
        self.assertIsNone(self.client._file_session.headers.get("Authorization"))

    def test_both_sessions_carry_a_retry_adapter(self):
        from requests.adapters import HTTPAdapter

        for session in (self.client.session, self.client._file_session):
            for scheme in ("http://", "https://"):
                adapter = session.get_adapter(f"{scheme}example.com/")
                self.assertIsInstance(adapter, HTTPAdapter)
                self.assertGreater(adapter.max_retries.total, 0)

    def test_the_retry_policy_retries_blips_but_never_uploads(self):
        """The policy, read off the mounted adapter.

        `is_retry` is the predicate urllib3 actually consults before re-issuing
        a request -- `increment` merely decrements the budget and would say yes
        to anything.
        """
        retry = self.client.session.get_adapter("https://example.com/").max_retries

        # A 503 on a download is a blip worth retrying...
        self.assertTrue(retry.is_retry("GET", 503))
        # ...but a 404 is a real answer, not a blip.
        self.assertFalse(retry.is_retry("GET", 404))
        # ...and an upload must never be re-sent: a retried POST can duplicate
        # work we then cannot undo.  urllib3's default set excludes POST; this
        # asserts we did not widen it.
        self.assertFalse(retry.is_retry("POST", 503))
        # `raise_on_status=False` keeps the final response returnable, so the
        # callers' status-code handling is unchanged.
        self.assertFalse(retry.raise_on_status)


class TestTransferRetries(unittest.TestCase):
    """A blip mid-transfer must not fail the whole fetch (#7).

    Driven against a real socket on purpose: the retry adapter lives inside
    `requests`/`urllib3`, so a mocked session would prove nothing about whether
    a transient failure is actually retried.
    """

    @staticmethod
    def _start_server(payload_statuses):
        """Start a stub server; `payload_statuses` is consumed per payload GET."""
        import http.server
        import threading

        seen = {"payload": 0, "api": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                port = self.server.server_address[1]
                if "/api2/repos/" in self.path:
                    seen["api"] += 1
                    body = json.dumps(
                        f"http://127.0.0.1:{port}/seafhttp/files/abc/a.pack"
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                seen["payload"] += 1
                idx = min(seen["payload"] - 1, len(payload_statuses) - 1)
                code = payload_statuses[idx]
                body = b"PACKBYTES" if code == 200 else b""
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, seen

    def _client(self, server):
        port = server.server_address[1]
        return SeafileClient(server_url=f"http://127.0.0.1:{port}", token="test-tok")

    def test_a_transient_503_is_retried_and_the_download_succeeds(self):
        server, seen = self._start_server([503, 503, 200])
        try:
            client = self._client(server)
            self.assertEqual(client.get_file_bytes("repo1", "/a.pack"), b"PACKBYTES")
            self.assertEqual(
                seen["payload"], 3, "the two 503s should have been retried"
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_a_persistent_failure_still_surfaces_as_an_api_error(self):
        """Retries must not change what a real failure looks like.

        With `raise_on_status=False` the exhausted retry returns the last
        response rather than raising, so the caller still turns it into the
        same SeafileAPIError -- not a urllib3 RetryError leaking out.
        """
        server, seen = self._start_server([503])
        try:
            client = self._client(server)
            with self.assertRaises(SeafileAPIError):
                client.get_file_bytes("repo1", "/a.pack")
            self.assertEqual(seen["payload"], 4, "one attempt plus three retries")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
