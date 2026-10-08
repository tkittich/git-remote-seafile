#!/usr/bin/env python3
"""Run the test suite across processes.

Why this exists: ``python -m unittest discover tests`` is single-threaded, and
the end-to-end module dominates the run.  Measured on a 12-core / 24-thread
machine (AMD 5900X), 223 tests:

    python -m unittest discover tests       170.2 s    <- the baseline
    this runner,  -j 1                      218.1 s    <- not a baseline; see below
    this runner,  -j 2                      116.7 s
    this runner,  -j 4                       67.8 s
    this runner,  -j 6                       52.3 s
    this runner, -j 12                       43.1 s    <- default here (one per CPU)
    this runner, -j 24                       42.5 s

``test_e2e`` is most of the suite because every case shells out to a real
``git`` and starts a stub HTTP server: its ten classes are about two thirds of
the total.  So distributing the work takes the wall time down towards the
longest single unit of work -- 18.8 s here -- and not towards zero.

``-j 1`` is *slower* than the plain serial command, and that is the granularity
argument in miniature: one process per class pays an interpreter start-up and a
fresh ``import requests`` for each of the 56 targets, about 1.1 s apiece and
~60 s over the run.  Use ``python -m unittest discover tests`` for a baseline;
``-j 1`` is not one.

**Granularity is the test class.**  Not the module, and not the test:

    class granularity, -j 12                43.1 s    <- the default
    test  granularity, -j 12                70.4 s

* Per module would leave the whole of ``test_e2e`` on one worker -- about 1.5x,
  which is not worth the complexity.
* Per test is *worse*, which is not obvious.  ``E2ETestCase`` does its expensive
  work in ``setUpClass`` (a temp directory, a stub HTTP server, a git
  environment); scheduling per test repeats all of that for every test in the
  class.  The 223 extra interpreter start-ups cost less than the setup they
  duplicate.

**The curve flattens early.**  Twelve workers are 1.6x four, not 3x, and 24
workers buy 0.6 s over 12.  The heavy targets are themselves multi-process --
each E2E case runs ``git`` several times and serves HTTP -- so a dozen of them
at once contend for the same cores.  Six to twelve workers is the useful range
on this machine.

A class is the smallest thing ``unittest`` can load on its own, and each worker
is handed the next class the moment it finishes, so one slow class does not
leave the other workers idle.  ``--granularity test`` remains available for a
suite whose classes are lopsided; it is not an upgrade for this one.

**No dependencies, deliberately.**  CI installs with ``pip install -e .`` and
nothing else; this project has never had a test dependency.  ``pytest-xdist``
would be the off-the-shelf answer and is a fine one, but it is not worth a new
requirement for a runner that fits in a page.

Usage::

    python tools/run_tests_parallel.py              # auto: one worker per CPU
    python tools/run_tests_parallel.py -j 4
    python tools/run_tests_parallel.py -j 1         # one worker (not a baseline)
    python tools/run_tests_parallel.py --list       # show the units of work
    python tools/run_tests_parallel.py -k safety    # only matching targets
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "tests"
_PATTERN = "test*.py"


def _iter_tests(suite):
    """Yield every leaf test in a (possibly nested) suite."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _collect_targets(pattern_filter: str | None = None, granularity: str = "class") -> list[str]:
    """Return the loadable name of every unit of work the discovery run finds.

    Derived from ``discover`` rather than from the filesystem so that the set of
    tests is exactly the set the serial command would run -- including anything
    contributed through the ``load_tests`` protocol.

    ``top_level_dir`` is left alone so this mirrors ``python -m unittest
    discover tests`` precisely: with the start directory as its own top level,
    ``unittest`` imports the modules as ``test_cli``, not ``tests.test_cli``.
    Passing ``top_level_dir`` instead would make ``unittest`` demand a
    ``tests/__init__.py`` that this repository deliberately does not have.

    ``granularity`` decides the unit of work.  ``"class"`` is the default
    because a class is the smallest thing ``unittest`` can load on its own and
    the per-process start-up cost is paid once per class rather than once per
    test; ``"test"`` schedules individual tests, which balances better when a
    handful of classes are far heavier than the rest.
    """
    loader = unittest.TestLoader()
    suite = loader.discover(str(_TESTS), pattern=_PATTERN)
    names = set()
    for test in _iter_tests(suite):
        test_id = test.id()
        # test_e2e.TestPushFetchRoundTrip.test_push_then_clone
        #   -> test_e2e.TestPushFetchRoundTrip      (class granularity)
        #   -> test_e2e.TestPushFetchRoundTrip.test_push_then_clone  (test)
        # For a module-level function, class granularity yields the module,
        # which loads it all.
        names.add(test_id if granularity == "test" else test_id.rsplit(".", 1)[0])

    targets = sorted(names)
    if pattern_filter:
        needle = pattern_filter.lower()
        targets = [t for t in targets if needle in t.lower()]
    return targets


