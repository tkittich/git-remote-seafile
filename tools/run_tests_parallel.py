#!/usr/bin/env python3
"""Run the test suite across processes.

Why this exists: ``python -m unittest discover tests`` is single-threaded, and
the end-to-end and snapshot modules dominate the run.  Measured on a 12-core /
24-thread machine (AMD 5900X), 693 tests across 138 targets:

    this runner,  -j 4                      339.0 s
    this runner,  -j 8                      271.6 s
    this runner,  -j 8 --granularity test   387.9 s    <- worse; see below

Read the column as indicative rather than exact.  On an idle machine repeated
runs spread by around a tenth, and the box's throughput drifts by much more
between sessions -- the same ``-j 8`` run has measured 232.9 s, 243.3 s, 245.9 s,
250.6 s, 271.6 s, 310.2 s and 328.9 s across sessions and suite sizes on this box
-- so the *shape* of the curve is what these numbers are here to show, not the
seconds or the ratio.  The 339.0 s and 271.6 s above are one back-to-back pair on
this box and give 1.25x; an earlier pair gave 1.40x.  Both are real, which is the
point.

The single-threaded baseline is not carried forward.  It was 204.4 s when the
suite held 268 tests, and the suite has since more than doubled, so that figure
is exactly the kind of stale number this docstring was rewritten to remove; run
``python -m unittest discover tests`` if you want a current one.

**The critical path is the floor.**  ``test_snapshot_tool.ByteExactnessTests``
is the longest single unit of work -- 95.7 s of the 271.6 s wall time at
``-j 8``, measured on its own at ``-j 1`` -- and a class is the smallest thing
the runner can schedule, so distributing the work takes the wall time down
towards that, not towards zero.
``test_snapshot_tool`` and ``test_e2e`` are most of the suite because every case
builds temp trees and shells out to a real ``git`` repeatedly, which is why the
work is I/O- and subprocess-bound rather than CPU-starved.

``-j 1`` is *slower* than the plain serial command, and that is the granularity
argument in miniature: one process per class pays an interpreter start-up and a
fresh ``import requests`` for each of the 138 targets.  Use ``python -m unittest
discover tests`` for a baseline; ``-j 1`` is not one.

**Granularity is the test class.**  Not the module, and not the test:

    class granularity, -j 8                 271.6 s    <- 138 targets
    test  granularity, -j 8                 387.9 s    <- 693 targets

* Per module would leave the whole of ``test_snapshot_tool`` on one worker, and
  that module is the bulk of the suite's work -- more than the class-granularity
  wall time above -- which is not worth the complexity.
* Per test is *worse*, which is not obvious.  ``E2ETestCase`` does its expensive
  work in ``setUpClass`` (a temp directory, a stub HTTP server, a git
  environment); scheduling per test repeats all of that for every test in the
  class.  The 555 extra interpreter start-ups cost less than the setup they
  duplicate.

**The curve flattens early.**  Eight workers are clearly better than four and
nowhere near twice as good, and beyond eight the wall time barely moves --
measured at 642 tests, ``-j 16`` was 243.4 s and ``-j 24`` 227.9 s against
232.9 s at ``-j 8``, a spread inside the run-to-run noise the column above
already disclaims, and not an ordering.  The default is one worker per CPU, which
on a wide machine is more than the suite can use.  The heavy targets are
themselves multi-process -- each E2E case runs ``git`` several times and serves
HTTP -- so a dozen of them at once contend for the same cores.

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
    python tools/run_tests_parallel.py --timeout 600  # kill a target after 10 minutes

A worker that runs longer than ``--timeout`` (30 minutes by default) is killed with
everything it started and reported as a failure naming the target.  Without it one
wedged ``git`` held the whole run open with no output and no way to tell why.  Ctrl-C
stops every worker (and the processes *they* started) before the runner exits 130, so
an interrupted run leaves nothing behind to hold a temporary directory open.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "tests"
_PATTERN = "test*.py"


def _ensure_importable() -> None:
    """Put the repository root and the test directory on ``sys.path``.

    The driver is run as ``python tools/run_tests_parallel.py``, so ``sys.path[0]``
    is ``tools/`` -- *not* the repository root -- and ``git_remote_seafile`` is
    therefore not importable from a checkout that was never ``pip install``ed.
    Discovery then fails module by module and ``unittest`` collapses every one of
    those failures into a single ``unittest.loader._FailedTest`` target, so the
    suite reports *73 of 138* targets and a green-looking exit rather than
    saying it did not load.  The workers need the same two entries, which is why
    this is called from both.
    """
    for entry in (str(_ROOT), str(_TESTS)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


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
    _ensure_importable()
    summary = {
        "target": target,
        "tests": 0,
        "failures": [],
        "errors": [],
        "skipped": 0,
        "skips": [],
        "unexpected_successes": [],
    }
    try:
        suite = unittest.TestLoader().loadTestsFromName(target)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        summary["tests"] = result.testsRun
        summary["failures"] = [t.id() for t, _ in result.failures]
        summary["errors"] = [t.id() for t, _ in result.errors]
        summary["skipped"] = len(result.skipped)
        summary["skips"] = [
            (test.id(), (reason or "").strip().splitlines()[0] if (reason or "").strip() else "")
            for test, reason in result.skipped
        ]
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
    """Spawn a worker for *target*.

    Returns ``(target, proc, log_file, log_path, result_path, started)``.  The
    open log file travels in the tuple rather than being stashed on the
    ``Popen``: the reaper has to close it once the process is gone, and an
    attribute invented on a stdlib object is invisible to a type checker.
    """
    log_path = log_dir / f"job-{job_index}.log"
    result_path = log_dir / f"job-{job_index}.json"
    log_file = open(log_path, "wb")

    # `unittest discover tests` puts the start directory on sys.path so the test
    # modules import as top-level names.  Reproduce that for the worker, which
    # imports the target by name -- and add the repository root, or the worker
    # cannot import `git_remote_seafile` from a checkout that was never
    # installed.  Prepend rather than replace: anything the caller already had
    # on PYTHONPATH (a sandbox hook, a virtualenv helper) must survive.
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    entries = [str(_TESTS), str(_ROOT)]
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)

    try:
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
            # Its own process group on POSIX, so a stop can reach what the worker
            # started (git, helpers, the stub's threads' children) in one call.
            start_new_session=(os.name != "nt"),
        )
    except BaseException:
        log_file.close()  # an unclosed handle here pinned the temp directory
        raise
    return target, proc, log_file, log_path, result_path, time.monotonic()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop *proc* and everything it started; harmless if it already exited."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            # getattr: neither name exists on Windows, and this branch never
            # runs there.
            getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))  # noqa: B009
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _reap(entry, timed_out_after: float | None = None) -> dict:
    """Wait for one worker, close its log, and return its result."""
    target, proc, log_file, log_path, result_path, started = entry
    proc.wait()
    log_file.close()
    elapsed = time.monotonic() - started

    try:
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except Exception:
        why = (
            f"<timed out after {timed_out_after:.0f}s and was killed>"
            if timed_out_after is not None else "<no result>"
        )
        result = {
            "target": target, "tests": 0, "failures": [], "errors": [f"{target} {why}"],
            "skipped": 0, "unexpected_successes": [],
        }

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
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="seconds one target may run before it is killed and reported "
                             "(default 1800; 0 disables)")
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
    _ensure_importable()

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

    # ignore_cleanup_errors: on Windows a stray child can still hold a file in
    # the log directory when the run is over; that must not turn a finished,
    # green run into a traceback and a non-zero exit.
    with tempfile.TemporaryDirectory(prefix="grs-parallel-", ignore_cleanup_errors=True) as tmp:
        log_dir = Path(tmp)
        done = 0
        total = len(targets)

        try:
            while pending or running:
                while pending and len(running) < jobs:
                    target = pending.pop(0)
                    running.append(_start_worker(target, log_dir, job_index))
                    job_index += 1

                # Poll rather than use multiprocessing.connection.wait: the poll
                # interval is 50 ms against a ~25 s run, and it behaves identically
                # on Windows and POSIX without depending on handle semantics.
                entry = None
                timed_out: float | None = None
                while entry is None:
                    for candidate in running:
                        if candidate[1].poll() is not None:
                            entry = candidate
                            break
                        if args.timeout and time.monotonic() - candidate[5] > args.timeout:
                            _kill_tree(candidate[1])
                            entry, timed_out = candidate, args.timeout
                            break
                    if entry is None:
                        time.sleep(0.05)

                running.remove(entry)
                result = _reap(entry, timed_out)
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
        except BaseException:
            # Ctrl-C, or anything else escaping the loop: stop every worker and
            # what it started, so none outlives the runner.
            for _target, proc, log_file, *_rest in running:
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                log_file.close()
            print(f"\ninterrupted: stopped {len(running)} running worker(s), "
                  f"{len(pending)} target(s) never started", file=sys.stderr, flush=True)
            raise

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
    skips = [entry for r in results for entry in r.get("skips", [])]
    tests = sum(r["tests"] for r in results)

    print("\n" + "-" * 70)
    print(
        f"Ran {tests} tests across {total} targets in {elapsed:.1f}s "
        f"({jobs} worker{'s' if jobs != 1 else ''})"
    )
    empty = [r["target"] for r in results if r["tests"] == 0 and not r["errors"] and not r["failures"]]
    if empty:
        print(f"Warning: {len(empty)} target(s) ran no tests: {', '.join(sorted(empty)[:5])}")
    if skipped:
        # A count alone reads as coverage: a run on a machine that quietly
        # cannot exercise a guard (junctions, symlinks, POSIX permissions)
        # looks identical to one that can (ROADMAP S18).  The platform
        # blind-spot problem becomes visible instead of inferred.
        print(f"skipped: {skipped}")
        for test_id, reason in skips:
            print(f"  skipped: {test_id}" + (f" -- {reason}" if reason else ""))

    if failures or errors or unexpected:
        print(f"FAILED (failures={len(failures)}, errors={len(errors)})")
        for name in failures + errors + unexpected:
            print(f"  {name}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
