"""The git credential fallback inside SeafileClient's credential search (CREDENTIAL.md D2)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_remote_seafile import gitcred
from git_remote_seafile.client import SeafileAuthError, SeafileClient

URL = "https://sf.example/seafile"


@unittest.skipUnless(gitcred.have_git(), "git is not installed")
class GitFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        store = str(self.home / "credentials").replace("\\", "/")
        config = self.home / "gitconfig"
        config.write_text(f"[credential]\n\thelper = store --file {store}\n", encoding="utf-8")
        env = {
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        }
        for patcher in (
            patch.dict("os.environ", env, clear=True),
            patch("pathlib.Path.home", return_value=self.home),
            patch("git_remote_seafile.client.get_candidate_db_paths", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_token_comes_from_git_when_nothing_else_has_one(self) -> None:
        gitcred.approve(URL, "seafile-token", "from-git")
        client = SeafileClient(URL)
        self.assertEqual(client.token, "from-git")
        self.assertEqual(client.session.headers.get("Authorization"), "Token from-git")

    def test_the_account_name_is_tried_first(self) -> None:
        gitcred.approve(URL, "seafile-token+alice", "alices")
        gitcred.approve(URL, "seafile-token", "bare")
        self.assertEqual(SeafileClient(URL, username="alice").token, "alices")

    def test_git_is_never_run_when_a_token_is_already_known(self) -> None:
        with patch.dict("os.environ", {"SEAFILE_SERVER": "https://sf.example", "SEAFILE_TOKEN": "env"}), \
             patch.object(gitcred, "fill", side_effect=AssertionError("git must not be asked")):
            self.assertEqual(SeafileClient(URL).token, "env")

    def test_a_config_for_another_server_does_not_lend_its_account(self) -> None:
        (self.home / ".git-seafile.json").write_text(
            '{"server": "https://other.example", "username": "alice"}', encoding="utf-8"
        )
        gitcred.approve(URL, "seafile-token+alice", "alices")
        gitcred.approve(URL, "seafile-token", "bare")
        self.assertEqual(SeafileClient(URL).token, "bare")

    def test_a_git_config_file_asks_git_before_the_desktop_clients_accounts(self) -> None:
        (self.home / ".git-seafile.json").write_text(
            '{"server": "https://sf.example/seafile", "username": "alice", "credential_store": "git"}',
            encoding="utf-8",
        )
        gitcred.approve(URL, "seafile-token+alice", "alices")
        with patch.object(SeafileClient, "_credentials_from_accounts_db", side_effect=AssertionError("too early")):
            self.assertEqual(SeafileClient(URL).token, "alices")

    def test_the_error_says_the_helper_was_tried(self) -> None:
        with self.assertRaises(SeafileAuthError) as ctx:
            SeafileClient(URL, username="alice")
        message = str(ctx.exception)
        self.assertIn("git credential helper", message)
        self.assertIn("seafile-token+alice", message)

    def test_a_server_with_nothing_to_ask_about_is_unchanged(self) -> None:
        with patch.object(gitcred, "fill", side_effect=AssertionError("no server, nothing to ask")):
            with self.assertRaises(SeafileAuthError):
                SeafileClient()

    def test_a_hostile_server_address_is_not_sent_to_git(self) -> None:
        with patch.object(gitcred, "_run", side_effect=AssertionError("must be refused first")):
            with self.assertRaises(SeafileAuthError):
                SeafileClient("https://user:pw@sf.example/")


if __name__ == "__main__":
    unittest.main()
