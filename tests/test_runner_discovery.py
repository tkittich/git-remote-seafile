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

try:
    from _helpers import remove_tree  # noqa: E402
except ImportError:  # run as ``python -m unittest tests.<module>``
    from tests._helpers import remove_tree  # noqa: E402

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
            # Class granularity: the default plan splits heavy classes by the last
            # run's record, which this guard is not about.
            [sys.executable, str(_RUNNER), "--list", "--granularity", "class"],
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


class RunnerRobustnessTests(unittest.TestCase):
    """The runner must not leave processes behind, hang silently, or leak a handle.

    All three happened: a worker that wedged held the whole run open with no
    output for ten minutes; Ctrl-C orphaned workers (and the git they started);
    and a failed spawn leaked the log handle that pinned the temp directory.
    """

    def setUp(self):
        self.runner = _load_runner()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-runner-"))
        self.addCleanup(remove_tree, self.tmp)

    def _sleeper(self):
        log = open(self.tmp / "job.log", "wb")
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=log, stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return ("tests.fake.Target", proc, log, self.tmp / "job.log", self.tmp / "job.json",
                __import__("time").monotonic())

    def test_a_failed_spawn_closes_the_log_it_opened(self):
        import unittest.mock

        opened = []
        real_open = open

        def spy(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            opened.append(handle)
            return handle

        with unittest.mock.patch("builtins.open", spy), \
                unittest.mock.patch.object(self.runner.subprocess, "Popen", side_effect=OSError("no")):
            with self.assertRaises(OSError):
                self.runner._start_worker("tests.x.Y", self.tmp, 0)
        logs = [h for h in opened if str(self.tmp) in str(getattr(h, "name", ""))]
        self.assertTrue(logs)
        self.assertTrue(all(h.closed for h in logs))

    def test_kill_tree_stops_the_worker(self):
        entry = self._sleeper()
        self.assertIsNone(entry[1].poll())
        self.runner._kill_tree(entry[1])
        entry[1].wait(timeout=15)
        self.assertIsNotNone(entry[1].poll())
        entry[2].close()

    def test_a_timed_out_worker_is_named_not_reported_as_no_result(self):
        entry = self._sleeper()
        self.runner._kill_tree(entry[1])
        result = self.runner._reap(entry, timed_out_after=30)
        self.assertTrue(any("timed out after 30s" in e for e in result["errors"]), result)
        self.assertEqual(self.runner._status_of(result), "FAIL")

    def test_an_interrupt_stops_every_running_worker_and_propagates(self):
        import unittest.mock

        entry = self._sleeper()
        real_sleep = self.runner.time.sleep
        calls = {"n": 0}

        def interrupt_once(seconds):
            # time is the process-wide module: later sleeps (inside
            # Popen.wait on POSIX) must be real, or the cleanup is what breaks.
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            real_sleep(seconds)

        with unittest.mock.patch.object(self.runner, "_collect_targets", return_value=["tests.fake.Target"]), \
                unittest.mock.patch.object(self.runner, "_start_worker", return_value=entry), \
                unittest.mock.patch.object(self.runner.time, "sleep", side_effect=interrupt_once), \
                unittest.mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            with self.assertRaises(KeyboardInterrupt):
                self.runner.main(["-j", "1"])
        self.assertIsNotNone(entry[1].poll(), "the worker outlived the runner")
        self.assertIn("interrupted: stopped 1 running worker", err.getvalue())

    def test_the_log_directory_cleanup_cannot_fail_a_finished_run(self):
        # ignore_cleanup_errors is what keeps a stray handle on Windows from
        # turning green results into a traceback after they were printed.
        text = _RUNNER.read_text(encoding="utf-8")
        self.assertIn("ignore_cleanup_errors=True", text)



class PlanTests(unittest.TestCase):
    """The order and the unit of work, from the last run's record."""

    BY_CLASS = {
        "m.Short": ["m.Short.test_a"],
        "m.Heavy": ["m.Heavy.test_a", "m.Heavy.test_b", "m.Heavy.test_c"],
        "m.Middle": ["m.Middle.test_a", "m.Middle.test_b"],
        "m.New": ["m.New.test_a"],
    }

    def test_with_no_record_the_plan_is_by_class_in_name_order(self):
        plan = _load_runner()._plan(self.BY_CLASS, "auto", {})
        self.assertEqual(plan, [(k, False) for k in sorted(self.BY_CLASS)])

    def test_a_heavy_class_runs_as_single_tests_and_the_longest_go_first(self):
        runner = _load_runner()
        timings = {"m.Short": 5.0, "m.Heavy": 600.0, "m.Middle": 90.0}
        plan = runner._plan(self.BY_CLASS, "auto", timings)
        # The class with no record first (it may be long), then the heavy class's
        # tests (600/3 = 200 s each, over the split bound), then by time.
        self.assertEqual([t for t, _ in plan],
                         ["m.New", "m.Heavy.test_a", "m.Heavy.test_b", "m.Heavy.test_c",
                          "m.Middle", "m.Short"])
        self.assertEqual({t for t, split in plan if split},
                         {"m.Heavy.test_a", "m.Heavy.test_b", "m.Heavy.test_c"})
        self.assertGreater(600.0, runner._SPLIT_SECONDS)
        self.assertLess(90.0, runner._SPLIT_SECONDS)

    def test_class_granularity_never_splits_and_test_granularity_always_does(self):
        runner = _load_runner()
        timings = {"m.Heavy": 600.0}
        self.assertNotIn(True, {s for _t, s in runner._plan(self.BY_CLASS, "class", timings)})
        self.assertEqual(len(runner._plan(self.BY_CLASS, "test", timings)), 7)

    def test_the_record_is_merged_and_a_split_class_is_summed(self):
        runner = _load_runner()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "t.json"
            path.write_text('{"m.Short": 5.0, "m.Other": 1.0, "bad": true}', encoding="utf-8")
            runner._save_timings([
                {"target": "m.Short", "elapsed": 6.0, "split": False},
                {"target": "m.Heavy.test_a", "elapsed": 100.0, "split": True},
                {"target": "m.Heavy.test_b", "elapsed": 50.0, "split": True},
            ], path)
            got = runner._load_timings(path)
        self.assertEqual(got, {"m.Short": 6.0, "m.Other": 1.0, "m.Heavy.test_a": 100.0,
                               "m.Heavy.test_b": 50.0, "m.Heavy": 150.0})

    def test_an_unreadable_record_is_no_record(self):
        runner = _load_runner()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "t.json"
            for text in ("not json", "[1, 2]", ""):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(runner._load_timings(path), {})
            self.assertEqual(runner._load_timings(pathlib.Path(tmp) / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
