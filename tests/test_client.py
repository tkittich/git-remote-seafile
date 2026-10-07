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

    def test_adjust_url(self):
        raw_url = "http://internal-docker-host:8082/seafhttp/files/abc123/file.pack?token=xyz"
        adjusted = self.client._adjust_url(raw_url)
        self.assertEqual(
            adjusted,
            "https://seafile.example.com/seafhttp/files/abc123/file.pack?token=xyz"
        )

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

    @patch("requests.get")
    def test_get_file_bytes_and_text(self, mock_requests_get):
        # 404 on download link
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=404))
        self.assertIsNone(self.client.get_file_bytes("repo1", "/missing.txt"))
        self.assertIsNone(self.client.get_file_text("repo1", "/missing.txt"))

        # 500 on download link
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=500))
        with self.assertRaises(SeafileAPIError):
            self.client.get_file_bytes("repo1", "/bad.txt")

        # Success flow
        self.client.session.get = MagicMock(
            return_value=MagicMock(status_code=200, text='"https://seafile.example.com/files/123/file.txt"')
        )
        mock_requests_get.return_value = MagicMock(status_code=200, content=b"Hello World\n")

        data = self.client.get_file_bytes("repo1", "/file.txt")
        self.assertEqual(data, b"Hello World\n")
        self.assertEqual(self.client.get_file_text("repo1", "/file.txt"), "Hello World")

        # 404 on content download
        mock_requests_get.return_value = MagicMock(status_code=404)
        self.assertIsNone(self.client.get_file_bytes("repo1", "/file.txt"))

        # 500 on content download
        mock_requests_get.return_value = MagicMock(status_code=500)
        with self.assertRaises(SeafileAPIError):
            self.client.get_file_bytes("repo1", "/file.txt")

    @patch("requests.post")
    def test_upload_file(self, mock_requests_post):
        # Mock dir_exists so mkdir_p doesn't post
        self.client.dir_exists = MagicMock(return_value=True)

        # Upload link success + post success
        self.client.session.get = MagicMock(
            return_value=MagicMock(status_code=200, text='"https://seafile.example.com/seafhttp/upload/xyz"')
        )
        mock_requests_post.return_value = MagicMock(status_code=200)

        self.assertTrue(self.client.upload_file("repo1", "/seafile", "test.txt", b"content"))

        # Upload link failure
        self.client.session.get = MagicMock(return_value=MagicMock(status_code=403, text="Forbidden"))
        with self.assertRaises(SeafileAPIError):
            self.client.upload_file("repo1", "/seafile", "test.txt", b"content")

        # Post upload failure
        self.client.session.get = MagicMock(
            return_value=MagicMock(status_code=200, text='"https://seafile.example.com/seafhttp/upload/xyz"')
        )
        mock_requests_post.return_value = MagicMock(status_code=500, text="Upload failed")
        with self.assertRaises(SeafileAPIError):
            self.client.upload_file("repo1", "/seafile", "test.txt", b"content")


if __name__ == "__main__":
    unittest.main()
