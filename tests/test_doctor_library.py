"""test_doctor_library.py - doctor creates a missing library and reports its history."""

from __future__ import annotations

import io
import sys
import unittest
from unittest.mock import MagicMock, patch

from git_remote_seafile.cli import main
from git_remote_seafile.client import SeafileClient, SeafilePermanentError, SeafileAPIError
from git_remote_seafile.library_setup import (
    RECOMMENDED_HISTORY_DAYS,
    history_finding,
    looks_like_library_id,
)

MISSING = "Seafile library not found: 'newlib'. Available libraries: code."


class _Helper:
    """What doctor needs of a RemoteHelper; the library resolves only once it exists."""

    def __init__(self, exists: bool, history=30, library="newlib"):
        self.client = MagicMock()
        self.client.server_url = "https://s.example"
        self.client.session.get.return_value = MagicMock(status_code=200, json=lambda: {"email": "me@x"})
        self.client.get_file_bytes.return_value = b"git-seafile doctor"
        self.client.delete_entry.return_value = True
        self.client.list_dir.return_value = []
        self.client.create_library.return_value = "new-id"
        self.client.set_history_limit.return_value = True
        self.client.get_history_limit.return_value = history
        self.library_name = library
        self.repo_path = "/git-repo"
        self.path_defaulted = False
        self._rid = "rid" if exists else None

    @property
    def repo_id(self):
        if self._rid is None:
            raise SeafilePermanentError(MISSING)
        return self._rid

    @repo_id.setter
    def repo_id(self, value):
        self._rid = value


def run_doctor(helper, *flags, tty=False, answer=""):
    out, err = io.StringIO(), io.StringIO()
    argv = ["git-remote-seafile", "doctor", "seafile://newlib/x", *flags]
    stdin = MagicMock()
    stdin.isatty.return_value = tty
    with patch.object(sys, "argv", argv), patch("sys.stdout", out), patch("sys.stderr", err), \
            patch("sys.stdin", stdin), patch("builtins.input", return_value=answer), \
            patch("git_remote_seafile.cli.SeafileClient", return_value=helper.client), \
            patch("git_remote_seafile.cli.RemoteHelper", return_value=helper), \
            patch("git_remote_seafile.cli.get_local_work_tree", return_value=None), \
            patch("git_remote_seafile.cli.discover_local_synced_libraries", return_value=[]), \
            patch("git_remote_seafile.cli.check_preflight_safety", return_value=[]):
        code = main()
    return code, out.getvalue(), err.getvalue()


