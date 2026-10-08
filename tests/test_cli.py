"""test_cli.py - Unit tests for CLI entry points and subcommands."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.cli import main
from git_remote_seafile.client import SeafileAuthError
from git_remote_seafile.safety import SafetyError


class TestCLIBasics(unittest.TestCase):
    def test_help(self):
        with patch.object(sys, "argv", ["git-remote-seafile"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("git-remote-seafile - Git remote helper", mock_out.getvalue())

        for arg in ["-h", "--help", "help"]:
            with patch.object(sys, "argv", ["git-remote-seafile", arg]):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    code = main()
                    self.assertEqual(code, 0)
                    self.assertIn("Commands:", mock_out.getvalue())

    def test_version(self):
        for arg in ["-v", "--version", "version"]:
            with patch.object(sys, "argv", ["git-remote-seafile", arg]):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    code = main()
                    self.assertEqual(code, 0)
                    self.assertIn("git-remote-seafile v", mock_out.getvalue())


class TestCLICheckAuth(unittest.TestCase):
    @patch("git_remote_seafile.cli.SeafileClient")
    def test_check_auth_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.server_url = "https://seafile.example.com"
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"email": "dev@example.com"}
        mock_client.session.get.return_value = mock_resp

        with patch.object(sys, "argv", ["git-remote-seafile", "check-auth"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("dev@example.com", mock_out.getvalue())

    @patch("git_remote_seafile.cli.SeafileClient")
    def test_check_auth_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.server_url = "https://seafile.example.com"
        mock_resp = MagicMock(status_code=401, text="Unauthorized")
        mock_client.session.get.return_value = mock_resp

        with patch.object(sys, "argv", ["git-remote-seafile", "check-auth"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Authentication failed", mock_out.getvalue())

    @patch("git_remote_seafile.cli.SeafileClient")
    def test_check_auth_exception(self, mock_client_cls):
        mock_client_cls.side_effect = RuntimeError("Network error")
        with patch.object(sys, "argv", ["git-remote-seafile", "check-auth"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Error: Network error", mock_out.getvalue())


class TestCLICheckSafety(unittest.TestCase):
    def test_check_safety_missing_args(self):
        with patch.object(sys, "argv", ["git-remote-seafile", "check-safety"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Usage:", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.safety.check_preflight_safety")
    @patch("git_remote_seafile.safety.discover_local_synced_libraries")
    @patch("git_remote_seafile.safety.get_local_work_tree")
    def test_check_safety_passed(self, mock_wt, mock_synced, mock_check, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.client.server_url = "https://seafile.example.com"
        mock_helper.library_name = "code"
        mock_helper.repo_path = "myproject"
        mock_wt.return_value = "/local/path"
        mock_synced.return_value = [{"name": "Documents", "worktree": "/docs"}]
        mock_check.return_value = []

        with patch.object(sys, "argv", ["git-remote-seafile", "check-safety", "seafile://code/myproject"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("[PASSED]", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.safety.check_preflight_safety")
    def test_check_safety_blocked(self, mock_check, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_check.side_effect = SafetyError("Collision detected")

        with patch.object(sys, "argv", ["git-remote-seafile", "check-safety", "seafile://code/myproject"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("[BLOCKED - SAFETY ERROR]", mock_out.getvalue())


class TestCLITestsAndGC(unittest.TestCase):
    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.safety.check_preflight_safety")
    def test_cli_test_command(self, mock_check, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.client.server_url = "https://seafile.example.com"
        mock_helper.library_name = "code"
        mock_helper.repo_id = "repo123"
        mock_helper.repo_path = "repo"
        mock_check.return_value = ["Minor tip"]

        with patch.object(sys, "argv", ["git-remote-seafile", "test", "seafile://code/repo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("Connection successful!", mock_out.getvalue())
                self.assertIn("Minor tip", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.gc.compact_repository")
    def test_cli_gc_reports_a_growth_honestly(self, mock_gc, mock_helper_cls):
        """Pin the printed line, because that is what a user actually reads.

        ``compact_repository`` is mocked but ``describe_size_delta`` is not, so
        this also proves the CLI routes the value through the helper instead of
        formatting it inline -- reverting the call site fails this test.
        """
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.client = MagicMock()
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "repo"

        mock_gc.return_value = {"status": "ok", "old_packs": 3, "new_packs": 1, "saved_kb": -7}
        with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()

        output = mock_out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("grew by ~7 KB", output)
        self.assertNotIn("saved -7", output)

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.gc.compact_repository")
    def test_cli_gc_command(self, mock_gc, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.client = MagicMock()
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "repo"

        # Success case
        mock_gc.return_value = {"status": "ok", "old_packs": 5, "new_packs": 1, "saved_kb": 100}
        with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo", "--min-packs", "3"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("Compacted 5 packfiles into 1", mock_out.getvalue())

        # Skipped case
        mock_gc.return_value = {"status": "skipped", "message": "Only 1 packfile present."}
        with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("Only 1 packfile present.", mock_out.getvalue())


class TestCLISetHeadAndLFSTransfer(unittest.TestCase):
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_cli_set_head(self, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "/git-repo"
        mock_helper.client.list_dir.return_value = [{"name": "dev", "type": "file"}]
        mock_helper.client.get_file_text.return_value = "1" * 40

        with patch.object(sys, "argv", ["git-remote-seafile", "set-head", "seafile://code/repo", "dev"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                mock_helper.client.upload_file.assert_called_once_with(
                    "repo1", "/git-repo", "HEAD", b"ref: refs/heads/dev\n", replace=True
                )
                self.assertIn("Updated remote HEAD", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_cli_set_head_rejects_nonexistent_branch(self, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "/git-repo"
        mock_helper.client.list_dir.return_value = [{"name": "main", "type": "file"}]
        mock_helper.client.get_file_text.return_value = "1" * 40

        with patch.object(sys, "argv", ["git-remote-seafile", "set-head", "seafile://code/repo", "dev"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("does not exist", mock_err.getvalue())
                mock_helper.client.upload_file.assert_not_called()

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_cli_gc_min_packs_validation(self, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "/git-repo"

        with patch("git_remote_seafile.gc.compact_repository") as mock_compact:
            mock_compact.return_value = {"status": "ok", "old_packs": 3, "new_packs": 1, "saved_kb": 10}

            # Valid integer
            with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo", "--min-packs", "5"]):
                code = main()
                self.assertEqual(code, 0)
                mock_compact.assert_called_with(mock_helper.client, "repo1", "/git-repo", min_packs=5)

            # Invalid non-integer
            with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo", "--min-packs", "invalid"]):
                with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                    code = main()
                    self.assertEqual(code, 0)
                    self.assertIn("invalid --min-packs value", mock_err.getvalue())
                    mock_compact.assert_called_with(mock_helper.client, "repo1", "/git-repo", min_packs=2)

            # Invalid non-positive integer
            with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo", "--min-packs", "0"]):
                with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                    code = main()
                    self.assertEqual(code, 0)
                    self.assertIn("--min-packs must be a positive integer", mock_err.getvalue())
                    mock_compact.assert_called_with(mock_helper.client, "repo1", "/git-repo", min_packs=2)

            # Missing integer value
            with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo", "--min-packs"]):
                with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                    code = main()
                    self.assertEqual(code, 0)
                    self.assertIn("--min-packs requires an integer value", mock_err.getvalue())
                    mock_compact.assert_called_with(mock_helper.client, "repo1", "/git-repo", min_packs=2)

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.lfs.LFSTransferAgent")
    def test_cli_lfs_transfer(self, mock_agent_cls, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent

        with patch.object(sys, "argv", ["git-remote-seafile", "lfs-transfer", "seafile://code/repo"]):
            code = main()
            self.assertEqual(code, 0)
            mock_agent.run.assert_called_once()


class TestCLIUnknownCommand(unittest.TestCase):
    """A mistyped subcommand must not be handed to the remote-helper path.

    Git invokes the helper as ``git-remote-seafile <remote> <url>``, or with the
    URL alone.  The CLI treated *anything* it did not recognise as such a call,
    so ``git-remote-seafile chck-auth`` parsed "chck-auth" as the remote URL and
    failed with a baffling "library not found" or an authentication error --
    instead of simply saying the command does not exist.
    """

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unknown_command_reports_usage_and_exits_2(self, mock_helper_cls):
        for typo in ("chck-auth", "verison", "lfs-transfe", "--bogus"):
            with self.subTest(arg=typo):
                mock_helper_cls.reset_mock()
                with patch.object(sys, "argv", ["git-remote-seafile", typo]):
                    with patch("sys.stdout", new_callable=io.StringIO) as out:
                        with patch("sys.stderr", new_callable=io.StringIO) as err:
                            code = main()
                self.assertEqual(code, 2, f"{typo}: expected exit 2, got {code}")
                self.assertIn("Commands:", out.getvalue(), f"{typo}: no help shown")
                self.assertIn(typo, err.getvalue(), f"{typo}: not named in the error")
                mock_helper_cls.assert_not_called()

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_invocation_without_a_seafile_url_is_not_a_helper_call(self, mock_helper_cls):
        # Two arguments, but neither is a seafile:// URL -- still a typo, not a
        # helper invocation.
        with patch.object(sys, "argv", ["git-remote-seafile", "origin", "not-a-seafile-url"]):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                with patch("sys.stderr", new_callable=io.StringIO):
                    code = main()
        self.assertEqual(code, 2)
        self.assertIn("Commands:", out.getvalue())
        mock_helper_cls.assert_not_called()


class TestCLIRemoteHelperInvocation(unittest.TestCase):
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_remote_helper_run(self, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper

        with patch.object(sys, "argv", ["git-remote-seafile", "origin", "seafile://code/repo"]):
            code = main()
            self.assertEqual(code, 0)
            mock_helper.run.assert_called_once()

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_remote_helper_fatal_error(self, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.run.side_effect = RuntimeError("Fatal crash")

        with patch.object(sys, "argv", ["git-remote-seafile", "origin", "seafile://code/repo"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("git-remote-seafile fatal error: Fatal crash", mock_err.getvalue())


class TestCLIDesktopUrl(unittest.TestCase):
    def test_desktop_url_missing_arg(self):
        with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Usage:", mock_out.getvalue())

    @patch("git_remote_seafile.cli.SeafileClient")
    def test_desktop_url_matched_and_unmatched(self, mock_client_cls):
        import sqlite3
        import tempfile
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.server_url = "https://seafile.example.com"

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            ccnet_dir = fake_home / "ccnet"
            ccnet_dir.mkdir()
            rdb = ccnet_dir / "repo.db"

            con = sqlite3.connect(str(rdb))
            con.execute("CREATE TABLE RepoProperty (repo_id TEXT, key TEXT, value TEXT)")
            worktree_dir = fake_home / "Documents" / "myrepo"
            worktree_dir.mkdir(parents=True)
            con.execute("INSERT INTO RepoProperty VALUES ('r1', 'worktree', ?)", (str(worktree_dir),))
            con.commit()
            con.close()

            with patch("pathlib.Path.home", return_value=fake_home):
                # Matched
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(worktree_dir / "subdir")]):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                        code = main()
                        self.assertEqual(code, 0)
                        self.assertIn("seafile://seafile.example.com/myrepo/subdir", mock_out.getvalue())

                # Matched with plain HTTP server
                mock_client.server_url = "http://seafile.local:8000"
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(worktree_dir / "subdir")]):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                        code = main()
                        self.assertEqual(code, 0)
                        self.assertIn("seafile://http://seafile.local:8000/myrepo/subdir", mock_out.getvalue())

                # Unmatched
                outside_path = fake_home / "outside" / "dir"
                outside_path.mkdir(parents=True)
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(outside_path)]):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                        code = main()
                        self.assertEqual(code, 1)
                        self.assertIn("Could not match", mock_out.getvalue())

    @patch("git_remote_seafile.cli.SeafileClient")
    def test_desktop_url_builds_the_client_without_requiring_credentials(self, mock_client_cls):
        """The client must be constructed without demanding a token.

        `desktop-url` makes no authenticated request, so requiring credentials
        made it fail with "Could not find Seafile credentials" for users who
        have the desktop client installed but no reachable token -- the exact
        audience for the command.
        """
        import tempfile

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.server_url = "https://seafile.example.com"

        with tempfile.TemporaryDirectory() as td:
            with patch("pathlib.Path.home", return_value=Path(td)):
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(Path(td) / "x")]):
                    with patch("sys.stdout", new_callable=io.StringIO):
                        main()

        mock_client_cls.assert_called_once_with(require_credentials=False)

    @patch("git_remote_seafile.cli.SeafileClient")
    def test_desktop_url_names_the_real_problem_when_no_server_is_known(self, mock_client_cls):
        mock_client_cls.side_effect = SeafileAuthError("Could not determine the Seafile server URL.")

        with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", "/tmp/whatever"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()

        self.assertEqual(code, 1)
        self.assertIn("Cannot determine the Seafile server", mock_out.getvalue())


class TestCLILockManagement(unittest.TestCase):
    def test_lock_status_missing_args(self):
        with patch.object(sys, "argv", ["git-remote-seafile", "lock-status"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Usage:", mock_out.getvalue())

    @patch("git_remote_seafile.lock.RemoteLock")
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_lock_status_unlocked(self, mock_helper_cls, mock_lock_cls):
        mock_lock = MagicMock()
        mock_lock_cls.return_value = mock_lock
        mock_lock.get_status.return_value = {"locked": False}

        with patch.object(sys, "argv", ["git-remote-seafile", "lock-status", "seafile://code/myrepo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("UNLOCKED", mock_out.getvalue())

    @patch("git_remote_seafile.lock.RemoteLock")
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_lock_status_locked(self, mock_helper_cls, mock_lock_cls):
        mock_lock = MagicMock()
        mock_lock_cls.return_value = mock_lock
        mock_lock.get_status.return_value = {
            "locked": True,
            "owner": "alice",
            "machine": "nodeA",
            "pid": 1234,
            "nonce": "nonce123",
            "protocol": "ticket",
            "expires_in": 45,
        }

        with patch.object(sys, "argv", ["git-remote-seafile", "lock-status", "seafile://code/myrepo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                output = mock_out.getvalue()
                self.assertIn("LOCKED", output)
                self.assertIn("alice", output)
                self.assertIn("nodeA", output)
                self.assertIn("1234", output)

    def test_unlock_missing_args(self):
        with patch.object(sys, "argv", ["git-remote-seafile", "unlock"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Usage:", mock_out.getvalue())

    @patch("git_remote_seafile.lock.RemoteLock")
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unlock_success(self, mock_helper_cls, mock_lock_cls):
        mock_lock = MagicMock()
        mock_lock_cls.return_value = mock_lock

        with patch.object(sys, "argv", ["git-remote-seafile", "unlock", "seafile://code/myrepo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("Unlocked repository", mock_out.getvalue())
                mock_lock.unlock.assert_called_once_with(force=False)

    @patch("git_remote_seafile.lock.RemoteLock")
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unlock_force(self, mock_helper_cls, mock_lock_cls):
        mock_lock = MagicMock()
        mock_lock_cls.return_value = mock_lock

        with patch.object(sys, "argv", ["git-remote-seafile", "unlock", "seafile://code/myrepo", "--force"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 0)
                self.assertIn("Forcibly unlocked repository", mock_out.getvalue())
                mock_lock.unlock.assert_called_once_with(force=True)

    @patch("git_remote_seafile.lock.RemoteLock")
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unlock_held_by_other_fails_without_force(self, mock_helper_cls, mock_lock_cls):
        from git_remote_seafile.lock import RepositoryLockedError
        mock_lock = MagicMock()
        mock_lock_cls.return_value = mock_lock
        mock_lock.unlock.side_effect = RepositoryLockedError("Repository is locked by bob on otherhost")

        with patch.object(sys, "argv", ["git-remote-seafile", "unlock", "seafile://code/myrepo"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Error: Repository is locked by bob on otherhost", mock_err.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_cli_uppercase_scheme_dispatches_to_helper(self, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        with patch.object(sys, "argv", ["git-remote-seafile", "origin", "SEAFILE://code/myrepo"]):
            code = main()
            self.assertEqual(code, 0)
            mock_helper_cls.assert_called_once_with("origin", "SEAFILE://code/myrepo")
            mock_helper.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
