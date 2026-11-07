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
import os
import pathlib
import subprocess
import sys
import tempfile
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


class TestAStaleGeneratedFileIsNamed(unittest.TestCase):
    """The build step rewrites snapshot_tool.py, so every later gate checks the
    file on disk; CI checks the committed one (ROADMAP B7)."""

    def _git(self, root, *args):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def test_a_rebuilt_file_that_differs_from_head_is_named_and_one_that_matches_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target = root / "git_remote_seafile" / "snapshot_tool.py"
            target.parent.mkdir()
            target.write_text("one" + chr(10), encoding="utf-8")
            self._git(root, "init", "-q")
            self._git(root, "add", "--", "git_remote_seafile/snapshot_tool.py")
            self._git(root, "commit", "-qm", "c")
            self.assertIsNone(gates.generated_differs_from_head(root))
            target.write_text("two" + chr(10), encoding="utf-8")
            note = gates.generated_differs_from_head(root)
            self.assertIsNotNone(note)
            self.assertIn("git_remote_seafile/snapshot_tool.py", note)
            self.assertIn("commit it", note)

    def test_no_repository_is_no_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_dir = os.environ.get("GIT_CEILING_DIRECTORIES")
            os.environ["GIT_CEILING_DIRECTORIES"] = str(pathlib.Path(tmp).parent)
            try:
                self.assertIsNone(gates.generated_differs_from_head(pathlib.Path(tmp)))
            finally:
                if env_dir is None:
                    os.environ.pop("GIT_CEILING_DIRECTORIES", None)
                else:
                    os.environ["GIT_CEILING_DIRECTORIES"] = env_dir

    def test_the_check_follows_the_build_step(self):
        # main() recognises the build step by its argv; a renamed step would
        # silently drop the note.
        label, command = gates.STEPS[0]
        self.assertEqual(command[1:], [str(gates._ROOT / "tools" / "build_snapshot.py")], label)



class TestTheChangedMap(unittest.TestCase):
    """``--changed``: a mistake in the map may cost time, never coverage."""

    def test_shared_and_unknown_code_runs_everything(self):
        for path in ("git_remote_seafile/client.py", "git_remote_seafile/git_util.py",
                     "tests/e2e_harness.py", "tests/_helpers.py", "pyproject.toml",
                     "somewhere/new.py", "git_remote_seafile/brand_new.py"):
            with self.subTest(path=path):
                self.assertIsNone(gates.suite_for_changes([path]))
        # One shared file among many still runs everything.
        self.assertIsNone(gates.suite_for_changes(["README.md", "git_remote_seafile/client.py"]))

    def test_documents_alone_run_no_suite(self):
        self.assertEqual(gates.suite_for_changes(["README.md", "DESIGN.md", "notes.txt"]), [])

    def test_an_area_runs_its_modules_and_the_always_set(self):
        got = gates.suite_for_changes(["git_remote_seafile/lfs.py", "README.md"])
        self.assertIn("test_lfs", got)
        self.assertIn("test_e2e", got)
        self.assertIn("test_message_style", got)
        self.assertNotIn("test_snapshot_payload", got)
        snap = gates.suite_for_changes(["tools/_snapshot/76_payload_gc.py"])
        self.assertIn("test_snapshot_payload", snap)
        self.assertNotIn("test_lfs", snap)
        self.assertEqual(gates.suite_for_changes(["tests/test_url.py"]),
                         sorted({"test_url", *[m for m in gates._ALWAYS]}))

    def test_every_module_the_map_names_exists(self):
        names = set(gates._ALWAYS) | set(gates._SNAPSHOT_TESTS)
        names |= {m for mods in gates._TOOL_TESTS.values() for m in mods}
        root = gates._ROOT / "tests"
        self.assertEqual({n for n in names if not (root / f"{n}.py").exists()}, set())
        for stem in gates._SHARED_STEMS:
            self.assertTrue((gates._ROOT / "git_remote_seafile" / f"{stem}.py").exists(), stem)

    def test_every_test_module_that_imports_a_changed_module_runs(self):
        # Review 06cc623: a hand-kept map sent a change to safety.py to no snapshot
        # test and a change to library_setup.py to no unit test at all.  Checked
        # here with the standard library's own parser, not the map's regexes: every
        # test module that imports a package module by any absolute form runs when
        # that module changes.
        import ast

        root = gates._ROOT
        for test in sorted((root / "tests").glob("test_*.py")):
            tree = ast.parse(test.read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module == "git_remote_seafile":
                        imported.update(alias.name for alias in node.names)
                    elif node.module.startswith("git_remote_seafile."):
                        imported.add(node.module.split(".")[1])
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("git_remote_seafile."):
                            imported.add(alias.name.split(".")[1])
            for stem in sorted(imported):
                path = root / "git_remote_seafile" / f"{stem}.py"
                if not path.exists():
                    continue
                with self.subTest(test=test.stem, module=stem):
                    got = gates.suite_for_changes([f"git_remote_seafile/{stem}.py"])
                    if got is not None:
                        self.assertIn(test.stem, got)

    def test_a_module_the_snapshot_tool_imports_runs_the_snapshot_tests(self):
        got = gates.suite_for_changes(["git_remote_seafile/safety.py"])
        if got is not None:
            self.assertIn("test_snapshot_tool", got)
            self.assertIn("test_snapshot_payload", got)

    def test_a_module_shared_code_imports_is_shared(self):
        # client.py imports seafile_paths and sqlite_read: a change to either reaches
        # everything the client does.
        for stem in ("seafile_paths", "sqlite_read"):
            with self.subTest(stem=stem):
                self.assertIsNone(gates.suite_for_changes([f"git_remote_seafile/{stem}.py"]))

    def test_k_and_changed_together_are_refused(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as raised:
            gates.main(["--list", "-k", "test_url", "--changed"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("cannot be combined", err.getvalue())


if __name__ == "__main__":
    unittest.main()
