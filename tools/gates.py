#!/usr/bin/env python3
"""Run every pre-commit gate in one command.

Why this exists
===============
AGENTS.md used to list the gates as seven separate commands, and every session
retyped them by hand.  Two things went wrong with that, more than once:

* the list drifted -- one rewrite of AGENTS.md described the same suite as
  "several minutes" and "about 2 minutes" in one section, and nobody could
  tell which was current;
* a command typed into a pipeline (``... | tail``) reports the *pipe's* exit
  status, so a red suite scrolled off the top read as green and the run moved
  on to the commit.

One entry point makes the sequence exist in exactly one place: AGENTS.md and
CONTRIBUTING.md point here, and ``STEPS`` below *is* the list.  Each command is
echoed before it runs, output is streamed rather than captured (the suite's
progress lines are the point of running it), and the first failure stops the
run carrying that step's own exit code.

``build_snapshot.py --check`` is deliberately not a step: the build step has
just rewritten the generated file, so the check cannot fail here, and neither
can ``tests/test_snapshot_build.py``, which runs it against the file *on disk*.
What CI checks is the committed file, and a rebuilt one left out of the commit
passes every local gate.  So after the build the driver compares the generated
file with ``HEAD`` and, when they differ, says so after the build and again at
the end: commit it with its parts (a warning, not a failure -- mid-change they
differ on purpose).

Usage::

    python tools/gates.py                           # everything, in order
    python tools/gates.py -k test_snapshot_payload  # -k reaches the suite step
    python tools/gates.py --list                    # print the commands, run nothing
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_PY = sys.executable

#: The file the first step generates, relative to the repository root.
GENERATED = "git_remote_seafile/snapshot_tool.py"

#: The gates, in order.  A label names the step in the failure line; the argv
#: is what runs.  Kept as data (not a chain of calls) so ``--list`` and the
#: test suite can read the sequence without executing it.
STEPS: list[tuple[str, list[str]]] = [
    ("build the snapshot tool from its parts",
     [_PY, str(_ROOT / "tools" / "build_snapshot.py")]),
    ("ruff", [_PY, "-m", "ruff", "check", "."]),
    ("mypy", [_PY, "-m", "mypy"]),
    ("mypy as CI checks Linux on 3.11",
     [_PY, "-m", "mypy", "--platform", "linux", "--python-version", "3.11"]),
    ("docs guards", [_PY, str(_ROOT / "tools" / "verify_docs_guards.py")]),
    ("docs consistency tests", [_PY, "-m", "unittest", "tests.test_docs_consistency"]),
    ("the test suite",
     [_PY, str(_ROOT / "tools" / "run_tests_parallel.py"), "-j", "0"]),
]


def _pretty(command: list[str]) -> str:
    """The command as a contributor would type it, for the echo line."""
    shown = ["python" if arg == _PY else arg for arg in command]
    root = str(_ROOT)
    prefix = root + os.sep
    return " ".join(arg[len(prefix):] if arg.startswith(prefix) else arg for arg in shown)


def generated_differs_from_head(root: Path = _ROOT, path: str = GENERATED) -> str | None:
    """A warning when *path* differs from its copy in ``HEAD``, else ``None``.

    ``None`` too when git cannot tell (no repository, no ``HEAD`` yet, git
    missing): the note is a reminder, and a reminder that cannot be computed
    must not stop the gates.
    """
    # Outside a repository `git diff` exits 1 as well (it falls back to
    # --no-index usage), so HEAD has to resolve first.
    def git(*args: str) -> int:
        return subprocess.run(
            ["git", *args], cwd=str(root),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode

    try:
        if git("rev-parse", "--verify", "-q", "HEAD") != 0:
            return None
        differs = git("diff", "--quiet", "HEAD", "--", path)
    except OSError:
        return None
    if differs != 1:
        return None
    return (
        f"gates: note -- {path} was rebuilt and differs from the committed copy; "
        "commit it with its parts (CI checks the committed file)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run every pre-commit gate, stopping at the first failure."
    )
    parser.add_argument(
        "-k", dest="filter", default=None, metavar="SUBSTRING",
        help="passed through to the suite step only, as in run_tests_parallel.py",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="print the commands this would run, and exit",
    )
    args = parser.parse_args(argv)

    steps = list(STEPS)
    if args.filter:
        label, command = steps[-1]
        steps[-1] = (label, command + ["-k", args.filter])

    if args.list:
        for label, command in steps:
            print(f"{_pretty(command)}   # {label}")
        return 0

    # The steps read pyproject.toml and relative paths against the repository
    # root, so the driver behaves the same wherever it was invoked from.
    os.chdir(_ROOT)

    note: str | None = None
    try:
        for index, (label, command) in enumerate(steps, 1):
            print(f"gates: [{index}/{len(steps)}] {_pretty(command)}", flush=True)
            code = subprocess.run(command, cwd=str(_ROOT)).returncode
            if code != 0:
                print(
                    f"gates: stopped at step {index} of {len(steps)} -- {label} "
                    f"(exit {code}); the steps above it were green.",
                    file=sys.stderr,
                )
                return code
            if command[1:] == [str(_ROOT / "tools" / "build_snapshot.py")]:
                note = generated_differs_from_head()
                if note:
                    print(note, file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        # Same contract as run_tests_parallel.py: an interrupted run is 130,
        # not a traceback.
        return 130

    if note:
        print(note, file=sys.stderr)
    print("gates: all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
