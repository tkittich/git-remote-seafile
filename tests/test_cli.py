"""test_cli.py - Unit tests for CLI entry points and subcommands."""

from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from git_remote_seafile.cli import _COMMANDS, main, print_command_help
from git_remote_seafile.client import SeafileAuthError
from git_remote_seafile.lock import RepositoryLockedError
from git_remote_seafile.safety import SafetyError


class TestDoctor(unittest.TestCase):
    def _run(self, url=None, landed=b"git-seafile doctor", **overrides):
        client = MagicMock()
        client.server_url = "https://s.example"
        client.session.get.return_value = MagicMock(status_code=200, json=lambda: {"email": "me@x"})
        client.get_file_bytes.return_value = landed
        client.delete_entry.return_value = True
        helper = MagicMock(client=client, library_name="lib", repo_path="/git-repo",
                           repo_id="rid", path_defaulted=True)
        for k, v in overrides.items():
            setattr(helper, k, v)
        argv = ["git-remote-seafile", "doctor"] + ([url] if url else [])
        out = io.StringIO()
        with patch.object(sys, "argv", argv), patch("sys.stdout", out),              patch("git_remote_seafile.cli.SeafileClient", return_value=client),              patch("git_remote_seafile.cli.RemoteHelper", return_value=helper),              patch("git_remote_seafile.cli.get_local_work_tree", return_value=None),              patch("git_remote_seafile.cli.discover_local_synced_libraries", return_value=[]),              patch("git_remote_seafile.cli.check_preflight_safety", return_value=[]):
            code = main()
        return code, out.getvalue(), client

    def test_login_only(self):
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("logged in to https://s.example as me@x", out)

    def test_full_run_interprets_the_url_and_probes_a_write(self):
        code, out, client = self._run("seafile://lib")
        self.assertEqual(code, 0, out)
        self.assertIn("library 'lib', path '/git-repo'", out)
        self.assertIn("no path was given", out)
        self.assertIn("can write and delete", out)
        client.upload_file.assert_called_once()
        client.delete_entry.assert_called_once()

    def test_an_account_answer_that_is_not_an_object_fails_without_a_traceback(self):
        """A 200 carrying a JSON array (or no JSON) is a proxy or a login page;
        it killed the first step of ``doctor`` with an AttributeError."""
        def not_json():
            raise ValueError("no json")

        for body in (lambda: ["not", "an", "object"], not_json):
            client = MagicMock()
            client.server_url = "https://s.example"
            client.session.get.return_value = MagicMock(status_code=200, json=body)
            out = io.StringIO()
            with patch.object(sys, "argv", ["git-remote-seafile", "doctor"]), \
                    patch("sys.stdout", out), \
                    patch("git_remote_seafile.cli.SeafileClient", return_value=client):
                code = main()
            self.assertEqual(code, 1, out.getvalue())
            self.assertIn("[FAIL]", out.getvalue())
            self.assertIn("not a Seafile account object", out.getvalue())

    def test_a_probe_that_does_not_round_trip_fails(self):
        code, out, _ = self._run("seafile://lib/x", landed=b"something else")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] write probe", out)


class TestTerminationUnwinds(unittest.TestCase):
    def test_sigterm_runs_finally_blocks_so_a_lock_ticket_is_released(self):
        """A bare SIGTERM skipped ``RemoteLock.__exit__`` and left the ticket on
        the server for a full lease; it must unwind like an exception."""
        import signal

        from git_remote_seafile.cli import _unwind_on_termination

        saved = {n: signal.getsignal(n) for n in (signal.SIGTERM,)}
        try:
            _unwind_on_termination()
            released = []
            with self.assertRaises(SystemExit) as ctx:
                try:
                    signal.raise_signal(signal.SIGTERM)
                finally:
                    released.append("ticket deleted")
            self.assertEqual(released, ["ticket deleted"])
            self.assertEqual(ctx.exception.code, 128 + int(signal.SIGTERM))
        finally:
            for number, handler in saved.items():
                signal.signal(number, handler)

    def test_main_installs_the_handler_before_anything_else(self):
        import signal

        before = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(sys, "argv", ["git-remote-seafile", "--version"]),                     patch("sys.stdout", new_callable=io.StringIO):
                main()
            self.assertNotEqual(signal.getsignal(signal.SIGTERM), before)
        finally:
            signal.signal(signal.SIGTERM, before)


