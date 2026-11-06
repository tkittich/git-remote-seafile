"""Measure what ``git add`` costs when it is handed a long list of paths.

``git add --pathspec-from-file`` (and any ``git add`` with many arguments) matches
the listed paths against the index and the work tree, so the time grows with the
number of files *times* the number of paths listed.  ``git update-index --add
--stdin`` looks each path up by name instead.  This script builds a scratch
repository of N tiny files with every file already tracked, adds a few new ones,
and times the three ways of staging them:

* ``add`` given every path (what the snapshot tool did before 0.9.22 on a run that
  left files out, and on every Windows run);
* ``update-index`` given every path;
* ``add .`` (what an unfiltered run does; no list at all).

Nothing outside a temporary directory is touched.  Usage::

    python tools/measure_pathspec_cost.py [N ...]      # default: 5000 10000 20000 40000

The last column is the growth from the previous N; a ratio near the ratio of the
sizes is linear, a ratio near its square is quadratic.  See SNAPSHOT.md, "Git's own
costs".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NEW_FILES = 20


class StepFailed(Exception):
    """A git step failed: which one, and what git said."""


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    """Run git in *root*; a failure names the command and git's own message.

    A bare ``CalledProcessError`` traceback was all a reviewer got when an antivirus held
    a file in the scratch tree (triage T22): the step and git's stderr are what say so.
    """
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    proc = subprocess.run(["git", *args], cwd=root, env=env, input=data, capture_output=True)
    if proc.returncode != 0:
        said = proc.stderr.decode("utf-8", "replace").strip() or "(no message)"
        raise StepFailed(f"git {' '.join(args[:3])} failed in {root} (exit {proc.returncode}): {said}")
    return proc.stdout


def make_tree(root: Path, count: int) -> None:
    for i in range(count):
        folder = root / f"d{i % 200}"
        folder.mkdir(exist_ok=True)
        (folder / f"f{i}.txt").write_text(str(i))
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", "-A", "-f", ".")  # warm index: every file tracked


def add_new_files(root: Path, tag: str) -> list[str]:
    names = [f"d0/{tag}{i}.txt" for i in range(NEW_FILES)]
    for name in names:
        (root / name).write_text("new")
    return names


def seconds(root: Path, *args: str, data: bytes | None = None) -> float:
    started = time.monotonic()
    git(root, *args, data=data)
    return time.monotonic() - started


def measure(count: int) -> tuple[float, float, float]:
    root = Path(tempfile.mkdtemp(prefix="grs-pathspec-"))
    try:
        make_tree(root, count)
        everything = git(root, "ls-files", "-z")
        listed = everything + b"".join(
            f"{name}\0".encode() for name in add_new_files(root, "a")
        )
        add_list = seconds(
            root, "add", "-A", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", data=listed
        )
        listed = everything + b"".join(
            f"{name}\0".encode() for name in add_new_files(root, "b")
        )
        index_list = seconds(
            root, "update-index", "--add", "--remove", "-z", "--stdin", data=listed
        )
        add_new_files(root, "c")
        add_dot = seconds(root, "add", "-A", "-f", ".")
        return add_list, index_list, add_dot
    finally:
        def clear_read_only(function, target, _error):
            os.chmod(target, 0o700)  # git's object files are read-only on Windows
            function(target)

        if sys.version_info >= (3, 12):
            shutil.rmtree(root, onexc=clear_read_only)
        else:
            shutil.rmtree(root, onerror=clear_read_only)


def main(argv: list[str]) -> int:
    sizes = [int(a) for a in argv] or [5000, 10000, 20000, 40000]
    print(f"git: {git(Path.cwd(), '--version').decode().strip()}")
    print(f"{'files':>8} {'add + every path':>18} {'update-index + all':>20} {'add .':>8}  growth of add")
    previous: float | None = None
    for count in sizes:
        try:
            add_list, index_list, add_dot = measure(count)
        except StepFailed as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(
                "(a file held open by another program -- an antivirus scanning the "
                "scratch tree, for example -- fails a step this way; run again, or exclude the "
                "temporary directory from the scan)",
                file=sys.stderr,
            )
            return 1
        growth = f"x{add_list / previous:.1f}" if previous else ""
        print(f"{count:>8} {add_list:>17.2f}s {index_list:>19.2f}s {add_dot:>7.2f}s  {growth}")
        previous = add_list
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
