"""An option a mode accepts but never reads is warned about now and refused later (deprecation pass, 0.10.x).

The per-mode table in ``test_snapshot_flag_coverage`` lists what each mode accepts.  The options in it that the mode
does not read were found by reading the code (review c305b0a: fable-S3-8, S4-13, gemini-S3-6, S4-3).  Refusing them at
once would break a script that has passed one for years, so the first release says so (``Warning:``), UPGRADING names
the release that refuses, and the one after refuses.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402


def _args(*argv: str):
    with contextlib.redirect_stderr(io.StringIO()):
        return snap.parse_args(list(argv))


class IgnoredOptionTests(unittest.TestCase):
    def test_a_drill_names_the_payload_options_it_never_reads(self):
        args = _args("--restore-drill", "--remote", "r", "--payload-jobs", "2", "--no-payload",
                     "--payload-auto-ratio", "0.5", "--payload-skip-changing", "--payload-report-days", "3")
        self.assertEqual(snap.ignored_option_warnings(args), [
            "--payload-jobs", "--no-payload", "--payload-auto-ratio", "--payload-skip-changing",
            "--payload-report-days"])

    def test_a_status_names_what_a_report_never_reads(self):
        args = _args("--status", "--message", "hi", "--allow-low-disk", "--payload-report-days", "3")
        self.assertEqual(snap.ignored_option_warnings(args), ["--message", "--allow-low-disk", "--payload-report-days"])

    def test_housekeeping_names_what_it_never_reads(self):
        args = _args("--maintain", "--remote", "r", "--branch", "b", "--allow-low-disk", "--payload-jobs", "2")
        self.assertEqual(snap.ignored_option_warnings(args), ["--remote", "--branch", "--allow-low-disk", "--payload-jobs"])

    def test_the_payload_retention_commands_name_the_snapshot_options_they_never_read(self):
        for base, mode in ((["--payload-gc", "--remote", "r"], "--payload-gc"),
                           (["--payload-verify", "all", "--remote", "r"], "--payload-verify")):
            args = _args(*base, "--reset-to-remote", "--no-repack", "--source", "s", "--branch", "b", "--message", "m")
            self.assertEqual(snap.ignored_option_warnings(args),
                             ["--source", "--branch", "--message", "--reset-to-remote", "--no-repack"], mode)
            self.assertEqual(snap.mode_name(args), mode)
        self.assertEqual(snap.ignored_option_warnings(_args("--payload-gc", "--remote", "r", "--vault", "v",
                                                            "--allow-low-disk", "--payload-keep", "2")), [])

    def test_a_payload_view_names_the_snapshot_and_payload_options_it_never_reads(self):
        """gemini-S3-6 (review c305b0a): ``--payload-view`` accepted every option of a snapshot run."""
        args = _args("--payload-view", "x", "--remote", "r", "--vault", "v", "--payload-view-name", "n",
                     "--exclude", "e", "--payload-jobs", "2", "--payload-rehash", "--payload-skip-changing",
                     "--payload-report-days", "3", "--allow-low-disk", "--source", "s")
        self.assertEqual(snap.ignored_option_warnings(args), [
            "--source", "--exclude", "--allow-low-disk", "--payload-jobs", "--payload-rehash",
            "--payload-skip-changing", "--payload-report-days"])
        self.assertEqual(snap.mode_name(args), "--payload-view")
        self.assertEqual(snap.ignored_option_warnings(_args(
            "--payload-view", "x", "--remote", "r", "--vault", "v", "--payload-view-name", "n",
            "--payload-view-replace")), [])

    def test_options_a_mode_does_read_are_not_named(self):
        self.assertEqual(snap.ignored_option_warnings(_args("--restore-drill", "--remote", "r", "--branch", "b",
                                                            "--ref", "x", "--allow-low-disk", "--allow-synced")), [])
        self.assertEqual(snap.ignored_option_warnings(_args("--status", "--remote", "r", "--exclude", "x",
                                                            "--payload-min-size", "1M")), [])
        self.assertEqual(snap.ignored_option_warnings(_args("--maintain", "--full-repack", "--allow-synced")), [])
        self.assertEqual(snap.ignored_option_warnings(_args("--remote", "r")), [])        # a snapshot run reads them all

    def test_the_warning_is_said_before_the_run_and_does_not_change_it(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main(["--restore-drill", "--remote", str(_ROOT / "no-such-remote.git"), "--no-payload"])
        self.assertNotEqual(rc, 0)                         # the drill itself fails: there is no remote
        text = out.getvalue() + err.getvalue()
        self.assertIn("Warning: --no-payload has no effect with --restore-drill", text)
        self.assertIn("refused from 0.11", text)


if __name__ == "__main__":
    unittest.main()
