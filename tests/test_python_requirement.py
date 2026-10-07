"""test_python_requirement.py - The advertised Python floor must be enforced.

The project documents "Python 3.9+" and uses runtime APIs that do not exist on
3.8 (``str.removeprefix``/``str.removesuffix``, ``pathlib.Path.is_relative_to``).
``from __future__ import annotations`` defers annotation evaluation but cannot
backfill those, so 3.8 genuinely fails -- just not at import time.

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
    def test_minimum_is_3_9(self):
        self.assertEqual(MINIMUM_PYTHON, (3, 9))

    def test_rejects_python_38(self):
        with self.assertRaises(RuntimeError) as ctx:
            check_python_version((3, 8))
        message = str(ctx.exception)
        self.assertIn("3.9", message)
        self.assertIn("3.8", message)

    def test_rejects_everything_older(self):
        for version in [(2, 7), (3, 0), (3, 6), (3, 7), (3, 8)]:
            with self.subTest(version=version):
                with self.assertRaises(RuntimeError):
                    check_python_version(version)

    def test_accepts_supported_versions(self):
        for version in [(3, 9), (3, 10), (3, 12), (3, 13), (4, 0)]:
            with self.subTest(version=version):
                check_python_version(version)  # must not raise

    def test_accepts_the_running_interpreter(self):
        # The suite runs on a supported interpreter, so the check that fires
        # at import time must pass when called with no argument.
        check_python_version()


if __name__ == "__main__":
    unittest.main()
