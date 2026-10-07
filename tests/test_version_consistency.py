"""The version must agree everywhere it is declared.

``pyproject.toml`` (what the installer records and PyPI serves) and
``git_remote_seafile.__version__`` (what ``git-remote-seafile --version``
prints) are two independent declarations of the same fact. Nothing keeps them
in sync, so a bump that touches only one ships a package whose CLI reports a
version that is not the one installed -- the same quiet inconsistency this
project has been eliminating elsewhere.

``pyproject.toml`` is parsed with a regex rather than ``tomllib`` because
``tomllib`` only exists on Python 3.11+ and the supported floor is 3.9.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from git_remote_seafile import __version__

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version() -> str:
    """Return the top-level ``version`` from pyproject.toml.

    Anchored to the start of a line so ``requires-python`` and the
    ``Programming Language :: Python :: 3.x`` classifiers cannot match.
    """
    text = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if match is None:
        raise AssertionError("no top-level version = \"...\" in pyproject.toml")
    return match.group(1)


class TestVersionConsistency(unittest.TestCase):
    def test_pyproject_and_package_versions_match(self):
        self.assertEqual(
            _pyproject_version(),
            __version__,
            "pyproject.toml and git_remote_seafile.__version__ disagree; "
            "bump both or the installed package and `--version` will differ",
        )

    def test_version_is_a_plain_semver_triple(self):
        # Not a general semver validator -- just a guard against a version
        # string that would break `pip`/PyPI (e.g. a stray 'v' prefix).
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
