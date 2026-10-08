"""test_client.py - Comprehensive unit tests for SeafileClient."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import time
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

    def test_load_credentials_scoped_to_server_url(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            ccnet_dir = fake_home / "ccnet"
            ccnet_dir.mkdir()
            db_path = ccnet_dir / "accounts.db"

            con = sqlite3.connect(str(db_path))
            con.execute("CREATE TABLE Accounts (url TEXT, token TEXT, lastVisited INTEGER)")
            con.execute("INSERT INTO Accounts VALUES ('https://other.example.com', 'other-tok', 99999)")
            con.execute("INSERT INTO Accounts VALUES ('https://target.example.com', 'target-tok', 12345)")
            con.commit()
            con.close()

            with patch.dict("os.environ", {}, clear=True), patch("pathlib.Path.home", return_value=fake_home):
                # Requesting target server must pick target-tok, NOT other-tok despite other having higher lastVisited
                client = SeafileClient(server_url="https://target.example.com")
                self.assertEqual(client.token, "target-tok")

                # Requesting an unrelated server must NOT pick other-tok or target-tok
                with self.assertRaises(SeafileAuthError):
                    SeafileClient(server_url="https://unrelated.example.com")

                # Substring / lookalike hosts must NOT receive the target token (e.g. truncated TLD, appended domain)
                with self.assertRaises(SeafileAuthError):
                    SeafileClient(server_url="https://target.example.co")
                with self.assertRaises(SeafileAuthError):
                    SeafileClient(server_url="https://sub.target.example.com")
                with self.assertRaises(SeafileAuthError):
                    SeafileClient(server_url="https://target.example.com.evil.com")

    def test_no_credential_search_when_a_server_is_given_and_not_required(self):
        # Passing a server explicitly and not requiring a token must not go
        # looking through the environment or the desktop client's databases.
        with patch.dict("os.environ", {"SEAFILE_TOKEN": "should-not-be-used"}, clear=True):
            client = SeafileClient(server_url="https://explicit.example.com", require_credentials=False)
            self.assertEqual(client.server_url, "https://explicit.example.com")
            self.assertIsNone(client.token)

    def test_load_credentials_netloc_normalization(self):
        with patch.dict("os.environ", {"SEAFILE_SERVER": "https://SEAFILE.EXAMPLE.COM:443", "SEAFILE_TOKEN": "norm-tok"}, clear=True):
            client = SeafileClient(server_url="https://seafile.example.com")
            self.assertEqual(client.token, "norm-tok")


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

    def test_adjust_url_upgrades_plain_http_on_the_api_host(self):
        """A plain-http link to the *same* authority is a proxy artefact.

        Reported by a real deployment: the API answers over https, but the
        upload link comes back as ``http://<same-host>/seafhttp/upload-api/..``.
        That is not a second file server -- it is the same host:port with the
        scheme a reverse proxy guessed wrong -- so the scheme is corrected even
        though host rewriting stays opt-in.
        """
        raw_url = "http://seafile.example.com/seafhttp/upload-api/abc123"
        self.assertFalse(self.client.force_file_host)
        self.assertEqual(
            self.client._adjust_url(raw_url),
            "https://seafile.example.com/seafhttp/upload-api/abc123",
        )

    def test_adjust_url_never_downgrades_https_to_http(self):
        """Upgrading is safe; downgrading is not.  Only ever go one way."""
        client = SeafileClient(server_url="http://seafile.example.com", token="t")
        raw_url = "https://seafile.example.com/seafhttp/files/abc123/file.pack"
        self.assertEqual(client._adjust_url(raw_url), raw_url)

    def test_adjust_url_leaves_a_same_host_different_port_alone(self):
        """Same hostname, different port is a different endpoint, not a typo.

        A file server on ``:8082`` over plain http is a real deployment shape,
        so authority comparison is exact -- the port is part of it.
        """
        raw_url = "http://seafile.example.com:8082/seafhttp/files/abc123/file.pack"
        self.assertEqual(self.client._adjust_url(raw_url), raw_url)

    def test_upload_posts_to_the_upgraded_url(self):
        """The scheme fix has to reach the POST, not just `_adjust_url`.

        This is the whole failure: Seafile answers the upload-link call with a
        plain-http URL, the POST is answered with a 301, and `requests`
        re-issues a redirected POST as a *GET* -- which the upload endpoint
        rejects with HTTP 400.  The push then dies at lock acquisition, before
        a single object is transferred.
        """
        self.client.dir_exists = MagicMock(return_value=True)
        self.client.session.get = MagicMock(
            return_value=MagicMock(
                status_code=200,
                text='"http://seafile.example.com/seafhttp/upload-api/abc123"',
            )
        )
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=200)
        self.client._transfer_session = MagicMock(return_value=session)

        self.client.upload_file("repo1", "/seafile", ".git-lock.json", b"{}")

        self.assertEqual(
            session.post.call_args[0][0],
            "https://seafile.example.com/seafhttp/upload-api/abc123",
        )

    def test_upload_failure_names_the_endpoint_but_not_the_token(self):
        """A 400 with an empty body should still say where the POST went."""
        self.client.dir_exists = MagicMock(return_value=True)
        self.client.session.get = MagicMock(
            return_value=MagicMock(
                status_code=200,
                text='"http://seafile.example.com/seafhttp/upload-api/secret-token"',
            )
        )
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=400, text="")
        self.client._transfer_session = MagicMock(return_value=session)

        with self.assertRaises(SeafileAPIError) as ctx:
            self.client.upload_file("repo1", "/seafile", ".git-lock.json", b"{}")

        message = str(ctx.exception)
        self.assertIn("https://seafile.example.com", message)
        # The path carries the upload token; it must not reach a terminal the
        # user may paste into a bug report.
        self.assertNotIn("secret-token", message)

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

    def test_get_repo_id_duplicate_owned_beats_shared(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {"id": "id-shared", "name": "project", "type": "srepo"},
            {"id": "id-owned", "name": "project", "type": "repo"},
        ]
        self.client.session.get = MagicMock(return_value=mock_resp)
        with patch("sys.stderr", io.StringIO()) as mock_stderr:
            self.assertEqual(self.client.get_repo_id("project"), "id-owned")
            self.assertIn("Warning: multiple Seafile libraries match 'project'", mock_stderr.getvalue())
            self.assertIn("Selecting owned library id-owned over shared", mock_stderr.getvalue())

    def test_get_repo_id_duplicate_owned_by_username_beats_shared(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {"id": "id-shared", "name": "project", "owner": "colleague@example.com", "type": "srepo"},
            {"id": "id-owned", "name": "project", "owner": "me@example.com"},
        ]
        self.client.session.get = MagicMock(return_value=mock_resp)
        self.client.username = "me@example.com"
        with patch("sys.stderr", io.StringIO()) as mock_stderr:
            self.assertEqual(self.client.get_repo_id("project"), "id-owned")
            self.assertIn("Warning: multiple Seafile libraries match 'project'", mock_stderr.getvalue())
            self.assertIn("Selecting owned library id-owned over shared", mock_stderr.getvalue())

    def test_get_repo_id_duplicate_true_tie_raises(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {"id": "id-s1", "name": "shared-vault", "type": "srepo"},
            {"id": "id-s2", "name": "shared-vault", "type": "srepo"},
        ]
        self.client.session.get = MagicMock(return_value=mock_resp)
        with self.assertRaises(SeafileAPIError) as ctx:
            self.client.get_repo_id("shared-vault")
        self.assertIn("Multiple Seafile libraries match 'shared-vault'", str(ctx.exception))
        self.assertIn("id-s1", str(ctx.exception))
        self.assertIn("id-s2", str(ctx.exception))

    def test_get_repo_id_by_uuid_with_duplicates(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {"id": "id-s1", "name": "shared-vault", "type": "srepo"},
            {"id": "id-s2", "name": "shared-vault", "type": "srepo"},
        ]
        self.client.session.get = MagicMock(return_value=mock_resp)
        self.assertEqual(self.client.get_repo_id("id-s2"), "id-s2")

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

    def test_api_quotes_paths(self):
        mock_resp = MagicMock(status_code=200, json=lambda: [])
        self.client.session.get = MagicMock(return_value=mock_resp)

        self.client.list_dir("repo1", "/branch with spaces/sub#dir")
        called_url = self.client.session.get.call_args[0][0]
        self.assertIn("p=/branch%20with%20spaces/sub%23dir", called_url)

    def test_api_strips_newline_and_quotes_from_urls(self):
        # A response with trailing newline and quotes: '"https://..."\n'
        link = '"https://seafile.example.com/seafhttp/files/123/file.txt"\n'

        def route(url, **kwargs):
            if "/api2/repos/" in url:
                return MagicMock(status_code=200, text=link)
            return MagicMock(status_code=200, content=b"content")

        self.client.session.get = MagicMock(side_effect=route)
        res = self.client.get_file_bytes("repo1", "/file.txt")
        self.assertEqual(res, b"content")

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

    def test_upload_file_with_string_payload_encoded_to_utf8(self):
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=200, text='"https://seafile.example.com/seafhttp/upload-api/123"'))
        with patch.object(self.client, "_upload") as mock_upload:
            mock_upload.return_value = True
            # Multi-line string payload should be encoded to bytes rather than opened as a path
            res = self.client.upload_file("repo1", "/seafile", "ref.txt", "ref: refs/heads/main\n")
            self.assertTrue(res)
            mock_upload.assert_called_once_with(
                "repo1", "/seafile", "ref.txt", b"ref: refs/heads/main\n", True, None
            )

    def test_client_username_stored_and_passed(self):
        c = SeafileClient(server_url="https://seafile.example.com", token="tok", username="dev@example.com")
        self.assertEqual(c.username, "dev@example.com")


class TestStreamingTransfers(unittest.TestCase):
    """Large payloads must stream, not be materialised in RAM (#4).

    The docs advertise multi-gigabyte LFS objects, but the client read the whole
    file into a `bytes` object and then handed those bytes to `requests` --
    roughly twice the object size in RAM, which is precisely the case LFS exists
    for.  A file object streams from disk and, because it supports seek/tell,
    still gives `requests` a Content-Length.
    """

    def setUp(self):
        self.client = SeafileClient(
            server_url="https://seafile.example.com", token="test-tok"
        )
        self.client.dir_exists = MagicMock(return_value=True)
        self.client.session.get = MagicMock(
            return_value=MagicMock(
                status_code=200,
                text='"https://seafile.example.com/seafhttp/upload/xyz"',
            )
        )

    def _capture_upload(self):
        """Capture the multipart payload handed to the transfer."""
        captured = {}

        def fake_post(url, files=None, data=None, **kwargs):
            if files and "file" in files:
                payload = files["file"][1]
            elif hasattr(data, "file_obj"):
                payload = data.file_obj
                if hasattr(data, "read"):
                    _ = data.read()
            else:
                payload = data
            captured["payload"] = payload
            # Read it while the caller's `with open(...)` is still holding it.
            if hasattr(payload, "read"):
                if hasattr(payload, "seek"):
                    payload.seek(0)
                captured["content"] = payload.read()
            else:
                captured["content"] = payload
            return MagicMock(status_code=200)

        self.client.session.post = MagicMock(side_effect=fake_post)
        return captured

    def test_streaming_multipart_memory_bounded(self):
        """StreamingMultipartFile must not buffer large files into RAM."""
        import tracemalloc
        from git_remote_seafile.client import StreamingMultipartFile
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"0" * (16 * 1024 * 1024))
            temp_path = Path(tf.name)

        try:
            tracemalloc.start()
            with open(temp_path, "rb") as fh:
                mp = StreamingMultipartFile(
                    fields={"parent_dir": "/x", "replace": "1"},
                    file_field="file",
                    filename="16mb.bin",
                    file_obj=fh,
                    file_size=16 * 1024 * 1024,
                )
                _, peak = tracemalloc.get_traced_memory()
                # Peak memory while preparing the streaming object should be tiny (< 1 MB for a 16 MB file)
                self.assertLess(peak, 1024 * 1024)
                self.assertEqual(len(mp), len(mp.header) + 16 * 1024 * 1024 + len(mp.footer))
        finally:
            tracemalloc.stop()
            temp_path.unlink(missing_ok=True)

    def test_upload_from_a_path_streams_a_file_object(self):
        captured = self._capture_upload()

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"LARGE-LFS-PAYLOAD")
            path = Path(tf.name)
        try:
            self.assertTrue(self.client.upload_file("repo1", "/lfs", "oid", path))
        finally:
            path.unlink(missing_ok=True)

        self.assertNotIsInstance(
            captured["payload"], (bytes, bytearray),
            "the file must be handed over as a file object, not read into bytes",
        )
        self.assertTrue(hasattr(captured["payload"], "read"))
        self.assertEqual(captured["content"], b"LARGE-LFS-PAYLOAD")

    def test_upload_still_accepts_bytes(self):
        """Small payloads (packfiles, refs) keep working unchanged."""
        captured = self._capture_upload()
        self.assertTrue(self.client.upload_file("repo1", "/seafile", "test.txt", b"content"))
        self.assertEqual(captured["payload"], b"content")

    def test_streaming_multipart_file_progress_callback(self):
        """StreamingMultipartFile invokes progress callback as chunks are read."""
        from git_remote_seafile.client import StreamingMultipartFile
        calls = []

        def on_progress(transferred, total):
            calls.append((transferred, total))

        raw_data = b"HELLO_STREAMING_PROGRESS"
        stream = io.BytesIO(raw_data)
        mp = StreamingMultipartFile(
            fields={"parent_dir": "/x", "replace": "1"},
            file_field="file",
            filename="data.bin",
            file_obj=stream,
            file_size=len(raw_data),
            progress_callback=on_progress,
        )

        read_data = []
        while True:
            chunk = mp.read(8)
            if not chunk:
                break
            read_data.append(chunk)

        full_content = b"".join(read_data)
        self.assertIn(raw_data, full_content)
        self.assertTrue(len(calls) > 0)
        self.assertEqual(calls[-1], (len(raw_data), len(raw_data)))

    def test_upload_bytes_triggers_progress_callback(self):
        calls = []
        def on_progress(transferred, total):
            calls.append((transferred, total))

        captured = self._capture_upload()
        self.assertTrue(
            self.client.upload_file(
                "repo1", "/test", "file.txt", b"12345", progress_callback=on_progress
            )
        )
        self.assertEqual(captured.get("content"), b"12345")
        self.assertEqual(calls, [(5, 5)])

    def test_streaming_multipart_file_seek_stateless(self):
        """Seeking/rewinding must calculate transferred progress statelessly."""
        from git_remote_seafile.client import StreamingMultipartFile
        calls = []
        def on_progress(transferred, total):
            calls.append((transferred, total))

        raw_data = b"0123456789"
        stream = io.BytesIO(raw_data)
        mp = StreamingMultipartFile(
            fields={},
            file_field="file",
            filename="data.bin",
            file_obj=stream,
            file_size=len(raw_data),
            progress_callback=on_progress,
        )
        # Read entire stream
        _ = mp.read()
        self.assertEqual(calls[-1], (10, 10))

        # Rewind to start as happens on HTTP redirect/retry
        mp.seek(0)
        calls.clear()
        _ = mp.read()
        # After rewind, progress must report 10 again, not 20
        self.assertEqual(calls[-1], (10, 10))

    def test_streaming_multipart_sanitizes_header_parameters(self):
        from git_remote_seafile.client import StreamingMultipartFile
        stream = io.BytesIO(b"data")
        mp = StreamingMultipartFile(
            fields={"bad\r\nname": "value", "quoted": 'a"b'},
            file_field='file"field',
            filename='exploit"test\r\n.bin',
            file_obj=stream,
            file_size=4,
        )
        hdr = mp.header.decode("utf-8")
        self.assertNotIn("\r\nname", hdr)
        self.assertIn('name="badname"', hdr)
        self.assertIn('name="quoted"', hdr)
        self.assertIn('name="file\\"field"', hdr)
        self.assertIn('filename="exploit\\"test.bin"', hdr)

    def test_streaming_multipart_respects_nonzero_file_tell_offset(self):
        from git_remote_seafile.client import StreamingMultipartFile
        stream = io.BytesIO(b"0123456789ABCDEF")
        stream.seek(5)  # Offset at '5'
        mp = StreamingMultipartFile(
            fields={},
            file_field="file",
            filename="data.bin",
            file_obj=stream,
            file_size=5,
        )
        content = mp.read()
        self.assertIn(b"56789", content)
        self.assertNotIn(b"01234", content)

    def test_download_file_to_triggers_progress_callback(self):
        calls = []
        def on_progress(transferred, total):
            calls.append((transferred, total))

        self.client.session.get.return_value = MagicMock(
            status_code=200, text='"https://seafile.example.com/seafhttp/files/abc"'
        )
        fake_transfer_resp = MagicMock(status_code=200)
        fake_transfer_resp.headers = {"Content-Length": "12"}
        fake_transfer_resp.iter_content.return_value = [b"chunk1", b"chunk2"]
        with patch.object(self.client, "_transfer_session") as mock_ts:
            mock_sess = MagicMock()
            mock_sess.get.return_value = fake_transfer_resp
            mock_ts.return_value = mock_sess

            with tempfile.NamedTemporaryFile(delete=False) as tf:
                dest_path = Path(tf.name)
            try:
                ok = self.client.download_file_to(
                    "repo1", "/path/file.bin", dest_path, progress_callback=on_progress
                )
                self.assertTrue(ok)
                self.assertEqual(calls, [(6, 12), (12, 12)])
            finally:
                dest_path.unlink(missing_ok=True)

    def test_server_time_offset_from_date_header(self):
        """HTTP Date header passively calibrates estimated server time."""
        from email.utils import formatdate
        resp = MagicMock()
        resp.headers = {"Date": formatdate(time.time() + 100, usegmt=True)}
        self.client._record_server_date_header(resp)
        self.assertAlmostEqual(self.client.get_server_time(), time.time() + 100, delta=2)

    def test_server_time_median_filtering(self):
        from email.utils import formatdate
        self.client._server_time_offsets = []
        now = time.time()
        for delta in [10.0, 100.0, 10.0, 11.0, 10.0]:
            resp = MagicMock()
            resp.headers = {"Date": formatdate(now + delta, usegmt=True)}
            self.client._record_server_date_header(resp)
        # Median of [10, 10, 10, 11, 100] is 10
        self.assertAlmostEqual(self.client.get_server_time(), now + 10, delta=2)

    def test_download_file_to_streams_to_disk(self):
        payload = MagicMock(status_code=200)
        payload.iter_content = MagicMock(side_effect=lambda chunk_size: iter([b"AAA", b"BBB", b""]))
        self.client.session.get = MagicMock(
            side_effect=[
                MagicMock(
                    status_code=200,
                    text='"https://seafile.example.com/seafhttp/files/1/a.bin"',
                ),
                payload,
            ]
        )

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "nested" / "a.bin"
            self.assertTrue(self.client.download_file_to("repo1", "/a.bin", dest))
            self.assertEqual(dest.read_bytes(), b"AAABBB")

        transfer_kwargs = self.client.session.get.call_args_list[1].kwargs
        self.assertTrue(
            transfer_kwargs.get("stream"),
            "without stream=True requests buffers the whole body in memory",
        )
        payload.close.assert_called_once()

    def test_download_file_to_reports_a_missing_object(self):
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=404))
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(
                self.client.download_file_to("repo1", "/missing.bin", Path(td) / "x")
            )

    def test_a_path_upload_transmits_the_file_over_a_real_socket(self):
        """The mechanism assertions above need one real transfer behind them.

        A file object is what lets `requests` set Content-Length; if it silently
        fell back to chunked encoding the upload would still "work" against a
        mock and fail against a real server.
        """
        import http.server
        import threading

        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                port = self.server.server_address[1]
                body = json.dumps(f"http://127.0.0.1:{port}/upload/xyz").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                received["body"] = self.rfile.read(length)
                received["content_length"] = length
                received["transfer_encoding"] = self.headers.get("Transfer-Encoding")
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = SeafileClient(
                server_url=f"http://127.0.0.1:{server.server_address[1]}", token="t"
            )
            client.dir_exists = MagicMock(return_value=True)

            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"STREAMED-PAYLOAD-BYTES")
                path = Path(tf.name)
            try:
                self.assertTrue(client.upload_file("repo1", "/lfs", "oid.bin", path))
            finally:
                path.unlink(missing_ok=True)
        finally:
            server.shutdown()
            server.server_close()

        self.assertIn(b"STREAMED-PAYLOAD-BYTES", received["body"])
        self.assertGreater(received["content_length"], 0)
        self.assertIsNone(
            received["transfer_encoding"],
            "a seekable file object should give requests a Content-Length, not chunked encoding",
        )


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
