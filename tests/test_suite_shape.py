"""Suite-shape invariants, checked against the test tree itself.

``tools/run_tests_parallel.py`` schedules one unit of work per *class*.  But
``unittest`` inherits test methods, so if a class that *defines* tests is also
used as a base class, every inherited case is discovered twice -- once under the
base's own name and once under the subclass's -- and the runner dutifully runs
each copy as its own work.  When that happened to ``test_snapshot_tool.py`` the
module took 525 s instead of 151 s, because 49 of its 124 cases were the same
code run twice.

The fix is a fixture *base* that carries no test methods (see
``_SnapshotFixture`` in ``test_snapshot_tool.py``).  This guard keeps it that way
across the whole suite: a base class may provide ``setUp`` and helpers, but not
``test_*`` methods.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_TESTS = pathlib.Path(__file__).resolve().parent


def _class_shape(path: pathlib.Path) -> dict[str, tuple[list[str], list[str]]]:
    """``{class name: ([test methods it defines], [base class names])}``."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    shape: dict[str, tuple[list[str], list[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        tests = [
            n.name
            for n in node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_")
        ]
        bases = [ast.unparse(base).split(".")[-1] for base in node.bases]
        shape[node.name] = (tests, bases)
    return shape


class NoTestBearingBaseClassTests(unittest.TestCase):
    """A test-bearing class must not be subclassed: that runs its cases twice."""

    def test_no_test_bearing_class_is_used_as_a_base(self):
        # Bases are resolved across ALL test modules: a test-bearing class
        # imported from another file and subclassed there used to be invisible
        # (the base name resolved only within the same file) -- one import hop
        # away from the double-run bug this guard exists to prevent.
        shapes = {path.stem: _class_shape(path) for path in sorted(_TESTS.glob("test_*.py"))}
        defined: dict[str, tuple[str, list[str]]] = {}
        for stem, shape in shapes.items():
            for name, (tests, _bases) in shape.items():
                defined.setdefault(name, (stem, tests))

        offenders = []
        for stem, shape in shapes.items():
            used_as_base = {b for _, bases in shape.values() for b in bases}
            for name in sorted(used_as_base):
                if name in shape:
                    where, own = stem, shape[name][0]
                elif name in defined:
                    where, own = defined[name]
                else:
                    continue  # not one of ours (unittest.TestCase, abc.ABC, ...)
                if own:
                    origin = "" if where == stem else f" (defined in {where}) "
                    offenders.append(
                        f"{stem}:{origin} '{name}' defines {len(own)} test(s) and is "
                        f"subclassed -- its cases would run twice"
                    )
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
