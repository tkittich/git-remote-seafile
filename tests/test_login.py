"""test_login.py - ``git-remote-seafile login`` trades a password for a stored token."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.cli import main
from git_remote_seafile.login import (
    LoginError,
    normalise_server,
    obtain_token,
    run_login,
    save_credentials,
)

SECRET_PASSWORD = "correct horse battery staple"


def _resp(status=200, body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.headers = headers or {}
    return r


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = Path(self._tmp.name) / ".git-seafile.json"

    def login(self, args, answers, post, **kw):
        """Run the command with scripted prompts; returns (rc, stdout, stderr)."""
        answers = list(answers)
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = run_login(
                args,
                ask=lambda prompt: answers.pop(0),
                ask_secret=lambda prompt: SECRET_PASSWORD,
                post=post,
                config_path=self.config,
                interactive=True,
                **kw,
            )
        return rc, out.getvalue(), err.getvalue()


class TestLogin(_Fixture):
    def test_a_token_is_saved_and_the_password_is_not(self):
        post = MagicMock(return_value=_resp(200, {"token": "tok123"}))
        rc, out, err = self.login(["seafile.example.com", "--user", "me@example.com"], [], post)
        self.assertEqual(rc, 0, err)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"server": "https://seafile.example.com", "token": "tok123",
                                 "username": "me@example.com"})
        self.assertNotIn(SECRET_PASSWORD, self.config.read_text(encoding="utf-8"))
        self.assertNotIn(SECRET_PASSWORD, out + err)
        self.assertNotIn("tok123", out + err)
        self.assertIn("(the password is not)", out)
        url = post.call_args.args[0]
        self.assertEqual(url, "https://seafile.example.com/api2/auth-token/")
        self.assertEqual(post.call_args.kwargs["data"]["password"], SECRET_PASSWORD)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_file_is_owner_only(self):
        post = MagicMock(return_value=_resp(200, {"token": "tok"}))
        self.login(["https://s.example", "--user", "u"], [], post)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_it_asks_for_what_it_was_not_given(self):
        post = MagicMock(return_value=_resp(200, {"token": "tok"}))
        rc, out, err = self.login([], ["s.example", "me"], post, known_server="https://s.example")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["username"], "me")

    def test_a_two_factor_code_is_asked_for_and_sent_in_the_otp_header(self):
        need_otp = _resp(400, {}, {"X-Seafile-OTP": "required"})
        post = MagicMock(side_effect=[need_otp, _resp(200, {"token": "tok"})])
        rc, out, err = self.login(["https://s.example", "--user", "u"], ["123456"], post)
        self.assertEqual(rc, 0, err)
        self.assertEqual(post.call_args_list[1].kwargs["headers"], {"X-SEAFILE-OTP": "123456"})

    def test_a_wrong_password_saves_nothing_and_says_so(self):
        post = MagicMock(return_value=_resp(400, {"non_field_errors": ["bad"]}))
        rc, out, err = self.login(["https://s.example", "--user", "u"], [], post)
        self.assertEqual(rc, 1)
        self.assertIn("refused that username or password", err)
        self.assertFalse(self.config.exists())

    def test_a_second_account_replaces_the_first_with_a_note_and_keeps_other_keys(self):
        self.config.write_text(json.dumps({"server": "https://old.example", "token": "old",
                                           "extra": 1}), encoding="utf-8")
        post = MagicMock(return_value=_resp(200, {"token": "new"}))
        rc, out, err = self.login(["https://s.example", "--user", "u"], [], post)
        self.assertEqual(rc, 0, err)
        self.assertIn("Note: the file held an account for https://old.example", err)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual((saved["server"], saved["token"], saved["extra"]),
                         ("https://s.example", "new", 1))

    def test_plain_http_is_refused_before_the_password_is_asked_for(self):
        post = MagicMock()
        rc, out, err = self.login(["http://seafile.example.com", "--user", "u"], [], post)
        self.assertEqual(rc, 1)
        self.assertIn("is plain HTTP", err)
        post.assert_not_called()
        self.assertEqual(normalise_server("http://localhost:8000"), "http://localhost:8000")

    def test_it_needs_a_terminal(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            rc = run_login([], interactive=False, config_path=self.config)
        self.assertEqual(rc, 1)
        self.assertIn("needs a terminal", err.getvalue())

    def test_an_unknown_option_is_a_usage_error(self):
        with patch("sys.stderr", io.StringIO()) as err:
            rc = run_login(["--password", "x"], interactive=True, config_path=self.config)
        self.assertEqual(rc, 2)
        self.assertIn("Usage: git-remote-seafile login", err.getvalue())

    def test_a_server_that_is_not_seafile_is_named(self):
        with self.assertRaises(LoginError) as ctx:
            obtain_token("https://s.example", "u", "p", ask=input,
                         post=MagicMock(return_value=_resp(404)))
        self.assertIn("has no Seafile API", str(ctx.exception))

    def test_ctrl_c_at_a_prompt_cancels_and_saves_nothing(self):
        def interrupt(prompt):
            raise KeyboardInterrupt

        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            rc = run_login(["https://s.example", "--user", "u"], ask_secret=interrupt,
                           config_path=self.config, interactive=True)
        self.assertEqual(rc, 1)
        self.assertIn("login cancelled; nothing was saved", err.getvalue())
        self.assertFalse(self.config.exists())

    def test_a_token_that_cannot_be_written_is_reported(self):
        post = MagicMock(return_value=_resp(200, {"token": "tok"}))
        with patch("git_remote_seafile.login.save_credentials", side_effect=PermissionError("denied")):
            rc, out, err = self.login(["https://s.example", "--user", "u"], [], post)
        self.assertEqual(rc, 1)
        self.assertIn("the token could not be saved to", err)

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        save_credentials(self.config, "https://s.example", "t", "u")
        self.assertEqual([p.name for p in self.config.parent.iterdir()], [self.config.name])

    def test_it_is_a_listed_subcommand(self):
        out = io.StringIO()
        with patch("sys.argv", ["git-remote-seafile", "login", "--bogus"]), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(), 2)
        with patch("sys.argv", ["git-remote-seafile", "--help"]), patch("sys.stdout", out):
            main()
        self.assertIn("login [<server>]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