class TestCreateLibrary(unittest.TestCase):
    def test_a_missing_library_is_not_created_unasked(self):
        helper = _Helper(exists=False)
        code, out, _ = run_doctor(helper)
        self.assertEqual(code, 1)
        # A close-match miss may be a typo, so it gets the hint after the failure.
        self.assertIn("[FAIL] Seafile library not found", out)
        self.assertIn("Hint: if 'newlib' is not a typo, re-run with --create-library", out)
        helper.client.create_library.assert_not_called()

    def test_a_message_that_already_names_the_flag_is_not_given_a_second_hint(self):
        helper = _Helper(exists=False)
        message = MISSING + " If it does not exist yet, create it in the Seafile web UI, or run " \
                            "'git-remote-seafile doctor <seafile://url> --create-library'."
        with patch.object(_Helper, "repo_id", property(
                lambda self: (_ for _ in ()).throw(SeafilePermanentError(message)))):
            code, out, _ = run_doctor(helper)
        self.assertEqual(code, 1)
        self.assertIn("--create-library'.", out)
        self.assertNotIn("Hint:", out)

    def test_create_library_makes_it_with_a_short_history_and_goes_on(self):
        helper = _Helper(exists=False)
        code, out, _ = run_doctor(helper, "--create-library")
        self.assertEqual(code, 0, out)
        helper.client.create_library.assert_called_once_with("newlib")
        helper.client.set_history_limit.assert_called_once_with("new-id", RECOMMENDED_HISTORY_DAYS)
        self.assertIn("created library 'newlib', keeping 30 days of history", out)
        self.assertIn("can write and delete", out)

    def test_on_a_terminal_it_asks_and_a_no_creates_nothing(self):
        helper = _Helper(exists=False)
        code, out, _ = run_doctor(helper, tty=True, answer="n")
        self.assertEqual(code, 1)
        helper.client.create_library.assert_not_called()

    def test_on_a_terminal_a_yes_creates_it(self):
        helper = _Helper(exists=False)
        code, out, _ = run_doctor(helper, tty=True, answer="y")
        self.assertEqual(code, 0, out)
        helper.client.create_library.assert_called_once_with("newlib")

    def test_an_existing_library_is_never_touched(self):
        helper = _Helper(exists=True)
        code, out, _ = run_doctor(helper, "--create-library")
        self.assertEqual(code, 0, out)
        helper.client.create_library.assert_not_called()

    def test_a_server_that_refuses_creation_says_why(self):
        helper = _Helper(exists=False)
        helper.client.create_library.side_effect = SeafileAPIError("HTTP 403 quota")
        code, out, _ = run_doctor(helper, "--create-library")
        self.assertEqual(code, 1)
        self.assertIn("could not create library 'newlib': HTTP 403 quota", out)

    def test_a_history_the_server_would_not_set_is_a_warning_not_a_failure(self):
        helper = _Helper(exists=False)
        helper.client.set_history_limit.return_value = False
        code, out, _ = run_doctor(helper, "--create-library")
        self.assertEqual(code, 0, out)
        self.assertIn("the history of 'newlib' could not be set", out)

    def test_a_library_id_is_never_created_by_name(self):
        helper = _Helper(exists=False, library="123e4567-e89b-12d3-a456-426614174000")
        code, out, _ = run_doctor(helper, "--create-library")
        self.assertEqual(code, 1)
        helper.client.create_library.assert_not_called()
        self.assertTrue(looks_like_library_id(helper.library_name))
        self.assertFalse(looks_like_library_id("code"))

    def test_the_flags_need_a_destination(self):
        with patch.object(sys, "argv", ["git-remote-seafile", "doctor", "--create-library"]), \
                patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(main(), 2)
        self.assertIn("need a <seafile://url> to act on", err.getvalue())


class TestHistory(unittest.TestCase):
    def test_full_history_and_none_are_warned_about_and_a_short_one_is_fine(self):
        self.assertEqual(history_finding(30), ("ok", "the library keeps 30 days of history"))
        self.assertEqual(history_finding(-1)[0], "warn")
        self.assertIn("keeps its full history", history_finding(-1)[1])
        self.assertEqual(history_finding(0)[0], "warn")
        self.assertIn("keeps no history", history_finding(0)[1])
        self.assertEqual(history_finding(None), ("skip", ""))

    def test_doctor_reports_the_history_it_reads(self):
        code, out, _ = run_doctor(_Helper(exists=True, history=-1))
        self.assertEqual(code, 0, out)
        self.assertIn("Warning: the library keeps its full history", out)
        code, out, _ = run_doctor(_Helper(exists=True, history=14))
        self.assertIn("[ ok ] the library keeps 14 days of history", out)

    def test_set_history_changes_it_and_says_so(self):
        helper = _Helper(exists=True)
        code, out, _ = run_doctor(helper, "--set-history", "7")
        self.assertEqual(code, 0, out)
        helper.client.set_history_limit.assert_called_once_with("rid", 7)
        self.assertIn("the library now keeps 7 days of history", out)

    def test_a_history_the_server_will_not_change_is_a_warning(self):
        helper = _Helper(exists=True)
        helper.client.set_history_limit.return_value = False
        code, out, _ = run_doctor(helper, "--set-history", "7")
        self.assertEqual(code, 0, out)
        self.assertIn("the server did not accept a history of 7 days", out)

    def test_set_history_rejects_a_silly_number(self):
        with patch.object(sys, "argv", ["git-remote-seafile", "doctor", "seafile://l/x",
                                        "--set-history", "0"]), patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(main(), 2)
        self.assertIn("whole number of days from 1 to 3650", err.getvalue())


