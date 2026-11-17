"""Every option of the snapshot tool is either checked against the modes it does not apply to, or says why not.

The review rounds kept finding a flag that one mode silently ignored (``--status --reset-to-remote``,
``--restore-drill --no-repack``): each fix added the flag to a hand-kept list, and the next new flag was
forgotten the same way.  This closes the shape.  An option the validation modules never mention
(``args.<name>``) must be in ``APPLIES_TO_EVERY_MODE`` with the reason, so adding an option without
deciding what it means in ``--status``, ``--restore``, ``--restore-drill`` and ``--maintain`` fails here.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import sys
import unittest
from unittest.mock import patch

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402

#: The parts that decide which options a mode refuses.
VALIDATION_PARTS = ("96_cli_checks", "82_status", "76_payload_gc", "78_payload_cli", "79_payload_view", "73_mtimes")

APPLIES_TO_EVERY_MODE = {
    "json": "the run report (SNAPSHOT.md, exit codes): every mode prints one",
    "exit_warnings": "the exit status of every mode; it only changes 0 into 3 when a warning was said",
}


def _dests() -> list[str]:
    seen: dict[str, argparse.ArgumentParser] = {}

    def grab(self, *args, **kwargs):
        seen["parser"] = self
        raise SystemExit(0)

    with patch.object(argparse.ArgumentParser, "parse_args", grab):
        try:
            snap.main([])
        except SystemExit:
            pass
    return [a.dest for a in seen["parser"]._actions if a.option_strings and a.dest != "help"]


class TestEveryOptionIsAccountedFor(unittest.TestCase):
    def test_an_option_no_validation_mentions_is_declared_to_apply_everywhere(self):
        text = "".join(
            (_ROOT / "tools" / "_snapshot" / f"{part}.py").read_text(encoding="utf-8") for part in VALIDATION_PARTS
        )
        dests = _dests()
        self.assertGreater(len(dests), 40, "the parser was not found")
        undecided = sorted(d for d in dests if f"args.{d}" not in text and d not in APPLIES_TO_EVERY_MODE)
        self.assertEqual(
            undecided, [],
            "these options are not refused by any mode and are not declared to apply to every mode: decide "
            "what each means in --status, --restore, --restore-drill and --maintain (96_cli_checks.py, "
            "82_status.py) or add it to APPLIES_TO_EVERY_MODE with the reason",
        )

    def test_the_declared_exceptions_are_options_that_exist(self):
        self.assertEqual(sorted(set(APPLIES_TO_EVERY_MODE) - set(_dests())), [])


#: What each mode accepts today, found by running ``validate_args`` with the option added to the mode's
#: smallest valid command line.  The text search above cannot see a mode that *ignores* an option it
#: mentions elsewhere; this does, per mode.  An option missing here is refused by the mode (good); a new
#: option that a mode accepts fails the test until someone decides what it means there and lists it.
#: Entries that a mode really uses are marked in ``USED``; the rest are accepted and ignored today
#: (ROADMAP AA73: review c305b0a fable-S3-8, S4-13, gemini-S3-6, S4-3) -- shrinking this list is the work.
ACCEPTED = {
    "restore": {"--branch", "--ref", "--allow-synced", "--allow-low-disk", "--payload-jobs", "--verify",
                "--json", "--exit-warnings"},
    "drill": {"--branch", "--ref", "--allow-synced", "--allow-low-disk", "--no-payload", "--payload-auto-ratio",
              "--payload-skip-changing", "--payload-jobs", "--payload-report-days", "--json", "--exit-warnings"},
    "status": {"--source", "--vault", "--branch", "--remote", "--code-remote", "--exclude", "--default-excludes",
               "--message", "--allow-synced", "--allow-low-disk", "--payload-min-size", "--no-payload",
               "--payload-auto-ratio", "--payload-report-days", "--json", "--exit-warnings"},
    "maintain": {"--source", "--vault", "--branch", "--remote", "--allow-synced", "--allow-low-disk",
                 "--payload-jobs", "--json", "--exit-warnings", "--no-object-compression", "--full-repack"},
}
MODE_LINES = {
    "restore": ["--restore", "--remote", "r", "--into", "d"],
    "drill": ["--restore-drill", "--remote", "r"],
    "status": ["--status"],
    "maintain": ["--maintain"],
}


def _value(action: argparse.Action) -> list[str]:
    if action.nargs in (0, "?"):
        return []
    kind = getattr(action.type, "__name__", "")
    return ["0.5"] if kind == "parse_ratio_argument" else ["1"] if action.type in (int, float) else ["x"]


class TestEveryModeDecidesEveryOption(unittest.TestCase):
    def test_an_option_warned_as_ignored_is_one_the_mode_accepts(self):
        """The deprecation pass (``ignored_option_warnings``) names only options in the table above."""
        modes = {"drill": ["--restore-drill", "--remote", "r"], "status": ["--status"], "maintain": ["--maintain"]}
        every = {"--payload-jobs": "1", "--no-payload": None, "--payload-auto-ratio": "0.5",
                 "--payload-skip-changing": None, "--payload-report-days": "1", "--message": "m",
                 "--allow-low-disk": None, "--remote": "r", "--branch": "b"}
        for mode, base in modes.items():
            argv = list(base)
            for flag, value in every.items():
                if flag not in base:
                    argv += [flag] + ([value] if value else [])
            with contextlib.redirect_stderr(io.StringIO()):
                named = snap.ignored_option_warnings(snap.parse_args(argv))
            self.assertTrue(named, mode)
            self.assertEqual(sorted(set(named) - ACCEPTED[mode]), [], f"--{mode}: warned about an option it refuses")

    def test_the_options_a_mode_accepts_are_the_listed_ones(self):
        seen: dict[str, argparse.ArgumentParser] = {}

        def grab(self, *args, **kwargs):
            seen["parser"] = self
            raise SystemExit(0)

        with patch.object(argparse.ArgumentParser, "parse_args", grab):
            try:
                snap.main([])
            except SystemExit:
                pass
        actions = [a for a in seen["parser"]._actions if a.option_strings and a.dest != "help"]
        for mode, base in MODE_LINES.items():
            accepted = set()
            for action in actions:
                option = action.option_strings[0]
                if option in base:
                    continue
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        args = snap.parse_args(base + [option] + _value(action))
                    except SystemExit:
                        continue
                try:
                    snap.validate_args(args)
                except snap.SnapshotError:
                    continue
                accepted.add(option)
            self.assertEqual(
                sorted(accepted ^ ACCEPTED[mode]), [],
                f"--{mode}: the options it accepts changed. A new one must be refused by the mode "
                "(96_cli_checks.py) or listed in ACCEPTED here with a decision; one that is refused now "
                "should leave the list",
            )


if __name__ == "__main__":
    unittest.main()
