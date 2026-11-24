"""Every option of the snapshot tool belongs to the commands that read it, and each command's table matches what it does.

The review rounds kept finding a flag that one mode silently ignored (``--status --reset-to-remote``,
``drill --no-repack``): each fix added the flag to a hand-kept list, and the next new flag was forgotten the
same way.  Since 0.11 one table (``subcommands.ALLOWED``) says what each command reads and
``cli_checks.options_for_other_commands`` refuses the rest, so the shape is closed by construction; this file
checks the table against the parser and, command by command, against what ``validate_args`` accepts.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402


class TestTheTableAndTheParserAgree(unittest.TestCase):
    def test_every_option_the_parser_defines_is_read_by_some_command(self):
        flags = frozenset(snap.option_table().values())
        self.assertGreater(len(flags), 40, "the parser was not found")
        read = set()
        for mode in snap.subcommands.BY_MODE:
            read |= snap.subcommands.allowed(mode, flags)
        self.assertEqual(sorted(flags - read), [], "an option no command reads can never be given")

    def test_the_table_names_only_options_that_exist(self):
        flags = set(snap.option_table().values())
        named = set(snap.subcommands.NOT_SNAPSHOT)
        for allowed in snap.subcommands.ALLOWED.values():
            named |= allowed
        self.assertEqual(sorted(named - flags), [])

    def test_a_snapshot_reads_everything_a_command_reads_alone_does_not(self):
        flags = frozenset(snap.option_table().values())
        snapshot = snap.subcommands.allowed("snapshot", flags)
        self.assertEqual(sorted(flags - snapshot), sorted(snap.subcommands.NOT_SNAPSHOT))

    def test_every_command_reads_the_options_of_a_run_report(self):
        for mode, allowed in snap.subcommands.ALLOWED.items():
            self.assertLessEqual(snap.subcommands.EVERY_COMMAND, allowed, mode)


#: What each mode accepts today, found by running ``validate_args`` with the option added to the mode's
#: smallest valid command line.  The text search above cannot see a mode that *ignores* an option it
#: mentions elsewhere; this does, per mode.  An option missing here is refused by the mode (good); a new
#: option that a mode accepts fails the test until someone decides what it means there and lists it.
#: Entries that a mode really uses are marked in ``USED``; the rest are accepted and ignored today
#: (ROADMAP AA73/AA74; 0.11 refused the options that 0.10.x warned about, ``test_snapshot_unused_options``).
ACCEPTED = {
    "restore": {"--branch", "--ref", "--only", "--allow-synced", "--allow-low-disk", "--verify", "--json", "--exit-warnings"},
    "drill": {"--branch", "--ref", "--allow-synced", "--allow-low-disk", "--json", "--exit-warnings"},
    "status": {"--source", "--vault", "--branch", "--remote", "--code-remote", "--exclude", "--default-excludes",
               "--allow-synced", "--payload-min-size", "--no-payload", "--payload-auto-ratio", "--json",
               "--exit-warnings"},
    "maintain": {"--source", "--vault", "--allow-synced", "--json", "--exit-warnings", "--no-object-compression",
                 "--full-repack"},
    "gc": {"--allow-low-disk", "--allow-shared-remote", "--allow-synced", "--exit-warnings", "--json", "--payload-keep-days",
           "--payload-grace-days", "--payload-keep", "--vault"},
    "verify": {"--allow-low-disk", "--allow-shared-remote", "--allow-synced", "--exclude", "--exit-warnings", "--json",
               "--payload-jobs", "--payload-rehash", "--payload-report-days", "--payload-skip-changing", "--vault"},
    "view": {"--allow-shared-remote", "--allow-synced", "--exit-warnings", "--json", "--payload-view-name",
             "--payload-view-replace", "--vault"},
}
MODE_LINES = {
    "restore": ["restore", "--remote", "r", "--into", "d"],
    "drill": ["drill", "--remote", "r"],
    "status": ["status"],
    "maintain": ["maintain"],
    "gc": ["payload", "gc", "--remote", "r"],
    "verify": ["payload", "verify", "all", "--remote", "r"],
    "view": ["payload", "view", "x", "--remote", "r"],
}


def _value(action: argparse.Action) -> list[str]:
    if action.nargs in (0, "?"):
        return []
    kind = getattr(action.type, "__name__", "")
    return ["0.5"] if kind == "parse_ratio_argument" else ["1"] if action.type in (int, float) else ["x"]


class TestEveryModeDecidesEveryOption(unittest.TestCase):
    def test_the_options_a_mode_accepts_are_the_listed_ones(self):
        parser = snap.build_parser("snapshot")
        actions = [a for a in parser._actions if a.option_strings and a.dest != "help"]
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
                "(subcommands.ALLOWED) or listed in ACCEPTED here with a decision; one that is refused now "
                "should leave the list",
            )
            self.assertLessEqual(accepted, snap.subcommands.ALLOWED[mode], f"{mode}: accepted but not in the table")


if __name__ == "__main__":
    unittest.main()
