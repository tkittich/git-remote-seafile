#!/usr/bin/env python3
"""Check that a commit's new tests fail without the commit's code (the "red before the fix" rule).

    python tools/check_red_before.py            # the commit at HEAD
    python tools/check_red_before.py <ref>      # any commit
    python tools/check_red_before.py --since <ref>   # every commit in <ref>..HEAD

For each commit that adds test methods *and* changes code (anything outside ``tests/`` and the
documents), the new tests are run against the commit's tree with the code put back as the parent had
it.  A new test that still passes there did not need the fix: it is listed and the exit status is 1.
A commit with no new tests, or no code change, is skipped (a documents-only commit is not asked for a
test).  Nothing is modified in the working tree: the check runs in a temporary worktree.

A test can legitimately pass without the fix (it pins a behaviour that was already right, a platform
branch this machine does not take): the list is for a person to read, not a verdict.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: What counts as "the code" of a commit: restored from the parent for the run.
CODE_PATHS = ("git_remote_seafile", "tools")


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd or ROOT, check=check, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def test_ids(source: str, module: str) -> set[str]:
    """``module.Class.test_name`` for every test method in *source*."""
    found: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                    found.add(f"{module}.{node.name}.{item.name}")
    return found


def new_tests(parent: str, commit: str) -> list[str]:
    """The test ids *commit* adds (or renames into existence) under ``tests/``."""
    names = _git("diff", "--name-only", "--diff-filter=AM", parent, commit, "--", "tests").stdout.split()
    added: list[str] = []
    for name in names:
        if not re.fullmatch(r"tests/test_\w+\.py", name):
            continue
        module = Path(name).stem
        new = _git("show", f"{commit}:{name}", check=False)
        old = _git("show", f"{parent}:{name}", check=False)
        before = test_ids(old.stdout, module) if old.returncode == 0 else set()
        added += sorted(test_ids(new.stdout, module) - before)
    return added


def changes_code(parent: str, commit: str) -> bool:
    names = _git("diff", "--name-only", parent, commit, "--", *CODE_PATHS).stdout.split()
    return any(not n.endswith((".md", ".txt")) for n in names)


def check_commit(commit: str) -> tuple[list[str], list[str]]:
    """``(tests that were red without the code, tests that were not)`` for one commit."""
    parent = _git("rev-parse", f"{commit}^").stdout.strip()
    commit = _git("rev-parse", commit).stdout.strip()
    ids = new_tests(parent, commit)
    if not ids or not changes_code(parent, commit):
        return [], []
    work = Path(tempfile.mkdtemp(prefix="grs-red-before-"))
    tree = work / "tree"
    try:
        _git("worktree", "add", "--detach", str(tree), commit)
        for path in CODE_PATHS:
            if _git("cat-file", "-e", f"{parent}:{path}", check=False).returncode == 0:
                _git("checkout", parent, "--", path, cwd=tree)
        red: list[str] = []
        green: list[str] = []
        for test in ids:
            ran = subprocess.run([sys.executable, "-m", "unittest", f"tests.{test}"], cwd=tree,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            (red if ran.returncode != 0 else green).append(test)
        return red, green
    finally:
        _git("worktree", "remove", "--force", str(tree), check=False)
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ref", nargs="?", default="HEAD")
    parser.add_argument("--since", help="check every commit in <since>..HEAD instead of one")
    parser.add_argument("--repo", type=Path, help="the repository to check (default: this one)")
    args = parser.parse_args(argv)
    global ROOT
    if args.repo is not None:
        ROOT = args.repo.resolve()
    commits = (_git("rev-list", "--reverse", f"{args.since}..HEAD").stdout.split()
               if args.since else [args.ref])
    not_red = 0
    for commit in commits:
        red, green = check_commit(commit)
        subject = _git("log", "-1", "--format=%h %s", commit).stdout.strip()
        if not red and not green:
            print(f"skip   {subject}  (no new test, or no code change)")
            continue
        print(f"{'ok  ' if not green else 'CHECK'}  {subject}  ({len(red)} red, {len(green)} not red)")
        for test in green:
            print(f"         not red without the code: {test}")
        not_red += len(green)
    return 1 if not_red else 0


if __name__ == "__main__":
    sys.exit(main())
