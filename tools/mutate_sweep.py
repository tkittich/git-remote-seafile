"""Mutate every guard of a part of the snapshot tool, in parallel, and list the mutations no test noticed.

    python tools/mutate_sweep.py tools/_snapshot/71_embedded.py --jobs 12 \
        --tests tests.test_snapshot_tool.IncrementalStagingTests tests.test_incremental_property

``tools/mutate_check.py`` does one hand-picked mutation in the working tree.  This generates them: a comparison
turned round, ``and`` / ``or`` swapped, a ``not`` dropped, an ``if`` forced either way, a ``return`` of a constant
flipped, a ``continue`` / ``break`` / call statement removed.  Each runs in its own copy of ``tools/``,
``tests/`` and ``git_remote_seafile/`` (the part is changed there, ``build_snapshot.py`` rebuilds the flat module,
the tests run with ``--failfast``), so the working tree is never touched and workers do not collide.

``--select`` (needs ``coverage``) tries to run less: it first runs every test class
under ``--tests`` and ``--then`` once and records which lines of the tool each reaches (``--cache FILE`` keeps that
map for the next part of the same tool).  A mutation is then run against the classes that reach its line only, the
class that has killed most so far first, one class at a time with ``--failfast``; a line no class reaches is listed
as SURVIVED (no test runs this line).  Measured on 67_hashing (2026-11-24) it was NOT faster: one slow class was the
only one to reach the lines, and the measuring cost 40 minutes.  Without it the ``--tests`` set runs, then the
``--then`` set for what it missed.  Give ``--select`` the same ``--then`` set, or lines only that set reaches are
reported as not run.
``--jobs`` defaults to 12; on a machine with the cores, 24 is faster.  Linux runs a process, and git, much faster than
Windows: ``wsl -d Debian`` in a clone on the Linux file system, or ``.github/workflows/mutate.yml`` on a runner.

A SURVIVED line is a candidate, not a verdict: many are equivalent (a changed message, a branch that cannot be
told apart).  Read it, write a test that fails on it when it is a real guard, drop it when it is not.
Exit status 0 when every mutation was killed, 1 when some survived.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from pathlib import Path

try:
    import coverage
except ImportError:      # the sweep still runs, without picking tests by the lines they run
    coverage = None     # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
_SWAPS = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">",
          ast.In: "not in", ast.NotIn: "in", ast.Is: "is not", ast.IsNot: "is"}


def _offsets(text: str) -> list[int]:
    """Character offset of the start of every line (``ast`` columns are UTF-8 byte offsets)."""
    starts, at = [0], 0
    for line in text.split("\n"):
        at += len(line) + 1
        starts.append(at)
    return starts


def mutants(text: str) -> list[tuple[int, str, int, int, str]]:
    """``(line, description, start, end, replacement)`` for every mutation, offsets into ``text``."""
    tree = ast.parse(text)
    starts = _offsets(text)
    lines = text.split("\n")

    def pos(line: Any, col: Any) -> int:
        return starts[line - 1] + len(lines[line - 1].encode("utf-8")[:col].decode("utf-8", "ignore"))

    def span(node: Any) -> tuple[int, int]:
        return pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset)

    found: list[tuple[int, str, int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _SWAPS:
            left_end = pos(node.left.end_lineno, node.left.end_col_offset)
            right_start = pos(node.comparators[0].lineno, node.comparators[0].col_offset)
            found.append((node.lineno, f"{type(node.ops[0]).__name__} -> {_SWAPS[type(node.ops[0])]}",
                          left_end, right_start, f" {_SWAPS[type(node.ops[0])]} "))
        elif isinstance(node, ast.BoolOp):
            for left, right in zip(node.values, node.values[1:], strict=False):
                word = "or" if isinstance(node.op, ast.And) else "and"
                found.append((left.lineno, f"{type(node.op).__name__} -> {word}",
                              pos(left.end_lineno, left.end_col_offset), pos(right.lineno, right.col_offset),
                              f" {word} "))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            start, end = span(node)
            inner_start, _ = span(node.operand)
            found.append((node.lineno, "not dropped", start, inner_start, ""))
        elif isinstance(node, (ast.If, ast.While)):
            start, end = span(node.test)
            for forced in ("False", "True"):
                found.append((node.lineno, f"condition -> {forced}", start, end, forced))
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value in (True, False):
            start, end = span(node.value)
            found.append((node.lineno, f"return {node.value.value} -> {not node.value.value}", start, end,
                          str(not node.value.value)))
        elif isinstance(node, (ast.Continue, ast.Break)):
            start, end = span(node)
            found.append((node.lineno, f"{type(node).__name__.lower()} -> pass", start, end, "pass"))
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
              and getattr(node.value.func, "id", "") != "print"):      # a message is not a guard
            start, end = span(node)
            found.append((node.lineno, "call statement removed", start, end, "pass"))
    return sorted(set(found))


GENERATED = Path("git_remote_seafile") / "snapshot_tool.py"


def generated_line(part: str, line: int) -> int:
    """The line of the generated tool that is line ``line`` of ``part`` (the tool is the parts, joined in name order)."""
    before = 0
    for path in sorted((ROOT / "tools" / "_snapshot").glob("*.py")):
        if path.name == Path(part).name:
            return before + line
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        before += text.count("\n") + (0 if text.endswith("\n") else 1)
    raise ValueError(f"{part} is not a part of the snapshot tool")


def expand_targets(targets: "list[str]") -> list[str]:
    """Every test class under the targets (a module, or a class, or a method stays as it is): a finer unit to pick by."""
    import unittest
    sys.path.insert(0, str(ROOT))
    found: list[str] = []

    def walk(suite: Any) -> None:
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                walk(test)
            else:
                unit = f"{type(test).__module__}.{type(test).__qualname__}"
                if unit not in found:
                    found.append(unit)

    for target in targets:
        loaded = unittest.defaultTestLoader.loadTestsFromName(target)
        before = len(found)
        walk(loaded)
        if len(found) == before:        # a failed import or nothing in it: keep what was named
            found.append(target)
    return found


class Selector:
    """Which tests to try for a mutation, and in which order: only those that run the line, the likeliest killer first.

    ``lines[target]`` is the set of generated-tool lines the target runs, ``cost[target]`` its time.  Every kill is
    recorded, so a test that keeps killing moves to the front for the mutations that follow.
    """

    def __init__(self, lines: "dict[str, set[int]]", cost: "dict[str, float]") -> None:
        self.lines, self.cost = lines, cost
        self.kills = dict.fromkeys(lines, 0)
        self._lock = threading.Lock()

    def stages(self, line: int) -> list[list[str]]:
        with self._lock:
            order = sorted((t for t, seen in self.lines.items() if line in seen),
                           key=lambda t: (-self.kills[t], self.cost.get(t, 0.0), t))
        return [[t] for t in order]

    def killed_by(self, stage: "list[str]") -> None:
        with self._lock:
            for target in stage:
                self.kills[target] = self.kills.get(target, 0) + 1


def measure(targets: "list[str]", jobs: int, cache: "Path | None") -> "tuple[dict[str, set[int]], dict[str, float]]":
    """Run each target once under ``coverage`` and note which lines of the generated tool it reaches (and how long it took)."""
    digest = hashlib.sha256((ROOT / GENERATED).read_bytes()).hexdigest()
    if cache is not None and cache.is_file():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("digest") == digest and saved.get("targets") == targets:
                return ({t: set(v) for t, v in saved["lines"].items()}, saved["cost"])
        except (OSError, ValueError, KeyError):
            pass
    lines: dict[str, set[int]] = {}
    cost: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="grs-cover-") as raw:
        def one(item: "tuple[int, str]") -> "tuple[str, set[int], float]":
            index, target = item
            folder = Path(raw) / str(index)
            folder.mkdir()
            rc = folder / "rc"
            rc.write_text("[run]\nparallel = true\npatch = subprocess\ninclude =\n    */git_remote_seafile/snapshot_tool.py\n",
                          encoding="utf-8")
            env = {**os.environ, "COVERAGE_FILE": str(folder / ".coverage"), "COVERAGE_RCFILE": str(rc)}
            began = time.monotonic()
            subprocess.run([sys.executable, "-m", "coverage", "run", "-m", "unittest", target], cwd=ROOT, env=env,
                           capture_output=True)
            took = time.monotonic() - began
            subprocess.run([sys.executable, "-m", "coverage", "combine", "--data-file", str(folder / ".coverage"),
                            str(folder)], cwd=ROOT, env=env, capture_output=True)
            seen: set[int] = set()
            data = coverage.CoverageData(basename=str(folder / ".coverage"))
            data.read()
            for name in data.measured_files():
                if name.replace("\\", "/").endswith("git_remote_seafile/snapshot_tool.py"):
                    seen |= set(data.lines(name) or ())
            return target, seen, took

        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            for target, seen, took in pool.map(one, enumerate(targets)):
                lines[target], cost[target] = seen, took
    if cache is not None:
        cache.write_text(json.dumps({"digest": digest, "targets": targets, "cost": cost,
                                     "lines": {t: sorted(v) for t, v in lines.items()}}), encoding="utf-8")
    return lines, cost


def _run_one(item: tuple[int, tuple[int, str, int, int, str]], part: str, stages: "list[list[str]] | Selector",
             work: Path, timeout: float) -> tuple[int, str, str, str]:
    """Mutate, rebuild, run the stages in turn until one fails.  With a ``Selector`` the stages are chosen for the line."""
    index, (line, what, start, end, replacement) = item
    text = (ROOT / part).read_text(encoding="utf-8")
    shown = text[start:end].strip().replace("\n", " ")[:50]
    if isinstance(stages, Selector):
        chosen = stages.stages(generated_line(part, line))
        if not chosen:
            return line, what, shown, "SURVIVED (no test runs this line)"
    else:
        chosen = stages
    copy = work / f"m{index}"
    for folder in ("tools", "tests", "git_remote_seafile"):
        shutil.copytree(ROOT / folder, copy / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target = copy / part
    target.write_text(text[:start] + replacement + text[end:], encoding="utf-8", newline="")
    outcome = "SURVIVED"
    try:
        built = subprocess.run([sys.executable, "tools/build_snapshot.py"], cwd=copy, capture_output=True)
        if built.returncode != 0:
            outcome = "BROKEN"      # the mutant does not build: not a finding
        else:
            for stage in chosen:
                ran = subprocess.run([sys.executable, "-m", "unittest", "--failfast", *stage], cwd=copy,
                                     capture_output=True, timeout=timeout)
                if ran.returncode != 0:
                    outcome = "killed"
                    if isinstance(stages, Selector):
                        stages.killed_by(stage)
                    break
    except subprocess.TimeoutExpired:
        outcome = "killed"          # a hang is noticed too
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    return line, what, shown, outcome


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("part", help="a part of the snapshot tool, e.g. tools/_snapshot/71_embedded.py")
    parser.add_argument("--tests", nargs="+", required=True, help="unittest targets that should notice")
    parser.add_argument("--then", nargs="+", help="a wider, slower set; run only for a mutation the first set missed")
    parser.add_argument("--jobs", type=int, default=min(12, os.cpu_count() or 4))
    parser.add_argument("--lines", help="only mutations on these lines, A-B")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--select", action="store_true",
                        help="pick the test classes by the lines they run (needs coverage; see the top of this file)")
    parser.add_argument("--cache", type=Path,
                        help="keep the coverage map here; a sweep of another part of the same tool reuses it")
    args = parser.parse_args(argv)

    part = Path(args.part).as_posix()
    text = (ROOT / part).read_text(encoding="utf-8")
    todo = mutants(text)
    if args.lines:
        low, _, high = args.lines.partition("-")
        todo = [m for m in todo if int(low) <= m[0] <= int(high or low)]
    began = time.monotonic()
    plan: "list[list[str]] | Selector"
    if not args.select or coverage is None:
        if coverage is None and args.select:
            print("coverage is not installed: running every mutation against the whole set (pip install coverage)")
        plan = [list(args.tests)] + ([list(args.then)] if args.then else [])
    else:
        units = expand_targets([*args.tests, *(args.then or [])])
        print(f"measuring which of {len(units)} test classes run which line ...", flush=True)
        lines, cost = measure(units, args.jobs, args.cache)
        plan = Selector(lines, cost)
        print(f"measured in {time.monotonic() - began:.0f} s", flush=True)
    print(f"{len(todo)} mutations of {part}, {args.jobs} at a time", flush=True)
    survivors = broken = 0
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="grs-mutants-") as raw:
        work = Path(raw)
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
            futures = [pool.submit(_run_one, (i, m), part, plan, work, args.timeout) for i, m in enumerate(todo)]
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                line, what, shown, outcome = future.result()
                if outcome.startswith("SURVIVED"):
                    survivors += 1
                    note = outcome[len("SURVIVED"):]
                    print(f"SURVIVED{note} {part}:{line}: {what}: {shown!r}", flush=True)
                elif outcome == "BROKEN":
                    broken += 1
                if done % 20 == 0:
                    print(f"  ... {done}/{len(todo)} after {time.monotonic() - began:.0f} s", flush=True)
    print(f"{len(todo)} mutations: {len(todo) - survivors - broken} killed, {survivors} survived, {broken} did not build")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
