"""test_python_requirement.py - The advertised Python floor must be enforced.

The project documents "Python 3.10+" and refuses older interpreters loudly:
3.9 reached end of life in October 2025, and ``requires-python`` is only
consulted by installers -- a source checkout running the helper directly needs
an explicit import-time guard.

A source checkout (``git clone`` + run the helper directly) never consults
``requires-python``, so without an explicit guard the user gets a cryptic
``AttributeError: 'str' object has no attribute 'removeprefix'`` from deep
inside a git operation. The package must refuse to load on an unsupported
interpreter, and the message must name both the requirement and the found
version.
"""

from __future__ import annotations

import unittest

from git_remote_seafile import MINIMUM_PYTHON, check_python_version


class TestPythonVersionGuard(unittest.TestCase):
    def test_minimum_is_3_10(self):
        self.assertEqual(MINIMUM_PYTHON, (3, 10))

    def test_rejects_python_39(self):
        with self.assertRaises(RuntimeError) as ctx:
            check_python_version((3, 9))
        message = str(ctx.exception)
        self.assertIn("3.10", message)
        self.assertIn("3.9", message)

    def test_rejects_everything_older(self):
        for version in [(2, 7), (3, 0), (3, 6), (3, 7), (3, 8), (3, 9)]:
            with self.subTest(version=version):
                with self.assertRaises(RuntimeError):
                    check_python_version(version)

    def test_accepts_supported_versions(self):
        for version in [(3, 10), (3, 11), (3, 12), (3, 13), (4, 0)]:
            with self.subTest(version=version):
                check_python_version(version)  # must not raise

    def test_accepts_the_running_interpreter(self):
        # The suite runs on a supported interpreter, so the check that fires
        # at import time must pass when called with no argument.
        check_python_version()


if __name__ == "__main__":
    unittest.main()
