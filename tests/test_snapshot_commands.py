"""The snapshot tool's commands (0.11): the words, the old mode flags that still choose them, and the help."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402


def _parse(*argv: str):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        return snap.parse_args(list(argv)), err.getvalue()


def _main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = snap.main(list(argv))
        except SystemExit as exc:
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


class CommandWordTests(unittest.TestCase):
    def test_each_command_word_chooses_its_mode(self):
        for argv, mode in (
            (["restore", "--remote", "r", "--into", "d"], "restore"), (["drill", "--remote", "r"], "drill"),
            (["status"], "status"), (["maintain"], "maintain"), (["snapshot", "--no-push"], "snapshot"),
            (["--no-push"], "snapshot"), ([], "snapshot"),
            (["payload", "gc", "--remote", "r"], "gc"), (["payload", "verify", "--remote", "r"], "verify"),
            (["payload", "view", "--remote", "r"], "view"),
        ):
            with self.subTest(argv=argv):
                args, err = _parse(*argv)
                self.assertEqual(args.mode, mode)
                self.assertEqual(err, "")

    def test_a_command_reads_its_operand(self):
        self.assertEqual(_parse("drill", "3", "--remote", "r")[0].restore_drill, "3")
        self.assertEqual(_parse("drill", "--remote", "r")[0].restore_drill, "10")
        self.assertEqual(_parse("payload", "verify", "all", "--remote", "r")[0].payload_verify, "all")
        self.assertEqual(_parse("payload", "verify", "--remote", "r")[0].payload_verify, "20")
        self.assertEqual(_parse("payload", "view", "snapshot~2", "--remote", "r")[0].payload_view, "snapshot~2")
        self.assertIsNone(_parse("status")[0].restore_drill)
        self.assertFalse(_parse("status")[0].restore)

    def test_payload_needs_gc_verify_or_view(self):
        for argv in (["payload"], ["payload", "delete"]):
            rc, out, err = _main(*argv)
            self.assertEqual(rc, snap.EXIT_USAGE)
            self.assertIn("payload needs one of: gc, verify, view", err)

    def test_the_options_typed_are_recorded(self):
        args, _ = _parse("restore", "--remote", "r", "--into", "d", "--payload-grace-days", "7")
        self.assertEqual(sorted(args.given), ["into", "payload_grace_days", "remote"])


class OldModeFlagTests(unittest.TestCase):
    def test_an_old_mode_flag_still_chooses_its_command_and_says_so(self):
        for argv, mode, flag in (
            (["--restore", "--remote", "r", "--into", "d"], "restore", "--restore"),
            (["--restore-drill", "--remote", "r"], "drill", "--restore-drill"),
            (["--status"], "status", "--status"),
            (["--maintain", "--full-repack"], "maintain", "--maintain"),
            (["--payload-gc", "--remote", "r"], "gc", "--payload-gc"),
        ):
            with self.subTest(flag=flag):
                args, err = _parse(*argv)
                self.assertEqual(args.mode, mode)
                self.assertIn(f"Notice: {flag} is now the command", err)
                self.assertIn("refused from 0.12", err)

    def test_the_operand_of_an_old_flag_moves_to_the_command(self):
        args, _ = _parse("--restore-drill", "4", "--remote", "r")
        self.assertEqual((args.mode, args.restore_drill), ("drill", "4"))
        args, _ = _parse("--payload-verify=all", "--remote", "r")
        self.assertEqual((args.mode, args.payload_verify), ("verify", "all"))
        args, _ = _parse("--payload-view", "--remote", "r")
        self.assertEqual((args.mode, args.payload_view), ("view", "snapshot"))
        args, _ = _parse("--remote", "r", "--payload-verify", "5")
        self.assertEqual(args.payload_verify, "5")

    def test_two_old_mode_flags_are_different_modes(self):
        rc, out, err = _main("--restore", "--maintain")
        self.assertEqual(rc, snap.EXIT_USAGE)
        self.assertIn("--maintain and --restore are different modes", err)

    def test_the_notice_is_not_a_warning(self):
        rc, out, err = _main("--status", "--exit-warnings", "--json", "--vault", "does-not-exist")
        self.assertNotIn("Warning: --status", err)
        self.assertNotEqual(rc, 3, err)
        document = json.loads(out.strip().splitlines()[-1])
        self.assertNotIn("--status is now the command", " ".join(document.get("warnings", [])))

    def test_the_notice_leaves_stdout_alone(self):
        rc, out, err = _main("--status", "--vault", "does-not-exist")
        self.assertNotIn("is now the command", out)
        self.assertIn("is now the command", err)


class HelpTests(unittest.TestCase):
    def test_a_command_help_lists_only_the_options_it_reads(self):
        rc, out, err = _main("restore", "--help")
        self.assertEqual(rc, 0)
        for shown in ("--remote", "--into", "--ref", "--verify"):
            self.assertIn(shown, out)
        for hidden in ("--payload-jobs", "--exclude", "--rebuild-index", "--message", "--payload-delete", "payload mode", "payload tools"):
            self.assertNotIn(hidden, out)

    def test_the_plain_help_lists_the_commands(self):
        rc, out, err = _main("--help")
        flat = " ".join(out.split())
        for word in ("restore", "drill", "status", "maintain", "payload gc", "payload verify", "payload view"):
            self.assertIn(word, flat)
        self.assertIn("a bare run is 'snapshot'", flat)


if __name__ == "__main__":
    unittest.main()