def _worker(target: str, out_path: str) -> int:
    """Run one target in this process and report the result as JSON.

    Invoked as a subprocess of the driver (``--_worker``).  Results go to a file
    rather than stdout so that the driver never has to parse unittest's prose,
    and stdout/stderr stay a plain log for a human to read on failure.
    """
    summary = {
        "target": target,
        "tests": 0,
        "failures": [],
        "errors": [],
        "skipped": 0,
        "unexpected_successes": [],
    }
    try:
        suite = unittest.TestLoader().loadTestsFromName(target)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        summary["tests"] = result.testsRun
        summary["failures"] = [t.id() for t, _ in result.failures]
        summary["errors"] = [t.id() for t, _ in result.errors]
        summary["skipped"] = len(result.skipped)
        summary["unexpected_successes"] = [
            t.id() for t, _ in getattr(result, "unexpectedSuccesses", [])
        ]
        ok = result.wasSuccessful()
    except Exception as ex:  # a target that will not even load is a failure
        summary["errors"] = [f"{target} <could not load: {ex!r}>"]
        ok = False

    Path(out_path).write_text(json.dumps(summary), encoding="utf-8")
    return 0 if ok else 1


def _start_worker(target: str, log_dir: Path, job_index: int) -> tuple:
    """Spawn a worker for *target*; return (target, proc, log_path, result_path, started)."""
    log_path = log_dir / f"job-{job_index}.log"
    result_path = log_dir / f"job-{job_index}.json"
    log_file = open(log_path, "wb")

    # `unittest discover tests` puts the start directory on sys.path so the test
    # modules import as top-level names.  Reproduce that for the worker, which
    # imports the target by name.
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_TESTS) + (os.pathsep + existing if existing else "")

    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_worker",
            target,
            "--out",
            str(result_path),
        ],
        cwd=str(_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    proc._grs_log_file = log_file  # closed by the caller when the proc is reaped
    return target, proc, log_path, result_path, time.monotonic()


def _reap(entry) -> dict:
    """Wait for one worker, close its log, and return its result."""
    target, proc, log_path, result_path, started = entry
    proc.wait()
    proc._grs_log_file.close()
    elapsed = time.monotonic() - started

    try:
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except Exception:
        result = {"target": target, "tests": 0, "failures": [], "errors": [f"{target} <no result>"]}

    result["elapsed"] = elapsed
    result["log"] = log_path.read_text(encoding="utf-8", errors="replace")
    return result


def _status_of(result: dict) -> str:
    if result["errors"] or result["failures"]:
        return "FAIL"
    if result["unexpected_successes"]:
        return "FAIL"
    return "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the test suite across processes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-j", "--jobs", type=int, default=0,
                        help="workers to run (0 = one per CPU, default)")
    parser.add_argument("-k", dest="filter", default=None,
                        help="only run targets whose name contains this substring")
    parser.add_argument("--granularity", choices=("class", "test"), default="class",
                        help="unit of work: a whole test class (default), or one "
                             "test at a time (better balance, more start-ups)")
    parser.add_argument("--list", action="store_true", help="list targets and exit")
    parser.add_argument("--_worker", dest="worker_target", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--out", dest="worker_out", default=None,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker_target is not None:
        return _worker(args.worker_target, args.worker_out)

    # The tests import `tests.*` and some shell out to git in the working
    # directory, so the driver must behave like the serial command: run from the
    # repository root regardless of where it was invoked from.
    os.chdir(_ROOT)

    targets = _collect_targets(args.filter, args.granularity)
    if not targets:
        print("no test targets matched", file=sys.stderr)
        return 2

    if args.list:
        for target in targets:
            print(target)
        return 0

    jobs = args.jobs if args.jobs > 0 else (os.cpu_count() or 1)
    jobs = max(1, min(jobs, len(targets)))

    pending = list(targets)
    running: list[tuple] = []
    results: list[dict] = []
    job_index = 0
    started_at = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="grs-parallel-") as tmp:
        log_dir = Path(tmp)
        done = 0
        total = len(targets)

        while pending or running:
            while pending and len(running) < jobs:
                target = pending.pop(0)
                running.append(_start_worker(target, log_dir, job_index))
                job_index += 1

            # Poll rather than use multiprocessing.connection.wait: the poll
            # interval is 50 ms against a ~25 s run, and it behaves identically
            # on Windows and POSIX without depending on handle semantics.
            entry = None
            while entry is None:
                for candidate in running:
                    if candidate[1].poll() is not None:
                        entry = candidate
                        break
                if entry is None:
                    time.sleep(0.05)

            running.remove(entry)
            result = _reap(entry)
            results.append(result)
            done += 1

            status = _status_of(result)
            count = result["tests"]
            plural = "test" if count == 1 else "tests"
            print(
                f"[{done:>3}/{total}] {status:<4} {result['target']:<52} "
                f"{count:>3} {plural:<5} {result['elapsed']:6.1f}s",
                flush=True,
            )

    elapsed = time.monotonic() - started_at

    # Print the captured log for anything that failed.  Passing runs stay quiet:
    # their logs are mostly git chatter, and 44 of them would bury the summary.
    for result in results:
        if _status_of(result) == "FAIL":
            print(f"\n{'=' * 70}\n{result['target']}\n{'=' * 70}")
            print(result["log"].rstrip())

    failures = [t for r in results for t in r["failures"]]
    errors = [t for r in results for t in r["errors"]]
    unexpected = [t for r in results for t in r["unexpected_successes"]]
    skipped = sum(r["skipped"] for r in results)
    tests = sum(r["tests"] for r in results)

    print("\n" + "-" * 70)
    print(
        f"Ran {tests} tests across {total} targets in {elapsed:.1f}s "
        f"({jobs} worker{'s' if jobs != 1 else ''})"
    )
    if skipped:
        print(f"skipped: {skipped}")

    if failures or errors or unexpected:
        print(f"FAILED (failures={len(failures)}, errors={len(errors)})")
        for name in failures + errors + unexpected:
            print(f"  {name}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