class TestGcTail(unittest.TestCase):
    def _run(self, result):
        helper = MagicMock(repo_id="rid", repo_path="/r")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["git-remote-seafile", "gc", "--tail", "seafile://lib/r"]),              patch("sys.stdout", out), patch("sys.stderr", err),              patch("git_remote_seafile.cli.RemoteHelper", return_value=helper),              patch("git_remote_seafile.gc.tail_merge", return_value=result) as merge:
            code = main()
        return code, out.getvalue(), err.getvalue(), merge

    def test_ok_reports_what_was_merged(self):
        code, out, _, merge = self._run({
            "status": "ok", "merged_packs": 5, "frozen_packs": 2,
            "new_pack_bytes": 1024, "tail_bytes": 4096,
        })
        self.assertEqual(code, 0)
        self.assertIn("Merged 5 small packfiles", out)
        self.assertIn("2 large packfile(s) left in place", out)
        merge.assert_called_once()

    def test_skipped_is_not_a_failure(self):
        code, out, _, _ = self._run({"status": "skipped", "message": "Only 1 movable packfile(s)"})
        self.assertEqual(code, 0)
        self.assertIn("Only 1 movable", out)

    def test_error_exits_nonzero_on_stderr(self):
        code, _, err, _ = self._run({"status": "error", "message": "boom"})
        self.assertEqual(code, 1)
        self.assertIn("boom", err)


class TestStderrEncoding(unittest.TestCase):
    def test_main_makes_stderr_utf8_with_replacement(self):
        fake_err = MagicMock()
        with patch.object(sys, "argv", ["git-remote-seafile", "version"]),              patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", fake_err):
            main()
        fake_err.reconfigure.assert_called_with(encoding="utf-8", errors="replace")


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


