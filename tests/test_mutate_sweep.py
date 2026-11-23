"""The parts of ``tools/mutate_sweep.py`` that choose which tests a mutation runs (the speed-up, so a wrong choice is silent)."""

from __future__ import annotations

import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import mutate_sweep  # noqa: E402


class GeneratedLineTests(unittest.TestCase):
    def test_a_line_of_a_part_is_found_in_the_generated_tool(self):
        generated = (_ROOT / "git_remote_seafile" / "snapshot_tool.py").read_text(encoding="utf-8").split("\n")
        parts = sorted((_ROOT / "tools" / "_snapshot").glob("*.py"))
        for part in (parts[0], parts[len(parts) // 2], parts[-1]):
            lines = part.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
            for number in (1, 2, len(lines) // 2):
                with self.subTest(part=part.name, line=number):
                    mapped = mutate_sweep.generated_line(f"tools/_snapshot/{part.name}", number)
                    self.assertEqual(generated[mapped - 1], lines[number - 1])

    def test_a_name_that_is_not_a_part_is_refused(self):
        with self.assertRaises(ValueError):
            mutate_sweep.generated_line("tools/_snapshot/99_missing.py", 1)


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.selector = mutate_sweep.Selector({"a": {1, 2}, "b": {2, 3}, "c": {2}}, {"a": 3.0, "b": 1.0, "c": 2.0})

    def test_only_the_classes_that_run_the_line_are_tried(self):
        self.assertEqual(self.selector.stages(1), [["a"]])
        self.assertEqual(self.selector.stages(3), [["b"]])

    def test_a_line_nothing_runs_has_no_stage(self):
        self.assertEqual(self.selector.stages(99), [])

    def test_the_cheapest_goes_first_until_one_has_killed(self):
        self.assertEqual(self.selector.stages(2), [["b"], ["c"], ["a"]])

    def test_the_class_that_kills_moves_to_the_front(self):
        self.selector.killed_by(["a"])
        self.assertEqual(self.selector.stages(2), [["a"], ["b"], ["c"]])
        self.selector.killed_by(["c"])
        self.selector.killed_by(["c"])
        self.assertEqual(self.selector.stages(2), [["c"], ["a"], ["b"]])


class ExpandTargetsTests(unittest.TestCase):
    def test_a_module_becomes_its_classes_and_a_class_stays_one(self):
        both = mutate_sweep.expand_targets(["tests.test_mutate_sweep"])
        self.assertEqual(sorted(both), ["tests.test_mutate_sweep.ExpandTargetsTests",
                                        "tests.test_mutate_sweep.GeneratedLineTests",
                                        "tests.test_mutate_sweep.SelectorTests"])
        self.assertEqual(mutate_sweep.expand_targets(["tests.test_mutate_sweep.SelectorTests"]),
                         ["tests.test_mutate_sweep.SelectorTests"])


if __name__ == "__main__":
    unittest.main()
