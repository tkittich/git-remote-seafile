"""``tools/gates.py`` is the one entry point for the pre-commit gates.

The list of gates living in a tool instead of in prose is the whole point of
the tool, so the cheap tests are the load-bearing ones: every gate AGENTS.md
documents must be named, every script the steps invoke must exist, and ``-k``
must reach the suite step only.  A gates list that silently stopped running
mypy would otherwise look exactly like a green run.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import gates  # noqa: E402


class TestGateList(unittest.TestCase):
    def test_every_gate_is_listed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gates.main(["--list"]), 0)
        text = out.getvalue()
        for fragment in (
            "build_snapshot.py",
            "-m ruff check .",
            "-m mypy",
            "--platform linux --python-version 3.11",
            "verify_docs_guards.py",
            "tests.test_docs_consistency",
            "run_tests_parallel.py -j 0",
        ):
            self.assertIn(fragment, text, f"--list no longer names: {fragment}")

    def test_every_script_the_steps_name_exists(self):
        for label, command in gates.STEPS:
            for arg in command:
                path = pathlib.Path(arg)
                if arg.endswith(".py") and path.is_absolute():
                    self.assertTrue(
                        path.exists(), f"the '{label}' step names {arg}, which is not there"
                    )


class TestFilterReachesTheSuiteOnly(unittest.TestCase):
    def test_k_appends_to_the_last_step_alone(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gates.main(["--list", "-k", "test_snapshot_payload"])
        lines = out.getvalue().splitlines()
        self.assertEqual(
            sum(" -k test_snapshot_payload" in line for line in lines), 1,
            "-k must reach exactly one step",
        )
        self.assertIn(" -k test_snapshot_payload", lines[-1], "-k must reach the suite step")


if __name__ == "__main__":
    unittest.main()
