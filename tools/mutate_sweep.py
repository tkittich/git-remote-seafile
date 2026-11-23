"""Mutate every guard of a module of the snapshot tool, in parallel, and list the mutations no test noticed.

    python tools/mutate_sweep.py git_remote_seafile/snapshot/embedded.py --jobs 12 \
        --tests tests.test_snapshot_tool.IncrementalStagingTests tests.test_incremental_property

``tools/mutate_check.py`` does one hand-picked mutation in the working tree.  This generates them: a comparison
turned round, ``and`` / ``or`` swapped, a ``not`` dropped, an ``if`` forced either way, a ``return`` of a constant
flipped, a ``continue`` / ``break`` / call statement removed.  Each runs in its own copy of ``tools/``,
``tests/`` and ``git_remote_seafile/`` (the module is changed there, the named tests run with
``--failfast``), so the working tree is never touched and workers do not collide.

A SURVIVED line is a candidate, not a verdict: many are equivalent (a changed message, a branch that cannot be
told apart).  Read it, write a test that fails on it when it is a real guard, drop it when it is not.
Exit status 0 when every mutation was killed, 1 when some survived.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from pathlib import Path

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


def _run_one(item: tuple[int, tuple[int, str, int, int, str]], part: str, tests: list[str], work: Path,
             timeout: float, then: "list[str] | None" = None) -> tuple[int, str, str, str]:
    index, (line, what, start, end, replacement) = item
    copy = work / f"m{index}"
    for folder in ("tools", "tests", "git_remote_seafile"):
        shutil.copytree(ROOT / folder, copy / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target = copy / part
    text = target.read_text(encoding="utf-8")
    target.write_text(text[:start] + replacement + text[end:], encoding="utf-8", newline="")
    shown = text[start:end].strip().replace("\n", " ")[:50]
    outcome = "SURVIVED"
    try:
        ran = subprocess.run([sys.executable, "-m", "unittest", "--failfast", *tests], cwd=copy,
                             capture_output=True, timeout=timeout)
        if ran.returncode != 0:
            outcome = "killed"
        elif then:      # a cheap first pass, the slow wider set only for what it missed
            again = subprocess.run([sys.executable, "-m", "unittest", "--failfast", *then], cwd=copy,
                                   capture_output=True, timeout=timeout)
            if again.returncode != 0:
                outcome = "killed"
    except subprocess.TimeoutExpired:
        outcome = "killed"          # a hang is noticed too
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    return line, what, shown, outcome


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("part", help="a module of the snapshot tool, e.g. git_remote_seafile/snapshot/embedded.py")
    parser.add_argument("--tests", nargs="+", required=True, help="unittest targets that should notice")
    parser.add_argument("--then", nargs="+", help="a wider, slower set run only for a mutation the first set missed")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--lines", help="only mutations on these lines, A-B")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)

    part = Path(args.part).as_posix()
    text = (ROOT / part).read_text(encoding="utf-8")
    todo = mutants(text)
    if args.lines:
        low, _, high = args.lines.partition("-")
        todo = [m for m in todo if int(low) <= m[0] <= int(high or low)]
    print(f"{len(todo)} mutations of {part}, {args.jobs} at a time", flush=True)
    survivors = broken = 0
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="grs-mutants-") as raw:
        work = Path(raw)
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
            futures = [pool.submit(_run_one, (i, m), part, args.tests, work, args.timeout, args.then) for i, m in enumerate(todo)]
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                line, what, shown, outcome = future.result()
                if outcome == "SURVIVED":
                    survivors += 1
                    print(f"SURVIVED {part}:{line}: {what}: {shown!r}", flush=True)
                elif outcome == "BROKEN":
                    broken += 1
                if done % 20 == 0:
                    print(f"  ... {done}/{len(todo)} after {time.monotonic() - began:.0f} s", flush=True)
    print(f"{len(todo)} mutations: {len(todo) - survivors - broken} killed, {survivors} survived, {broken} did not build")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
