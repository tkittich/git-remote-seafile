"""Every option of the snapshot tool is either checked against the modes it does not apply to, or says why not.

The review rounds kept finding a flag that one mode silently ignored (``--status --reset-to-remote``,
``--restore-drill --no-repack``): each fix added the flag to a hand-kept list, and the next new flag was
forgotten the same way.  This closes the shape.  An option the validation modules never mention
(``args.<name>``) must be in ``APPLIES_TO_EVERY_MODE`` with the reason, so adding an option without
deciding what it means in ``--status``, ``--restore``, ``--restore-drill`` and ``--maintain`` fails here.
"""

from __future__ import annotations

import argparse
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


if __name__ == "__main__":
    unittest.main()