class TestDoctorFlagsAStaleLfsLibrary(unittest.TestCase):
    def test_a_repository_whose_lfs_points_at_the_old_library_is_warned_about(self):
        stale = ("seafile://old/proj", ["seafile://code/proj"])
        with patch("git_remote_seafile.lfs_setup.stale_lfs_destination", return_value=stale):
            code, out, _ = run_doctor(_Helper(exists=True))
        self.assertEqual(code, 0, out)
        self.assertIn("Git LFS in this repository sends large files to seafile://old/proj", out)
        self.assertIn("run 'git-remote-seafile lfs-setup'", out)

    def test_nothing_is_said_when_lfs_agrees(self):
        with patch("git_remote_seafile.lfs_setup.stale_lfs_destination", return_value=None):
            code, out, _ = run_doctor(_Helper(exists=True))
        self.assertNotIn("Git LFS", out)


class TestAuthHintsNameLogin(unittest.TestCase):
    def test_doctor_with_a_rejected_token_says_how_to_sign_in_again(self):
        helper = _Helper(exists=True)
        helper.client.session.get.return_value = MagicMock(status_code=401)
        code, out, _ = run_doctor(helper)
        self.assertEqual(code, 1)
        self.assertIn("rejected the token (HTTP 401); run 'git-remote-seafile login'", out)

    def test_a_refused_library_listing_names_login(self):
        client = SeafileClient(server_url="https://s.example", token="t", require_credentials=False)
        client.session = MagicMock()
        client.session.get.return_value = MagicMock(status_code=401, text="")
        with self.assertRaises(SeafilePermanentError) as ctx:
            client.get_repo_id("code")
        self.assertIn("log in again with 'git-remote-seafile login'", str(ctx.exception))


class TestClientLibraryCalls(unittest.TestCase):
    def setUp(self):
        self.client = SeafileClient(server_url="https://s.example", token="t", require_credentials=False)
        self.client.session = MagicMock()

    def test_create_library_posts_the_name_and_returns_the_id(self):
        self.client.session.post.return_value = MagicMock(status_code=200, json=lambda: {"repo_id": "abc"})
        self.assertEqual(self.client.create_library("code"), "abc")
        args, kwargs = self.client.session.post.call_args
        self.assertEqual(args[0], "https://s.example/api2/repos/")
        self.assertEqual(kwargs["data"], {"name": "code"})

    def test_create_library_refusal_and_idless_answer_are_errors(self):
        self.client.session.post.return_value = MagicMock(status_code=403, text="no quota")
        with self.assertRaises(SeafileAPIError) as ctx:
            self.client.create_library("code")
        self.assertIn("HTTP 403 no quota", str(ctx.exception))
        self.client.session.post.return_value = MagicMock(status_code=200, json=lambda: {})
        with self.assertRaises(SeafileAPIError) as ctx:
            self.client.create_library("code")
        self.assertIn("did not return its id", str(ctx.exception))

    def test_history_limit_round_trip_and_unreadable_cases(self):
        self.client.session.get.return_value = MagicMock(status_code=200, json=lambda: {"keep_days": 30})
        self.assertEqual(self.client.get_history_limit("rid"), 30)
        self.client.session.get.return_value = MagicMock(status_code=404, json=lambda: {})
        self.assertIsNone(self.client.get_history_limit("rid"))
        self.client.session.get.return_value = MagicMock(status_code=200, json=lambda: {"keep_days": True})
        self.assertIsNone(self.client.get_history_limit("rid"))
        self.client.session.put.return_value = MagicMock(status_code=200)
        self.assertTrue(self.client.set_history_limit("rid", 30))
        self.assertEqual(self.client.session.put.call_args.kwargs["data"], {"keep_days": "30"})
        self.client.session.put.return_value = MagicMock(status_code=403)
        self.assertFalse(self.client.set_history_limit("rid", 30))


if __name__ == "__main__":
    unittest.main()
