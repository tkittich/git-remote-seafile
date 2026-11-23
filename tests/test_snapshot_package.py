"""The snapshot tool is a package of modules, and a name lives in exactly one of them.

It was one generated file (concatenated parts that shared a namespace) until 0.11.  What the generated file
guaranteed by construction is held here instead: a name is defined once, a module reaches another through the
module (never a copy of one of its names, which a patch of the defining module would miss), and nothing is
imported at import time from a module that comes later.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest
import unittest.mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402

PACKAGE = _ROOT / "git_remote_seafile" / "snapshot"


def _tree(module: str) -> ast.Module:
    return ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))


def _toplevel_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class PackageShapeTests(unittest.TestCase):
    def test_every_module_of_the_package_is_listed_in_import_order(self):
        on_disk = sorted(p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__")
        self.assertEqual(sorted(snap.MODULES), on_disk)

    def test_a_name_is_defined_in_one_module_only(self):
        owner: dict[str, str] = {}
        for module in snap.MODULES:
            for name in _toplevel_names(_tree(module)):
                self.assertNotIn(name, owner, f"{name} is defined in both {owner.get(name)} and {module}")
                owner[name] = module

    def test_no_module_copies_a_name_out_of_a_sibling(self):
        """``from .x import name`` binds a copy: a patch of ``x.name`` would not reach this module."""
        for module in snap.MODULES:
            for node in ast.walk(_tree(module)):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    for alias in node.names:
                        with self.subTest(module=module, imported=alias.name):
                            self.assertIn(alias.name, snap.MODULES, f"{module} imports a name, not a module")

    def test_nothing_is_imported_at_import_time_from_a_later_module(self):
        order = {name: i for i, name in enumerate(snap.MODULES)}
        for module in snap.MODULES:
            for node in _tree(module).body:      # top level only: function-level imports are call-time
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    for alias in node.names:
                        with self.subTest(module=module, imported=alias.name):
                            self.assertLess(order[alias.name], order[module],
                                            f"{module} imports the later module {alias.name} when it is loaded")


class PackageNamespaceTests(unittest.TestCase):
    def test_the_package_reads_as_the_old_single_namespace(self):
        for module in snap.MODULES:
            real = sys.modules[f"git_remote_seafile.snapshot.{module}"]
            for name in _toplevel_names(_tree(module)):
                with self.subTest(module=module, name=name):
                    self.assertIs(getattr(snap, name), getattr(real, name))

    def test_a_patch_of_the_package_namespace_is_refused_and_names_the_module_to_patch(self):
        with self.assertRaises(AttributeError) as caught:
            unittest.mock.patch.object(snap, "git", lambda *a, **k: None).start()
        self.assertIn("snapshot.gitrun", str(caught.exception))

    def test_a_patch_of_the_defining_module_changes_what_another_module_calls(self):
        calls: list[object] = []
        with unittest.mock.patch.object(snap.gitrun, "git", lambda *a, **k: calls.append(a) or None):
            # ``stage`` and ``vault_store`` call gitrun.git through the module, so both see the patch.
            self.assertIs(snap.stage.gitrun.git, snap.gitrun.git)
            self.assertIs(snap.vault_store.gitrun.git, snap.gitrun.git)
        self.assertIsNot(snap.gitrun.git, calls)


if __name__ == "__main__":
    unittest.main()
