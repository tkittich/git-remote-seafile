"""The documented runner command must be able to see the whole suite.

``CONTRIBUTING.md`` tells a contributor to run ``python tools/run_tests_parallel.py``.
The driver is started as a *script*, so ``sys.path[0]`` is ``tools/`` -- not the
repository root -- and on a checkout where the package has not been ``pip install``ed
``git_remote_seafile`` is unimportable.  Discovery then fails module by module, and
``unittest`` collapses every one of those failures into a single
``unittest.loader._FailedTest`` target: the run reported **73 of 138** targets and
exited 0, so the shortfall read as a green run rather than as a failure to load.
The driver now repairs ``sys.path`` itself -- ``_ensure_importable``, called both in
``main()`` and in each worker.

The guard below runs the real command as a subprocess with the repository's own
``PYTHONPATH`` entries removed, and compares its target list with the one an
already-importable process discovers.  Two honest limits:

* On CI the package *is* installed (``pip install -e .``), where this bug cannot
  occur and the guard therefore passes trivially.  It exists for the contributor on a
  bare checkout, which is exactly where it bit -- the same CI-blindness that
  ``test_launcher.py`` documents for the launcher.
* Only the entries pointing at this checkout are removed, not ``PYTHONPATH``
  wholesale, so an interpreter hook installed by the calling environment still loads.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "tests"
_RUNNER = _ROOT / "tools" / "run_tests_parallel.py"


def _load_runner():
    """Import ``tools/run_tests_parallel.py`` by path.

    It is a script rather than a module, so it has no importable name.  Executing it
    runs only its module level -- the ``__main__`` guard keeps ``main()`` out -- which
    is all this guard needs.
    """
    spec = importlib.util.spec_from_file_location("_runner_under_test", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _without_this_checkout(value: str) -> str:
    """Drop the entries of *value* that name this repository or its ``tests/``."""
    keep = []
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = pathlib.Path(entry).resolve()
        except OSError:
            resolved = None
        if resolved in (_ROOT, _TESTS):
            continue
        keep.append(entry)
    return os.pathsep.join(keep)


class RunnerDiscoveryTests(unittest.TestCase):
    """The runner's target list must not depend on the caller's ``sys.path``."""

    def test_the_documented_command_discovers_the_whole_suite(self):
        # What the suite looks like to a process that can already import the package.
        expected = _load_runner()._collect_targets()
        self.assertNotIn(
            "unittest.loader._FailedTest",
            expected,
            "in-process discovery already failed to load a module, so this guard "
            "could not tell a healthy run from a broken one",
        )

        # What the documented command reports when nothing else supplies the root.
        env = dict(os.environ)
        stripped = _without_this_checkout(env.get("PYTHONPATH", ""))
        if stripped:
            env["PYTHONPATH"] = stripped
        else:
            env.pop("PYTHONPATH", None)

        proc = subprocess.run(
            [sys.executable, str(_RUNNER), "--list"],
            capture_output=True,
            text=True,
            env=env,
            cwd=tempfile.gettempdir(),  # not the checkout: cwd must not be what saves it
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        actual = [line for line in proc.stdout.splitlines() if line.strip()]

        self.assertNotIn(
            "unittest.loader._FailedTest",
            actual,
            "the documented command could not import the suite:\n" + proc.stdout,
        )
        self.assertEqual(
            sorted(actual),
            sorted(expected),
            "python tools/run_tests_parallel.py --list found a different set of "
            "targets than an importable process did; the driver is not putting the "
            "repository root on sys.path",
        )


if __name__ == "__main__":
    unittest.main()
