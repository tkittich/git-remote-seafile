"""A command refuses an option it never reads (exit 2); 0.10.x only warned (UPGRADING, v0.11.0).

``subcommands.ALLOWED`` is the one table; ``test_snapshot_flag_coverage`` checks it against what each command
accepts.  The options refused here were found by reading the code (review c305b0a: fable-S3-8, S4-13,
gemini-S3-6, S4-3).  The old mode flags (``--restore`` ...) still choose their command in 0.11 and say so.
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

DRILL = ["drill", "--remote", "r"]
RESTORE = ["restore", "--remote", "r", "--into", "d"]
GC = ["payload", "gc", "--remote", "r"]
VERIFY = ["payload", "verify", "all", "--remote", "r"]
VIEW = ["payload", "view", "x", "--remote", "r"]

#: (mode line, mode named in the message, the options refused there with a value that parses)
REFUSED = (
    (DRILL, "drill", [("--payload-jobs", "2"), ("--no-payload",), ("--payload-auto-ratio", "0.5"),
                                ("--payload-skip-changing",), ("--payload-report-days", "3")]),
    (RESTORE, "restore", [("--payload-jobs", "2")]),
    (["status"], "status", [("--message", "hi"), ("--allow-low-disk",), ("--payload-report-days", "3")]),
    (["maintain"], "maintain", [("--remote", "r"), ("--branch", "b"), ("--allow-low-disk",),
                                    ("--payload-jobs", "2")]),
    (GC, "payload gc", [("--source", "s"), ("--branch", "b"), ("--code-remote", "c"), ("--default-excludes",),
                          ("--message", "m"), ("--reset-to-remote",), ("--no-repack",), ("--no-object-compression",),
                          ("--rebuild-index",)]),
    (VERIFY, "payload verify", [("--source", "s"), ("--branch", "b"), ("--code-remote", "c"),
                                  ("--default-excludes",), ("--message", "m"), ("--reset-to-remote",),
                                  ("--no-repack",), ("--no-object-compression",), ("--rebuild-index",)]),
    (VIEW, "payload view", [("--source", "s"), ("--branch", "b"), ("--code-remote", "c"), ("--exclude", "e"),
                              ("--default-excludes",), ("--message", "m"), ("--reset-to-remote",), ("--no-repack",),
                              ("--no-object-compression",), ("--rebuild-index",), ("--allow-low-disk",),
                              ("--payload-jobs", "2"), ("--payload-rehash",), ("--payload-skip-changing",),
                              ("--payload-report-days", "3")]),
    (["--no-push"], "--no-push", [("--payload-jobs", "2"), ("--payload-auto-ratio", "0.5"),
                                  ("--payload-skip-changing",), ("--payload-rehash",)]),
)


def _main(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = snap.main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class UnusedOptionTests(unittest.TestCase):
    def test_every_option_a_mode_never_reads_is_a_usage_error_that_names_it(self):
        for base, mode, options in REFUSED:
            for option in options:
                with self.subTest(mode=mode, option=option[0]):
                    rc, text = _main(*base, *option)
                    self.assertEqual(rc, snap.EXIT_USAGE)
                    self.assertIn(f"{option[0]} does not apply to {mode}", text)

    def test_the_old_warning_is_gone(self):
        self.assertFalse(hasattr(snap, "ignored_option_warnings"))
        rc, text = _main(*DRILL, "--no-payload")
        self.assertNotIn("will be refused from 0.11", text)

    def test_a_refused_option_names_the_commands_that_read_it(self):
        rc, text = _main(*RESTORE, "--payload-jobs", "2")
        self.assertEqual(rc, snap.EXIT_USAGE)
        self.assertIn("--payload-jobs does not apply to restore (it is an option of: snapshot, payload verify)", text)
        rc, text = _main("--ref", "x")
        self.assertIn("--ref does not apply to snapshot (it is an option of: restore, drill)", text)

    def test_a_value_after_a_refused_option_is_not_taken_for_a_source(self):
        rc, text = _main("status", "--message", "hi")
        self.assertIn("--message does not apply to status", text)
        self.assertNotIn("two different sources", text)
        self.assertNotIn("SOURCE", text)

    def test_options_a_mode_does_read_are_not_refused(self):
        for argv in (
            [*DRILL, "--branch", "b", "--ref", "x", "--allow-low-disk", "--allow-synced"],
            ["status", "--remote", "r", "--exclude", "x", "--payload-min-size", "1M"],
            ["maintain", "--full-repack", "--allow-synced"],
            [*GC, "--vault", "v", "--allow-low-disk", "--payload-keep", "2"],
            [*VIEW, "--vault", "v", "--payload-view-name", "n", "--payload-view-replace"],
            [*RESTORE, "--ref", "x", "--allow-low-disk"],
            ["--no-push"],
            ["--remote", "r", "--payload-jobs", "2", "--payload-rehash", "--message", "m"],
        ):
            with self.subTest(argv=argv):
                snap.validate_args(snap.parse_args(argv))


class GitlinkTests(unittest.TestCase):
    def test_no_embedded_repos_needs_allow_gitlinks(self):
        rc, text = _main("--no-push", "--no-embedded-repos")
        self.assertEqual(rc, snap.EXIT_USAGE)
        self.assertIn("--no-embedded-repos records an embedded repository as a gitlink", text)
        self.assertIn("add --allow-gitlinks", text)
        snap.validate_args(snap.parse_args(["--no-push", "--no-embedded-repos", "--allow-gitlinks"]))

    def test_allow_gitlinks_alone_or_in_another_mode_is_refused(self):
        rc, text = _main("--no-push", "--allow-gitlinks")
        self.assertEqual(rc, snap.EXIT_USAGE)
        self.assertIn("--allow-gitlinks only applies together with --no-embedded-repos", text)
        for base in (DRILL, RESTORE, ["status"], ["maintain"], GC):
            with self.subTest(base=base):
                rc, text = _main(*base, "--allow-gitlinks")
                self.assertEqual(rc, snap.EXIT_USAGE)
                self.assertIn(f"--allow-gitlinks does not apply to {' '.join(base[:2]) if base[0] == 'payload' else base[0]}", text)


if __name__ == "__main__":
    unittest.main()
