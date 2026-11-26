"""The ``git credential`` fallback (CREDENTIAL.md), against real git.

Git itself is the helper under test: a ``credential-store`` file for the round
trips (a scripted helper cannot imitate its handling of ``path``), a recording
askpass program for the never-prompt rule, a username-only helper for the case
the review found, and a helper that leaves a child behind for the limit.  Every
test runs with an isolated global git config and no system config.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from git_remote_seafile import gitcred


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


@unittest.skipUnless(gitcred.have_git(), "git is not installed")
class GitCredTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "credentials"
        self.config = self.tmp / "gitconfig"
        self.config.write_text(f"[credential]\n\thelper = store --file {_posix(self.store)}\n", encoding="utf-8")
        patcher = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(self.config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(self.tmp),
                "USERPROFILE": str(self.tmp),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_helper(self, helper: str) -> None:
        # Quoted: an unquoted ``;`` or ``#`` starts a comment in a git config value, so ``!f() { a; b; }; f`` reached git as
        # ``!f() { a`` (a syntax error) and every helper that was meant to answer failed at once: the tests that expect
        # "no answer" passed for that reason, not the one they name (found on Debian, round 8).
        value = '"' + helper.replace("\\", "\\\\").replace('"', '\\"') + '"' if helper else ""
        self.config.write_text(f"[credential]\n\thelper = {value}\n", encoding="utf-8")

    def askpass(self) -> Path:
        """A GIT_ASKPASS program that records every call and answers ``secret``."""
        record = self.tmp / "askpass-calls"
        if os.name == "nt":
            script = self.tmp / "askpass.cmd"
            script.write_text(f'@echo called>>"{record}"\r\n@echo secret\r\n', encoding="utf-8")
        else:
            script = self.tmp / "askpass.sh"
            script.write_text(f'#!/bin/sh\necho called >> "{record}"\necho secret\n', encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.record = record
        return script

    def calls(self) -> int:
        return len(self.record.read_text().split()) if self.record.exists() else 0


class RequestTests(unittest.TestCase):
    def test_fields_are_normalised(self) -> None:
        req = gitcred.build_request("HTTPS://Seafile.Example:443/seafile/", "seafile-token")
        self.assertEqual(
            req,
            {"protocol": "https", "host": "seafile.example", "path": "seafile", "username": "seafile-token"},
        )

    def test_a_non_default_port_is_kept_and_a_root_path_is_omitted(self) -> None:
        req = gitcred.build_request("http://host.example:8000", "seafile-token")
        self.assertEqual(req["host"], "host.example:8000")
        self.assertNotIn("path", req)

    def test_an_international_name_becomes_ascii(self) -> None:
        self.assertEqual(gitcred.build_request("https://bücher.example", "u")["host"], "xn--bcher-kva.example")

    def test_hostile_input_is_refused_before_any_call(self) -> None:
        for url in (
            "https://user:pass@host.example/",
            "https://host.example\nhost=evil.example/",
            "https://host.example/a\nb",
            "ftp://host.example/",
            "https:///nohost",
            "https://host.example/a b",
        ):
            with self.subTest(url=url):
                with self.assertRaises(gitcred.CredentialRequestError):
                    gitcred.build_request(url, "seafile-token")
        for name in ("a\nb", "a b", "", "x\x00y"):
            with self.subTest(username=name):
                with self.assertRaises(gitcred.CredentialRequestError):
                    gitcred.build_request("https://host.example/", name)

    def test_the_names_to_try(self) -> None:
        self.assertEqual(gitcred.usernames(None), ["seafile-token"])
        self.assertEqual(gitcred.usernames("alice"), ["seafile-token+alice", "seafile-token"])


class RoundTripTests(GitCredTestCase):
    URL = "https://sf.example/seafile"

    def test_approve_then_fill_returns_the_token_through_git_credential_store(self) -> None:
        self.assertTrue(gitcred.approve(self.URL, "seafile-token+alice", "tok123"))
        found = gitcred.fill(self.URL, "alice").found
        self.assertIsNotNone(found)
        self.assertEqual((found.token, found.username), ("tok123", "seafile-token+alice"))

    def test_approve_says_false_when_nothing_was_stored(self) -> None:
        """git exits 0 on approve with no helper or a failing helper; only a read-back proves it."""
        for helper in ("", "!f() { exit 1; }; f"):
            with self.subTest(helper=helper):
                self.use_helper(helper)
                self.assertFalse(gitcred.approve(self.URL, "seafile-token", "tok"))

    def test_the_bare_name_is_tried_after_the_account_name(self) -> None:
        gitcred.approve(self.URL, "seafile-token", "bare")
        outcome = gitcred.fill(self.URL, "alice")
        self.assertEqual(outcome.found.token, "bare")
        self.assertEqual(outcome.tried, ["seafile-token+alice", "seafile-token"])

    def test_nothing_stored_says_what_was_tried(self) -> None:
        outcome = gitcred.fill(self.URL, "alice")
        self.assertIsNone(outcome.found)
        self.assertEqual(outcome.tried, ["seafile-token+alice", "seafile-token"])

    def test_a_git_password_for_the_same_host_is_never_returned_or_erased(self) -> None:
        self.store.write_text("https://bob:GIT-PASSWORD@sf.example\n", encoding="utf-8")
        self.assertIsNone(gitcred.fill(self.URL, "alice").found)
        gitcred.approve(self.URL, "seafile-token+alice", "tok")
        self.assertTrue(gitcred.reject(self.URL, "seafile-token+alice"))
        self.assertIn("GIT-PASSWORD", self.store.read_text(), "reject must not touch a credential that is not ours")
        self.assertIsNone(gitcred.fill(self.URL, "alice").found)

    def test_reject_removes_our_entry_and_says_it_is_gone(self) -> None:
        gitcred.approve(self.URL, "seafile-token", "stale")
        self.assertTrue(gitcred.reject(self.URL, "seafile-token"))
        self.assertIsNone(gitcred.fill(self.URL).found)

    def test_a_server_at_the_host_root_round_trips_too(self) -> None:
        gitcred.approve("https://sf.example", "seafile-token", "root")
        self.assertEqual(gitcred.fill("https://sf.example").found.token, "root")

    def test_an_answer_under_another_username_is_ignored(self) -> None:
        self.use_helper("!f() { echo username=alice; echo password=hers; }; f")
        self.assertIsNone(gitcred.fill(self.URL).found)

    def test_the_token_is_not_in_the_environment_or_arguments(self) -> None:
        seen: list[tuple] = []
        real = gitcred.subprocess.Popen

        def spy(args, **kwargs):
            seen.append((args, kwargs.get("env")))
            return real(args, **kwargs)

        with mock.patch.object(gitcred.subprocess, "Popen", spy):
            gitcred.approve(self.URL, "seafile-token", "S3CRET-TOKEN")
        for args, env in seen:
            self.assertNotIn("S3CRET-TOKEN", " ".join(args))
            self.assertFalse(any("S3CRET-TOKEN" in v for v in (env or {}).values()))


class NeverPromptTests(GitCredTestCase):
    URL = "https://sf.example/seafile"

    def test_the_control_askpass_does_answer_when_only_the_terminal_switch_is_set(self) -> None:
        """Proof that the recorder is sensitive: git alone, with only GIT_TERMINAL_PROMPT=0, calls it."""
        import subprocess

        self.use_helper("")
        script = self.askpass()
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=str(script))
        subprocess.run(
            ["git", "credential", "fill"], input="protocol=https\nhost=sf.example\nusername=u\n\n",
            text=True, capture_output=True, env=env, timeout=30,
        )
        self.assertGreater(self.calls(), 0)

    def test_askpass_is_never_called_with_nothing_stored(self) -> None:
        self.use_helper("")
        script = self.askpass()
        with mock.patch.dict(os.environ, {"GIT_ASKPASS": str(script), "SSH_ASKPASS": str(script)}):
            self.assertIsNone(gitcred.fill(self.URL, "alice").found)
        self.assertEqual(self.calls(), 0)

    def test_askpass_is_never_called_when_a_helper_returns_a_username_and_no_password(self) -> None:
        self.use_helper("!f() { echo username=alice; }; f")
        script = self.askpass()
        with mock.patch.dict(os.environ, {"GIT_ASKPASS": str(script), "SSH_ASKPASS": str(script)}):
            self.assertIsNone(gitcred.fill(self.URL, "alice").found)
        self.assertEqual(self.calls(), 0)

    @unittest.skipIf(os.name == "nt", "the child-process check needs POSIX process ids")
    def test_a_helper_that_hangs_is_killed_with_its_child(self) -> None:
        pidfile = self.tmp / "child.pid"
        self.use_helper(f'!f() {{ sleep 60 & echo $! > "{_posix(pidfile)}"; wait; }}; f')
        start = time.monotonic()
        outcome = gitcred.fill("https://sf.example/", timeout=2)
        self.assertLess(time.monotonic() - start, 15)
        self.assertIsNone(outcome.found)
        self.assertIn("in time", outcome.note)
        pid = int(pidfile.read_text().strip())
        time.sleep(0.5)
        with self.assertRaises(OSError):
            os.kill(pid, 0)

    @unittest.skipUnless(os.name == "nt", "Windows twin of the POSIX test above")
    def test_a_helper_that_hangs_returns_within_the_limit_on_windows(self) -> None:
        self.use_helper("!f() { sleep 60; }; f")
        start = time.monotonic()
        outcome = gitcred.fill("https://sf.example/", timeout=2)
        self.assertLess(time.monotonic() - start, 20)
        self.assertIsNone(outcome.found)

    def test_a_missing_git_is_not_an_error(self) -> None:
        with mock.patch.object(gitcred.subprocess, "Popen", side_effect=FileNotFoundError):
            outcome = gitcred.fill("https://sf.example/", "alice")
        self.assertIsNone(outcome.found)


if __name__ == "__main__":
    unittest.main()
