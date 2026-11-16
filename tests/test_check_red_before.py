"""tools/check_red_before.py: a commit's new tests are run against the code as the parent had it."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_red_before as crb  # noqa: E402
from _helpers import remove_tree  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
                   check=True, capture_output=True)


class TestTestIds(unittest.TestCase):
    def test_only_test_methods_of_classes_are_ids(self):
        source = "class A:\n    def test_x(self): pass\n    def helper(self): pass\ndef test_loose(): pass\n"
        self.assertEqual(crb.test_ids(source, "test_m"), {"test_m.A.test_x"})
        self.assertEqual(crb.test_ids("def broken(:", "test_m"), set())


class TestARepositoryRun(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="grs-crb-"))
        self.addCleanup(remove_tree, self.repo)
        (self.repo / "git_remote_seafile").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "git_remote_seafile" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "git_remote_seafile" / "m.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")

    def _commit(self, code: str | None, test: str | None, message: str) -> None:
        if code is not None:
            (self.repo / "git_remote_seafile" / "m.py").write_text(code, encoding="utf-8")
        if test is not None:
            (self.repo / "tests" / "test_m.py").write_text(test, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", message)

    def _run(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = crb.main(["--repo", str(self.repo), "HEAD"])
        return rc, out.getvalue()

    def test_a_test_that_needs_the_fix_is_red_without_it(self):
        self._commit("VALUE = 2\n", "import unittest\nfrom git_remote_seafile import m\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_value(self):\n        self.assertEqual(m.VALUE, 2)\n", "fix")
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertIn("1 red, 0 not red", out)

    def test_a_test_that_passes_without_the_fix_is_listed(self):
        self._commit("VALUE = 2\n", "import unittest\n\n\nclass T(unittest.TestCase):\n"
                     "    def test_nothing(self):\n        self.assertTrue(True)\n", "fix")
        rc, out = self._run()
        self.assertEqual(rc, 1, out)
        self.assertIn("not red without the code: test_m.T.test_nothing", out)

    def test_a_commit_without_a_code_change_is_skipped(self):
        self._commit(None, "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n", "tests only")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("skip", out)


if __name__ == "__main__":
    unittest.main()
