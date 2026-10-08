"""The version is declared once, and the build reads it from there.

It used to be written twice -- in ``pyproject.toml`` (what the installer records
and PyPI serves) and in ``git_remote_seafile.__version__`` (what
``git-remote-seafile --version`` prints) -- with a test comparing the two.  A
test that catches drift is worth less than not having drift, so the declaration
now lives in the package and the build reads it from there.

These tests pin the *wiring* rather than the equality, because equality is now
structural.  If `dynamic` stops naming the version, or the dynamic source stops
pointing at the attribute the package defines, the build falls back to a version
that is not the one the CLI reports -- silently, since nothing else reads both.

``pyproject.toml`` is parsed with a regex rather than ``tomllib`` because
``tomllib`` only exists on Python 3.11+ and the supported floor is 3.9.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from git_remote_seafile import __version__

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_INIT = _ROOT / "git_remote_seafile" / "__init__.py"

_ATTR = "git_remote_seafile.__version__"


def _pyproject() -> str:
    return _PYPROJECT.read_text(encoding="utf-8")


class TestVersionIsDeclaredOnce(unittest.TestCase):
    def test_pyproject_does_not_hard_code_a_version(self):
        # A literal assignment.  The dynamic entry is `version = {attr = ...}`,
        # which is a table, so the quote right after `=` is what distinguishes a
        # second copy of the fact from the pointer to the only one.
        self.assertIsNone(
            re.search(r'(?m)^version\s*=\s*"', _pyproject()),
            "pyproject.toml declares a literal version again. The package's "
            "__version__ is the single source -- bumping two places is how the "
            "installed package and `--version` come to disagree",
        )

    def test_pyproject_marks_the_version_dynamic(self):
        self.assertRegex(
            _pyproject(), r'(?m)^dynamic\s*=\s*\[\s*"version"\s*\]',
            "the project table must declare `dynamic = [\"version\"]`, or the "
            "build looks for a literal version and finds none",
        )

    def test_the_dynamic_source_is_the_package_attribute(self):
        match = re.search(
            r'(?m)^version\s*=\s*\{\s*attr\s*=\s*"([^"]+)"\s*\}', _pyproject()
        )
        self.assertIsNotNone(
            match,
            'no [tool.setuptools.dynamic] entry of the form '
            'version = {attr = "..."}',
        )
        self.assertEqual(match.group(1), _ATTR)

    def test_the_attribute_is_a_literal_the_backend_can_read(self):
        # setuptools resolves `attr:` by parsing the module when the value is a
        # plain literal, and by *importing* it otherwise -- which would make the
        # build depend on the package's runtime dependencies.
        self.assertRegex(
            _INIT.read_text(encoding="utf-8"),
            r'(?m)^__version__\s*=\s*"[^"]+"\s*$',
            "__version__ must stay a plain string literal for the build to read "
            "it without importing the package",
        )

    def test_the_version_is_a_plain_semver_triple(self):
        # Not a general semver validator -- just a guard against a version
        # string that would break `pip`/PyPI (e.g. a stray 'v' prefix).
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
