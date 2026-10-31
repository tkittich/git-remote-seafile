"""The snapshot tool is one file to users and several parts to maintainers.

``tools/seafile_snapshot.py`` is *generated* from ``tools/_snapshot/*.py`` by
``tools/build_snapshot.py``.  These tests keep the two honest: the committed
file must be exactly what the parts build to, the parts must stay cohesive, and
a name must live in one part only (they share a flat namespace, so a duplicate
would silently shadow the earlier definition).
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TOOLS = _ROOT / "tools"
sys.path.insert(0, str(_TOOLS))

import build_snapshot  # noqa: E402


def _generated_text() -> str:
    return build_snapshot.OUTPUT.read_text(encoding="utf-8").replace("\r\n", "\n")


class TestGeneratedFileMatchesTheParts(unittest.TestCase):
    def test_the_committed_file_is_what_the_parts_build_to(self):
        self.assertEqual(
            _generated_text(),
            build_snapshot.build(),
            "git_remote_seafile/snapshot_tool.py is stale: edit tools/_snapshot/*.py, then run "
            "`python tools/build_snapshot.py` and commit the result",
        )

    def test_check_mode_agrees(self):
        self.assertEqual(build_snapshot.main(["--check"]), 0)

    def test_the_generated_file_says_it_is_generated(self):
        head = "\n".join(_generated_text().splitlines()[:4])
        self.assertIn("GENERATED FILE", head)
        self.assertIn("tools/_snapshot", head)
        self.assertTrue(_generated_text().startswith("#!"), "the shebang must stay on line 1")


class TestPartsStayCohesive(unittest.TestCase):
    def test_parts_exist_and_sort_into_a_stable_order(self):
        names = [p.name for p in build_snapshot.part_paths()]
        self.assertGreaterEqual(len(names), 5)
        self.assertEqual(names, sorted(names))
        prefixes = [n.split("_", 1)[0] for n in names]
        self.assertEqual(len(prefixes), len(set(prefixes)), f"duplicate part prefix in {names}")

    def test_no_part_has_grown_back_into_a_monolith(self):
        # The limit lives in build_snapshot.py so --check can print the live
        # sizes against it; the constant here would be a second copy.
        for path in build_snapshot.part_paths():
            lines = len(path.read_text(encoding="utf-8").splitlines())
            with self.subTest(part=path.name):
                self.assertLessEqual(
                    lines, build_snapshot.MAX_PART_LINES,
                    f"{path.name} is {lines} lines; split it by cohesion",
                )

    def test_the_size_report_names_the_real_largest_part(self):
        # --check's headroom line is only useful if it is true; a report that
        # silently ordered or counted wrong would flatter every part.
        top_name, top_lines = build_snapshot.largest_parts(1)[0]
        manual_lines, manual_name = max(
            (len(path.read_text(encoding="utf-8").splitlines()), path.name)
            for path in build_snapshot.part_paths()
        )
        self.assertEqual((top_lines, top_name), (manual_lines, manual_name))

    def test_imports_live_in_one_part(self):
        # Fragments share a namespace; scattering imports across them makes it
        # impossible to say what a part depends on.  Function-level imports are
        # fine (they are not module-level statements).
        offenders = []
        for path in build_snapshot.part_paths():
            if path.name.startswith(("00_", "10_")):
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.startswith(("import ", "from ")) and not line.startswith("from __future__"):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "module-level imports belong in 10_setup.py")

    def test_no_part_imports_another_part(self):
        for path in build_snapshot.part_paths():
            text = path.read_text(encoding="utf-8")
            with self.subTest(part=path.name):
                self.assertNotIn("from ._snapshot", text)
                self.assertNotIn("import _snapshot", text)


class TestOneDefinitionPerName(unittest.TestCase):
    def test_no_top_level_name_is_defined_twice(self):
        tree = ast.parse(_generated_text())
        seen: dict[str, int] = {}
        duplicates: list[str] = []
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            for name in names:
                if name in seen:
                    duplicates.append(f"{name} (lines {seen[name]} and {node.lineno})")
                seen[name] = node.lineno
        self.assertEqual(duplicates, [], "a name defined in two parts shadows the first")


if __name__ == "__main__":
    unittest.main()
