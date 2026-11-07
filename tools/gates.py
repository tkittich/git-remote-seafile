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

``--changed`` runs every step but narrows the suite to the test modules the
changed files can reach (``suite_for_changes`` below holds the map).  Shared
code, the stub, the test helpers and any file the map does not know run the
whole suite, so a mistake in the map costs time, not coverage; a change to
documents alone runs no suite (the docs steps check them).  It is for the commits
of a change in progress.  The whole suite still runs before a push, a release,
or a commit that touches shared code -- AGENTS.md "Gates".

Usage::

    python tools/gates.py                           # everything, in order
    python tools/gates.py --changed                 # only the tests the working changes reach
    python tools/gates.py --changed origin/main     # ... the changes since a commit
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


#: Package modules whose change reaches everything, the snapshot tool included
#: (it imports the client and the git runner): the whole suite.
_SHARED = {
    "git_remote_seafile/client.py", "git_remote_seafile/git_util.py",
    "git_remote_seafile/url.py", "git_remote_seafile/__init__.py",
    "git_remote_seafile/__main__.py", "tests/e2e_harness.py", "tests/_helpers.py",
    "pyproject.toml", "tools/build_snapshot.py",
}
#: Checked by the suite on any code change: message rules, the suite's shape, the
#: generated tool, versions.
_ALWAYS = ["test_message_style", "test_message_coverage", "test_suite_shape",
           "test_snapshot_build", "test_version_consistency"]
#: The helper's protocol modules: their own tests plus the end-to-end runs.
_HELPER_SIDE = {"helper", "packs", "refs", "lock", "gc", "repair", "lfs", "safety", "cli",
                "diagnose", "login", "lfs_setup", "library_setup", "settings_report",
                "config", "seafile_paths", "units", "sqlite_read"}
_SNAPSHOT_TESTS = ["test_snapshot_tool", "test_snapshot_payload", "test_snapshot_runreport",
                   "test_snapshot_climb", "test_snapshot_history_guard", "test_snapshot_build",
                   "test_launcher"]
_TOOL_TESTS = {
    "tools/gates.py": ["test_gates"],
    "tools/run_tests_parallel.py": ["test_runner_discovery", "test_gates", "test_suite_shape"],
    "tools/release_check.py": ["test_release_check"],
    "tools/realserver_ci.py": ["test_realserver_ci"],
    "tools/seafile_doctor.py": ["test_seafile_doctor", "test_doctor_library"],
    "tools/verify_docs_guards.py": [],
}
_DOC_SUFFIXES = (".md", ".txt")


def suite_for_changes(paths: list[str], root: Path = _ROOT) -> list[str] | None:
    """The test modules the changed *paths* can reach; ``None`` means the whole suite.

    Conservative by construction: a path the map does not know is ``None``, and so
    is shared code.  ``[]`` means documents only -- no suite (the docs steps run).
    """
    modules: set[str] = set()
    code = False
    for raw in paths:
        path = raw.replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        if path in _SHARED:
            return None
        if path.endswith(_DOC_SUFFIXES) and not path.startswith(("tests/", "tools/", "git_remote_seafile/")):
            continue
        code = True
        if path.startswith("git_remote_seafile/") and path.endswith(".py"):
            stem = name[:-3]
            if stem == "snapshot_tool":
                modules.update(_SNAPSHOT_TESTS)
            elif stem in _HELPER_SIDE:
                modules.update({f"test_{stem}", "test_e2e", "test_e2e_pack_split"})
            else:
                return None
        elif path.startswith("tools/_snapshot/") or path == "tools/seafile_snapshot.py":
            modules.update(_SNAPSHOT_TESTS)
        elif path in _TOOL_TESTS:
            modules.update(_TOOL_TESTS[path])
        elif path.startswith("tests/test_") and path.endswith(".py"):
            modules.add(name[:-3])
        elif path.startswith(".github/"):
            modules.update({"test_release_workflow", "test_ci_report_failures",
                            "test_realserver_ci", "test_docs_consistency"})
        elif path == "MANIFEST.in":
            modules.add("test_docs_consistency")
        else:
            return None
    if not code:
        return []
    modules.update(_ALWAYS)
    return sorted(m for m in modules if (root / "tests" / f"{m}.py").exists())


def changed_paths(base: str, root: Path = _ROOT) -> list[str] | None:
    """Files changed against *base*, staged or not, plus untracked ones; ``None`` if
    git cannot say (then the caller runs everything)."""
    out: list[str] = []
    for args in (["diff", "--name-only", base, "--"],
                 ["ls-files", "--others", "--exclude-standard"]):
        try:
            proc = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                                  text=True)
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        out.extend(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return sorted(set(out))


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
    parser.add_argument(
        "--changed", nargs="?", const="HEAD", default=None, metavar="BASE",
        help="run the suite only for the test modules the changes since BASE "
             "(default: HEAD, i.e. the working changes) can reach; shared code runs it all",
    )
    args = parser.parse_args(argv)

    steps = list(STEPS)
    if args.filter:
        label, command = steps[-1]
        steps[-1] = (label, command + ["-k", args.filter])
    elif args.changed is not None:
        paths = changed_paths(args.changed)
        modules = None if paths is None else suite_for_changes(paths)
        label, command = steps[-1]
        if modules is None:
            print("gates: --changed: shared or unmapped code changed, so the whole suite runs",
                  flush=True)
        elif not modules:
            print("gates: --changed: documents only; the docs steps check them, no suite",
                  flush=True)
            steps = steps[:-1]
        else:
            print(f"gates: --changed: the suite runs {len(modules)} module(s): "
                  + ", ".join(modules), flush=True)
            for module in modules:
                command = command + ["-k", f"{module}."]
            steps[-1] = (label, command)

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