class TestCLICommandHelp(unittest.TestCase):
    """D17: `--help` worked only with no subcommand, and the options existed
    nowhere but the strings that happened to print on a usage error."""

    def _run(self, argv: list[str]) -> tuple[int, str]:
        with patch.object(sys, "argv", ["git-remote-seafile", *argv]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
        return code, mock_out.getvalue()

    def test_every_subcommand_has_its_own_help(self):
        for name, usage, _, _ in _COMMANDS:
            with self.subTest(command=name):
                code, output = self._run([name, "--help"])
                self.assertEqual(code, 0)
                if name == "snapshot":
                    # The snapshot tool renders its own (much longer) help.
                    self.assertIn("--payload-min-size", output)
                    continue
                self.assertTrue(
                    output.startswith(f"Usage: {usage}\n"),
                    f"{name} --help printed {output!r}",
                )

    def test_the_listing_and_the_command_help_agree(self):
        """Both render from _COMMANDS, so the usage line cannot drift."""
        for name, usage, _, _ in _COMMANDS:
            with self.subTest(command=name):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    print_command_help(name)
                self.assertTrue(mock_out.getvalue().startswith(f"Usage: {usage}\n"))

    def test_the_help_flags_are_documented(self):
        _, listing = self._run([])
        self.assertIn("--min-packs", listing)
        self.assertIn("--force", listing)

        _, gc_help = self._run(["gc", "--help"])
        self.assertIn("--min-packs N", gc_help)

        _, unlock_help = self._run(["unlock", "--help"])
        self.assertIn("--force", unlock_help)

    def test_subcommand_help_never_reaches_the_network(self):
        with patch("git_remote_seafile.cli.RemoteHelper") as mock_helper_cls:
            code, _ = self._run(["gc", "--help"])
        self.assertEqual(code, 0)
        mock_helper_cls.assert_not_called()


class TestCLIFlagPlacement(unittest.TestCase):
    """D14: an option written before the URL was read as the URL.

    `unlock --force seafile://host/lib` reported "library not found:
    '--force'", and `gc --min-packs 3 seafile://host/lib` reported
    "library not found: '--min-packs'".
    """

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.gc.compact_repository")
    def test_gc_reads_the_url_past_a_leading_flag(self, mock_compact, mock_helper_cls):
        mock_compact.return_value = {"status": "skipped", "message": "Only 0 packfile(s) present."}
        with patch.object(sys, "argv", ["git-remote-seafile", "gc", "--min-packs", "3", "seafile://code/repo"]):
            with patch("sys.stdout", new_callable=io.StringIO):
                code = main()
        self.assertEqual(code, 0)
        mock_helper_cls.assert_called_once_with("gc", "seafile://code/repo")
        self.assertEqual(mock_compact.call_args.kwargs["min_packs"], 3)

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.gc.compact_repository")
    def test_gc_accepts_the_equals_form(self, mock_compact, mock_helper_cls):
        mock_compact.return_value = {"status": "skipped", "message": "Only 0 packfile(s) present."}
        with patch.object(sys, "argv", ["git-remote-seafile", "gc", "--min-packs=4", "seafile://code/repo"]):
            with patch("sys.stdout", new_callable=io.StringIO):
                code = main()
        self.assertEqual(code, 0)
        mock_helper_cls.assert_called_once_with("gc", "seafile://code/repo")
        self.assertEqual(mock_compact.call_args.kwargs["min_packs"], 4)

    @patch("git_remote_seafile.cli.RemoteLock")
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unlock_reads_the_url_past_force(self, mock_helper_cls, mock_lock_cls):
        with patch.object(sys, "argv", ["git-remote-seafile", "unlock", "--force", "seafile://code/myrepo"]):
            with patch("sys.stdout", new_callable=io.StringIO):
                code = main()
        self.assertEqual(code, 0)
        mock_helper_cls.assert_called_once_with("unlock", "seafile://code/myrepo")
        mock_lock_cls.return_value.unlock.assert_called_once_with(force=True)

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unlock_with_only_flags_prints_usage(self, mock_helper_cls):
        with patch.object(sys, "argv", ["git-remote-seafile", "unlock", "--force"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
        self.assertEqual(code, 2)
        self.assertIn("Usage:", mock_out.getvalue())
        mock_helper_cls.assert_not_called()


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
    def test_check_auth_with_a_body_that_is_not_an_object_is_an_error(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.server_url = "https://seafile.example.com"
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = ["not", "an", "object"]
        mock_client.session.get.return_value = mock_resp

        with patch.object(sys, "argv", ["git-remote-seafile", "check-auth"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                code = main()
        self.assertEqual(code, 1)
        self.assertIn("not a Seafile account object", mock_err.getvalue())

    @patch("git_remote_seafile.cli.SeafileClient")
    def test_check_auth_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.server_url = "https://seafile.example.com"
        mock_resp = MagicMock(status_code=401, text="Unauthorized")
        mock_client.session.get.return_value = mock_resp

        with patch.object(sys, "argv", ["git-remote-seafile", "check-auth"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Authentication failed", mock_out.getvalue())

    @patch("git_remote_seafile.cli.SeafileClient")
    def test_check_auth_exception(self, mock_client_cls):
        mock_client_cls.side_effect = RuntimeError("Network error")
        with patch.object(sys, "argv", ["git-remote-seafile", "check-auth"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Error: Network error", mock_out.getvalue())


class TestCLICheckSafety(unittest.TestCase):
    def test_check_safety_missing_args(self):
        with patch.object(sys, "argv", ["git-remote-seafile", "check-safety"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 2)
                self.assertIn("Usage:", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.cli.check_preflight_safety")
    @patch("git_remote_seafile.cli.discover_local_synced_libraries")
    @patch("git_remote_seafile.cli.get_local_work_tree")
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
    @patch("git_remote_seafile.cli.check_preflight_safety")
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
    @patch("git_remote_seafile.cli.check_preflight_safety")
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
    def test_test_command_surfaces_helper_failure_cleanly(self, mock_helper_cls):
        """A raising RemoteHelper must yield the intended message, not a traceback.

        Regression for the UnboundLocalError: the ``except SafetyError`` clause
        named a symbol imported *inside* the try but *after* RemoteHelper was
        constructed, so any failure before that import crashed the handler
        itself and the user saw a raw traceback instead of "Test failed: ...".
        """
        mock_helper_cls.side_effect = RuntimeError("network down")

        with patch.object(sys, "argv", ["git-remote-seafile", "test", "seafile://code/repo"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Test failed: network down", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_test_command_reports_safety_error_cleanly(self, mock_helper_cls):
        mock_helper_cls.side_effect = SafetyError("library root pollution")

        with patch.object(sys, "argv", ["git-remote-seafile", "test", "seafile://code/repo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("[Safety Warning] library root pollution", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unlock_command_surfaces_helper_failure_cleanly(self, mock_helper_cls):
        mock_helper_cls.side_effect = RuntimeError("no credentials")

        with patch.object(sys, "argv", ["git-remote-seafile", "unlock", "seafile://code/repo"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Failed to unlock repository: no credentials", mock_err.getvalue())

    @patch("git_remote_seafile.cli.RemoteLock")
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unlock_reports_locked_error_cleanly(self, mock_helper_cls, mock_lock_cls):
        mock_helper_cls.return_value = MagicMock()
        mock_lock_cls.return_value.unlock.side_effect = RepositoryLockedError("held by someone else")

        with patch.object(sys, "argv", ["git-remote-seafile", "unlock", "seafile://code/repo"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                code = main()
                self.assertEqual(code, 1)
                self.assertIn("Error: held by someone else", mock_err.getvalue())

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
        self.assertIn("grew by ~7.0 KiB", output)
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

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.gc.compact_repository")
    def test_cli_gc_error_status_exits_nonzero(self, mock_gc, mock_helper_cls):
        """An aborted compaction is a failure; scripts gate on the exit code."""
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.client = MagicMock()
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "repo"

        mock_gc.return_value = {
            "status": "error",
            "message": "Packfile pack-1.pack is listed at /repo/objects/pack but could not be downloaded",
        }
        with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
        self.assertEqual(code, 1)
        self.assertIn("could not be downloaded", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteHelper")
    @patch("git_remote_seafile.gc.compact_repository")
    def test_cli_gc_incomplete_status_exits_nonzero(self, mock_gc, mock_helper_cls):
        """N6 -- a compaction that stopped short is not a success.

        The consolidation is verified and nothing was lost, so this is not an
        error either.  It is a third outcome, and a scheduled job that reads only
        the exit code has to be able to tell it from a finished compaction.
        """
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.client = MagicMock()
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "repo"

        mock_gc.return_value = {
            "status": "incomplete",
            "old_packs": 3,
            "new_packs": 1,
            "saved_kb": 40,
            "cleanup_stopped": "stopped deleting obsolete packfiles: lease lost.",
        }
        with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()

        output = mock_out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Compacted 3 packfiles into 1", output)
        self.assertIn("did not finish removing the superseded ones", output)
        self.assertIn("stopped deleting obsolete packfiles", output)


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
                self.assertIn("(was " + "1" * 40 + ")", mock_out.getvalue())

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
                mock_compact.assert_called_with(
                    mock_helper.client, "repo1", "/git-repo", min_packs=5, config=ANY
                )

            # Invalid non-integer
            with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo", "--min-packs", "invalid"]):
                with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                    code = main()
                    self.assertEqual(code, 0)
                    self.assertIn("invalid --min-packs value", mock_err.getvalue())
                    mock_compact.assert_called_with(
                        mock_helper.client, "repo1", "/git-repo", min_packs=2, config=ANY
                    )

            # Invalid non-positive integer
            with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo", "--min-packs", "0"]):
                with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                    code = main()
                    self.assertEqual(code, 0)
                    self.assertIn("--min-packs must be a positive integer", mock_err.getvalue())
                    mock_compact.assert_called_with(
                        mock_helper.client, "repo1", "/git-repo", min_packs=2, config=ANY
                    )

            # Missing integer value
            with patch.object(sys, "argv", ["git-remote-seafile", "gc", "seafile://code/repo", "--min-packs"]):
                with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                    code = main()
                    self.assertEqual(code, 0)
                    self.assertIn("--min-packs requires an integer value", mock_err.getvalue())
                    mock_compact.assert_called_with(
                        mock_helper.client, "repo1", "/git-repo", min_packs=2, config=ANY
                    )

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
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 2)
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
                # Matched.  The library segment is the *repo id* ('r1'), not the
                # worktree folder name ('myrepo'): the folder is user-named and
                # need not match the library on the server.
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(worktree_dir / "subdir")]):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                        code = main()
                        self.assertEqual(code, 0)
                        self.assertIn("seafile://https://seafile.example.com/r1/subdir", mock_out.getvalue())

                # A server served under a path cannot be spelled in a
                # seafile:// URL (the segment after the host is the library),
                # so the short form is printed and the note names the setting.
                mock_client.server_url = "https://corp.example.com/seafile"
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(worktree_dir / "subdir")]):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_out,                             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                        code = main()
                        self.assertEqual(code, 0)
                        self.assertEqual(mock_out.getvalue().strip(), "seafile://r1/subdir")
                        self.assertIn("SEAFILE_SERVER=https://corp.example.com/seafile", mock_err.getvalue())

                # Matched with plain HTTP server
                mock_client.server_url = "http://seafile.local:8000"
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(worktree_dir / "subdir")]):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                        code = main()
                        self.assertEqual(code, 0)
                        self.assertIn("seafile://http://seafile.local:8000/r1/subdir", mock_out.getvalue())

                # Matched with mixed-case scheme (RFC 3986: schemes are caseless).
                # The scheme is emitted lowercased; the host keeps its spelling.
                mock_client.server_url = "HTTP://Seafile.Local:8000"
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(worktree_dir / "subdir")]):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                        code = main()
                        self.assertEqual(code, 0)
                        self.assertIn("seafile://http://Seafile.Local:8000/r1/subdir", mock_out.getvalue())

                # Matched with a bare host (no scheme at all).  The scheme is
                # supplied rather than emitted bare: a scheme-less first segment
                # is read as a *library* unless it looks like a host, so the old
                # bare spelling was the one form the grammar could misread.
                mock_client.server_url = "barehost.local"
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(worktree_dir / "subdir")]):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                        code = main()
                        self.assertEqual(code, 0)
                        self.assertIn("seafile://https://barehost.local/r1/subdir", mock_out.getvalue())

                # Unmatched
                outside_path = fake_home / "outside" / "dir"
                outside_path.mkdir(parents=True)
                with patch.object(sys, "argv", ["git-remote-seafile", "desktop-url", str(outside_path)]):
                    with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                        code = main()
                        self.assertEqual(code, 1)
                        self.assertIn("Could not match", mock_out.getvalue())

    @patch("git_remote_seafile.cli.SeafileClient")
    def test_desktop_url_emits_a_url_the_parser_reads_back(self, mock_client_cls):
        """The generated URL must round-trip: repo id, quoting, and the root.

        A12.16: the command emitted the *folder* name, left spaces and '#' in a
        path unencoded, and rendered the library root as a '.' path component --
        which the URL grammar refuses outright, so the URL it printed could not
        be pasted back into a remote.
        """
        import sqlite3
        import tempfile

        from git_remote_seafile.url import parse_seafile_url

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
            # A worktree whose *folder* name differs from the library name, and a
            # subfolder carrying a space and a '#' -- both meaningful in a URL.
            worktree_dir = fake_home / "Documents" / "Not The Library Name"
            (worktree_dir / "sub dir#1").mkdir(parents=True)
            con.execute(
                "INSERT INTO RepoProperty VALUES ('repo-uuid-42', 'worktree', ?)",
                (str(worktree_dir),),
            )
            con.commit()
            con.close()

            def run(path):
                with patch("pathlib.Path.home", return_value=fake_home):
                    with patch.object(
                        sys, "argv", ["git-remote-seafile", "desktop-url", str(path)]
                    ):
                        with patch("sys.stdout", new_callable=io.StringIO) as out:
                            code = main()
                return code, out.getvalue().strip()

            # 1. The library root: no path segment at all, and the URL still parses
            #    (a '.' component would have been refused).
            code, text = run(worktree_dir)
            self.assertEqual(code, 0)
            self.assertEqual(text, "seafile://https://seafile.example.com/repo-uuid-42")
            root = parse_seafile_url(text)
            self.assertEqual(root.library_name, "repo-uuid-42")
            self.assertEqual(root.repo_path, "/git-repo")

            # 2. A path with a space and a '#': encoded, so it survives the URL and
            #    reads back unchanged (a bare '#' would truncate it to a fragment).
            code, text = run(worktree_dir / "sub dir#1")
            self.assertEqual(code, 0)
            self.assertNotIn(" ", text)
            self.assertNotIn("#", text)
            nested = parse_seafile_url(text)
            self.assertEqual(nested.library_name, "repo-uuid-42")
            self.assertEqual(nested.repo_path, "/sub dir#1")

            # 3. Containment ignores case on Windows, where the filesystem does.
            #    Older Pythons compare `relative_to` case-sensitively there, so a
            #    differently-cased spelling of the same directory must still match
            #    rather than reporting "no library matched".
            if os.name == "nt":
                code, text = run(Path(str(worktree_dir).upper()))
                self.assertEqual(code, 0)
                self.assertIn("repo-uuid-42", text)

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
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()

        self.assertEqual(code, 1)
        self.assertIn("Cannot determine the Seafile server", mock_out.getvalue())


class TestCLILockManagement(unittest.TestCase):
    def test_lock_status_missing_args(self):
        with patch.object(sys, "argv", ["git-remote-seafile", "lock-status"]):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 2)
                self.assertIn("Usage:", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteLock")
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

    @patch("git_remote_seafile.cli.RemoteLock")
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
            with patch("sys.stderr", new_callable=io.StringIO) as mock_out:
                code = main()
                self.assertEqual(code, 2)
                self.assertIn("Usage:", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteLock")
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

    @patch("git_remote_seafile.cli.RemoteLock")
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_unlock_when_nothing_is_locked_says_so(self, mock_helper_cls, mock_lock_cls):
        # unlock() used to return True unconditionally, so the CLI reported a
        # successful unlock for a repository that had no lock at all.
        mock_lock = MagicMock()
        mock_lock.unlock.return_value = False
        mock_lock_cls.return_value = mock_lock

        with patch.object(sys, "argv", ["git-remote-seafile", "unlock", "seafile://code/myrepo"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                code = main()
        self.assertEqual(code, 0)
        self.assertIn("not locked; nothing to release", mock_out.getvalue())
        self.assertNotIn("Unlocked repository", mock_out.getvalue())

    @patch("git_remote_seafile.cli.RemoteLock")
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

    @patch("git_remote_seafile.cli.RemoteLock")
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


class TestModuleEntryPoint(unittest.TestCase):
    """``python -m git_remote_seafile`` is a documented way to run the tool.

    The CHANGELOG advertises it ("to allow running the tool directly via
    ``python -m git_remote_seafile``"), so the wiring has to keep working: the
    module must reach ``cli.main`` and hand its return value to ``sys.exit``,
    because that exit status is what the shell -- and git -- actually sees.

    Run through ``runpy`` rather than a subprocess on purpose: a child process's
    coverage is invisible to the parent run, so a subprocess test would leave
    ``__main__.py`` at 0% while claiming to cover it.
    """

    def test_running_the_module_dispatches_to_the_cli(self):
        import runpy

        with patch("sys.argv", ["git-remote-seafile", "--version"]), patch(
            "git_remote_seafile.cli.main", return_value=0
        ) as mock_main, patch("sys.exit") as mock_exit:
            runpy.run_module("git_remote_seafile.__main__", run_name="__main__")

        mock_main.assert_called_once()
        mock_exit.assert_called_once_with(0)

    def test_a_failing_command_still_sets_a_nonzero_exit_status(self):
        """A non-zero return from main must reach sys.exit unchanged.

        If the entry point swallowed the status, every failure -- a locked
        repository, an unreachable server, a bad URL -- would look like success
        to any script driving the tool.
        """
        import runpy

        with patch("sys.argv", ["git-remote-seafile", "test", "seafile://code/repo"]), patch(
            "git_remote_seafile.cli.main", return_value=3
        ), patch("sys.exit") as mock_exit:
            runpy.run_module("git_remote_seafile.__main__", run_name="__main__")

        mock_exit.assert_called_once_with(3)



class TestRemoteNamedLikeASubcommand(unittest.TestCase):
    """git runs `git-remote-seafile <remote name> <url>`; a remote may be named `gc`."""

    URL = "seafile://lib/repo"

    def _stdin(self, payload: bytes):
        return io.TextIOWrapper(io.BufferedReader(io.BytesIO(payload)), encoding="utf-8")

    def _run(self, argv, *, git_dir, stdin_bytes, tty=False):
        env = {"GIT_DIR": ".git"} if git_dir else {}
        stdin = self._stdin(stdin_bytes)
        if tty:
            stdin.isatty = lambda: True  # type: ignore[method-assign]
        handler = MagicMock(return_value=0)
        with patch.object(sys, "argv", ["git-remote-seafile", *argv]), \
                patch.dict("os.environ", env, clear=False), \
                patch("sys.stdin", stdin), \
                patch("git_remote_seafile.cli._run_helper", return_value=0) as helper, \
                patch.dict("git_remote_seafile.cli._HANDLERS", {"gc": handler, "test": handler}):
            if not git_dir:
                os.environ.pop("GIT_DIR", None)
            main()
        return helper, handler

    def test_git_driving_a_remote_named_gc_reaches_the_helper_not_the_compactor(self):
        helper, handler = self._run(["gc", self.URL], git_dir=True, stdin_bytes=b"capabilities\n")
        helper.assert_called_once()
        handler.assert_not_called()

    def test_a_person_running_the_subcommand_still_gets_the_subcommand(self):
        # no GIT_DIR, and a terminal
        helper, handler = self._run(["gc", self.URL], git_dir=False, stdin_bytes=b"", tty=True)
        handler.assert_called_once()
        helper.assert_not_called()

    def test_a_hook_calling_the_subcommand_is_not_mistaken_for_git(self):
        # GIT_DIR is set in a hook, but what is on stdin is not the protocol
        helper, handler = self._run(
            ["test", self.URL], git_dir=True, stdin_bytes=b"refs/heads/main abc refs/heads/main abc\n"
        )
        handler.assert_called_once()
        helper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
