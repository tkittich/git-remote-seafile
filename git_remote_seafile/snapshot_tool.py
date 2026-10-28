#!/usr/bin/env python3
# GENERATED FILE -- do not edit.  The source is tools/_snapshot/*.py;
# run `python tools/build_snapshot.py` after changing it.
"""Snapshot a working tree -- including gitignored files -- into a Seafile remote.

Why this exists
===============
``git push`` transfers *commits*.  Anything matched by ``.gitignore`` never
enters a commit, so no remote helper -- this project's included -- can carry it.

The obvious workaround is "rename ``.gitignore`` away, ``git add .``, push,
rename it back".  That is both unnecessary and unsafe:

* **Unnecessary.**  Renaming tracks nothing.  Ignored files merely become
  *visible* as untracked; you still have to ``git add`` them.  ``git add -f``
  stages ignored files directly, with no rename at all.
* **Unsafe.**  It mutates user-visible state -- the working tree *and* the
  repository's own index -- for the duration of the run.  A crash, a Ctrl-C, or
  a concurrent editor/IDE leaves the repository with no ``.gitignore`` and a
  fully-staged index.  There is no way to make that atomic, because the
  intermediate state is on disk and observable.

What this does instead
======================
A **vault**: a separate Git repository whose ``--work-tree`` points at your
source directory.  It records a full snapshot -- ignored files included -- as
ordinary commits and pushes them to Seafile::

    source dir  --(--work-tree)-->  vault repo  --git push-->  seafile://...
    (untouched)                     own .git, own index

Nothing in the source directory is modified: not ``.gitignore``, not ``.git``,
not the index.  Your code repository stays small, so ``git clone`` on a second
machine stays fast -- the snapshot never enters its history.

Atomicity
=========
* The snapshot is built in the vault's **own index**.  An interrupted run leaves
  at worst a stale index, which the next run overwrites.  The source is never at
  risk, because it is only ever read.
* The branch moves via ``git update-ref`` with an **expected old value**, so a
  concurrent snapshot loses loudly instead of silently clobbering.
* A vault-local lock file keeps two runs from interleaving.  It records the
  holding host and pid, so a lock left behind by a killed run is reclaimed
  automatically once that process is gone -- but only on the *same* host, since
  a pid from another machine means nothing here.
* The push is a single ref update, guarded by the helper's distributed lease.
* A **failed push rolls the local branch back** to what the remote actually has,
  so the vault never claims a snapshot the remote never took.  Without this, a
  rejected push left the branch advanced and every later run re-pushed the same
  doomed commit.

Updates
=======
Each run is a commit on top of the previous one, so the vault is a linear chain
of snapshots.  Only changed blobs are uploaded: Git is content-addressed, so a
snapshot that changed one file in a 486 KB tree pushed 31 KB, and a snapshot
that changed nothing pushes nothing at all (no commit is created).  The vault's
index keeps a stat cache, so unchanged files are not re-hashed either.

Several machines
================
When ``--remote`` is given the remote tip is fetched **before** the snapshot is
built, and the new commit is based on that tip, so several machines can append
to one shared vault.  A machine that is behind fast-forwards first; one that is
ahead (snapshots taken offline) simply pushes.  If the two have genuinely
diverged the run stops with an explanation and no changes -- pass
``--reset-to-remote`` to adopt the remote chain deliberately.  A push that loses
a race with another machine is re-parented on the new tip and retried once.

Because it is ordinary Git history, "roll back to last Tuesday" is ``git show``,
``git diff``, or a checkout of an older commit -- not a restore from a
proprietary archive format.

Restoring
=========
::

    python tools/seafile_snapshot.py --restore \
        --remote seafile://code/myproject-vault \
        --into C:/restore/myproject

    # any revision works -- e.g. roll back three snapshots
    ... --ref snapshot~3

The snapshot holds the *whole* tree, tracked files included, so this one command
reconstitutes the working directory.  Clone the code repository separately only
if you want its branches and history.

Every restore re-hashes the checked-out files against the snapshot's blobs and
fails loudly on any difference.  The check is byte-level on purpose: on a mangled
checkout ``git status`` reports a clean tree and a plain ``git hash-object``
matches, because both normalise the difference away.  Add ``--verify`` to run
``git fsck`` over the clone as well.

Do **not** simply ``git clone`` the vault and check out.  On Windows the clone
inherits ``core.autocrlf=true`` from Git for Windows' system config, and the
vault's ``info/attributes`` -- the thing that makes it byte-exact -- is not
carried by a clone, while the source's own ``.gitattributes`` is.  The result is
a restore with LF rewritten to CRLF.  ``--restore`` applies the byte-exact
settings to the clone *before* checking out, so the files match the originals.

Usage
=====
::

    # the whole thing, from inside the project -- no arguments at all
    cd C:/code/myproject
    python tools/seafile_snapshot.py

    # what that run would do: nothing is pushed, the branch does not move, and
    # the remembered settings are left alone -- but the vault is initialised if
    # missing and the snapshot is built into its index, so the diff is real
    python tools/seafile_snapshot.py --dry-run

    # the same, said explicitly
    python tools/seafile_snapshot.py --source C:/code/myproject \
        --vault C:/code/myproject-vault \
        --remote seafile://code/myproject-vault

    # also push the code repo itself first, in one command
    python tools/seafile_snapshot.py --code-remote origin

Defaults
========
Every option has one, so a bare run is a complete run.  They are resolved most
specific first:

1. **the flag you passed** -- always wins;
2. **what the vault remembered** from its last run (its own git config, under
   ``snapshot.*``);
3. **the source repository** -- a single ``seafile://`` remote names the vault's
   destination (``seafile://code/app`` -> ``seafile://code/app-vault``);
4. **the current directory** for the source, ``'<source>-vault'`` for the vault.

A snapshot is of a whole repository, as ``git push`` is.  Run from a subfolder
of a git repository, the source becomes the repository root (with a note); an
explicit ``--source`` that is a subfolder is refused, naming the root.

So the first run is the only one that needs an argument, and every explicit flag
is written back to the vault for next time.  ``--no-push`` overrides the
remembered or derived remote *for that run* -- it pushes nothing (the vault and
the code repo alike) and leaves the vault's memory alone, so a one-off local run
does not unhook a scheduled job; ``--dry-run`` previews the run (the vault is
initialised if missing and the snapshot is built into its index) without moving
the branch, pushing, or touching the remembered settings.

The recommended exclusions are **never applied silently**.  A snapshot is a
backup, and the one failure that cannot be forgiven is a backup that quietly
omitted the file that mattered -- ``node_modules`` is reproducible, ``.env`` is
not, and this tool cannot tell them apart.  It captures everything, then points
at the reproducible bulk it noticed::

    --exclude node_modules --exclude venv --exclude .venv \
    --exclude __pycache__ --exclude '*.pyc' --exclude dist --exclude build

``--default-excludes`` adds exactly that set.  A pattern with no slash matches
that name at *any* depth, so ``--exclude node_modules`` also drops
``packages/*/node_modules``.  Quote a wildcard so your own shell does not expand
it first.

Secrets
=======
This tool will happily back up ``.env``, keys and credentials -- that is what a
*full* backup means -- and Git history is permanent.  Every run therefore lists
the captured paths that look like credentials, so the choice of remote is made
with that in view.  Push the vault to a library only you can read; never point
the vault at a public remote.

A snapshot contains exactly the files ``.gitignore`` keeps *out* of the code
repository, so ``--remote`` is refused when it names a place the source
repository already pushes to -- ``--allow-shared-remote`` overrides that
deliberately.

Guarding against mistakes
==========================
The failures this tool can cause are quiet ones, so it refuses rather than
guesses wherever a guess would be silent:

* **A vault belongs to one source.**  It records the source directory it first
  saw, and refuses a run pointing a *different* tree at it -- mixing two trees
  into one linear chain of snapshots is not something a later reader could undo.
* **Neither the source nor the vault may live in a Seafile-synced folder** --
  the sync client rewrites files underneath it and competes with git for every
  path, which is the exact problem this tool exists to avoid.  Checked at
  startup, before anything is written.  Two states make a containing library
  harmless and are let through with an explanatory note: its auto sync is
  disabled in the client, or the path is covered by that library's
  seafile-ignore.txt.  ``--allow-synced`` overrides every remaining refusal.
* **``--code-remote`` must name a real remote** of the source, so a typo fails
  before the snapshot instead of after it.
* **Flags that would be ignored are errors, not no-ops.**  ``--ref`` without
  ``--restore`` is the sharp one: a run meant to restore an *old* snapshot would
  otherwise quietly take a *new* one.
* **``--no-push`` and ``--remote`` together** are contradictory and refused.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, NamedTuple, TypedDict

DEFAULT_BRANCH = "snapshot"

#: How old an unparseable lock file must be before it is reclaimed: a live run
#: writes its identity immediately after creating the file, so a fresh one may
#: simply be mid-write.
_UNPARSEABLE_LOCK_GRACE_SECONDS = 30.0

RECOMMENDED_EXCLUDES = (
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "*.pyc",
    "dist",
    "build",
)

# Settings a run remembers in the vault, so a bare re-run repeats the last one.
# ``source`` is recorded too, and a mismatch is refused (``remembered_settings``)
# -- the vault names, and belongs to, exactly one source directory.
_REMEMBERED_KEYS = ("source", "remote", "coderemote", "excludes", "branch")


class SnapshotError(RuntimeError):
    """A failure the operator can act on."""


#: How long one git subprocess may run before it is killed.  A snapshot holds
#: its vault lock for the whole run, so a git child that never returns blocks
#: every *other* local snapshot as well -- a credential-helper prompt on a
#: scheduled machine, or a server that accepts the connection and then stalls.
#: Long enough for a first push of a large tree, bounded so a hang is an error
#: rather than a wait.  Not a config key, for the same reason the library's own
#: ``GIT_TIMEOUT`` is not one.
GIT_TIMEOUT = 600

#: Long-running steps (staging a large tree, the first push of a large vault,
#: cloning it for a restore) would otherwise be silent for tens of minutes and
#: read as a hang: git prints progress only to a terminal, and this tool runs
#: under schedulers.  While a child marked with ``status=`` is still running,
#: one line per this many seconds goes to stderr instead -- enough to show the
#: step is alive and how far it has got without chattering on short runs.
STATUS_INTERVAL = 60

#: A status-marked step is killed when it shows no measurable progress for this
#: long: bytes newly present under its ``measure_dir``.  That replaces a flat
#: wall-clock timeout, which cannot tell "hashing a 130 GB tree" (fine) from "dead
#: disk" (not): the first keeps making objects appear for an hour, the second
#: makes none.  The limit is a *sliding* one -- every observed growth restarts it.
#:
#: Steps with no measurable progress -- above all the push, whose upload phase
#: is silent while it runs -- are passed ``stall_limit=None`` and are never
#: killed: they get the heartbeat only, and a dead link fails on its own through
#: the helper's per-request HTTP timeout.
#:
#: Overridable with ``SEAFILE_SNAPSHOT_STALL_LIMIT`` (seconds) for a machine whose
#: disks are slow enough that half an hour without a new object is normal.
STALL_LIMIT = 1800


def _stall_limit_from_env() -> float:
    raw = os.environ.get("SEAFILE_SNAPSHOT_STALL_LIMIT", "").strip()
    if not raw:
        return STALL_LIMIT
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        print(
            f"Warning: SEAFILE_SNAPSHOT_STALL_LIMIT={raw!r} is not a positive number "
            f"of seconds; using {STALL_LIMIT}.",
            file=sys.stderr,
        )
        return STALL_LIMIT
    return value


class _UnsetStall:
    """Sentinel meaning "use the stall limit" (distinct from ``None``, which is "never kill")."""

    __slots__ = ()


_UNSET_STALL = _UnsetStall()


def _dir_size(path: Path) -> int:
    """Total bytes of regular files under *path*; unreadable entries count as zero."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _fmt_elapsed(seconds: float) -> str:
    if seconds >= 3600:
        hours, rem = divmod(int(seconds), 3600)
        return f"{hours}h{rem // 60:02d}m"
    if seconds >= 60:
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"


def _kill_tree(proc: "subprocess.Popen[Any]") -> None:
    """Kill *proc* and, where the platform lets us, what it started.

    ``git push`` spawns the remote helper, which holds the pipes open; killing
    only the parent would leave ``communicate`` waiting on a child nobody can
    see.  Windows has ``taskkill /T``; elsewhere the parent is killed and the
    pipe drain below is bounded.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run(
    cmd: list[str],
    *,
    status: str | None = None,
    measure_dir: Path | None = None,
    stall_limit: "float | None | _UnsetStall" = _UNSET_STALL,
    expect_bytes: int = 0,
    live_stderr: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` for git, failing as a ``SnapshotError`` when it hangs.

    Without ``status`` this is a plain run bounded by ``GIT_TIMEOUT``.

    With ``status`` the child runs under a *no-progress* watchdog instead of a
    wall-clock one, driven from this thread by retrying
    ``communicate(timeout=STATUS_INTERVAL)`` -- which keeps stdin, stdout and
    stderr intact across the timeouts, so no helper threads are needed.  Each
    time the interval passes without the child exiting:

    * one line goes to stderr ("still staging the tree ... 4m12s (9.0 GiB written
      to objects; up to about 28.6 GiB expected, at most 9m30s left)"), because
      nothing else speaks during such a step.  The expectation comes from
      ``expect_bytes`` (an upper bound: what staging is estimated to add) and is
      left out when there is none or the step has already passed it;
    * bytes newly present under ``measure_dir`` count as progress and restart the
      stall clock;
    * if nothing has grown for ``STALL_LIMIT`` seconds the child (and, on
      Windows, its process tree) is killed and a ``SnapshotError`` raised.

    ``stall_limit=None`` is heartbeat-only mode, for steps with no measurable
    progress (a push's silent upload, a verification that re-reads every file
    and writes nothing): the lines keep coming and nothing is ever killed.

    With ``live_stderr`` the child's stderr is piped to one reader thread that
    echoes every line through *Python's* ``sys.stderr`` the moment it arrives.
    That keeps the live stream (git's progress, the remote helper's own
    warnings) *and* puts it through whatever ``sys.stderr`` currently is --
    under ``--json --exit-warnings`` that is the run-report tap, so the
    helper's ``Warning:`` lines count for exit 3 and reach the document's
    ``warnings`` instead of bypassing both on an inherited file descriptor
    (REVIEW.sonnet-5.5 D9, reproduced).  The reader is joined before the call
    returns; it is not a daemon and never outlives the child.
    """
    if live_stderr:
        return _run_live(
            cmd, status=status, measure_dir=measure_dir, stall_limit=stall_limit,
            expect_bytes=expect_bytes, **kwargs,
        )
    if status is None:
        kwargs.setdefault("timeout", GIT_TIMEOUT)
        try:
            return subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired:
            raise SnapshotError(
                f"git {' '.join(cmd[1:])} did not finish within {GIT_TIMEOUT:g}s and was killed"
            ) from None

    # isinstance, not ``is``: mypy narrows the union on the former, and the
    # sentinel's type is as good a discriminator as its identity.
    limit = _stall_limit_from_env() if isinstance(stall_limit, _UnsetStall) else stall_limit

    stdin_data = kwargs.pop("input", None)
    if stdin_data is not None and kwargs.get("stdin") != subprocess.PIPE:
        # communicate() can only feed a pipe we opened ourselves.
        kwargs["stdin"] = subprocess.PIPE
    kwargs.pop("timeout", None)
    proc = subprocess.Popen(cmd, **kwargs)

    baseline = _dir_size(measure_dir) if measure_dir is not None else 0
    start = last_progress = time.monotonic()
    last_seen = 0
    first = True
    while True:
        try:
            out, err = proc.communicate(
                input=stdin_data if first else None, timeout=STATUS_INTERVAL
            )
            break
        except subprocess.TimeoutExpired:
            first = False
        now = time.monotonic()
        growth = max(0, _dir_size(measure_dir) - baseline) if measure_dir is not None else 0
        if growth > last_seen:
            last_seen, last_progress = growth, now
        line = f"still {status} ... {_fmt_elapsed(now - start)}"
        if measure_dir is not None and growth > 0:
            detail = f"{human_bytes(growth)} written to {measure_dir.name}"
            if expect_bytes > growth:
                rate = growth / max(now - start, 1e-9)
                detail += (f"; up to about {human_bytes(expect_bytes)} expected, at most "
                           f"{_fmt_elapsed((expect_bytes - growth) / rate)} left")
            line += f" ({detail})"
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        if limit is not None and now - last_progress >= limit:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            raise SnapshotError(
                f"git {' '.join(cmd[1:])} made no measurable progress for "
                f"{limit:g}s and was killed (stalled?)"
            ) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _run_live(
    cmd: list[str],
    *,
    status: str | None,
    measure_dir: Path | None,
    stall_limit: "float | None | _UnsetStall",
    expect_bytes: int,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """A run whose stderr is echoed line-by-line through ``sys.stderr``.

    Same watchdog contract as the status loop above (wall-clock ``GIT_TIMEOUT``
    when there is nothing to measure, no-progress stall kill when there is),
    but the wait is a poll loop because the stderr pipe belongs to the reader
    thread -- ``communicate`` would race it for the same pipe.
    """
    limit = _stall_limit_from_env() if isinstance(stall_limit, _UnsetStall) else stall_limit
    wall = None if limit is not None or status is not None else GIT_TIMEOUT

    stdin_data = kwargs.pop("input", None)
    if stdin_data is not None and kwargs.get("stdin") != subprocess.PIPE:
        kwargs["stdin"] = subprocess.PIPE
    kwargs.pop("timeout", None)
    kwargs["stderr"] = subprocess.PIPE
    kwargs.setdefault("stdout", subprocess.PIPE)
    proc = subprocess.Popen(cmd, **kwargs)
    collected: list[str] = []

    def _pump() -> None:
        try:
            for raw in proc.stderr:      # type: ignore[union-attr]
                collected.append(raw)
                sys.stderr.write(raw)    # the tap under --json; the term otherwise
                sys.stderr.flush()
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_pump, name="grs-live-stderr")
    reader.start()
    try:
        baseline = _dir_size(measure_dir) if measure_dir is not None else 0
        start = last_progress = time.monotonic()
        last_seen = 0
        next_line = start
        deadline = start + wall if wall is not None else None
        killed = False
        while proc.poll() is None:
            time.sleep(0.5)
            now = time.monotonic()
            growth = max(0, _dir_size(measure_dir) - baseline) if measure_dir is not None else 0
            if growth > last_seen:
                last_seen, last_progress = growth, now
            if status is not None and now >= next_line:
                next_line = now + STATUS_INTERVAL
                line = f"still {status} ... {_fmt_elapsed(now - start)}"
                if measure_dir is not None and growth > 0:
                    detail = f"{human_bytes(growth)} written to {measure_dir.name}"
                    if expect_bytes > growth:
                        rate = growth / max(now - start, 1e-9)
                        detail += (f"; up to about {human_bytes(expect_bytes)} expected, at most "
                                   f"{_fmt_elapsed((expect_bytes - growth) / rate)} left")
                    line += f" ({detail})"
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
            if limit is not None and now - last_progress >= limit:
                _kill_tree(proc)
                killed = True
                break
            if deadline is not None and now >= deadline:
                _kill_tree(proc)
                killed = True
                break
        out = proc.stdout.read() if proc.stdout is not None else None
        if stdin_data is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        proc.wait()
        reader.join(timeout=10)
        if killed:
            reason = (
                f"made no measurable progress for {limit:g}s and was killed (stalled?)"
                if limit is not None and (deadline is None or now - last_progress >= limit)
                else f"did not finish within {GIT_TIMEOUT:g}s and was killed"
            )
            raise SnapshotError(f"git {' '.join(cmd[1:])} {reason}") from None
    finally:
        if reader.is_alive():
            reader.join(timeout=5)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, "".join(collected))



#: Variables git uses to pick the repository it operates on; a run launched from
#: a hook or CI step inherits them, and they override --git-dir.  A copy of
#: ``git_remote_seafile.git_util._GIT_REPO_ENV_VARS`` (this file must run
#: without the package installed); tests/test_git_util.py fails if they differ.
_REPO_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_CEILING_DIRECTORIES",
)


def git(
    git_dir: Path,
    work_tree: Path | None,
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    stdin: str | None = None,
    status: str | None = None,
    measure_dir: Path | None = None,
    live_stderr: bool = False,
    stall_limit: "float | None | _UnsetStall" = _UNSET_STALL,
    expect_bytes: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run git against an explicit --git-dir/--work-tree pair.

    Passing both explicitly is what keeps this out of the source repository's
    own ``.git``: git never consults it, and never writes to it -- but
    ``--git-dir`` alone does not guarantee that.  A run launched from a git
    hook, alias, or CI step inherits ``GIT_INDEX_FILE``/``GIT_DIR`` in the
    environment, and ``GIT_INDEX_FILE`` has *no* command-line override: index
    operations would go to the caller's index, writing the whole tree
    (ignored files included) into whatever repository launched us.  The
    library's own ``clean_git_env`` exists for exactly this and gc.py uses
    it; the snapshot tool does the same scrubbing here.

    ``GIT_TERMINAL_PROMPT=0`` plus a detached stdin turn a credential prompt
    under a scheduler into a fast, clean failure instead of a hang holding
    the vault lock.

    ``status``/``measure_dir``/``stall_limit`` are passed to :func:`_run` for
    steps that run long enough to look dead (staging, pushing, restoring).  With
    ``live_stderr`` the child's stderr is echoed line-by-line through Python's
    ``sys.stderr`` as it arrives, so a push shows git's and the remote helper's
    own output when it happens *and* the run-report tap sees it -- an inherited
    file descriptor did the first and silently skipped the second.
    """
    cmd = ["git", f"--git-dir={git_dir}"]
    if work_tree is not None:
        cmd.append(f"--work-tree={work_tree}")
    cmd.extend(args)
    env = dict(os.environ)
    for var in _REPO_ENV_VARS:
        env.pop(var, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Every path this tool hands git is meant literally.  Without this, the
    # entries fed through `--pathspec-from-file` are *pathspecs*: a file named
    # `a[b].txt` also matches an unrelated `ab.txt` and both get staged, and a
    # name that reads as magic (`*.log`, `:x`) can make `git add` fail outright
    # rather than stage the file.  The exclude pre-filter has already decided
    # exactly which paths may be staged, so glob semantics can only add
    # mistakes -- and a mistake here means an excluded file's bytes reaching the
    # vault, which is the one thing the pre-filter exists to prevent.
    env["GIT_LITERAL_PATHSPECS"] = "1"
    # Two guards read git's English stderr (an unreadable directory, an index
    # lock).  git translates its messages, so under another locale those
    # matches would silently stop firing -- the very hole they close.  LC_ALL
    # outranks LC_MESSAGES, so it is overridden only when the user set it.
    env["LANGUAGE"] = "C"
    env["LC_MESSAGES"] = "C"
    if "LC_ALL" in env:
        env["LC_ALL"] = "C"
    # A child that runs the remote helper (push, fetch, clone) inherits the
    # helper's own per-command ceiling, which defaults to 300 s and is meant for
    # an interactive `git push`.  This tool's no-progress watchdog is the guard
    # for long steps, so the helper's wall-clock ceiling is lifted to the same
    # horizon -- unless the user set one, which always wins.
    env.setdefault("SEAFILE_GIT_TIMEOUT", str(int(STALL_LIMIT)))
    proc = _run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        # Not "replace": a file name that is not UTF-8 (legal on POSIX) came
        # back from `ls-files` as U+FFFD, and the exclude pass and the
        # pathspec file then named a path that does not exist -- `git add`
        # stopped with "did not match any files" (reproduced in Debian with
        # `bad_\xff.txt` and `--exclude`).  surrogateescape round-trips the
        # raw bytes through str and back to git's stdin unchanged.
        errors="surrogateescape",
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        status=status,
        measure_dir=measure_dir,
        stall_limit=stall_limit,
        expect_bytes=expect_bytes,
        live_stderr=live_stderr,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        hint = ""
        if "index.lock" in detail and "exist" in detail:
            hint = (
                "\nA git process left its lock behind (the earlier run was probably killed). "
                "If no git process is running on this vault, delete the lock file named "
                "above and re-run; the vault itself is not damaged."
            )
        if "unstable object source data" in detail:
            hint = (
                "\nA file was rewritten while git was reading it (git does not say which). "
                "Nothing was snapshotted; run again. If it keeps happening, something is "
                "writing to the tree all the time (a database, a log): --exclude it."
            )
        raise SnapshotError(
            f"git {' '.join(args)} failed (rc={proc.returncode})\n{detail}{hint}"
        )
    return proc


def resolve_paths(source: Path, vault: Path) -> tuple[Path, Path]:
    source = source.expanduser().resolve()
    vault = vault.expanduser().resolve()

    if not source.is_dir():
        raise SnapshotError(f"source directory does not exist: {source}")
    if vault == source:
        raise SnapshotError("vault and source must be different directories")
    if source in vault.parents:
        raise SnapshotError(
            f"vault must live outside the source directory, or the snapshot "
            f"would swallow itself:\n  vault:  {vault}\n  source: {source}"
        )
    if vault in source.parents:
        raise SnapshotError(
            f"source must not live inside the vault:\n"
            f"  source: {source}\n  vault:  {vault}"
        )
    return source, vault


def ensure_vault(vault: Path, branch: str) -> Path:
    """Create the vault repository if it does not exist yet."""
    git_dir = vault / ".git"
    if git_dir.is_dir():
        return git_dir

    vault.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ["git", "init", "-q", str(vault)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"git init failed in {vault} (rc={proc.returncode})\n"
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    # Name the branch with `symbolic-ref`, not `git init -b`.  `-b` is Git 2.28
    # and this tool is documented to run on Git 2.20 (USER_GUIDE section 5); the
    # initial branch is only an unborn-HEAD symref, so pointing it is the same
    # operation on every version, and it keeps the documented floor honest
    # instead of raising it to match an implementation detail.  For the same
    # reason `init.defaultBranch` (also 2.28) cannot be the mechanism here.
    proc = _run(
        ["git", "-C", str(vault), "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"could not name the vault's initial branch {branch!r} "
            f"(rc={proc.returncode})\n{(proc.stderr or proc.stdout).strip()}"
        )
    return git_dir


def _config(path: Path, key: str) -> str:
    """Read a git config value, with the usual global/system fallbacks."""
    proc = _run(
        ["git", "-C", str(path), "config", "--get", key],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return proc.stdout.strip()


def ensure_identity(
    vault: Path,
    source: Path,
    vault_config: dict[str, list[str]] | None = None,
    write_identity: bool = True,
) -> None:
    """Give the vault a committer identity, inheriting the source repo's.

    A vault created by ``git init`` has none of its own, so it falls back to the
    global identity.  When that is missing too, ``commit-tree`` dies with a bare
    "Author identity unknown" -- so resolve it up front and prefer the source
    repository's own identity, which is whose code this is.

    Only the repositories' *own* config is consulted when deciding whether to
    inherit.  ``_config`` cannot be used for that decision: it reads through to
    the global file, so it reports an identity for a vault that has none, and
    the source repository is then never consulted.  That was a real bug -- the
    inheritance worked only in the test, which cleared the global config.

    Both configs are read in one call each (see :func:`_read_local_config_map`),
    and a value that is already correct is not rewritten -- the common case
    after the first run is that there is nothing to do.  Pass ``vault_config``
    (a map already read for this vault) to avoid a second read of it.
    """
    if vault_config is None:
        vault_config = _read_local_config_map(vault)
    source_config = _read_local_config_map(source)
    for key in ("user.name", "user.email"):
        have = vault_config.get(key) or []
        if have and have[0]:
            continue
        inherited = (source_config.get(key) or [""])[0]
        if not inherited:
            # No local identity anywhere.  A global one is fine -- git resolves
            # it at commit time -- but if there is none either, fail here with
            # an actionable message rather than inside commit-tree.
            if _config(vault, key):
                continue
            raise SnapshotError(
                f"no git identity is configured for the vault.\n"
                f"Set one and retry:\n"
                f"  git -C \"{vault}\" config user.name  \"Your Name\"\n"
                f"  git -C \"{vault}\" config user.email \"you@example.com\""
            )
        if not write_identity:
            # A --dry-run validates the identity is resolvable without
            # writing any: a preview must not mutate the vault, but
            # surfacing a missing identity early is part of the preview.
            continue
        proc = _run(
            ["git", "-C", str(vault), "config", key, inherited],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise SnapshotError(
                f"could not set {key} on the vault (rc={proc.returncode})\n"
                f"{(proc.stderr or proc.stdout).strip()}"
            )


BYTE_EXACT_MARKER = "* -text -filter -ident -working-tree-encoding"

BYTE_EXACT_ATTRIBUTES = (
    "# Written by seafile_snapshot.py.  The vault exists to hold byte-exact\n"
    "# copies of the source tree, so every path is marked non-text and\n"
    "# filter-free.  info/attributes has the highest precedence in Git's\n"
    "# attribute chain, so this outranks any .gitattributes in the source tree.\n"
    "#\n"
    "#   -text    end-of-line conversion, on add and on checkout.\n"
    "#   -filter  clean/smudge filters -- Git LFS above all.  Without this, a\n"
    "#            source 'filter=lfs' attribute makes `git add` store a ~130\n"
    "#            byte pointer instead of the file, and the real bytes are\n"
    "#            uploaded nowhere, because the vault has no LFS remote.\n"
    "#            Silent data loss in a tool whose job is a full backup.\n"
    "#   -ident   $Id$ keyword expansion.\n"
    "#   -working-tree-encoding\n"
    "#            a source '*.txt working-tree-encoding=UTF-16LE' makes `git add`\n"
    "#            store the file re-encoded as UTF-8, and a restore then differs\n"
    "#            from the original.\n"
    "* -text -filter -ident -working-tree-encoding\n"
)


def ensure_byte_exact(
    vault: Path,
    git_dir: Path,
    vault_config: dict[str, list[str]] | None = None,
) -> None:
    """Stop git from rewriting line endings inside the vault.

    Git for Windows ships ``core.autocrlf=true`` in its *system* config, so a
    freshly initialised vault converts LF to CRLF on checkout even though
    ``git config --global core.autocrlf`` reports nothing.  The stored blob is
    correct; the *restored* file is not -- and a backup whose restore differs
    from the original is not a backup.

    Both levers are pulled: the config, and an ``info/attributes`` override that
    outranks any ``.gitattributes`` in the source tree.

    The config values are only written when they are not already correct,
    so a vault that has been snapshotted before costs no *writes* here (one
    config read -- see :func:`_read_local_config_map`).  Reading the *local*
    config is what makes that safe: a global ``core.autocrlf`` is irrelevant,
    because the vault has to pin its own.
    """
    if vault_config is None:
        vault_config = _read_local_config_map(vault)
    # core.longpaths is not a line-ending lever: Git for Windows refuses a path
    # over 260 characters unless it is set, and a deep source tree (or a restore
    # into a deep destination) would otherwise fail for a reason unrelated to
    # the data.  Harmless where the limit does not exist.
    # core.quotepath=false ensures international / UTF-8 filenames (e.g. Thai,
    # CJK, accented Latin) are displayed cleanly in git status and diff output
    # rather than C-quoted with octal escapes (\340\270...).
    # core.fsmonitor/core.untrackedCache: the vault inherits the *global*
    # config, and a global fsmonitor=true left a detached fsmonitor--daemon
    # watching the source tree after the tool exited (measured on Windows) --
    # the source is meant to be only ever read.  Both watchers are pinned off
    # for the vault's own commands; a user who wants them for their own git
    # still has them everywhere else.
    for key, wanted in (("core.autocrlf", "false"), ("core.safecrlf", "false"),
                        ("core.longpaths", "true"), ("core.quotepath", "false"),
                        ("core.fsmonitor", "false"), ("core.untrackedCache", "false")):
        have = (vault_config.get(key) or [""])[0]
        if have == wanted:
            continue
        git(git_dir, vault, ["config", key, wanted], cwd=vault)

    attributes = git_dir / "info" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    existing = attributes.read_text(encoding="utf-8") if attributes.exists() else ""
    # Checked on the marker line, not on "-text": a vault created by an earlier
    # version carries only "* -text", and appending is safe because a later line
    # overrides an earlier one within the same attributes file.
    if BYTE_EXACT_MARKER not in existing:
        attributes.write_text(existing + BYTE_EXACT_ATTRIBUTES, encoding="utf-8")


def disable_object_compression(vault: Path, git_dir: Path) -> None:
    """Set ``core.compression=0`` so new objects skip real deflation.

    Level 0 keeps zlib's wrapper but stores the content in uncompressed blocks:
    nothing shrinks, yet git reads it back byte-identical.  Measured on a tree
    dominated by already-compressed archives, that traded zero bytes for 3x the
    staging speed (41 -> 126 MiB/s).  Existing objects keep the form they were
    written in -- a blob SHA covers content, not storage encoding -- so flipping
    this either way is safe at any point.
    """
    if _read_local_config_map(vault).get("core.compression") == ["0"]:
        return
    git(git_dir, vault, ["config", "core.compression", "0"], cwd=vault)


def _object_store_stats(git_dir: Path, work_tree: Path) -> dict[str, int]:
    """The numbers from ``git count-objects -v``, as ints."""
    proc = git(git_dir, None, ["count-objects", "-v"], cwd=work_tree)
    stats: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        try:
            stats[key.strip()] = int(value.strip())
        except ValueError:
            pass  # newer git versions add non-numeric lines; the ones we print are ints
    return stats


# The automatic incremental repack gates itself on the size of the loose tier:
# below it, a pass over the object directory is not worth it; above it, packing
# is cheap next to the push that follows.  Only *bytes* are gated.  An object
# *count* trigger already exists in git itself -- `git fetch`, which this tool
# runs every time a remote is configured, launches `gc --auto` once roughly
# 6,700 loose objects exist -- whereas nothing in git looks at size, and a vault
# of a few dozen huge files never trips a count.
_REPACK_MIN_LOOSE_BYTES = 256 * 1024 * 1024


def auto_incremental_repack(git_dir: Path, work_tree: Path) -> None:
    """Pack the loose tier after a snapshot commit; never fail the snapshot.

    ``repack -d`` moves reachable loose objects into packs (existing packs are
    untouched and serve as delta bases); unreachable ones stay loose so the
    following ``prune`` can delete them -- dead space is never sealed into a
    pack.  Git swaps in packfiles atomically, so even an interrupted run leaves
    the repository intact; that is what entitles this step to be best-effort.
    """
    stats = _object_store_stats(git_dir, work_tree)
    loose_bytes = stats.get("size", 0) * 1024
    count = stats.get("count", 0)
    if loose_bytes < _REPACK_MIN_LOOSE_BYTES:
        return
    print(f"consolidating {count} loose object(s), {human_bytes(loose_bytes)}, "
          f"before the push ...")
    try:
        git(git_dir, None, ["repack", "-d"], cwd=work_tree,
            status="packing the vault's loose objects", measure_dir=git_dir / "objects")
        git(git_dir, None, ["prune"], cwd=work_tree)
    except SnapshotError as exc:
        print(f"Note: incremental repack failed ({str(exc).splitlines()[0]}); "
              f"the snapshot itself succeeded", file=sys.stderr)


# -- Large-payload profile --------------------------------------------------------
#
# Two git costs scale with *raw* bytes and buy nothing on data that does not
# compress or deltify, and both showed up on a 20 GB+ tree that timed out:
#
#   * staging (`git add`) deflates every blob: 42 MB/s on incompressible data,
#     131 MB/s at core.compression=0;
#   * `pack-objects` -- the helper's push, and the vault's own background
#     `gc --auto` -- runs a delta search over every blob below
#     core.bigFileThreshold (default 512 MiB): 8-10 MB/s regardless of the
#     compression level, against 320 MB/s when the big blobs are streamed.
#
# The profile is decided once, from a small sample taken before the first
# staging, and then remembered in the vault's config (`snapshot.profile`).
# Nothing here changes content: object *storage encoding* and *packing
# strategy* only, both of which are safe to flip at any time.

#: Below this much raw data neither cost is worth a decision.
_PROFILE_MIN_TREE_BYTES = 2 * 1024 ** 3
#: A file this big is "payload"; smaller ones (source, config) stay as they are.
_PROFILE_BIG_FILE_BYTES = 1024 ** 2
#: How many of the largest files are sampled, and how much of each.
_PROFILE_SAMPLE_FILES = 32
_PROFILE_SAMPLE_CHUNK = 256 * 1024
#: Sampled data that keeps at least this fraction of its size under a fast
#: deflate is treated as not worth compressing.  This is a break-even, not a
#: purity test: deflate costs ~1/45 s per MiB on one core against ~1/186 raw
#: (an extra ~0.017 s/MiB), and the bytes it saves are worth (1 - ratio)/32
#: s/MiB at the ~32 MiB/s upload measured on the real server.  The two meet
#: near a ratio of 0.45, so anything above 0.5 is faster raw end to end --
#: measured on the flood tree, whose sample kept 85 %: deflating staged it at
#: ~45 MiB/s (about 50 minutes for 131 GiB) where raw takes about 11.  The old
#: 0.9 cut-off sent exactly such mixed data down the slow path.
_PROFILE_INCOMPRESSIBLE_RATIO = 0.5
#: What to set for a raw payload.  The threshold is the one measured in
#: git_remote_seafile.git_util.PACK_BIG_FILE_THRESHOLD (kept as a literal so
#: this file does not need the package just to name a string).
_PROFILE_BIG_FILE_THRESHOLD = "50m"


def apply_large_payload_profile(vault: Path, git_dir: Path) -> None:
    """Store new objects uncompressed and stream big blobs past delta search.

    Idempotent.  Both settings are repo-local, so they travel with the vault and
    are read by every git process that touches it -- the helper's push-time
    ``pack-objects`` and the vault's own background ``gc --auto`` included, which
    is why the profile is applied here and not only on the helper's command
    line.
    """
    disable_object_compression(vault, git_dir)
    if _read_local_config_map(vault).get("core.bigfilethreshold") != [_PROFILE_BIG_FILE_THRESHOLD]:
        git(git_dir, vault, ["config", "core.bigFileThreshold", _PROFILE_BIG_FILE_THRESHOLD], cwd=vault)
    git(git_dir, vault, ["config", "snapshot.profile", "raw"], cwd=vault)


def _walk_sizes(
    source: Path, excludes: list[str], skip: "frozenset[str] | None" = None
) -> list[tuple[int, str]]:
    """``(size, relative posix path)`` for every regular file under *source*.

    Stat only -- no content is read.  ``.git`` directories are skipped (a source
    that is itself a repository), and excluded paths are skipped, so the
    decision describes what will actually be staged.
    """
    found: list[tuple[int, str]] = []
    root = str(source)

    def _keep(d: str) -> bool:
        if d == ".git":
            return False
        try:
            return not (getattr(os.lstat(os.path.join(dirpath, d)), "st_file_attributes", 0) & 0x400)
        except OSError:
            # Vanished between the listing and the stat: nothing to walk.
            return False

    for dirpath, dirnames, filenames in os.walk(root):
        # A junction or mount point is a directory to os.walk but not a symlink,
        # and git does not follow it: neither may a walk that decides what is
        # uploaded, or one junction to a drive root would hash the drive.
        dirnames[:] = [d for d in dirnames if _keep(d)]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if excludes and any(exclude_matches(rel, pat) for pat in excludes):
                continue
            if skip and rel in skip:
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                found.append((st.st_size, rel))
    return found


def sample_payload(
    source: Path, excludes: list[str], skip: "frozenset[str] | None" = None
) -> dict[str, float]:
    """Measure what staging this tree will cost, from a small sample.

    Returns ``total`` (bytes of all files), ``payload`` (bytes in files of at
    least ``_PROFILE_BIG_FILE_BYTES``), ``sampled`` (bytes actually read) and
    ``ratio`` (compressed / raw over the sample; 0.0 when nothing was sampled).
    At most ``_PROFILE_SAMPLE_FILES`` of the largest files are read, three
    chunks of ``_PROFILE_SAMPLE_CHUNK`` each (start, middle, end), so the cost
    is bounded by ~24 MiB of reads however large the tree is.
    """
    files = _walk_sizes(source, excludes, skip)
    total = sum(size for size, _ in files)
    big = sorted(((s_, r) for s_, r in files if s_ >= _PROFILE_BIG_FILE_BYTES), reverse=True)
    payload = sum(size for size, _ in big)
    raw = comp = 0
    for size, rel in big[:_PROFILE_SAMPLE_FILES]:
        try:
            with open(source / rel, "rb") as fh:
                for offset in sorted({0, max(0, size // 2 - _PROFILE_SAMPLE_CHUNK // 2),
                                       max(0, size - _PROFILE_SAMPLE_CHUNK)}):
                    fh.seek(offset)
                    chunk = fh.read(_PROFILE_SAMPLE_CHUNK)
                    if chunk:
                        raw += len(chunk)
                        comp += len(zlib.compress(chunk, 1))
        except OSError:
            continue
    return {
        "total": float(total),
        "payload": float(payload),
        "sampled": float(raw),
        "ratio": (comp / raw) if raw else 0.0,
    }


def decide_profile(measure: dict[str, float]) -> str:
    """``"raw"`` or ``"default"`` from :func:`sample_payload`'s numbers.

    A raw-data profile needs all three: a tree big enough for the costs to
    matter, a payload that dominates it (so small source files are not being
    deprived of delta compression for the sake of a few large ones), and a
    sample that really does not compress.
    """
    if measure["total"] < _PROFILE_MIN_TREE_BYTES:
        return "default"
    if measure["payload"] < 0.5 * measure["total"]:
        return "default"
    if measure["sampled"] <= 0 or measure["ratio"] < _PROFILE_INCOMPRESSIBLE_RATIO:
        return "default"
    return "raw"


def _use_profile_for_this_process_only() -> None:
    """Give this process's git children the raw-payload settings, writing nothing.

    A preview stages the whole tree -- that is how it can report the diffstat,
    the secrets and the gitlinks -- and until this was added it did so with
    git's defaults while the real run would use the raw profile.  Measured on
    the flood tree: deflating incompressible zips ran at ~46 MiB/s on one core
    (the machine looked idle: 1 of 24 threads busy) against ~186 MiB/s with
    ``core.compression=0``, so the preview took four times as long as the run
    it was previewing.  The settings go in through git's own environment
    channel, so no config file is touched and the vault stays exactly as a dry
    run promises.
    """
    base = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    settings = (("core.compression", "0"), ("core.bigFileThreshold", _PROFILE_BIG_FILE_THRESHOLD))
    for offset, (key, value) in enumerate(settings):
        os.environ[f"GIT_CONFIG_KEY_{base + offset}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{base + offset}"] = value
    os.environ["GIT_CONFIG_COUNT"] = str(base + len(settings))


def choose_profile(
    vault: Path, git_dir: Path, source: Path, excludes: list[str],
    vault_config: dict[str, list[str]], *, write: bool, recheck: bool = False,
    skip: "frozenset[str] | None" = None,
) -> None:
    """Decide the vault's storage profile on the first run that needs it.

    Skipped when the profile is already ``raw``, or when the user pinned
    ``core.compression`` themselves -- a vault someone tuned by hand is not
    second-guessed.  A ``default`` verdict stands until the tree has doubled
    since it was made (:func:`profile_recheck_due`), or *recheck* is set: a vault
    that began small and became a 20 GB payload would otherwise stay on the slow
    settings forever.  That test runs here, *before* staging, from the same stat
    walk the sample uses.  It once ran after staging, so the run that grew the
    tree still deflated all of it (8 minutes of a 30 GB run, then the verdict
    arrived for the next one).  Under ``--dry-run`` (``write=False``) the sample
    is still taken and reported, but nothing is written.
    """
    profile = (vault_config.get("snapshot.profile") or [""])[0]
    if profile == "raw" or vault_config.get("core.compression"):
        return
    if profile and not recheck:
        total = sum(size for size, _ in _walk_sizes(source, excludes, skip))
        if not profile_recheck_due(vault_config, total):
            return
    measure = sample_payload(source, excludes, skip)
    verdict = decide_profile(measure)
    if verdict == "raw":
        what = (
            f"large raw payload detected ({human_bytes(measure['payload'])} in big files of "
            f"{human_bytes(measure['total'])}; the sample keeps {measure['ratio']:.0%} of its "
            f"size under deflate)"
        )
        if write:
            apply_large_payload_profile(vault, git_dir)
            print(f"{what}: storing objects uncompressed and skipping delta search for big "
                  f"blobs (core.compression=0, core.bigFileThreshold={_PROFILE_BIG_FILE_THRESHOLD}). "
                  f"Undo with `git -C {vault} config --unset core.compression` and "
                  f"`--unset core.bigFileThreshold`.")
        else:
            _use_profile_for_this_process_only()
            print(f"dry run -- {what}: a real run would store objects uncompressed and "
                  f"skip delta search for big blobs (applied to this preview only, "
                  f"so it stages at the speed a real run would).")
    elif write:
        git(git_dir, vault, ["config", "snapshot.profile", "default"], cwd=vault)
        git(git_dir, vault, ["config", "snapshot.profilebytes", str(int(measure["total"]))], cwd=vault)


#: Head-room kept free after staging: the index, the repack's temporary
#: packfile and whatever else the volume is doing.
_FREE_SPACE_MARGIN_BYTES = 1024 ** 3


def estimate_staging_bytes(
    git_dir: Path, vault: Path, source: Path, excludes: list[str],
    vault_config: dict[str, list[str]], skip: "frozenset[str] | None" = None,
) -> tuple[int, int]:
    """``(tree_bytes, new_bytes)``: the tree's size and what staging will add.

    The estimate is deliberately an upper bound on the *new* data: the tree's
    size, scaled by the sampled compressibility unless objects are stored
    uncompressed, minus what the vault's object store already holds.  A second
    run of an unchanged tree therefore estimates ~0; a first run of a 130 GiB
    tree estimates ~130 GiB.  It can over-estimate (renamed files hash to
    objects the vault already has) but not by enough to matter against a
    refusal that is one flag to override.
    """
    measure = sample_payload(source, excludes, skip)
    total = measure["total"]
    uncompressed = (
        (vault_config.get("snapshot.profile") or [""])[0] == "raw"
        or (vault_config.get("core.compression") or [""])[0] == "0"
        or decide_profile(measure) == "raw"
    )
    ratio = measure["ratio"]
    factor = 1.0 if (uncompressed or not 0.0 < ratio < 1.0) else ratio
    stats = _object_store_stats(git_dir, vault)
    held = (stats.get("size", 0) + stats.get("size-pack", 0)) * 1024
    return int(total), max(0, int(total * factor) - held)


def check_free_space(
    git_dir: Path, vault: Path, source: Path, excludes: list[str],
    vault_config: dict[str, list[str]], *, allow_low_disk: bool = False,
    skip: "frozenset[str] | None" = None,
) -> int:
    """Refuse to start staging a tree the vault's volume cannot hold; else return
    the bytes staging is estimated to add (an upper bound, for the heartbeat).

    ``git add`` writes every new object before anything can be undone, and a
    full volume does not fail cleanly: the vault is left with a partial object
    store and everything else on that disk -- including the Seafile client's
    own data -- starts failing too.  Staging a 130 GiB tree onto a volume with
    60 GiB free is exactly that, and a dry run stages as much as a real one.
    """
    tree, need = estimate_staging_bytes(
        git_dir, vault, source, excludes, vault_config, skip
    )
    free = shutil.disk_usage(vault).free
    if need + _FREE_SPACE_MARGIN_BYTES <= free or allow_low_disk:
        return need
    raise SnapshotError(
        f"refusing to stage: the tree is {human_bytes(tree)} and staging it needs about "
        f"{human_bytes(need)} of new object storage in {vault}, but only {human_bytes(free)} is free "
        f"on that volume (and {human_bytes(_FREE_SPACE_MARGIN_BYTES)} is kept in reserve). "
        "Put the vault on a larger volume with --vault, narrow the tree with --exclude, "
        "or pass --allow-low-disk if the data is far more compressible than the sample "
        "suggests."
    )


def profile_recheck_due(vault_config: dict[str, list[str]], tree_bytes: int) -> bool:
    """Whether a ``default`` vault has grown enough to be worth sampling again.

    True only for a vault decided ``default`` whose tree is now past the size
    where the profile matters and at least twice what it was when last sampled,
    so a large-but-compressible tree is sampled once per doubling, not per run.
    """
    if (vault_config.get("snapshot.profile") or [""])[0] != "default":
        return False
    if tree_bytes < _PROFILE_MIN_TREE_BYTES:
        return False
    try:
        last = int((vault_config.get("snapshot.profilebytes") or ["0"])[0])
    except ValueError:
        last = 0
    return tree_bytes >= 2 * last


#: A temporary pack younger than this may belong to a git process that is still
#: writing it; older ones were abandoned.
_TEMP_PACK_MIN_AGE_SECONDS = 3600


def sweep_temp_packs(git_dir: Path, *, min_age: float = _TEMP_PACK_MIN_AGE_SECONDS) -> int:
    """Delete ``tmp_pack_*`` files an interrupted run left in the object store.

    ``git pack-objects`` and ``repack`` write ``objects/pack/tmp_pack_<random>``
    and rename it when complete; a run that is killed mid-write leaves the file
    behind, and git never removes it (``count-objects`` reports it as "garbage").
    On the flood vault one such file was 22.5 GiB -- nearly half the free space
    on its volume -- and nothing in the tool mentioned it.  Called with the
    vault lock held, so no other snapshot run can be writing one; the age
    threshold covers a git process the user started by hand.  Returns bytes
    freed.
    """
    freed = 0
    cutoff = time.time() - min_age
    pack_dir = git_dir / "objects" / "pack"
    try:
        entries = list(pack_dir.glob("tmp_pack_*"))
    except OSError:
        return 0
    failed: list[str] = []
    for path in entries:
        try:
            st = path.stat()
            if st.st_mtime > cutoff:
                continue
            size = st.st_size
            try:
                path.unlink()
            except PermissionError:
                # Windows refuses to delete a read-only file, and git leaves its
                # temporary packs read-only.  Clear the attribute and retry; if
                # that fails too, something still has the file open.
                os.chmod(path, stat.S_IWRITE)
                path.unlink()
            freed += size
        except OSError as exc:
            failed.append(f"{path.name} ({exc.strerror or exc})")
    if freed:
        print(f"removed leftover temporary pack file(s) from an interrupted run "
              f"({human_bytes(freed)} freed)")
    if failed:
        print("Warning: could not remove leftover temporary pack file(s): " + "; ".join(failed))
    return freed


def maintain_vault(vault: Path, *, full_repack: bool = False) -> int:
    """Housekeep the vault's object store locally.

    Everything here works on ``.git/objects`` alone -- the worktree and the
    source tree are never read for content or written at all, which is what
    makes this safe beside a live snapshot job (the two only contend for git's
    lock).  A full repack reads every object once and writes the new packfile
    next to the old one until it is swapped in, so the free-space check below
    uses the whole store size as the headroom it needs.

    Without *full_repack* this is pure reporting: what is loose vs packed, how
    much that is, and whether a full repack would fit on this volume.  Snapshot
    runs maintain the same store incrementally (loose tier only) before each
    push; this command's ``--full-repack`` is the one-shot reset of it.
    """
    git_dir = vault / ".git"
    if not git_dir.is_dir():
        raise SnapshotError(f"no vault repository at {vault} -- nothing to maintain")

    stats = _object_store_stats(git_dir, vault)
    loose_kb = stats.get("size", 0)
    packed_kb = stats.get("size-pack", 0)
    store_bytes = (loose_kb + packed_kb) * 1024
    free = shutil.disk_usage(vault).free

    print(f"vault object store: {stats.get('count', 0)} loose object(s), "
          f"{human_bytes(loose_kb * 1024)} loose, {human_bytes(packed_kb * 1024)} "
          f"in {stats.get('packs', 0)} packfile(s)")
    if stats.get("garbage", 0):
        print(f"Note: {stats['garbage']} leftover temporary file(s) from an interrupted run "
              f"hold {human_bytes(stats.get('size-garbage', 0) * 1024)} in objects/pack; "
              "a snapshot run (or --maintain --full-repack) removes them")

    profile = _read_config_key(git_dir, "snapshot.profile")
    if profile:
        print(f"storage profile: {profile}"
              + (" (core.compression=0, core.bigFileThreshold="
                 f"{_PROFILE_BIG_FILE_THRESHOLD})" if profile == "raw" else ""))

    if not full_repack:
        need = human_bytes(store_bytes)
        have = human_bytes(free)
        verdict = "fits" if free >= store_bytes else "does NOT fit -- free space first"
        print(f"a full repack would need roughly {need} of headroom; this volume has {have} ({verdict}).")
        print("re-run with --maintain --full-repack to consolidate the whole store.")
        return 0

    if free < store_bytes:
        raise SnapshotError(
            f"refusing to repack: the object store is {human_bytes(store_bytes)} but only "
            f"{human_bytes(free)} is free on this volume; a repack keeps the old packfiles "
            "until the new ones are complete, so it needs roughly that much headroom"
        )

    with VaultLock(git_dir):
        sweep_temp_packs(git_dir)

    print("repacking (this reads every object once; large vaults take a while) ...")
    git(
        git_dir, None, ["repack", "-a", "-d"], cwd=vault,
        status="repacking the vault's object store", measure_dir=git_dir / "objects",
    )
    git(git_dir, None, ["prune"], cwd=vault)

    after = _object_store_stats(git_dir, vault)
    print(f"done: {after.get('count', 0)} loose object(s), "
          f"{human_bytes(after.get('size-pack', 0) * 1024)} in "
          f"{after.get('packs', 0)} packfile(s).")
    return 0


_KERNEL32: "object | None" = None


def _win_kernel32():
    """Build (once) a properly prototyped kernel32 for the pid probe."""
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (
            ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong,
        )
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
        )
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        )
        _KERNEL32 = kernel32
    return _KERNEL32


def process_start_token(pid: int) -> str | None:
    """An opaque value that changes if ``pid`` is later given to another process.

    A pid alone cannot say whether the run that took the lock is the process
    now holding that number: Windows recycles them quickly, so a crashed
    nightly run's pid soon belongs to something long-lived, which then looked
    "alive" and blocked every later run.  The process's creation time is what
    tells them apart.  ``None`` where it cannot be read (the lock then behaves
    as before).
    """
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = _win_kernel32()
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
                ok = kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user),
                )
                return str(created.value) if ok else None
            finally:
                kernel32.CloseHandle(handle)
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # the command name is in parentheses and may hold spaces: split after it
        fields = stat_line.rpartition(")")[2].split()
        return fields[19]  # field 22 of proc(5): start time in clock ticks since boot
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` is a running process *on this machine*.

    ``os.kill(pid, 0)`` is the POSIX idiom, but on Windows ``os.kill`` is
    ``TerminateProcess`` for every signal except the two console events -- so
    probing a pid that way would *kill* it.  Windows goes through the documented
    API instead, and treats "cannot tell" as alive so a lock is never stolen on
    a guess.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # Same prototype discipline as git_remote_seafile.lock._is_pid_alive:
        # ctypes.windll.kernel32.GetLastError() can return a stale value
        # (ctypes' own foreign calls may clobber the thread's last error
        # between OpenProcess failing and this read), and without restype the
        # 64-bit HANDLE truncates to c_int -- either defect can read a *live*
        # lock holder as dead and let a concurrent run steal its lock.  Built
        # and prototyped once at import, not per call (SNAP-10): every probe
        # was paying the four function-pointer reconfigurations.
        kernel32 = _win_kernel32()
        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            # A live process owned by another (elevated) user denies
            # OpenProcess outright; treating that as "dead" would let a
            # concurrent run steal its lock.  Only "no such process" proves
            # death -- anything else reads as alive (see the docstring).
            return ctypes.get_last_error() != error_invalid_parameter  # type: ignore[attr-defined,unused-ignore]
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        # A pid too large for the OS's pid_t (the stale-lock fixture uses
        # 2**32-1 to mean "certainly dead") cannot name a live process.  This
        # killed every POSIX CI run at v0.8.0 with an OverflowError before the
        # reclaim could happen; Windows masked it, because OpenProcess takes a
        # DWORD and simply fails for such a pid.
        return False
    return True


class VaultLock:
    """An exclusive lock so two snapshots cannot interleave.

    The lock records the pid and hostname that took it, so one left behind by a
    killed run can be recognised and reclaimed instead of deadlocking the vault
    until someone deletes the file by hand.  Reclamation only happens when the
    recorded host is *this* host: a pid from another machine means nothing here,
    and guessing would be worse than refusing.
    """

    def __init__(self, git_dir: Path) -> None:
        self.path = git_dir / "seafile-snapshot.lock"
        self._fd: int | None = None

    def holder(self) -> tuple[str, int, str | None] | None:
        """The lock file's recorded holder, read without touching it (--status)."""
        return self._holder()

    def _holder(self) -> tuple[str, int, str | None] | None:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        parts = text.split()
        if len(parts) < 2:
            return None
        try:
            return parts[0], int(parts[1]), (parts[2] if len(parts) > 2 else None)
        except ValueError:
            return None

    def _reclaim_if_stale(self) -> bool:
        holder = self._holder()
        if holder is None:
            # An empty or unreadable lock file is a run that died between
            # creating it and writing its identity.  A *live* run's lock is
            # written immediately after creation, so a fresh unparseable lock
            # may be a run mid-write -- require some age before reclaiming.
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            if age < _UNPARSEABLE_LOCK_GRACE_SECONDS:
                return False
            try:
                self.path.unlink()
            except OSError:
                # Not just FileNotFoundError: a Windows scanner holding a
                # transient read lock raises PermissionError, and crashing the
                # reclaim over it would defeat the point of reclaiming.  Leave
                # the file; the next run tries again.
                return False
            print("reclaiming an unparseable lock file left behind earlier "
                  f"(that run is gone): {self.path}")
            return True
        host, pid, started = holder
        if host != socket.gethostname():
            return False
        if pid_is_alive(pid):
            # A live pid is the holder only if it is the *same process*: one
            # whose start time differs is a recycled pid, and the run that took
            # this lock is gone.  Without a recorded or readable start time we
            # keep the old, cautious answer.
            now = process_start_token(pid) if started else None
            if not started or now is None or now == started:
                return False
        try:
            self.path.unlink()
        except OSError:
            # Same breadth as __exit__'s retry loop: a sharing violation is a
            # "not this time", not a crash.
            return False
        print(
            f"reclaiming a stale lock left by pid {pid} on this machine "
            f"(that run is gone)"
        )
        return True

    def __enter__(self) -> "VaultLock":
        for attempt in (1, 2):
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if attempt == 2 or not self._reclaim_if_stale():
                    holder = self._holder()
                    who = (
                        f"{holder[0]} pid {holder[1]}"
                        if holder
                        else "an unknown run"
                    )
                    raise SnapshotError(
                        f"another snapshot is running ({who}); lock: {self.path}.\n"
                        f"If you are certain that run is dead, delete the lock "
                        f"file and retry."
                    ) from None
        else:  # pragma: no cover - the loop always breaks or raises
            raise SnapshotError(f"could not take the lock: {self.path}")
        token = process_start_token(os.getpid())
        try:
            os.write(
                self._fd,
                f"{socket.gethostname()} {os.getpid()}{' ' + token if token else ''}".encode(),
            )
            os.fsync(self._fd)
        except OSError as exc:
            # A full disk or a failing share: without this the descriptor
            # leaked and an empty lock file stayed behind until its grace
            # period ran out, blocking the next run for that long.
            os.close(self._fd)
            self._fd = None
            try:
                self.path.unlink()
            except OSError:
                pass
            raise SnapshotError(
                f"could not record the lock holder in {self.path}: {exc}"
            ) from exc
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # A Windows scanner can hold the just-closed file for a moment, and a
        # sharing violation here would report a *successful* run as failed.
        # Retry briefly; if it still will not unlink, leave it -- the stale
        # reclaim above recognises it on the next run.
        for _ in range(5):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.05)


def current_tip(git_dir: Path, work_tree: Path, branch: str) -> str | None:
    proc = git(
        git_dir,
        work_tree,
        ["rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
        cwd=work_tree,
        check=False,
    )
    return proc.stdout.strip() or None


def is_ancestor(git_dir: Path, work_tree: Path, older: str, newer: str) -> bool:
    """True iff ``older`` is an ancestor of ``newer`` (equal counts).

    ``merge-base --is-ancestor`` answers 0 (yes) or 1 (no); anything else is git
    failing -- an unknown commit, a damaged object -- and is *not* "no".  Reading
    it as "no" told users their history had diverged and pointed them at
    ``--reset-to-remote``, the one flag that drops snapshots.
    """
    proc = git(
        git_dir,
        work_tree,
        ["merge-base", "--is-ancestor", older, newer],
        cwd=work_tree,
        check=False,
    )
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        raise SnapshotError(
            f"could not compare {older[:12]} with {newer[:12]}: {detail}"
        )
    return proc.returncode == 0


def set_branch(
    git_dir: Path, work_tree: Path, branch: str, commit: str, expected: str | None
) -> None:
    """Point ``refs/heads/<branch>`` at ``commit``.

    ``expected`` is the compare-and-swap guard: when given, the update only
    happens if the branch still points there, so a racing writer loses loudly
    instead of being clobbered.  ``None`` means an unconditional update, used
    when deliberately adopting the remote's history.
    """
    args = ["update-ref", f"refs/heads/{branch}", commit]
    if expected:
        args.append(expected)
    git(git_dir, work_tree, args, cwd=work_tree)


def delete_branch(git_dir: Path, work_tree: Path, branch: str, expected: str) -> bool:
    """Delete the branch only if it still points at *expected*; say whether it did.

    A compare-and-swap that lost (the branch moved) leaves the branch where it
    is and exits non-zero; that is not an error here, but a caller that reports
    the rollback must not claim a deletion that did not happen.
    """
    proc = git(
        git_dir,
        work_tree,
        ["update-ref", "-d", f"refs/heads/{branch}", expected],
        cwd=work_tree,
        check=False,
    )
    return proc.returncode == 0


def fetch_remote_tip(
    git_dir: Path, work_tree: Path, remote: str, branch: str
) -> str | None:
    """Fetch ``refs/heads/<branch>`` from ``remote`` and return its commit.

    ``None`` means the remote simply has no such branch yet -- a brand-new
    vault remote, which is not an error.  Any *other* fetch failure is raised:
    carrying on after a network error would build a snapshot on a stale base
    and have it rejected at push time, with the reason buried in the push.
    """
    proc = git(
        git_dir,
        work_tree,
        ["fetch", "--quiet", "--no-tags", remote, f"refs/heads/{branch}"],
        cwd=work_tree,
        check=False,
    )
    if proc.returncode != 0:
        # "No such branch on the remote" is the not-an-error case, but it
        # is decided by asking the remote, not by matching words in git's
        # output: the rejection text is locale-dependent (and differs
        # between git transports), the same reason push_snapshot refuses
        # to sniff it.  An ls-remote that succeeds and finds no ref names
        # the branch as absent; an ls-remote failure is a transport
        # problem and is raised.
        listing = git(
            git_dir,
            work_tree,
            ["ls-remote", "--heads", remote, f"refs/heads/{branch}"],
            cwd=work_tree,
            check=False,
        )
        if listing.returncode == 0 and not listing.stdout.strip():
            return None
        raise SnapshotError(
            f"could not fetch '{branch}' from {remote} (rc={proc.returncode})\n"
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    out = git(
        git_dir,
        work_tree,
        ["rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        cwd=work_tree,
        check=False,
    )
    return out.stdout.strip() or None


DIVERGENCE_HELP = (
    "the local vault and the remote have diverged: each holds snapshots the "
    "other does not.\n"
    "That happens when this machine snapshot while offline and another machine "
    "pushed to the same remote in the meantime.\n"
    "Nothing has been changed.  To resolve, either:\n"
    "  * keep the remote chain and re-snapshot this tree on top of it --\n"
    "    re-run with --reset-to-remote (the local-only snapshots are dropped,\n"
    "    but the current tree is captured again by the new snapshot); or\n"
    "  * keep this machine's chain -- push it to a different --remote, or to a\n"
    "    different --branch."
)


def reconcile_with_remote(
    git_dir: Path,
    work_tree: Path,
    branch: str,
    tip: str | None,
    *,
    reset_to_remote: bool,
    move: bool,
) -> str | None:
    """Decide which commit the next snapshot should extend, after a fetch.

    The vault is an append-only log of tree states, so the correct base is the
    *remote* tip whenever this machine can fast-forward to it.  Building on the
    local branch instead is what breaks a shared vault: a second machine starts
    from its own unrelated genesis, its push is rejected as non-fast-forward,
    and because the rejection never moves its branch back it is rejected again
    on every later run.

    Returns the commit to use as the new snapshot's parent, and (unless ``move``
    is false, as in a dry run) brings the local branch in line with it.
    """
    local = current_tip(git_dir, work_tree, branch)

    if tip is None:
        # Nothing on the remote yet: the local branch is all there is.
        return local

    if local is None or local == tip:
        if move and local is None:
            set_branch(git_dir, work_tree, branch, tip, None)
        return tip

    if is_ancestor(git_dir, work_tree, local, tip):
        # This machine is behind: fast-forward, then extend the remote chain.
        if move:
            set_branch(git_dir, work_tree, branch, tip, local)
        return tip

    if is_ancestor(git_dir, work_tree, tip, local):
        # This machine is ahead -- snapshots taken while offline.  Keep them;
        # the push fast-forwards.
        return local

    if not reset_to_remote:
        raise SnapshotError(DIVERGENCE_HELP)
    if move:
        set_branch(git_dir, work_tree, branch, tip, None)
    return tip


#: A vault joining a chain whose source shares less than this fraction of the
#: previous snapshot's git-stored paths is probably not the same tree.
_SAME_TREE_OVERLAP = 0.5

#: Below this many git-stored paths in the previous snapshot an overlap says
#: nothing (a payload-only tree, a handful of files).
_SAME_TREE_MIN_PATHS = 5


def warn_if_foreign_chain(
    git_dir: Path, work_tree: Path, base: str, tree: str, remote: str, branch: str
) -> None:
    """Say what a fresh vault is about to continue, and warn when it looks like another tree.

    A vault that does not hold the branch builds on whatever the remote has --
    that is how a second machine joins one backup (MultiMachineTests), and it is
    also how a *different* project pointed at the same ``--remote`` ends up on
    top of the first one's history, silently (REVIEW.sonnet-5.5-v0.9.14 E2: the
    chain's newest tree was the other project's, and a later ``--payload-keep``
    expires the first project's snapshots).  Nothing travels with the chain that
    names its source, so the comparison is the only evidence there is: how many
    of the previous snapshot's paths this source still has.
    """
    meta = git(
        git_dir, work_tree, ["show", "-s", "--format=%cI%x00%s", base], cwd=work_tree, check=False
    ).stdout
    when, _sep, subject = meta.partition("\x00")

    def paths(rev: str) -> set[str]:
        listing = git(
            git_dir, work_tree, ["ls-tree", "-r", "--name-only", "-z", rev], cwd=work_tree,
            check=False,
        ).stdout
        return {
            p for p in listing.split("\0")
            if p and p != PAYLOAD_RESERVED_DIR and not p.startswith(PAYLOAD_RESERVED_DIR + "/")
        }

    before, now = paths(base), paths(tree)
    shared = len(before & now)
    if len(before) >= _SAME_TREE_MIN_PATHS and shared < _SAME_TREE_OVERLAP * len(before):
        print(
            f"\nWarning: this vault is new, so it will continue the snapshot chain already on "
            f"{remote} ({branch}), last written {when.strip()} -- \"{subject.strip()}\" -- but "
            f"only {shared} of its {len(before)} files exist in this source.  This does not look "
            "like the same tree.  If it is a different project, stop: put it on another --remote "
            "or --branch, or the two trees share one history (and a --payload-keep run would "
            "expire the first one's snapshots).",
            flush=True,
        )
    else:
        print(
            f"Note: this vault is new, so it continues the snapshot chain already on {remote} "
            f"({branch}), last written {when.strip()} -- \"{subject.strip()}\".  That is how a "
            "second machine joins one backup; for a different tree use another --remote or "
            "--branch.",
            flush=True,
        )


def commit_tree(
    git_dir: Path, work_tree: Path, tree: str, parent: str | None, message: str
) -> str:
    """Write a commit object for ``tree`` on top of ``parent``."""
    args = ["commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", message]
    return git(git_dir, work_tree, args, cwd=work_tree).stdout.strip()


#: How many paths to hand to a single ``update-index`` invocation.  A tree can
#: hold far more matches than one command line can carry, and Windows' limit is
#: the smallest of the platforms this runs on.
_INDEX_BATCH = 500


def exclude_matches(path: str, pattern: str) -> bool:
    """Whether ``path`` (a slash-separated index path) matches ``pattern``.

    Deliberately *not* ``git rm``'s pathspec semantics, which are the wrong
    shape here and fail silently.  A literal pathspec is anchored at the tree
    root, so ``--exclude node_modules`` removes the top-level ``node_modules/``
    and leaves ``packages/api/node_modules/`` in the snapshot -- measured on a
    tree holding ``node_modules/x.js``, ``pkg/node_modules/y.js`` and
    ``a/b/node_modules/z.js``, where it removed one of the three.

    The rule used instead is the one people expect:

    * the pattern as a path *prefix* -- ``--exclude packages/web/build``
      excludes everything under it, and matches case-insensitively so one
      command line excludes the same set on every platform;
    * ``fnmatch`` against the whole path, which is what makes a wildcard work
      (``*.pyc`` matches ``a/b/c.pyc``: fnmatch's ``*`` crosses ``/``); or
      * any single path *component* equal to the pattern (case-insensitively),
      which is what makes a bare name mean "a directory by that name, at any
      depth".

    Both sides are compared in Unicode Normalization Form C: a source holding a
    decomposed name (``cafe\\u0301.txt``, the spelling a macOS tool or an HFS+
    volume writes) against the composed pattern a user types does not match
    byte-for-byte, and the file sailed into the snapshot behind the user's back
    (REVIEW.iq3_s E3, reproduced on Linux).  The comparison already ignores
    case; normalisation is the same concession to humans typing patterns.
    """
    # The pattern travels the same road as the index: forward slashes, no
    # trailing slash, and -- so one command line means one snapshot -- a case
    # comparison that does not depend on the OS (T-11/SNAP-06; index paths are
    # case-sensitive data, and fnmatch's normcase would make Windows behave
    # differently from every other platform).
    pattern = pattern.strip().replace("\\", "/").rstrip("/")
    if not pattern:
        return False
    path = unicodedata.normalize("NFC", path)
    pattern = unicodedata.normalize("NFC", pattern)
    # A pattern naming a directory prefix (--exclude packages/web/build)
    # excludes everything inside it -- the more-precise spelling must exclude
    # at least as much as the bare name it refines (SNAP-03).
    if path == pattern or path.startswith(pattern + "/"):
        return True
    if fnmatch.fnmatch(path, pattern):
        return True
    # Wildcard patterns keep their fnmatch shape, so match the prefix form of
    # those too; the component check is exact (and case-sensitive both ways)
    # so a bare name still means "a directory by that name, at any depth".
    if any(ch in pattern for ch in "*?[") and fnmatch.fnmatch(path, pattern + "/*"):
        return True
    parts = [component.lower() for component in path.split("/")]
    pattern_parts = [component.lower() for component in pattern.split("/")]
    # Multi-segment patterns match the path's leading components (the
    # prefix rule, case-insensitively); single-segment ones match any
    # component.
    if len(pattern_parts) > 1:
        return parts[: len(pattern_parts)] == pattern_parts
    return pattern_parts[0] in parts


def drop_from_index(git_dir: Path, work_tree: Path, paths: list[str]) -> None:
    """Remove *paths* from the vault index, in batches.

    ``update-index --force-remove`` with NUL-separated paths on stdin, rather
    than one ``git rm`` per path: the matching is ours (see
    :func:`exclude_matches`), and batching keeps the argument list short -- a
    tree with a large ``node_modules`` holds far more paths than one command
    line can carry, and Windows' limit is the smallest of the platforms this
    runs on.
    """
    for start in range(0, len(paths), _INDEX_BATCH):
        batch = paths[start : start + _INDEX_BATCH]
        git(
            git_dir,
            work_tree,
            ["update-index", "--force-remove", "-z", "--stdin"],
            cwd=work_tree,
            stdin="\0".join(batch) + "\0",
        )


def apply_excludes(git_dir: Path, work_tree: Path, excludes: list[str]) -> list[str]:
    """Drop index paths matching *excludes*; return what was dropped.

    This is the post-staging **safety net**, not the mechanism: staging already
    skips excluded paths (see :func:`stageable_paths`), so a non-empty return
    means a path reached the index that the pre-filter did not account for --
    worth knowing about, and worth not letting into a snapshot.
    """
    if not excludes:
        return []
    listing = git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout
    dropped = [
        path
        for path in listing.split("\0")
        if path and any(exclude_matches(path, pat) for pat in excludes)
    ]
    if dropped:
        drop_from_index(git_dir, work_tree, dropped)
    return dropped


#: What git prints (and exits 0 after) for a directory it cannot read.  An
#: unreadable *file* is an error and stops the run; an unreadable *directory*
#: is only this warning, and everything under it silently missing from the
#: snapshot is exactly the failure a backup must not have.
_UNREADABLE_DIR = re.compile(r"could not open directory '([^']+)'")


def unreadable_directories(*stderr_texts: "str | None") -> list[str]:
    """Directories git said it could not open, from the stderr of staging commands."""
    found: list[str] = []
    for text in stderr_texts:
        for match in _UNREADABLE_DIR.finditer(text or ""):
            path = match.group(1).rstrip("/")
            if path not in found:
                found.append(path)
    return found


def stageable_paths(
    git_dir: Path,
    work_tree: Path,
    nested_out: "list[str] | None" = None,
    warnings_out: "list[str] | None" = None,
) -> list[str]:
    """Every path ``git add -A -f .`` would consider, as the index spells it.

    ``ls-files -c -o`` *without* ``--exclude-standard``: ``-c`` is what is
    already staged and ``-o`` everything untracked **including ignored files**,
    which is exactly the set ``-f`` forces in -- adding ``--exclude-standard``
    would silently drop the ignored files that ``-f`` exists to capture.

    Two adjustments make the list safe to hand back as literal pathspecs (the
    git environment sets ``GIT_LITERAL_PATHSPECS=1``, so these names are never
    read as globs).  A nested repository is reported as ``nested/`` while the
    index records it as ``nested``, so the trailing slash goes.  And
    ``--deleted`` entries are dropped: they are in the index but gone from disk,
    and a pathspec naming a missing file makes ``git add`` fail outright ("did
    not match any files") rather than staging the deletion.
    """
    listed = git(git_dir, work_tree, ["ls-files", "-z", "-c", "-o"], cwd=work_tree)
    listing = listed.stdout
    if warnings_out is not None:
        warnings_out.extend(unreadable_directories(listed.stderr))
    deleted = {
        path.rstrip("/")
        for path in git(
            git_dir, work_tree, ["ls-files", "-z", "--deleted"], cwd=work_tree
        ).stdout.split("\0")
        if path
    }
    entries = [entry for entry in listing.split("\0") if entry]
    if nested_out is not None:
        # A trailing slash marks a directory with its own .git.  The caller
        # captures those by walking them; handing them to ``git add`` is what
        # failed for one with no commit.
        nested_out.extend(e.rstrip("/") for e in entries if e.endswith("/"))
        entries = [e for e in entries if not e.endswith("/")]
    return [
        path
        for path in (entry.rstrip("/") for entry in entries)
        if path not in deleted
    ]


def build_snapshot(
    git_dir: Path,
    work_tree: Path,
    parent: str | None,
    excludes: list[str],
    message: str,
    make_commit: bool = True,
    skip_paths: "frozenset[str] | None" = None,
    manifest: "dict[str, Any] | None" = None,
    capture_embedded: bool = True,
    embedded_report: "list[tuple[str, int]] | None" = None,
    record_mtimes: bool = False,
    staging_bytes: int = 0,
) -> tuple[str, str | None, list[str]]:
    """Stage the tree into the vault index and write a commit object.

    ``staging_bytes`` is the estimate of what staging will add to the object
    store; it only lets the heartbeat say how much is expected.

    Excluded paths are filtered out **before** staging.  ``git add -A -f .``
    reads, hashes and writes a loose object for every file in the tree and only
    then lets :func:`apply_excludes` drop the matching entries, so excluded bulk
    -- a 2 GB ``node_modules``, say -- is read, compressed and left in the
    object store on every run; and because the excluded paths never reach the
    index, the stat cache cannot help, so each run re-reads them (B2).  The file
    list is enumerated instead and the survivors handed to git, so an excluded
    path is never opened.  With no exclusions there is nothing to filter and the
    plain ``add`` is used unchanged.

    Returns ``(tree, commit, excluded)``, where ``commit`` is ``None`` when the
    tree is identical to ``parent`` -- an unchanged run should not pile up empty
    commits -- or when ``make_commit`` is false: a preview stops at the tree,
    because a commit would need an identity the preview has deliberately not
    written, and every report the preview prints is derived from the tree
    alone.  ``parent`` is passed in rather than read from the branch, because
    the caller may have just re-based on the remote tip.  The branch is *not*
    moved here: that is the caller's job, so a dry run can stop one step short.

    An embedded repository is captured as its files (:func:`inline_embedded_repositories`)
    unless ``capture_embedded`` is false; each one is appended to ``embedded_report``.
    """
    reserved = (work_tree / PAYLOAD_RESERVED_DIR).exists()
    # A clock probe a killed run left at the root must not be captured.
    probe_debris = any(True for _ in work_tree.glob(_PROBE_PREFIX + "*"))
    # Windows: git follows a junction and the walks that decide payload and
    # embedded-repository capture do not, so the three would disagree about
    # what the tree is.  Paths behind a junction are left out and reported; that
    # needs the candidate list, hence the filtered route on Windows.
    behind_junction = _junction_filter(work_tree) if os.name == "nt" else None
    filtered = bool(excludes or skip_paths or reserved or probe_debris or behind_junction)
    excluded: list[str] = []
    unreadable: list[str] = []
    if not filtered:
        try:
            added = git(
                git_dir,
                work_tree,
                ["add", "-A", "-f", "."],
                cwd=work_tree,
                status="staging the tree",
                measure_dir=git_dir / "objects",
                expect_bytes=staging_bytes,
            )
            unreadable.extend(unreadable_directories(added.stderr))
        except SnapshotError as exc:
            # A directory with its own .git and no commit cannot be recorded
            # as a commit id; the filtered route captures its files instead.
            if "does not have a commit checked out" not in str(exc):
                raise
            filtered = True
    if filtered:
        nested: list[str] = []
        candidates = stageable_paths(
            git_dir, work_tree, nested_out=nested if capture_embedded else None,
            warnings_out=unreadable,
        )
        if behind_junction is not None:
            dropped = [p for p in candidates if behind_junction(p)] + [
                d for d in nested if behind_junction(d, True)
            ]
            if dropped:
                gone = set(dropped)
                candidates = [p for p in candidates if p not in gone]
                nested = [p for p in nested if p not in gone]
                print(
                    f"\nWarning: {len(dropped)} path(s) are behind a Windows junction and were not "
                    f"captured (git would follow the junction, the payload walk does not, so "
                    f"neither is done):\n  " + "\n  ".join(dropped[:10])
                    + (f"\n  ... and {len(dropped) - 10} more" if len(dropped) > 10 else "")
                    + "\nMove what they point at inside the tree, or --exclude the junction."
                )
        excluded = [
            path
            for path in candidates
            if any(exclude_matches(path, pat) for pat in excludes)
        ]
        excluded_set = set(excluded)
        # Payload files are not excluded in the user's sense -- they are
        # recorded in the manifest and live on the server -- so they are
        # kept out of the index without being reported as excluded.
        # macOS hands the walk decomposed (NFD) names while git reports them
        # precomposed, so the two sides are compared in one form.
        skipped = {unicodedata.normalize("NFC", p) for p in (skip_paths or ())}
        kept = [
            path for path in candidates
            if path not in excluded_set
            and unicodedata.normalize("NFC", path) not in skipped
            and path != PAYLOAD_RESERVED_DIR
            and not path.startswith(PAYLOAD_RESERVED_DIR + "/")
            and not is_clock_probe_debris(path)
        ]
        kept_set = set(kept)
        if kept:
            try:
                added = git(
                    git_dir,
                    work_tree,
                    ["add", "-A", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
                    cwd=work_tree,
                    stdin="\0".join(kept) + "\0",
                    status="staging the tree",
                    measure_dir=git_dir / "objects",
                    expect_bytes=staging_bytes,
                )
                unreadable.extend(unreadable_directories(added.stderr))
            except SnapshotError as exc:
                if "does not have a commit checked out" not in str(exc):
                    raise
                raise SnapshotError(
                    "a directory in the source is a git repository with no commit, which cannot "
                    "be recorded as a commit id (--no-embedded-repos). Leave that flag out to "
                    "capture its files, or --exclude the directory.\n" + str(exc)
                ) from None
        # `git add` stages no deletion through a pathspec, and this index
        # persists between runs, so anything it still holds that is not in
        # `kept` -- a file deleted from the source, or one an exclude now
        # covers -- has to be dropped explicitly.
        indexed = git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout
        stale = [
            path for path in indexed.split("\0") if path and path not in kept_set
        ]
        if stale:
            drop_from_index(git_dir, work_tree, stale)
        # The pre-filter above is the mechanism; this is the net under it, so a
        # path that reached the index anyway is reported rather than shipped.
        missed = apply_excludes(git_dir, work_tree, excludes)
        if missed:
            excluded = sorted(set(excluded) | set(missed))
        extra_dirs = [
            d for d in nested
            if not any(exclude_matches(d, pat) for pat in excludes)
        ]
    else:
        extra_dirs = []

    # Embedded repositories: their files (the default), or their commit id.
    # Decided from the *directories* that hold a .git, not from gitlinks in the
    # index: from the second run on the index already holds the files, so a
    # gitlink-based test saw nothing and silently ignored the flag.  The walk
    # appends to ``unreadable``: git cannot warn about a directory inside a
    # repository it refuses to enter, so its stderr guard is blind there.
    handle_embedded_repositories(
        git_dir, work_tree, excludes, skip_paths,
        capture=capture_embedded, extra_dirs=extra_dirs, report=embedded_report,
        unreadable=unreadable,
    )

    unreadable = [
        d for d in dict.fromkeys(unreadable)
        if not any(exclude_matches(d, pat) for pat in excludes)
    ]
    if unreadable:
        raise SnapshotError(
            f"{len(unreadable)} director{'y' if len(unreadable) == 1 else 'ies'} could not be "
            "read, so what is under "
            f"{'it' if len(unreadable) == 1 else 'them'} is not in the snapshot:\n  "
            + "\n  ".join(unreadable[:10])
            + (f"\n  ... and {len(unreadable) - 10} more" if len(unreadable) > 10 else "")
            + "\nMake them readable, or leave them out on purpose with --exclude."
        )

    if record_mtimes:
        record_mtimes_in_index(git_dir, work_tree, parent)

    if manifest is not None:
        add_manifest_to_index(git_dir, work_tree, manifest)
    tree = git(git_dir, work_tree, ["write-tree"], cwd=work_tree).stdout.strip()

    if not make_commit:
        return tree, None, excluded

    if parent:
        parent_tree = git(
            git_dir, work_tree, ["rev-parse", f"{parent}^{{tree}}"], cwd=work_tree
        ).stdout.strip()
        if parent_tree == tree:
            return tree, None, excluded

    return tree, commit_tree(git_dir, work_tree, tree, parent, with_tool_trailer(message)), excluded


def describe_tree(git_dir: Path, work_tree: Path, tree: str) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for a tree."""
    listing = git(
        git_dir, work_tree, ["ls-tree", "-r", "-l", "--full-tree", tree], cwd=work_tree
    ).stdout
    count = 0
    total = 0
    for line in listing.splitlines():
        if not line.strip():
            continue
        count += 1
        meta = line.split("\t", 1)[0].split()
        if len(meta) >= 4 and meta[3].isdigit():
            total += int(meta[3])
    return count, total


def find_gitlinks(git_dir: Path, work_tree: Path, tree: str) -> list[str]:
    """Paths recorded as gitlinks -- embedded repositories whose contents are absent.

    ``git add`` records a directory that contains its own ``.git`` as a single
    reference to that repository's commit, and the commit is *not* copied.  A
    restore therefore produces a broken directory rather than the files, which
    is silent data loss in something sold as a full backup.
    """
    listing = git(git_dir, work_tree, ["ls-tree", "-r", tree], cwd=work_tree).stdout
    return [
        line.split("\t", 1)[1]
        for line in listing.splitlines()
        if line.startswith("160000 commit ")
    ]


#: Names that mean "this file is a credential".  Matched against the basename,
#: except for the ones containing a slash, which are matched against the path.
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "secrets.*",
    "*.secret",
    ".aws/credentials",
    ".ssh/*",
)


def find_secrets(paths: list[str]) -> list[str]:
    """Return the captured paths that look like credentials.

    The tool backs up ``.env`` and private keys quite happily -- that is what a
    *full* backup means.  But Git history is permanent, so the moment before the
    first push is the last moment the user can still choose a different remote.
    Saying so costs one line; not saying so is how secrets end up in a library
    someone else can read.

    A pattern with a slash (``.ssh/*``) names a location, not a filename, so it
    is matched against the whole path -- but anchored at any depth, not just
    the tree root: ``deploy/.ssh/known_hosts`` must be caught as readily as a
    top-level ``.ssh`` directory.

    Files of installed packages (``site-packages``, ``node_modules``) are not the
    user's credentials: certifi's ``cacert.pem`` is a public CA bundle, and a
    warning that cries wolf on every virtualenv teaches people to ignore it.
    """
    found: list[str] = []
    for path in paths:
        parts = path.split("/")
        if "site-packages" in parts[:-1] or "node_modules" in parts[:-1]:
            continue
        name = parts[-1]
        for pattern in SECRET_PATTERNS:
            if "/" in pattern:
                if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(
                    path, f"*/{pattern}"
                ):
                    found.append(path)
                    break
            elif fnmatch.fnmatch(name, pattern):
                found.append(path)
                break
    return found


def index_paths(git_dir: Path, work_tree: Path) -> list[str]:
    """Every path staged in the vault's index."""
    listing = git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout
    return [path for path in listing.split("\0") if path]


def source_tracked_paths(source: Path) -> list[str]:
    """Paths the source's own git repository tracks; empty when the source is not a repository root.

    Read-only (``ls-files`` writes nothing).  Used so that a *recommended*
    exclude never drops source the user has committed: ``build`` also names
    ``tools/build/make.py``.
    """
    if not (source / ".git").exists():
        return []
    proc = _run(
        ["git", "-C", str(source), "ls-files", "-z"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.split("\0") if p]


def find_virtualenvs(source: Path, excludes: list[str]) -> list[str]:
    """Directories under *source* that are Python virtualenvs, as index-style paths.

    A virtualenv is a directory holding ``pyvenv.cfg``; the name says nothing
    (``.venv-audit``, ``env``, ``py312``).  The walk does not descend into one, into
    ``.git``, into anything *excludes* already covers, or through a Windows
    junction (git does not follow it either).  The source's own ``pyvenv.cfg`` is
    not a virtualenv *inside* it, so the root is never reported.
    """
    found: list[str] = []
    root = str(source)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel != "." and "pyvenv.cfg" in filenames:
            found.append(rel)
            dirnames[:] = []
            continue
        keep = []
        for name in dirnames:
            child = name if rel == "." else f"{rel}/{name}"
            if name == ".git" or any(exclude_matches(child, pat) for pat in excludes):
                continue
            try:
                if getattr(os.lstat(os.path.join(dirpath, name)), "st_file_attributes", 0) & 0x400:
                    continue
            except OSError:
                continue  # vanished between the listing and the stat; not a venv either way
            keep.append(name)
        dirnames[:] = keep
    return sorted(found)


def suggest_excludes(
    git_dir: Path, work_tree: Path, excluded: list[str]
) -> list[str]:
    """Name reproducible junk that was captured, and how to leave it out.

    Deliberately a *suggestion* and not a default.  A snapshot is a backup; the
    one failure that cannot be forgiven is a backup that quietly omitted the file
    that mattered.  ``node_modules`` is reproducible and ``.env`` is not, and the
    tool cannot tell them apart -- so it captures everything, and points at what
    is merely bulky.

    Returns the patterns found (for testing); prints the advice.
    """
    present = {pattern for pattern in RECOMMENDED_EXCLUDES} - set(excluded)
    listing = git(
        git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree
    ).stdout
    paths = [p for p in listing.split("\0") if p]
    found: list[str] = []
    for pattern in sorted(present):
        hit = any(exclude_matches(path, pattern) for path in paths)
        if hit:
            found.append(pattern)
    # A virtualenv under any name (``.venv-audit``, ``env``, ``py312``): ``pyvenv.cfg``
    # is what makes it one, and it is rebuilt from a requirements file.  Not named
    # when an exclude already covers it.
    for path in paths:
        if path.endswith("/pyvenv.cfg"):
            venv = path[: -len("/pyvenv.cfg")]
            if not any(exclude_matches(venv, pat) for pat in [*excluded, *found]):
                found.append(venv)
    if not found:
        return []
    flags = " ".join(f"--exclude {p!r}" for p in found)
    print(
        f"\nWarning: the snapshot includes reproducible build output that is "
        f"usually not worth\nbacking up.  Leave it out next time with "
        f"--default-excludes, or explicitly:\n  {flags}"
    )
    return found


# -- Embedded repositories (see 70_stage.py) ---------------------------------------


def embedded_repositories(git_dir: Path, work_tree: Path) -> list[str]:
    """Paths the vault index holds as gitlinks (``160000``): a repository, not its files."""
    listing = git(git_dir, work_tree, ["ls-files", "-s", "-z"], cwd=work_tree).stdout
    found: list[str] = []
    for entry in listing.split("\0"):
        meta, _sep, path = entry.partition("\t")
        if path and meta.startswith("160000 "):
            found.append(path)
    return found


def _files_of_repository(
    root: Path, prefix: str, unreadable: "list[str] | None" = None
) -> list[str]:
    """Index paths for everything under *root* except ``.git`` itself.

    ``os.walk`` rather than ``git ls-files``: git treats a directory holding a
    ``.git`` as one opaque thing and will not list what is inside it, which is
    the very reason the contents were missing.  A symlink is its own entry (git
    stores the link, not its target), and a junction is not followed, as in
    :func:`_walk_sizes`.  A repository nested inside this one is walked on the
    same terms, so depth does not matter.

    ``unreadable`` collects directories the walk cannot open.  git cannot warn
    about a directory it refuses to enter, so its stderr guard (70_stage)
    covers the rest of the tree but is structurally blind here; without this,
    an unreadable directory inside an embedded repository left a hole in a
    "full" backup with exit 0 (REVIEW.iq3_s E1, reproduced on Linux).
    """
    found: list[str] = []

    def _deny(err: OSError) -> None:
        if unreadable is not None:
            unreadable.append(prefix.rstrip("/"))

    for dirpath, dirnames, filenames in os.walk(root, onerror=_deny):
        keep: list[str] = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if name == ".git":
                continue
            try:
                is_link = os.path.islink(full)
                is_reparse = bool(
                    getattr(os.lstat(full), "st_file_attributes", 0) & 0x400
                )
            except OSError:
                continue  # vanished between listing and stat
            if is_link:
                found.append(f"{prefix}/{os.path.relpath(full, root)}".replace(os.sep, "/"))
            elif not is_reparse:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if name == ".git":  # a submodule's or linked worktree's pointer file
                continue
            full = os.path.join(dirpath, name)
            found.append(f"{prefix}/{os.path.relpath(full, root)}".replace(os.sep, "/"))
    return found


def inline_embedded_repositories(
    git_dir: Path,
    work_tree: Path,
    excludes: list[str],
    skip_paths: "frozenset[str] | None" = None,
    extra_dirs: "list[str] | None" = None,
    unreadable: "list[str] | None" = None,
) -> list[tuple[str, int]]:
    """Replace each gitlink in the index by the files under it; ``[(path, files)]``.

    ``git add`` records a directory that has its own ``.git`` as a pointer to that
    repository's commit and copies nothing, so a restore produced an empty
    directory in something sold as a *full* backup.  The working tree is what a
    snapshot is for, so the files go in -- ignored ones too, like everywhere else
    in the tree, and subject to ``--exclude`` and to payload mode.  The nested
    ``.git`` is not captured: the snapshot has the files as they were, not the
    repository's history, and the report says so.

    The index is written with ``update-index`` rather than ``add`` because
    ``add`` goes through the same directory scan that refuses to look inside.
    """
    links = embedded_repositories(git_dir, work_tree)
    for extra in extra_dirs or ():
        if extra not in links:
            links.append(extra)  # a repository with no commit has no gitlink
    if not links:
        return []
    skipped = {unicodedata.normalize("NFC", p) for p in (skip_paths or ())}
    drop_from_index(git_dir, work_tree, links)
    report: list[tuple[str, int]] = []
    for link in links:
        root = work_tree / link
        files = _files_of_repository(root, link, unreadable) if root.is_dir() else []
        files = [
            path for path in files
            if not any(exclude_matches(path, pat) for pat in excludes)
            and unicodedata.normalize("NFC", path) not in skipped
        ]
        for start in range(0, len(files), _INDEX_BATCH):
            batch = files[start : start + _INDEX_BATCH]
            git(
                git_dir,
                work_tree,
                ["update-index", "--add", "-z", "--stdin"],
                cwd=work_tree,
                stdin="\0".join(batch) + "\0",
            )
        report.append((link, len(files)))
    return report


def embedded_note(report: list[tuple[str, int]]) -> str:
    """The notice printed when embedded repositories were captured as plain files."""
    lines = [
        "\nNote: these paths are embedded git repositories.  Their files were captured "
        "as ordinary\nfiles; their .git directories (history, branches, stashes) were not:"
    ]
    lines += [f"  {path} ({count} file(s))" for path, count in report]
    lines.append("  Back the history up separately, or pass --no-embedded-repos to record "
                 "only each repository's commit id.")
    return "\n".join(lines)


def _junction_filter(work_tree: Path) -> Any:
    """A predicate: is this index path behind a Windows junction (a reparse-point directory)?

    Symlinks are not junctions (git stores a symlink as itself); only a
    directory reparse point that is not a symlink is followed by git for
    Windows and by nothing else here.
    """
    cache: dict[str, bool] = {}

    def is_junction(directory: str) -> bool:
        if directory not in cache:
            try:
                st = os.lstat(work_tree / directory)
                cache[directory] = bool(
                    getattr(st, "st_file_attributes", 0) & 0x400 and not stat.S_ISLNK(st.st_mode)
                )
            except OSError:
                cache[directory] = False
        return cache[directory]

    def behind(rel: str, self_too: bool = False) -> bool:
        # ``self_too`` is for a directory that is itself the thing being
        # considered (a nested repository): a junction *to* a repository is
        # followed just as one above it is.
        parent = rel if self_too else rel.rpartition("/")[0]
        while parent:
            if is_junction(parent):
                return True
            parent = parent.rpartition("/")[0]
        return False

    return behind


def embedded_roots(paths: list[str], work_tree: Path) -> list[str]:
    """The directories (never the source root) that hold their own ``.git`` and contain *paths*."""
    exists: dict[str, bool] = {}
    roots: set[str] = set()
    for path in paths:
        prefix = ""
        for part in path.split("/")[:-1]:
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix not in exists:
                exists[prefix] = os.path.lexists(work_tree / prefix / ".git")
            if exists[prefix]:
                roots.add(prefix)
    return sorted(roots)


def handle_embedded_repositories(
    git_dir: Path,
    work_tree: Path,
    excludes: list[str],
    skip_paths: "frozenset[str] | None",
    *,
    capture: bool,
    extra_dirs: "list[str] | None" = None,
    report: "list[tuple[str, int]] | None" = None,
    unreadable: "list[str] | None" = None,
) -> None:
    """Leave the index holding every embedded repository the way the run asked.

    With ``capture`` (the default) the files go in, gitlinks and repositories
    with no commit included.  Without it each embedded repository is recorded
    as its commit id again, and any files an earlier run captured are removed,
    so the flag means the same on the second run as on the first.  ``report``
    receives ``(directory, files)`` for every embedded repository whose files
    are in the snapshot, on every run (the note that their history is not
    captured used to be printed on the first run only).  ``unreadable`` is the
    list build_snapshot's refusal reads: a directory inside an embedded
    repository the walk cannot open lands there, because git's own warning
    cannot see inside one.
    """
    if capture:
        inline_embedded_repositories(
            git_dir, work_tree, excludes, skip_paths, extra_dirs, unreadable
        )
    paths = index_paths(git_dir, work_tree)
    # Outermost repositories only: a repository inside another is part of that
    # one's files, and its own gitlink would make "a file and a directory".
    outer: list[str] = []
    for root in embedded_roots(paths, work_tree):
        if not any(root.startswith(kept + "/") for kept in outer):
            outer.append(root)
    if not outer:
        return
    under: dict[str, list[str]] = {root: [] for root in outer}
    outer_set = set(outer)
    for path in paths:  # one pass: the nearest enclosing root claims the path
        ancestor = path.rpartition("/")[0]
        while ancestor:
            if ancestor in outer_set:
                under[ancestor].append(path)
                break
            ancestor = ancestor.rpartition("/")[0]
    if capture:
        if report is not None:
            for root in outer:
                report.append((root, len(under[root])))
        return
    # Opt-out: the commit id, not the files -- of the *nested* repository.  The
    # vault's own git directory must not be consulted for it: ``git()`` always
    # passes --git-dir, so a ``-C`` through it names the vault's HEAD.
    env = dict(os.environ)
    for var in _REPO_ENV_VARS:
        env.pop(var, None)
    for root in outer:
        if not under[root]:
            continue
        head = _run(
            ["git", "-C", str(work_tree / root), "rev-parse", "--verify", "-q", "HEAD"],
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        )
        sha = head.stdout.strip() if head.returncode == 0 else ""
        if not sha:
            raise SnapshotError(
                f"{root} is a git repository with no commit, which cannot be recorded as a "
                "commit id (--no-embedded-repos). Leave that flag out to capture its files, "
                "or --exclude the directory."
            )
        drop_from_index(git_dir, work_tree, under[root])
        git(
            git_dir, work_tree,
            ["update-index", "--add", "--cacheinfo", f"160000,{sha},{root}"],
            cwd=work_tree,
        )


# -- Payload safeguards: provenance, preflight, markers (see PAYLOAD.md D13) --------
#
# Everything here is advisory or a refusal *before* something expensive or
# destructive starts; none of it changes what a snapshot contains.  The tool
# version goes in the commit message (a trailer), never in the tree, so a tree
# that did not change still commits nothing after an upgrade.

TOOL_TRAILER = "Git-Remote-Seafile"
#: The oldest tool that reads the payload manifests this tool writes correctly.
#: Raised only when a change to the manifest's meaning needs it, never per release.
PAYLOAD_MIN_TOOL = "0.9.0"
_VERIFIED_NAME = "verified.json"
_SEEN_NAME = "gc-seen.json"
_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


def tool_version() -> str:
    try:
        from git_remote_seafile import __version__
    except Exception:  # a copy of this file run on its own
        return "unknown"
    return str(__version__)


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    match = _VERSION_RE.match(text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def with_tool_trailer(message: str) -> str:
    """The snapshot message with the writing tool's version as a git trailer."""
    return f"{message.rstrip()}\n\n{TOOL_TRAILER}: {tool_version()}\n"


def tool_of_commit(git_dir: Path, work_tree: Path, rev: str | None) -> str | None:
    """The tool version recorded in a snapshot commit, or ``None`` (older tool)."""
    if not rev:
        return None
    proc = git(git_dir, work_tree, ["show", "-s", "--format=%B", rev], cwd=work_tree, check=False)
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        key, sep, value = line.partition(":")
        if sep and key.strip() == TOOL_TRAILER:
            return value.strip() or None
    return None


def warn_if_newer_tool(git_dir: Path, work_tree: Path, rev: str | None) -> None:
    """Say so when the snapshot this run builds on was written by a newer tool."""
    theirs, ours = tool_of_commit(git_dir, work_tree, rev), tool_version()
    a, b = _version_tuple(theirs), _version_tuple(ours)
    if a is not None and b is not None and a > b:
        print(
            f"Warning: the snapshot this run builds on was written by git-remote-seafile {theirs}; this is "
            f"{ours}. A newer tool may have changed what a vault holds -- upgrade before "
            "relying on this run.",
            file=sys.stderr,
            flush=True,
        )


def check_min_tool(manifest: dict[str, Any], where: str) -> None:
    """Refuse a manifest that says it needs a newer tool than this one."""
    needed = manifest.get("min_git_remote_seafile")
    need, have = _version_tuple(needed if isinstance(needed, str) else None), \
        _version_tuple(tool_version())
    if need is not None and have is not None and need > have:
        raise SnapshotError(
            f"{where} needs git-remote-seafile {needed} or newer (this is {tool_version()}). "
            "Upgrade it before restoring or running retention"
        )


# -- preflight: say it before hours of upload, not after -------------------------


def _account_quota(client: Any) -> tuple[int, int] | None:
    """``(usage, total)`` bytes of the signed-in account, or ``None`` if unknown/unlimited."""
    resp = client.session.get(f"{client.server_url}/api2/account/info/", timeout=client.timeout)
    if resp.status_code != 200:
        return None
    data = resp.json()
    usage, total = data.get("usage"), data.get("total")
    if isinstance(usage, bool) or isinstance(total, bool):
        return None
    if not isinstance(usage, int) or not isinstance(total, int) or total <= 0:
        return None
    return usage, total


def quota_note(client: Any, need_bytes: int, *, action: str = "uploads") -> str | None:
    """A warning when the upload would not fit the account's quota.

    *action* names what the run does with the bytes: a payload view copies
    server-side and moves nothing, but the quota counts each copy at its full
    size, so "uploads" would be the wrong word there.

    A warning, not a refusal: the quota that counts is the library owner's, which
    is not necessarily this account.
    """
    try:
        quota = _account_quota(client)
    except Exception:  # informational only
        return None
    if quota is None:
        return None
    usage, total = quota
    free = total - usage
    if need_bytes <= free:
        return None
    return (
        f"Warning: this run {action} {human_bytes(need_bytes)} but the account has "
        f"{human_bytes(max(free, 0))} of quota left ({human_bytes(usage)} of "
        f"{human_bytes(total)} used); it will probably fail partway. Free space, or "
        "ask for more, first -- already stored objects are kept and the next run resumes."
    )


def synced_payload_library_note(rid: str) -> str | None:
    """A warning when this machine's Seafile client syncs the library holding the payload."""
    try:
        import git_remote_seafile.safety as _safety
        libraries = _safety.discover_local_synced_libraries()
    except Exception:
        return None
    for lib in libraries:
        if lib.get("repo_id") == rid and lib.get("auto_sync", True):
            return (
                f"Warning: the library holding the payload is synced on this machine "
                f"({lib.get('worktree')}). The desktop client will download every payload "
                "object and fight over the vault directory. Sync a different library, turn "
                "auto sync off for this one, or add the vault's path to seafile-ignore.txt."
            )
    return None


def payload_preflight(client: Any, rid: str, need_bytes: int, *, action: str = "uploads") -> None:
    for note in (synced_payload_library_note(rid), quota_note(client, need_bytes, action=action)):
        if note:
            print(note, file=sys.stderr, flush=True)


def restore_space_problem(
    files: list[dict[str, Any]], into: Path, *, allow_low_disk: bool
) -> str | None:
    """A reason the payload would not fit where it is being restored, or ``None``."""
    need = 0
    for item in files:
        dest = into / item["path"]
        try:
            if dest.is_file() and dest.stat().st_size == item["size"]:
                continue
        except OSError:
            pass
        need += item["size"]
    try:
        free = shutil.disk_usage(into).free
    except OSError:
        return None
    if need <= free or allow_low_disk:
        return None
    return (
        f"the payload needs {human_bytes(need)} and {into} has {human_bytes(free)} free; "
        "free space or restore elsewhere (--allow-low-disk to try anyway)"
    )


# -- server-side markers ----------------------------------------------------------


def _read_json_marker(client: Any, rid: str, base: str, name: str) -> dict[str, Any]:
    try:
        text = client.get_file_text(rid, f"{base}/{name}")
        data = json.loads(text) if text else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_marker(client: Any, rid: str, base: str, name: str, data: dict[str, Any]) -> None:
    try:
        client.mkdir_p(rid, base)
        body = json.dumps(data, sort_keys=True).encode("ascii")
        client.upload_file(rid, base, name, body)
    except Exception as exc:  # a marker must never fail the run that has just succeeded
        print(f"Warning: could not write {name}: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return
    # Read the marker back (ROADMAP S3, write half): the upload's 2xx is not
    # evidence the bytes are there, and a baseline that never landed erases
    # the history-rewrite fence exactly like a torn one.  The markers are
    # written rarely (once per delete or verify run), so the read costs
    # nothing that matters.
    try:
        landed = client.get_file_text(rid, f"{base}/{name}")
        if landed is not None and landed.strip() != body.decode("ascii"):
            print(f"Warning: {name} did not read back as what was written; a later "
                  "run may re-decide from an empty or stale marker",
                  file=sys.stderr, flush=True)
    except Exception:
        pass  # the write itself succeeded; an unreachable read proves nothing


def record_verification(client: Any, rid: str, base: str, checked: int, total: int) -> None:
    _write_json_marker(client, rid, base, _VERIFIED_NAME,
                       {"at": int(time.time()), "checked": checked, "of": total})


def verification_note(client: Any, rid: str, base: str) -> str:
    data = _read_json_marker(client, rid, base, _VERIFIED_NAME)
    at = data.get("at")
    if isinstance(at, bool) or not isinstance(at, int):
        return "last --payload-verify: never (stored objects are not re-checked by a snapshot run)"
    days = max(0, int((time.time() - at) // 86400))
    return (f"last --payload-verify: {days} day(s) ago "
            f"({data.get('checked', '?')} of {data.get('of', '?')} object(s))")


def read_seen_tips(client: Any, rid: str, base: str) -> dict[str, str]:
    """The ref tips the last delete run saw, or ``{}`` for a first run.

    A marker that is *there* but unreadable or unparseable is an error, not an
    empty history: treating it as ``{}`` erased the history-rewrite fence, and
    a delete run then marked objects that rewritten snapshots still need
    (ROADMAP S3, read half).  The remedy names the file, like the reader for
    ``gc-pending.json`` beside it.
    """
    try:
        text = client.get_file_text(rid, f"{base}/{_SEEN_NAME}")
    except Exception as exc:
        raise SnapshotError(
            f"could not read {base}/{_SEEN_NAME} ({type(exc).__name__}: {exc}); the "
            "history-rewrite fence cannot be checked. Retry, or remove the file (it "
            "will be re-created from the tips this run sees)"
        ) from None
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise SnapshotError(
            f"{base}/{_SEEN_NAME} is unreadable; the history-rewrite fence cannot be "
            "checked. Remove the file to start a fresh baseline -- after checking "
            "that no snapshot history was rewritten"
        ) from None
    if not isinstance(data, dict):
        raise SnapshotError(
            f"{base}/{_SEEN_NAME} is not an object; the history-rewrite fence cannot "
            "be checked. Remove the file to start a fresh baseline"
        )
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def write_seen_tips(client: Any, rid: str, base: str, tips: dict[str, str]) -> None:
    _write_json_marker(client, rid, base, _SEEN_NAME, tips)


def change_time_ns(path: Path, st: os.stat_result) -> int:
    """The inode's change time, which an in-place edit moves even when the
    modification time is put back.

    On POSIX that is ``st_ctime``.  On Windows ``st_ctime`` is the *creation*
    time (Python 3.12 deprecated it for exactly that reason): it never moves, so
    a same-size in-place write followed by ``utime`` left size, mtime, inode and
    "ctime" all unchanged and the cache vouched for the old hash (measured on
    NTFS, Python 3.14).  NTFS keeps a real change time, reachable through
    ``GetFileInformationByHandleEx(FileBasicInfo)``; it moved in the same
    experiment.  Where it cannot be read the creation time is returned, which is
    what the key held before -- a cache entry written then differs in this field
    and is hashed once more, deliberately.
    """
    if os.name != "nt":
        return st.st_ctime_ns
    try:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        # FILE_READ_ATTRIBUTES, all sharing, OPEN_EXISTING, BACKUP_SEMANTICS |
        # OPEN_REPARSE_POINT (the file itself, as lstat saw it).
        handle = kernel32.CreateFileW(
            str(extended_path(path)), 0x80, 7, None, 3, 0x02000000 | 0x00200000, None
        )
        if handle is None or handle == wintypes.HANDLE(-1).value:
            return st.st_ctime_ns
        try:
            info = _Basic()
            if not kernel32.GetFileInformationByHandleEx(
                handle, 0, ctypes.byref(info), ctypes.sizeof(info)
            ):
                return st.st_ctime_ns
            return int(info.ChangeTime) * 100
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return st.st_ctime_ns


# -- Modification times of the files git stores (see 70_stage.py) ------------------
#
# Git records content and a mode, not a timestamp, so a restore used to give
# every git-tracked file the time of the checkout.  Payload files already carry
# their mtime in the manifest.  This records the rest in one more file inside
# the reserved directory, ``.seafile-payload/mtimes.json``, and a restore puts
# the times back.

_MTIMES_NAME = "mtimes.json"
_MTIMES_FORMAT = 1


def mtimes_path() -> str:
    """Where the record lives in a snapshot tree."""
    return f"{PAYLOAD_RESERVED_DIR}/{_MTIMES_NAME}"


def add_mtimes_arguments(parser: Any) -> None:
    parser.add_argument(
        "--record-mtimes",
        action="store_true",
        help="record the modification time of every git-stored file in "
        ".seafile-payload/mtimes.json so a restore can put them back (default for a run "
        "that pushes to a seafile:// remote; remembered by the vault)",
    )
    parser.add_argument(
        "--no-record-mtimes",
        action="store_true",
        help="do not record modification times (the vault remembers this; a restore then "
        "gives git-stored files the time of the checkout, as before 0.9.2)",
    )


def mtimes_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.record_mtimes and args.no_record_mtimes:
        problems.append("--record-mtimes and --no-record-mtimes contradict each other")
    if (args.record_mtimes or args.no_record_mtimes) and (
        args.restore or args.maintain or args.payload_gc or args.payload_verify is not None
        or args.payload_view is not None
    ):
        problems.append(
            "--record-mtimes and --no-record-mtimes only apply to a snapshot run"
        )
    return problems


def parent_has_mtimes(git_dir: Path, work_tree: Path, parent: str | None) -> bool:
    """Whether snapshot ``parent`` carries a times record."""
    if not parent:
        return False
    proc = git(
        git_dir, work_tree, ["cat-file", "-e", f"{parent}:{mtimes_path()}"],
        cwd=work_tree, check=False,
    )
    return proc.returncode == 0


def resolve_record_mtimes(
    args: argparse.Namespace, vault: Path, *, remote: str | None, no_push: bool,
    parent_has_record: bool = False,
) -> bool:
    """The flag as typed, else what the vault remembered, else what the last snapshot did,
    else on for a run that pushes to a Seafile remote.

    A default is not a request, and neither is a run's route: a ``--no-push``
    run on a vault whose last snapshot has a record keeps recording, rather
    than dropping the record (and committing the difference) until the next
    pushing run rebuilds it.  With no seafile:// remote and no such history it
    stays off, so nothing that worked before changes.
    """
    if args.no_record_mtimes:
        return False
    if args.record_mtimes:
        return True
    if (vault / ".git").exists():
        remembered = _config(vault, "snapshot.mtimes")
        if remembered in ("0", "1"):
            return remembered == "1"
    return parent_has_record or payload_by_default_possible(remote, no_push)


def remember_record_mtimes(vault: Path, args: argparse.Namespace, *, dry_run: bool) -> None:
    """Remember an explicit choice after a complete real run (never a default)."""
    if dry_run or not (args.record_mtimes or args.no_record_mtimes):
        return
    _run(
        ["git", "-C", str(vault), "config", "snapshot.mtimes", "0" if args.no_record_mtimes else "1"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _blobs_of_tree(git_dir: Path, work_tree: Path, rev: str) -> dict[str, str]:
    """``{path: blob id}`` for every file in snapshot ``rev``."""
    listing = git(git_dir, work_tree, ["ls-tree", "-r", "-z", rev], cwd=work_tree).stdout
    found: dict[str, str] = {}
    for entry in listing.split("\0"):
        meta, _sep, path = entry.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[1] == "blob":
            found[path] = parts[2]
    return found


def _recorded_mtimes(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, int]:
    """The times recorded in snapshot ``rev``; empty when it has none or they are unusable."""
    if not rev:
        return {}
    proc = git(
        git_dir, work_tree, ["show", f"{rev}:{mtimes_path()}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
        files = data["files"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}
    if not isinstance(files, dict):
        return {}
    return {
        path: value for path, value in files.items()
        if isinstance(path, str) and isinstance(value, int) and not isinstance(value, bool)
        and 0 <= value < _MAX_MTIME_NS
    }


def _recorded_sizes(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, int]:
    """The sizes recorded in snapshot ``rev`` beside the times; empty for a snapshot that has none.

    An additive key of the same document (``"sizes"``), so a tool that does not
    know it reads ``"files"`` as before.  Only regular files carry one: a
    symlink's own size is its target's length and says nothing about content.
    """
    if not rev:
        return {}
    proc = git(
        git_dir, work_tree, ["show", f"{rev}:{mtimes_path()}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return {}
    try:
        sizes = json.loads(proc.stdout)["sizes"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}
    if not isinstance(sizes, dict):
        return {}
    return {
        path: value for path, value in sizes.items()
        if isinstance(path, str) and isinstance(value, int) and not isinstance(value, bool)
        and value >= 0
    }


def record_mtimes_in_index(git_dir: Path, work_tree: Path, parent: str | None) -> int:
    """Write ``.seafile-payload/mtimes.json`` into the vault index; return how many times.

    The times are the real ones.  Only when nothing in the tree changed since
    the parent snapshot is the parent's record reused unchanged: a touch is not
    a change (a build that rewrites a generated file with the same bytes must
    not make a new snapshot every night).  Carrying times forward per file was
    tried and is wrong: a source reverted to an older version kept its old
    time, which could sort it *before* an object built from the version it
    replaced, so ``make`` kept the stale object.
    """
    listing = git(git_dir, work_tree, ["ls-files", "-s", "-z"], cwd=work_tree).stdout
    current: dict[str, str] = {}
    for entry in listing.split("\0"):
        meta, _sep, path = entry.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[0] != "160000":
            current[path] = parts[1]
    wanted = {
        path: blob for path, blob in current.items()
        if path != PAYLOAD_RESERVED_DIR and not path.startswith(PAYLOAD_RESERVED_DIR + "/")
    }
    if parent and wanted == {
        path: blob for path, blob in _blobs_of_tree(git_dir, work_tree, parent).items()
        if path != PAYLOAD_RESERVED_DIR and not path.startswith(PAYLOAD_RESERVED_DIR + "/")
    }:
        previous = git(
            git_dir, work_tree, ["rev-parse", "--verify", "-q", f"{parent}:{mtimes_path()}"],
            cwd=work_tree, check=False,
        )
        if previous.returncode == 0 and previous.stdout.strip():
            git(
                git_dir, work_tree,
                ["update-index", "--add", "--cacheinfo",
                 f"100644,{previous.stdout.strip()},{mtimes_path()}"],
                cwd=work_tree,
            )
            return len(_recorded_mtimes(git_dir, work_tree, parent))

    times: dict[str, int] = {}
    sizes: dict[str, int] = {}
    for path in wanted:
        try:
            info = os.lstat(extended_path(work_tree / path))
        except OSError:
            continue
        stamp = info.st_mtime_ns
        if 0 <= stamp < _MAX_MTIME_NS:
            times[path] = stamp
            if stat.S_ISREG(info.st_mode):
                sizes[path] = info.st_size
    if not times:
        return 0
    record: dict[str, Any] = {"format": _MTIMES_FORMAT, "files": times}
    if sizes:
        # Beside the times, not instead of them: `--status` compares size too, so a
        # rewrite that restores the mtime no longer reads as "unchanged".
        record["sizes"] = sizes
    document = json.dumps(record, sort_keys=True, separators=(",", ":"))
    blob_id = git(
        git_dir, work_tree, ["hash-object", "-w", "--no-filters", "--stdin"],
        cwd=work_tree, stdin=document,
    ).stdout.strip()
    git(
        git_dir, work_tree,
        ["update-index", "--add", "--cacheinfo", f"100644,{blob_id},{mtimes_path()}"],
        cwd=work_tree,
    )
    return len(times)


def apply_recorded_mtimes(into: Path) -> tuple[int, int]:
    """Set the recorded times on a restored tree; ``(applied, skipped)``.

    Best effort and bounded to *into*: the record came from a remote, so each
    path goes through the same checks as a manifest path, and a time that does
    not fit is skipped rather than written.
    """
    record = into / PAYLOAD_RESERVED_DIR / _MTIMES_NAME
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        files = data["files"]
        if not isinstance(files, dict):
            return 0, 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return 0, 0
    root = into.resolve()
    applied = skipped = 0
    for rel, stamp in files.items():
        if (
            not isinstance(rel, str) or isinstance(stamp, bool) or not isinstance(stamp, int)
            or not 0 <= stamp < _MAX_MTIME_NS or path_security_problem(rel) is not None
            or windows_name_problem(rel) is not None
            # A record entry is remote data like a manifest entry, so it gets
            # the same Windows-name check: `.git::$INDEX_ALLOCATION/config`
            # passed the security check and moved .git/config's time
            # (REVIEW.sonnet-5.5 D10, reproduced).
        ):
            skipped += 1
            continue
        plain = into / rel
        target = extended_path(plain)  # past 260 characters on Windows too
        try:
            if os.path.commonpath([str(root), str(plain.parent.resolve())]) != str(root):
                skipped += 1
                continue
            if target.is_symlink():
                # A platform that cannot set a link's own time (Windows) is not a
                # failure to report: the link is restored, only its time is not.
                if os.utime in os.supports_follow_symlinks:
                    os.utime(target, ns=(stamp, stamp), follow_symlinks=False)
                    applied += 1
            elif target.is_file():
                os.utime(target, ns=(stamp, stamp))
                applied += 1
            else:
                skipped += 1
        except (OSError, ValueError, OverflowError):
            skipped += 1
    return applied, skipped


# -- Payload manifests and the per-machine hash cache (see 75_payload.py) ----------

# -- manifests ---------------------------------------------------------------


#: The snapshot id this tool writes into a manifest: derived, never user text.
_SNAPSHOT_ID_RE = re.compile(r"p-[0-9a-f]{16}")


def check_manifest_shape(manifest: Any, where: str = "the payload manifest") -> list[dict[str, Any]]:
    """The manifest's file entries, if it is a manifest this tool understands.

    Structure only (types, hash format, bounds): path *safety* is
    :func:`validate_manifest`'s job.  Anything unreadable raises -- a reader that
    treats "cannot parse" as "references nothing" turns a version skew into
    deleted data.
    """
    if not isinstance(manifest, dict):
        raise SnapshotError(f"{where} is not an object")
    fmt = manifest.get("format")
    if isinstance(fmt, bool) or not isinstance(fmt, int) or fmt < 2:
        raise SnapshotError(f"{where} has no usable format")
    if fmt > PAYLOAD_FORMAT:
        raise SnapshotError(
            f"{where} is format {fmt}; this tool understands up to {PAYLOAD_FORMAT}. "
            "Upgrade it before restoring or running retention"
        )
    check_min_tool(manifest, where)
    ratio = manifest.get("ratio")
    if ratio is not None and (
        isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio <= 1
    ):
        # The field feeds the cross-machine cut-off guard, so a hostile or
        # hand-written value is refused rather than compared.
        raise SnapshotError(f"{where} has a bad ratio {ratio!r}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise SnapshotError(f"{where} has no file list")
    ident = manifest.get("snapshot")
    if isinstance(ident, str) and not _SNAPSHOT_ID_RE.fullmatch(ident):
        # The field is a name this tool derives, never user text; a value that
        # is not the derived shape is a manifest someone wrote by hand, and it
        # reaches a server-side directory name in --payload-view.  Refused as
        # unparseable rather than trusted (REVIEW.iq3_s E2).
        raise SnapshotError(f"{where} has a snapshot id that is not one this tool writes: {ident!r}")
    for item in files:
        if not isinstance(item, dict):
            raise SnapshotError(f"{where} holds an entry that is not an object")
        rel, sha, size, mtime = item.get("path"), item.get("sha256"), item.get("size"), item.get("mtime_ns")
        if not isinstance(rel, str) or not rel:
            raise SnapshotError(f"{where} has an entry with no path")
        if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
            raise SnapshotError(f"{where} has a bad SHA-256 for {rel!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotError(f"{where} has a bad size for {rel!r}")
        if item.get("mode") not in ("100644", "100755"):
            raise SnapshotError(f"{where} has a bad mode for {rel!r}")
        if isinstance(mtime, bool) or not isinstance(mtime, int) or not 0 <= mtime < _MAX_MTIME_NS:
            raise SnapshotError(f"{where} has a bad mtime for {rel!r}")
    return files


def load_manifest(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, Any] | None:
    """The manifest recorded in snapshot ``rev``, or ``None`` when it has none.

    A manifest that is there but unusable raises: it came from a remote, and
    carrying on as though the vault had never used payload mode is exactly how
    large files would end up in git.
    """
    if not rev:
        return None
    proc = git(
        git_dir, work_tree, ["show", f"{rev}:{PAYLOAD_MANIFEST}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (ValueError, RecursionError):
        raise SnapshotError(
            f"the payload manifest in {rev[:12]} is not valid JSON; refusing to continue"
        ) from None
    check_manifest_shape(data, f"the payload manifest in {rev[:12]}")
    return data


#: Code points HFS+ ignores in a name (the set git's own ``.git`` check strips):
#: ``.git<U+200C>`` is ``.git`` there.
_HFS_IGNORABLE = dict.fromkeys(
    [*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
)


def _component_is_git(part: str) -> bool:
    # An NTFS stream suffix is stripped first: Windows resolves
    # `.git::$INDEX_ALLOCATION/x` and `.git:$I30:x` to the real `.git`, and
    # those spellings passed this test as strings (REVIEW.sonnet-5.5 D10).
    # Everything before the first colon is what the filesystem reads.
    part = part.split(":", 1)[0]
    folded = part.translate(_HFS_IGNORABLE).rstrip(". ").casefold()
    return folded == ".git" or folded.startswith("git~")


def path_security_problem(rel: str) -> str | None:
    """Why *rel* may never be written by a restore, or ``None``.

    These are refusals of the whole manifest: a path that leaves the destination,
    or reaches ``.git`` (a ``core.fsmonitor`` or a hook there runs code on the
    next ``git status``) or the reserved payload directory.
    """
    if "\0" in rel:
        return "contains a NUL"
    parts = rel.split("/")
    if os.name == "nt":
        parts = [piece for part in parts for piece in part.split("\\")]
    for part in parts:
        if part in ("", ".", ".."):
            return "is absolute or has an empty, '.' or '..' component"
        if _component_is_git(part):
            return "reaches .git"
    if parts[0].translate(_HFS_IGNORABLE).rstrip(". ").casefold() == PAYLOAD_RESERVED_DIR:
        return f"is inside the reserved {PAYLOAD_RESERVED_DIR} directory"
    if re.match(r"^[A-Za-z]:", parts[0]) and os.name == "nt":
        return "names a drive"
    return None


def windows_name_problem(rel: str) -> str | None:
    """Why Windows cannot hold *rel* under that name, or ``None``.

    Legal on Linux, so not a security refusal: a restore on Windows skips and
    reports these, and a restore anywhere else is not affected.
    """
    for part in rel.split("/"):
        if re.search(r'[<>:"|?*\\\x00-\x1f]', part):
            return "has a character Windows forbids"
        if part != part.rstrip(". "):
            return "ends with a dot or a space (Windows would drop it)"
        if _WIN_RESERVED.match(part):
            return "is a reserved device name"
    return None


def validate_manifest(manifest: Any, into: Path) -> list[dict[str, Any]]:
    """The manifest's file entries, or ``SnapshotError`` if any is unsafe.

    A restore writes wherever a manifest entry says, and a manifest arrives from
    a remote that anyone with write access to the library could have altered.
    """
    files = check_manifest_shape(manifest)
    root = into.resolve()
    for item in files:
        rel = item["path"]
        problem = path_security_problem(rel)
        if problem is not None:
            raise SnapshotError(f"the payload manifest names a path that {problem}: {rel!r}")
        resolved = (into / rel).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise SnapshotError(
                f"the payload manifest names a path outside the restore: {rel!r}"
            ) from None
        # A symlink the checkout created (``tool -> .git``) makes a path with no
        # ".git" in it land inside the repository: ``tool/hooks/post-checkout``
        # would be an executable hook.  Inside the tree is not enough; the
        # protected directories are refused after links are followed.
        folded = os.path.normcase(str(resolved))
        for guarded in (root / ".git", root / PAYLOAD_RESERVED_DIR):
            prefix = os.path.normcase(str(guarded))
            if folded == prefix or folded.startswith(prefix + os.sep):
                raise SnapshotError(
                    f"the payload manifest names a path that reaches {guarded.name} "
                    f"through a link: {rel!r}"
                )
    return files


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    # One line, ASCII, no trailing newline: nothing between here and the object
    # store (text-mode pipes, filters) has a newline to translate.
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("ascii")


# What someone finds when they clone a vault with plain git: the large files are
# not in it, and nothing else says where they are.  Recorded beside the manifest.
PAYLOAD_README_TEXT = (
    "This snapshot keeps its large files outside git.\n"
    "\n"
    "manifest.json lists each of them (path, size, SHA-256); the bytes are stored on\n"
    "the Seafile server by hash, in the payload/objects directory next to this vault's\n"
    "remote. A plain `git clone` or `git checkout` therefore does not contain them.\n"
    "\n"
    "To get the complete tree back, verified file by file:\n"
    "\n"
    "    git seafile-snapshot --restore --remote <the vault's seafile:// URL> --into <dir>\n"
    "\n"
    "Do not edit or delete this directory; a restore and the payload tools read it.\n"
)


def add_manifest_to_index(
    git_dir: Path, work_tree: Path, manifest: dict[str, Any]
) -> None:
    """Record the manifest, and a note on reading it, in the vault index."""
    # The note has newlines, which a text-mode pipe would translate on Windows and
    # so give the same tree a different blob on each platform; hand git a file.
    note = git_dir / "payload-readme.tmp"
    note.write_bytes(PAYLOAD_README_TEXT.encode("ascii"))
    try:
        readme = git(
            git_dir, work_tree, ["hash-object", "-w", "--no-filters", str(note)],
            cwd=work_tree,
        ).stdout.strip()
    finally:
        note.unlink(missing_ok=True)
    blob = git(
        git_dir, work_tree, ["hash-object", "-w", "--no-filters", "--stdin"],
        cwd=work_tree, stdin=manifest_bytes(manifest).decode("ascii"),
    ).stdout.strip()
    for path, sha in ((PAYLOAD_MANIFEST, blob), (PAYLOAD_README, readme)):
        git(
            git_dir, work_tree,
            ["update-index", "--add", "--cacheinfo", f"100644,{sha},{path}"],
            cwd=work_tree,
        )


# -- the per-machine hash cache ------------------------------------------------
#
# Kept in the vault's own git dir and never read from a remote: a hash that came
# from a manifest anyone with write access could have written is not a hash this
# machine computed.  An entry is keyed by path and is only trusted while size,
# mtime *and inode* are unchanged -- a rename keeps size and mtime, and tiles
# extracted from one archive share both, so (size, mtime) alone would hand a
# renumbered file the hash of its predecessor.


def _machine_id() -> str:
    """A stable per-installation identifier for the hash cache's provenance.

    The cache is documented as per-machine (PAYLOAD D6), but the file lives in
    the vault, which two machines can share (a source on a NAS with the vault
    beside it): an entry's validity depends on the writing machine's clock and
    its probe of the share clock, so a reader on another machine cannot vouch
    for it (REVIEW.iq3_s D5).
    """
    import uuid

    return f"{socket.gethostname()}/{hex(uuid.getnode())}"


def load_machine_hash_cache(git_dir: Path) -> tuple[dict[str, dict[str, int | str]], str | None]:
    """The hash cache entries and the machine that wrote them.

    ``(entries, None)`` for a cache without a stamp (an older tool wrote it;
    its entries are adopted, as before), ``(entries, this machine)`` when the
    stamp matches, and ``({}, other machine)`` when it does not -- a reader
    cannot vouch for another machine's clock, so every file is hashed again.
    """
    try:
        data = json.loads((git_dir / _CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    if "entries" in data and isinstance(data["entries"], dict):
        machine = data.get("machine")
        if not isinstance(machine, str):
            return data["entries"], None
        return (data["entries"] if machine == _machine_id() else {}), machine
    return data, None


def load_hash_cache(git_dir: Path) -> dict[str, dict[str, int | str]]:
    """The hash cache entries, whatever machine wrote them (legacy reader)."""
    entries, _machine = load_machine_hash_cache(git_dir)
    return entries


def save_hash_cache(git_dir: Path, cache: dict[str, dict[str, int | str]]) -> None:
    tmp = git_dir / (_CACHE_NAME + ".tmp")
    document = json.dumps({"machine": _machine_id(), "entries": cache},
                          separators=(",", ":"))
    try:
        tmp.write_text(document, encoding="utf-8")
        os.replace(tmp, git_dir / _CACHE_NAME)
    except OSError:
        pass  # a cache that cannot be written only costs a rehash


#: Filesystem types whose clock is another machine's.
_NETWORK_FS_TYPES = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "afpfs", "webdav", "9p", "davfs", "fuse.sshfs",
     "fuse.rclone"}
)
#: A share whose clock is *ahead* by less than this is treated as in step: its
#: files then look newer than they are, which only costs a rehash.
_CLOCK_SKEW_NOISE_NS = 5_000_000_000
#: A share *behind* by more than this is applied.  Ignoring it is the unsafe
#: side (an edit looks older than it is), and the racy window is 2 s, so a
#: floor wider than a small fraction of it let a 4 s skew through.
_CLOCK_BEHIND_NOISE_NS = 250_000_000


_DARWIN_MOUNT_LINE = re.compile(r"^(?P<src>.+?) on (?P<mp>/.*) \((?P<opts>[^)]*)\)\s*$")


def _darwin_mount_is_network(resolved: str, mount_output: str | None = None) -> bool:
    """Whether the longest mount point containing *resolved* is a network filesystem.

    macOS has no ``/proc``; ``mount`` prints ``//user@host/share on /Volumes/x
    (smbfs, nodev, ...)``, the filesystem type being the first option.
    """
    if mount_output is None:
        try:
            mount_output = subprocess.run(
                [shutil.which("mount") or "/sbin/mount"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
    best, kind = "", ""
    for line in mount_output.splitlines():
        m = _DARWIN_MOUNT_LINE.match(line)
        if not m:
            continue
        mount = m.group("mp")
        inside = resolved == mount or resolved.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) >= len(best):
            best, kind = mount, m.group("opts").split(",")[0].strip()
    return kind in _NETWORK_FS_TYPES


def _is_network_fs(path: Path) -> bool:
    """Whether ``path`` lives on a filesystem another machine's clock stamps."""
    text = str(path)
    if os.name == "nt":
        if text.startswith(chr(92) * 2) and not text.startswith(chr(92) * 2 + "?" + chr(92)):
            return True
        try:
            import ctypes

            anchor = Path(text).anchor
            dll = "windll"  # a name, not an attribute: ctypes has none off Windows
            kernel32 = getattr(ctypes, dll).kernel32
            return bool(anchor) and kernel32.GetDriveTypeW(anchor) == 4
        except (OSError, AttributeError, ValueError):
            return False
    if sys.platform == "darwin":
        return _darwin_mount_is_network(os.path.realpath(text))
    try:
        best, kind = "", ""
        resolved = os.path.realpath(text)
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            head, _sep, tail = line.partition(" - ")
            fields = head.split()
            if len(fields) < 5 or not tail:
                continue
            mount = fields[4].replace("\\040", " ")
            inside = resolved == mount or resolved.startswith(mount.rstrip("/") + "/")
            if inside and len(mount) >= len(best):
                best, kind = mount, tail.split()[0]
        return kind in _NETWORK_FS_TYPES
    except (OSError, ValueError):
        return False


#: The probe file's name prefix.  A run killed between creating and removing it
#: leaves one behind, and the next snapshot would capture it as content.
_PROBE_PREFIX = ".grs-clock-probe-"
#: Debris older than this is removed by the next probe (generous: a clock that
#: is hours off must not make a live probe look stale).
_PROBE_DEBRIS_AGE_S = 6 * 3600


def is_clock_probe_debris(rel: str) -> bool:
    """True for a probe file at the source root, which never belongs in a snapshot."""
    return "/" not in rel and rel.startswith(_PROBE_PREFIX)


def _probe_fs_clock(directory: Path) -> int | None:
    """The filesystem's own "now", from a file created and removed at once; ``None`` if it cannot be."""
    try:
        for stale in directory.glob(_PROBE_PREFIX + "*"):
            try:
                if abs(time.time() - stale.stat().st_mtime) > _PROBE_DEBRIS_AGE_S:
                    stale.unlink()
            except OSError:
                pass
    except OSError:
        pass
    name = directory / f"{_PROBE_PREFIX}{os.getpid()}-{time.time_ns()}"
    try:
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return None
    try:
        os.close(fd)
        return os.stat(name).st_mtime_ns
    except OSError:
        return None
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


#: The racy window used where the clock cannot be trusted to be this machine's:
#: a share that cannot be probed, or a different device under the source.  A
#: file edited within this long of being hashed is hashed again (a cost), so a
#: clock that is off by less than this cannot hide an edit (a risk).
_UNPROBED_WINDOW_NS = 3600 * 1_000_000_000


def fs_clock_offset_ns(
    source: Path,
    *,
    is_network: Any = _is_network_fs,
    probe: Any = _probe_fs_clock,
    note: Any = print,
) -> int:
    """Just the offset of :func:`fs_clock_probe` (for callers that need nothing else)."""
    return int(fs_clock_probe(source, is_network=is_network, probe=probe, note=note)[0])


def fs_clock_probe(
    source: Path,
    *,
    is_network: Any = _is_network_fs,
    probe: Any = _probe_fs_clock,
    note: Any = print,
) -> tuple[int, bool]:
    """``(offset_ns, unprobed)``: the source filesystem's clock minus this machine's.

    ``unprobed`` is True on a network share whose clock could not be measured
    (no write access): the machine-clock rule is then the one thing known to be
    wrong there, so the caller widens the racy window instead of using it.

    The racy rule (D6) needs "now" in the clock that stamps the files.  On a
    local disk that is this machine's; on a share it is the server's, and a
    server running an hour behind makes every freshly edited file look old
    enough to trust -- a same-size edit within one timestamp granule then keeps
    its stale hash.  A probe file measures the difference; it is made only on a
    network filesystem, so a local source tree is never written to.
    """
    if not is_network(source):
        return 0, False
    before = time.time_ns()
    stamp = probe(source)
    if stamp is None:
        note("Note: the source is on a network share and its clock could not be measured "
             "(no write access); a file is trusted from the hash cache only when it is more "
             "than an hour older than its hash, and --payload-rehash re-checks everything")
        return 0, True
    offset = int(stamp) - before
    return (0 if -_CLOCK_BEHIND_NOISE_NS < offset < _CLOCK_SKEW_NOISE_NS else offset), False


def manifest_snapshot_id(entries: list[dict[str, Any]]) -> str:
    """The derived id of a file list: path, size, mode and hash, never a time or user text."""
    ident = hashlib.sha256()
    for item in entries:
        ident.update(f"{item['path']}\0{item['size']}\0{item['mode']}\0{item['sha256']}\n".encode())
    return "p-" + ident.hexdigest()[:16]


def build_manifest(
    candidates: dict[str, Candidate],
    source: Path,
    cache: dict[str, dict[str, int | str]],
    min_size: int,
    *,
    quiet: bool = False,
    rule: str = "size",
    ratio: float | None = None,
    clock_offset_ns: int = 0,
    racy_window_ns: int | None = None,
    root_dev: int | None = None,
    checkpoint: Callable[[dict[str, dict[str, int | str]]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, int | str]]]:
    """Hash every payload file; return ``(manifest, updated cache)``.

    ``checkpoint`` receives the cache so far about once a minute, so a pass that
    is killed (not merely interrupted) keeps what it hashed: a 131 GiB tree takes
    minutes to hash and the result is only useful as a whole.

    ``clock_offset_ns`` is the source filesystem's clock minus this machine's
    (:func:`fs_clock_offset_ns`).  The racy rule compares a file's mtime with the
    moment it was hashed, and an mtime is written by the *filesystem's* clock, so
    the moment is recorded in that clock's domain.  ``racy_window_ns`` is how much
    older than its hash a file must be to be trusted; a file on a device other
    than ``root_dev`` (a mount under the source) gets the wide window, because
    its clock was not probed.  The cache key also holds the device and the
    inode change time, so an in-place edit that puts the old mtime back (which
    still moves ``ctime``) is a new file, not a trusted one.
    """
    entries: list[dict[str, Any]] = []
    fresh: dict[str, dict[str, int | str]] = {}
    total = len(candidates)
    stale = 0
    # Files whose filesystem reports no change time distinct from the mtime
    # (the FAT family; any volume that answers the query with the mtime).
    coarse = [0]

    def vouched(rel: str) -> tuple[dict[str, int | str], bool]:
        """The cache entry for *rel* and whether it may stand in for a fresh hash."""
        size, mtime_ns, _mode, ino, *rest = candidates[rel]
        dev = rest[0] if rest else None
        ctime_ns = rest[1] if len(rest) > 1 else None
        old = cache.get(rel) or {}
        hashed_at = old.get("at")
        window = _RACY_WINDOW_NS if racy_window_ns is None else racy_window_ns
        if root_dev is not None and dev is not None and dev != root_dev:
            window = max(window, _UNPROBED_WINDOW_NS)
        if mtime_ns % 1_000_000_000 == 0:
            # A whole second is a coarse clock (FAT's two seconds, HFS+'s one):
            # an edit can share the stamp of the one that was hashed, so the
            # margin doubles rather than the entry being distrusted forever,
            # which re-read the whole tree on every run of such a volume.
            window *= 2
        if ctime_ns is not None and ctime_ns == mtime_ns:
            # The filesystem has no change time of its own: the FAT family
            # keeps one timestamp in the directory entry, and a volume or API
            # that reports the modification time back as the change time does
            # the same.  A same-size in-place rewrite with the mtime put back
            # then leaves *every* field of the key unchanged and the cache
            # vouches for the old hash (measured on vfat, REVIEW.iq3_s D2).
            # Treat it like an unprobed share: the one-hour margin, so a
            # scheduled run in steady state re-hashes nothing but an edit
            # inside the hour cannot hide.
            window = max(window, _UNPROBED_WINDOW_NS)
            coarse[0] += 1
        trusted = (
            old.get("size") == size
            and old.get("mtime_ns") == mtime_ns
            and old.get("ino") == ino
            and isinstance(old.get("sha256"), str)
            and isinstance(hashed_at, int)
            and mtime_ns < hashed_at - window
            # A timestamp taken in this machine's clock says nothing about a
            # skewed share's, so it cannot vouch for a file there.
            and (not clock_offset_ns or old.get("fs") is True)
            # ctime and device are new to the key: an entry written before them
            # adopts the current values (below); one that has them must match.
            and (ctime_ns is None or old.get("ctime") in (None, ctime_ns))
            and (dev is None or old.get("dev") in (None, dev))
        )
        return old, trusted

    # What will really be read, so the progress line's rate and estimate describe
    # the hashing and not the files the cache vouches for.
    to_hash = 0 if quiet else sum(
        candidates[rel][0] for rel in candidates if not vouched(rel)[1]
    )
    hashed = 0
    started_at = last_pulse = last_checkpoint = time.monotonic()
    for index, rel in enumerate(sorted(candidates), 1):
        size, mtime_ns, mode, ino, *rest = candidates[rel]
        dev = rest[0] if rest else None
        ctime_ns = rest[1] if len(rest) > 1 else None
        old, trusted = vouched(rel)
        if trusted:
            digest = str(old["sha256"])
            fresh[rel] = dict(old)
            if ctime_ns is not None:
                fresh[rel]["ctime"] = ctime_ns
            if dev is not None:
                fresh[rel]["dev"] = dev
        else:
            stale += isinstance(old.get("sha256"), str)
            if not quiet:
                print(f"hashing [{index}/{total}] {rel} ({human_bytes(size)})", flush=True)
            started = time.time_ns() + clock_offset_ns
            digest = _sha256_file(source / rel)
            hashed += size
            now = time.monotonic()
            if not quiet and now - last_pulse >= _PULSE_SECONDS:
                last_pulse = now
                print(_progress_line(hashed, to_hash, now - started_at, "hashed"), flush=True)
            if checkpoint is not None and now - last_checkpoint >= _CHECKPOINT_SECONDS:
                last_checkpoint = now
                checkpoint({**cache, **fresh, rel: {
                    "size": size, "mtime_ns": mtime_ns, "ino": ino, "sha256": digest,
                    "at": started, **({"ctime": ctime_ns} if ctime_ns is not None else {}),
                    **({"dev": dev} if dev is not None else {}),
                    **({"fs": True} if clock_offset_ns else {}),
                }})
            fresh[rel] = {
                "size": size, "mtime_ns": mtime_ns, "ino": ino, "sha256": digest, "at": started,
            }
            if ctime_ns is not None:
                fresh[rel]["ctime"] = ctime_ns
            if dev is not None:
                fresh[rel]["dev"] = dev
            if clock_offset_ns:
                fresh[rel]["fs"] = True
        entries.append(
            {"path": rel, "size": size, "mode": mode, "sha256": digest, "mtime_ns": mtime_ns}
        )
    if coarse[0] and not quiet:
        print(
            f"Note: {coarse[0]} file(s) are on a filesystem with no change time of its own "
            "(FAT/exFAT, or a volume that answers with the modification time); a cached hash "
            "for them is trusted only after an hour, and --payload-rehash re-checks everything",
            flush=True,
        )
    if stale and not quiet:
        print(
            f"Note: {stale} file(s) were hashed again because their size, timestamp or inode "
            "changed since the last run (or are too recent to trust); a copy tool that resets "
            "timestamps does this to every file",
            flush=True,
        )
    manifest = {
        "format": PAYLOAD_FORMAT,
        "snapshot": manifest_snapshot_id(entries),
        "min_size": min_size,
        "min_git_remote_seafile": PAYLOAD_MIN_TOOL,
        "rule": rule,
        "files": entries,
    }
    if rule == "auto" and ratio is not None:
        # Which cut-off produced this snapshot, recorded beside the rule it
        # belongs to: two machines whose cut-offs differ move files in the
        # 4-64 MiB band between git and the payload on alternate runs, and
        # the fresh-vault guard can only see the difference if it is here
        # (ROADMAP T6).  check_manifest_shape validates it.
        manifest["ratio"] = ratio
    return manifest, fresh


# -- Payload mode --------------------------------------------------------------
#
# Large immutable files do not go through git at all.  Each one is stream-hashed
# (SHA-256) and stored on the server *by its hash*:
#
#     <path>/payload/objects/<first two hex>/<sha256>
#
# inside the vault's own remote directory (the helper ignores what it does not
# know; measured against its push, clone, doctor, gc --tail and gc).  The vault
# commit carries a *manifest* -- path, mode, size, SHA-256, mtime -- so git keeps
# what git is good at (the file list, the checksums, the history of both) and
# Seafile keeps what Seafile is good at (big files, shared blocks).
#
# Why by hash: an object is never replaced by different bytes, so a snapshot is
# just a manifest and costs nothing to keep; a rename or a duplicate uploads
# nothing; concurrent writers cannot conflict (the same name always has the same
# content); and what the server holds depends on no file name, so the server's
# name rules (NFC, ``"`` -> ``%22``, 255 bytes) never apply.
#
# What makes "the same name always has the same content" true is that an object
# is written under a temporary name, hashed *as it is sent*, and renamed to its
# hash only when the bytes that went out are the bytes the name promises.
#
# Specification, requirements and the review that shaped this: PAYLOAD.md.

#: Where the manifest lives inside the snapshot tree.  The directory is reserved:
#: nothing under it is ever staged from the source.
PAYLOAD_RESERVED_DIR = ".seafile-payload"
PAYLOAD_MANIFEST = PAYLOAD_RESERVED_DIR + "/manifest.json"
PAYLOAD_README = PAYLOAD_RESERVED_DIR + "/README.txt"
PAYLOAD_FORMAT = 2
_PAYLOAD_HASH_BLOCK = 8 * 1024 * 1024
#: A cached hash is trusted only if the file's mtime is at least this much older
#: than the moment *this machine* hashed it (git's "racily clean" rule): a
#: same-size edit inside the timestamp granularity would otherwise look untouched.
_RACY_WINDOW_NS = 2_000_000_000
_CACHE_NAME = "payload-cache.json"
#: Objects are uploaded under ``<sha>.part-<token>`` and renamed; retention
#: collects an interrupted run's temporaries through its two phases (D9).
_PART_SUFFIX = ".part-"
#: The ``auto`` selection rule (PAYLOAD.md D1): a file is payload when it is at
#: least ``_AUTO_UPPER`` bytes, or at least ``_AUTO_LOWER`` bytes *and* a sample
#: of it does not compress.
_AUTO_UPPER = 64 * 1024 ** 2
_AUTO_LOWER = 4 * 1024 ** 2
_AUTO_INCOMPRESSIBLE = 0.9
_AUTO_SAMPLE_CHUNK = 256 * 1024
#: Seconds between progress lines while objects upload (an upload of a large tree
#: runs for an hour, and a line per file says nothing about how much is left).
_PULSE_SECONDS = 60.0
#: Seconds between saves of the hash cache while a tree is hashed: what a killed pass
#: loses is at most this much work.
_CHECKPOINT_SECONDS = 60.0
_SIZE_RE = re.compile(r"^\s*(\d+)\s*([kmgt]?)(?:i?b)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
_MAX_MTIME_NS = 2 ** 62

#: ``(size, mtime_ns, mode, inode, device, ctime_ns)`` for one candidate payload file.
Candidate = tuple[int, int, str, int, int, int]


def parse_size(text: str) -> int:
    """``"64M"``, ``"1g"``, ``"500k"`` or ``"1048576"`` as a byte count (0 = off).

    Whole numbers only: ``-1`` would select every file (empty ones too) and
    ``0.5`` would read as "off" without saying so.
    """
    match = _SIZE_RE.match(text)
    if not match:
        raise SnapshotError(
            f"not a size: {text!r} (examples: 64M, 1G, 500k, 1048576, or auto). A bare "
            "--payload-min-size takes the next argument as its value: put SOURCE first, "
            "or write --payload-min-size=auto"
        )
    return int(match.group(1)) * _SIZE_UNITS[match.group(2).lower()]


def payload_location(remote: str) -> tuple[str, str, str | None]:
    """``(library, payload_dir, server_url)`` for a vault remote URL."""
    from git_remote_seafile.url import parse_seafile_url

    try:
        parsed = parse_seafile_url(remote)
    except ValueError as exc:
        raise SnapshotError(f"payload mode needs a seafile:// remote: {exc}") from None
    return parsed.library_name, parsed.repo_path.rstrip("/") + "/payload", parsed.server_url


def object_dir(sha: str) -> str:
    return f"objects/{sha[:2]}"


def _open_client(server_url: str | None) -> Any:
    try:
        from git_remote_seafile.client import SeafileClient
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SnapshotError(f"payload mode needs the git_remote_seafile package: {exc}") from None
    return SeafileClient(server_url=server_url)


def _api(call: Any, what: str) -> Any:
    """Run a server call, turning transport and API errors into a snapshot error."""
    try:
        return call()
    except SnapshotError:
        raise
    except Exception as exc:  # SeafileAPIError, requests errors, JSON errors
        raise SnapshotError(f"{what} failed: {type(exc).__name__}: {exc}") from None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(_PAYLOAD_HASH_BLOCK)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        # A file another process holds open exclusively (a database, a GIS
        # project on Windows) cannot be read; a backup that skipped it quietly
        # would be worse than one that stops and says which.
        raise SnapshotError(f"cannot read {path}: {exc}") from None
    return digest.hexdigest()


class _HashingFile:
    """A read-only file object that hashes the bytes handed to the uploader.

    The uploader reads the file itself, possibly in chunks and possibly twice
    (a retried chunk seeks back).  Only a *forward-contiguous* first read is
    hashed, so the digest is that of the bytes the server received, in order.
    ``complete`` says whether every byte passed through.
    """

    def __init__(self, fh: Any, size: int) -> None:
        self._fh = fh
        self._size = size
        self._hash = hashlib.sha256()
        self._hashed = 0
        #: True once any byte was read a second time (a retried chunk).  The
        #: digest then describes the *first* attempt, not what the server kept.
        self.rewound = False

    def read(self, size: int = -1) -> bytes:
        at = self._fh.tell()
        data = self._fh.read(size)
        if data and at < self._hashed:
            self.rewound = True
        if data and at == self._hashed:
            self._hash.update(data)
            self._hashed += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return int(self._fh.seek(offset, whence))

    def tell(self) -> int:
        return int(self._fh.tell())

    @property
    def complete(self) -> bool:
        return self._hashed == self._size

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _sample_ratio(path: Path, size: int) -> float | None:
    """Compressed / raw over three 256 KiB chunks of *path* under a fast deflate.

    ``None`` when nothing could be read: unreadable here is unreadable later, and
    git reports it.  Incompressible data comes out a little *above* 1 (stored
    blocks cost bytes), up to about 1.06.
    """
    raw = comp = 0
    try:
        with open(path, "rb") as fh:
            for offset in sorted({0, max(0, size // 2 - _AUTO_SAMPLE_CHUNK // 2),
                                  max(0, size - _AUTO_SAMPLE_CHUNK)}):
                fh.seek(offset)
                chunk = fh.read(_AUTO_SAMPLE_CHUNK)
                raw += len(chunk)
                comp += len(zlib.compress(chunk, 1))
    except OSError:
        return None
    return comp / raw if raw > 0 else None


def _incompressible(path: Path, size: int, ratio: float = _AUTO_INCOMPRESSIBLE) -> bool:
    """Whether a sample of *path* keeps at least *ratio* of its size under deflate
    (``--payload-auto-ratio``; 0.9 unless tuned)."""
    measured = _sample_ratio(path, size)
    return measured is not None and measured >= ratio


def new_survey() -> dict[str, Any]:
    """An empty record for :func:`find_payload_files`: ``small`` and ``big`` are
    ``[count, bytes]``; ``mid`` is ``(size, sample ratio, path)`` per 4-64 MiB file."""
    return {"small": [0, 0], "big": [0, 0], "mid": []}


_CLOUD_PLACEHOLDER_ATTRS = 0x1000 | 0x400000  # OFFLINE | RECALL_ON_DATA_ACCESS


def find_payload_files(
    source: Path, excludes: list[str], min_size: int, auto: bool = False,
    ratio: float = _AUTO_INCOMPRESSIBLE, survey: dict[str, Any] | None = None,
    sampled: dict[str, float] | None = None,
) -> dict[str, Candidate]:
    """``{relative path: (size, mtime_ns, mode, inode)}`` for every payload file.

    A payload file is a *regular* file that no ``--exclude`` removes and that the
    rule selects: size alone (``size >= min_size``), or with *auto* the rule in
    PAYLOAD.md D1.  Symlinks stay with git (they are a few bytes and git stores
    them exactly), as does everything the rule leaves out.  A Windows cloud
    placeholder (OneDrive "files on demand") is refused by name: reading it would
    download it.

    With *auto* and a *survey* (see :func:`new_survey`) it also records what the
    rule saw -- the files under the floor and the sample of every file in the
    4-64 MiB band, not only those it kept -- so the placement report and its
    what-if cost no second pass over the tree.
    """
    found: dict[str, Candidate] = {}
    placeholders: list[str] = []
    floor = _AUTO_LOWER if auto else min_size
    for size, rel in _walk_sizes(source, excludes):
        if rel == PAYLOAD_RESERVED_DIR or rel.startswith(PAYLOAD_RESERVED_DIR + "/"):
            continue
        try:
            st = os.lstat(source / rel)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if getattr(st, "st_file_attributes", 0) & _CLOUD_PLACEHOLDER_ATTRS:
            # Before the size floor (ROADMAP S4): a placeholder below the
            # floor used to fall through to `git add`, which hydrates it (or
            # fails) on a tree whose operator was told placeholders are
            # refused.  One extra lstat per file -- the walk already did one.
            placeholders.append(rel)
            continue
        if size < floor:
            if survey is not None and auto:
                survey["small"][0] += 1
                survey["small"][1] += size
            continue
        if auto and size < _AUTO_UPPER:
            # *sampled* carries an earlier pass's ratios, so choosing a new cut-off
            # does not read the band again.
            measured = sampled[rel] if sampled and rel in sampled else _sample_ratio(source / rel, size)
            if survey is not None and measured is not None:
                survey["mid"].append((size, measured, rel))
            if measured is None or measured < ratio:
                continue
        if survey is not None and auto and size >= _AUTO_UPPER:
            survey["big"][0] += 1
            survey["big"][1] += size
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        found[rel] = (st.st_size, st.st_mtime_ns, mode, st.st_ino, st.st_dev,
                      change_time_ns(source / rel, st))
    if placeholders:
        shown = ", ".join(placeholders[:5]) + (" ..." if len(placeholders) > 5 else "")
        raise SnapshotError(
            f"{len(placeholders)} payload file(s) are cloud placeholders (not stored locally), "
            f"and reading them would download them: {shown}. Make them available offline, "
            "or --exclude them"
        )
    return found


# -- the server side -----------------------------------------------------------


def _remote_objects(client: Any, rid: str, base: str, shas: set[str]) -> dict[str, int]:
    """``{sha: size}`` of the objects among *shas* the server already holds.

    Lists only the fan-out directories those hashes fall in (at most 256).
    """
    held: dict[str, int] = {}
    for prefix in sorted({sha[:2] for sha in shas}):
        entries = _api(
            lambda p=prefix: client.list_dir(rid, f"{base}/objects/{p}"),
            f"listing {base}/objects/{prefix}",
        )
        for entry in entries:
            name = entry.get("name")
            if isinstance(name, str) and name in shas and entry.get("type") != "dir":
                size = entry.get("size", -1)
                held[name] = size if isinstance(size, int) and not isinstance(size, bool) else -1
    return held


def _unchanged(source: Path, rel: str, size: int, mtime_ns: int) -> bool:
    try:
        st = os.lstat(source / rel)
    except OSError:
        return False
    return st.st_size == size and st.st_mtime_ns == mtime_ns


def _commit_object(
    client: Any, rid: str, directory: str, temp: str, sha: str, size: int
) -> bool:
    """Give the verified temporary file its hash name.

    The server's rename is *create-or-duplicate*: on a taken name it answers
    success and leaves the target untouched with a ``<sha> (1)`` copy beside it
    (measured on CE 13.0.28 -- the stub models it since 0.9.13; the old code
    assumed the refusal the stub used to answer, so a damaged object blocked
    every later snapshot that needed the content, each retry leaving another
    full-size copy; REVIEW.sonnet-5.5 E7).  So after every rename the directory
    is listed once, and:

    * no duplicate -- the rename landed on a free name, done;
    * a duplicate and the existing ``<sha>`` at the *right* size -- another
      writer stored the same bytes a moment ago (the temporary's digest was
      proved equal to ``sha`` before any rename), so the copy is dropped;
    * a duplicate and ``<sha>`` at the *wrong* size -- damage, and the R3
      carve-out in action: the stale object is deleted and the rename retried
      once, with the duplicate copies swept either way.
    """
    marker = f"{sha} ("
    stale = f"{directory}/{sha}"
    temp_path = f"{directory}/{temp}"

    def _duplicates(entries: list) -> list[str]:
        return [
            str(e.get("name"))
            for e in entries
            if isinstance(e, dict) and str(e.get("name", "")).startswith(marker)
        ]

    def _sweep(dups: list[str]) -> None:
        for dup in dups:
            client.delete_entry(rid, f"{directory}/{dup}")

    if client.rename_entry(rid, temp_path, sha):
        entries = client.list_dir(rid, directory)
        dups = _duplicates(entries)
        if not dups:
            return True
        existing = next(
            (e for e in entries if isinstance(e, dict) and e.get("name") == sha), None
        )
        if existing is not None and existing.get("size") == size:
            # Same bytes under the name already (content addressing): the
            # duplicate copy the rename left is redundant, not evidence.
            _sweep(dups)
            return True
        # A wrong-size object under the hash name is damage; replace it.  The
        # create-or-duplicate rename consumed the temporary into the first
        # duplicate, so that copy is the bytes to move onto the freed name.
        if not client.delete_entry(rid, stale):
            return False
        if not client.rename_entry(rid, f"{directory}/{dups[0]}", sha):
            return False
        _sweep(_duplicates(client.list_dir(rid, directory)))
        return True
    # The rename was refused outright (a deployment that does fail on a taken
    # name): fall back to judging the existing object by its size.
    for entry in client.list_dir(rid, directory):
        if entry.get("name") == sha and entry.get("size") == size:
            client.delete_entry(rid, temp_path)
            return True
    if not client.delete_entry(rid, stale):
        return False
    if not client.rename_entry(rid, temp_path, sha):
        return False
    _sweep(_duplicates(client.list_dir(rid, directory)))
    return True


def _progress_line(done: int, total: int, elapsed: float, verb: str = "uploaded") -> str:
    """``progress: 41.2 GiB of 292.0 GiB uploaded (14%), 52 MiB/s, about 1h20m left``.

    *verb* names the work (uploaded, downloaded, hashed, checked), so every long
    phase reports in the same words.
    """
    rate = done / 1048576 / elapsed if elapsed > 0 else 0.0
    left = f", about {_fmt_elapsed((total - done) / 1048576 / rate)} left" if rate > 0 else ""
    percent = done * 100 // total if total else 100
    return (f"progress: {human_bytes(done)} of {human_bytes(total)} {verb} ({percent}%), "
            f"{rate:.0f} MiB/s{left}")


def _upload_all(
    client: Any, rid: str, server_url: str | None, base: str, source: Path,
    todo: dict[str, dict[str, Any]], jobs: int, skip_changing: bool = False,
) -> list[str]:
    """Upload each object in *todo* (``sha -> manifest entry``), *jobs* at once.

    Returns the paths left out because they changed (always empty unless
    *skip_changing*, which turns "a file changed" from an error into a list the
    caller drops from the snapshot; a failed upload is still an error).

    Largest first.  Measured on the real server: one upload runs at ~32 MiB/s and
    three at once reach ~53 MiB/s in aggregate, so a small pool helps and a large
    one does not.  Directories are created up front in this thread; each worker
    thread then uses a client of its own (a ``requests`` session is not meant to
    be shared).  The first *server* failure stops the queue and is raised once the
    running uploads have finished -- the next run resumes from what landed.  A file
    that changed (or vanished) after it was hashed is that file's problem, not the
    server's: the rest of the queue still goes up, so one live file, such as a
    database something keeps writing, does not throw away hours of upload, and the
    run then fails naming every such file.

    Each object goes up under a temporary name through a hashing reader and is
    renamed to its hash only if the bytes sent are the bytes the name promises.
    A file edited after it was hashed (or rewritten in place without touching its
    mtime, which a memory-mapped writer on Windows can do) therefore never ends up
    under another content's name -- the failure that otherwise survives every
    later run, because "present at the right size" is all a listing can prove.
    """
    for prefix in sorted({sha[:2] for sha in todo}):
        _api(lambda p=prefix: client.mkdir_p(rid, f"{base}/objects/{p}"),
             f"creating {base}/objects/{prefix}")
    order = sorted(todo, key=lambda sha: (-todo[sha]["size"], todo[sha]["path"]))
    local = threading.local()
    lock = threading.Lock()
    counter = [0]
    failed: list[str] = []
    changed: list[str] = []
    changed_paths: list[str] = []
    total_bytes = sum(todo[sha]["size"] for sha in order)
    uploaded = [0]
    started = time.monotonic()
    last_pulse = [started]

    def one(sha: str) -> None:
        if failed:
            return
        item = todo[sha]
        rel, size = item["path"], item["size"]
        if not _unchanged(source, rel, size, item["mtime_ns"]):
            changed.append(f"{rel}: changed since it was hashed; run again")
            changed_paths.append(rel)
            return
        with lock:
            counter[0] += 1
            print(
                f"uploading [{counter[0]}/{len(order)}] {rel} ({human_bytes(size)})",
                flush=True,
            )
        directory = f"{base}/{object_dir(sha)}"
        temp = f"{sha}{_PART_SUFFIX}{os.urandom(4).hex()}"
        try:
            worker = getattr(local, "client", None)
            if worker is None:
                worker = local.client = _open_client(server_url)
            try:
                fh = open(source / rel, "rb")
            except OSError as exc:
                changed.append(f"{rel}: cannot be read any more ({exc}); run again")
                changed_paths.append(rel)
                return
            with fh:
                reader = _HashingFile(fh, size)
                ok = worker.upload_file(rid, directory, temp, reader)
            if not ok:
                failed.append(f"{rel}: the server did not accept the upload")
                return
            # A retried chunk means the digest describes the first attempt, not the
            # bytes the server kept: only a fresh read of the whole file, equal to the
            # name and with an unchanged stat, vouches for the object then.
            if reader.rewound:
                clean = (_sha256_file(source / rel) == sha
                         and _unchanged(source, rel, size, item["mtime_ns"]))
            else:
                clean = reader.complete and reader.hexdigest() == sha
            if not clean:
                worker.delete_entry(rid, f"{directory}/{temp}")
                changed.append(f"{rel}: changed while it was being uploaded; run again")
                changed_paths.append(rel)
                return
            if not _commit_object(worker, rid, directory, temp, sha, size):
                worker.delete_entry(rid, f"{directory}/{temp}")
                failed.append(f"{rel}: the server would not name the object")
            else:
                with lock:
                    uploaded[0] += size
                    now = time.monotonic()
                    if now - last_pulse[0] >= _PULSE_SECONDS:
                        last_pulse[0] = now
                        print(_progress_line(uploaded[0], total_bytes, now - started), flush=True)
        except OSError as exc:
            failed.append(f"{rel}: {exc}")
        except Exception as exc:  # SeafileAPIError, requests errors: reported once, below
            failed.append(f"{rel}: {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(one, order))
    if failed:
        more = (f"; {len(changed)} file(s) also changed while the run was in progress"
                if changed else "")
        raise SnapshotError(
            f"{len(failed)} payload upload(s) failed (the next run resumes); first: {failed[0]}{more}"
        )
    if changed and not skip_changing:
        raise SnapshotError(
            f"{len(changed)} payload file(s) changed while the run was in progress; every "
            f"other file was uploaded and the next run resumes. First: {changed[0]}. Stop "
            "whatever writes them, leave them out with --exclude, or pass "
            "--payload-skip-changing to snapshot the rest without them"
        )
    return sorted(set(changed_paths))


def verify_payload_before_push(
    git_dir: Path,
    source: Path,
    remote: str,
    remote_tip: str | None,
    tip: str,
    manifest: dict[str, Any] | None,
    jobs: int = 3,
) -> None:
    """Make sure every object a commit about to be pushed names is on the server.

    A run lists the server once, near its start; hours later (or, for an
    earlier un-pushed snapshot, days) the objects it relied on may be gone.  This
    re-lists, just before the push, every object named by every manifest in
    ``remote_tip..tip``.  Missing ones that the current manifest names are
    re-uploaded from the source; anything else is a snapshot that could not be
    restored, and the push is refused naming it.
    """
    span = f"{remote_tip}..{tip}" if remote_tip else tip
    listed = git(git_dir, source, ["rev-list", span], cwd=source, check=False)
    if listed.returncode != 0:
        # An error is not "no commits": this is the guard that stops a push from
        # naming an object the server lost, and it must not fail open.
        detail = (listed.stderr or "").strip() or f"exit {listed.returncode}"
        raise SnapshotError(
            f"could not list the commits about to be pushed ({detail}); refusing to push "
            "without checking their payload objects on the server"
        )
    commits = [c for c in listed.stdout.split() if c]
    if not commits:
        return
    specs = "".join(f"{c}:{PAYLOAD_MANIFEST}\n" for c in commits)
    checked = git(git_dir, source, ["cat-file", "--batch-check"], cwd=source, stdin=specs).stdout
    needed: dict[str, int] = {}
    owners: dict[str, str] = {}
    for line in checked.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        body = git(git_dir, source, ["cat-file", "blob", parts[0]], cwd=source).stdout
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise SnapshotError(
                f"a payload manifest ({parts[0][:12]}) in the commits about to be pushed is "
                "unreadable; refusing to push it"
            ) from None
        for item in check_manifest_shape(data):
            needed[item["sha256"]] = item["size"]
            owners.setdefault(item["sha256"], item["path"])
    if not needed:
        return
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    held = _remote_objects(client, rid, base, set(needed))
    missing = {sha for sha, size in needed.items() if held.get(sha) != size}
    if not missing:
        return
    current = {item["sha256"]: item for item in (manifest or {}).get("files", [])}
    redo = {
        sha: current[sha] for sha in missing
        if sha in current and _unchanged(source, current[sha]["path"],
                                         current[sha]["size"], current[sha]["mtime_ns"])
    }
    if redo:
        print(f"re-uploading {len(redo)} payload object(s) that went missing before the push",
              flush=True)
        _upload_all(client, rid, server_url, base, source, redo, jobs)
        after = _remote_objects(client, rid, base, set(redo))
        missing -= {sha for sha, item in redo.items() if after.get(sha) == item["size"]}
    if missing:
        sample = ", ".join(sorted(owners[sha] for sha in missing)[:5])
        raise SnapshotError(
            f"{len(missing)} payload object(s) named by snapshots about to be pushed are "
            f"missing from the server and the source no longer has them (e.g. {sample}); a "
            "restore of those snapshots would fail. Not pushing"
        )


def verify_payload_after_push(
    source: Path,
    remote: str,
    manifest: dict[str, Any] | None,
    jobs: int = 3,
) -> None:
    """Look once more, after the push, for the objects the new manifest names.

    The check before the push cannot see a ``--payload-delete`` on another
    machine that removes an object between that listing and the ref landing: the
    retention run takes no remote lock, and a lock would not help, because the
    push verifies before it takes the lock.  The window is a few minutes at
    most, so one more listing here finds what it took.  An object the source
    still holds as it was is uploaded again; anything else is reported, because
    the snapshot just pushed cannot be restored without it.
    """
    files = (manifest or {}).get("files") or []
    if not files:
        return
    needed = {item["sha256"]: item for item in files}
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    held = _remote_objects(client, rid, base, set(needed))
    missing = {sha for sha, item in needed.items() if held.get(sha) != item["size"]}
    if not missing:
        return
    redo = {
        sha: needed[sha] for sha in missing
        if _unchanged(source, needed[sha]["path"], needed[sha]["size"], needed[sha]["mtime_ns"])
    }
    if redo:
        print(f"re-uploading {len(redo)} payload object(s) that went missing during the push "
              "(a --payload-delete on another machine?)", flush=True)
        _upload_all(client, rid, server_url, base, source, redo, jobs)
        after = _remote_objects(client, rid, base, set(redo))
        missing -= {sha for sha, item in redo.items() if after.get(sha) == item["size"]}
    if missing:
        sample = ", ".join(sorted(needed[sha]["path"] for sha in missing)[:5])
        raise SnapshotError(
            f"the snapshot was pushed, but {len(missing)} payload object(s) it names are missing "
            f"from the server and the source no longer has them as they were (e.g. {sample}); "
            "a restore of this snapshot would fail. Take a new snapshot once the files are back"
        )


# -- Payload retention ---------------------------------------------------------
#
# A snapshot run never deletes an object.  This is the one place that does, and
# it is built around the one way it could lose data: an object that *looks*
# unreferenced because the reader did not see every manifest that names it, or
# because a run that is about to name it has not pushed yet.
#
# So, in order of how much each guard matters:
#
# 1. **Two phases.**  A run with ``--payload-delete`` does not delete what it
#    finds unreferenced; it *marks* it, with this machine's clock, in a small
#    state file on the server.  A later run deletes an object only if it has been
#    marked unreferenced for the whole grace period *and is still unreferenced*.
#    An object a run reused in the meantime is referenced again by the time the
#    second run looks -- unless that run never pushed, in which case nothing names
#    it and it is garbage.  No clock comparison with the server is involved.
# 2. **Every manifest.**  It reads the manifest of every commit reachable from
#    every ref of the remote -- and of the local vault, whose un-pushed snapshots
#    may name objects already uploaded.  A manifest it cannot read, or whose
#    format it does not know, stops it; it never treats "cannot parse" as "names
#    nothing".
# 3. **No refs it cannot see.**  A ref the helper cannot list (a Seafile conflict
#    copy) would hide a snapshot, so their presence stops it.
# 4. **Only exact object names**, nothing else in the directory.
#
# Deleting frees quota at once; disk is freed only after the library's history
# expires and the server's GC runs (SNAPSHOT.md section 15.1).

#: How often the deletion loop looks at the remote's refs again: every this many
#: objects, or this many seconds, whichever comes first.
_RECHECK_EVERY = 25
_RECHECK_SECONDS = 10.0

_OBJECT_NAME = re.compile(r"^[0-9a-f]{64}$")


def _grace_now(client: Any) -> float:
    """The clock the grace period runs on: the server's ``Date`` estimate.

    The mark and the delete are different runs, often on different days, and
    the wall clock of *whichever machine happens to run the delete* decided
    both ends: a machine whose clock jumped forward eight days between the
    runs deleted an object that was marked seconds before (measured,
    REVIEW.sonnet-5.5 D3).  The server's time estimate cannot jump with
    either machine's wall clock, which is the whole property the grace period
    needs.  Entries written by older versions carry local-clock stamps; the
    error is the one-off skew between that machine's clock and the server's,
    the same bounded quantity §6.1 already records as accepted.
    """
    try:
        return float(client.get_server_time())
    except Exception:
        return time.time()

#: An object, or the temporary an interrupted upload left beside it.
_COLLECTABLE_NAME = re.compile(r"^[0-9a-f]{64}(\.part-[0-9a-f]+)?$")
DEFAULT_GRACE_DAYS = 7.0
#: A grace period shorter than this is refused: it would let a retention run
#: delete what a snapshot run, started hours earlier, is about to reference.
_MIN_GRACE_DAYS = 1.0
_PENDING_NAME = "gc-pending.json"
#: The mark list holds one ~88-byte entry per unused object, so the client's
#: 16 MiB text limit stopped a store at about 190,000 of them (measured: 87.7
#: bytes per entry) with a message that named no way out.  The file is one this
#: tool wrote, so the limit only has to stop a planted one: 64 MiB is ~760,000
#: entries.
_PENDING_MAX_BYTES = 64 * 1024 * 1024


def _read_pending(client: Any, rid: str, base: str) -> str:
    """The mark list as text ('' when there is none); too large names the way out."""
    try:
        data = client.get_file_bytes(rid, f"{base}/{_PENDING_NAME}", max_bytes=_PENDING_MAX_BYTES)
    except Exception as exc:
        if "refusing to read it" in str(exc):
            raise SnapshotError(
                f"{base}/{_PENDING_NAME} is larger than {human_bytes(_PENDING_MAX_BYTES)}, more "
                "than a store of unused objects should leave. Remove it and run again: every "
                "unused object is then marked afresh and is deleted only after a full grace "
                "period from now, so nothing is lost, only delayed"
            ) from None
        raise SnapshotError(f"reading gc state failed: {type(exc).__name__}: {exc}") from None
    return data.decode("utf-8", errors="replace").strip() if data else ""


def add_payload_gc_arguments(parser: Any) -> None:
    parser.add_argument(
        "--payload-gc",
        action="store_true",
        help="payload retention: report the stored objects that no snapshot in the remote's "
        "history (nor the local vault) references. With --payload-delete, mark them, and "
        "delete those that stayed unreferenced for --payload-grace-days. Needs --remote; "
        "--vault adds the local vault's refs",
    )
    parser.add_argument(
        "--payload-verify",
        nargs="?",
        const="20",
        default=None,
        metavar="N|all",
        help="download N randomly chosen payload objects (default 20, or 'all') of the newest "
        "snapshot of the remote and check each against its SHA-256. The server cannot report a "
        "content hash, so this is the only way to find an object that was damaged after it was "
        "stored. Needs --remote",
    )
    parser.add_argument(
        "--payload-delete",
        action="store_true",
        help="with --payload-gc: act. The first run only marks unreferenced objects; a "
        "later run deletes those still unreferenced after the grace period",
    )
    parser.add_argument(
        "--payload-keep",
        type=int,
        default=None,
        metavar="N",
        help="with --payload-gc: count only the newest N snapshots of each branch and tag "
        "as using objects. Objects only older snapshots need become unused and, with "
        "--payload-delete, are removed after the grace period -- those older snapshots can "
        "then no longer be restored. Default: keep every snapshot",
    )
    parser.add_argument(
        "--payload-no-vault",
        action="store_true",
        help="with --payload-gc --payload-delete: delete without --vault. Snapshots that "
        "exist only in a vault on some machine (not yet pushed) would then count as "
        "unreferenced, so this must be said out loud",
    )
    parser.add_argument(
        "--payload-accept-loss",
        action="store_true",
        help="with --payload-gc --payload-delete: go on although snapshots would become "
        "unrestorable -- the remote's history was rewritten since the last --payload-delete "
        "run (a branch or tag deleted, a force push), or --payload-keep expires older "
        "snapshots",
    )
    parser.add_argument(
        "--payload-grace-days",
        type=float,
        default=DEFAULT_GRACE_DAYS,
        metavar="DAYS",
        help=f"with --payload-gc: how long an object must stay unreferenced before it is "
        f"deleted (default {DEFAULT_GRACE_DAYS:g}, minimum {_MIN_GRACE_DAYS:g})",
    )


def payload_gc_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.payload_verify is not None:
        for flag, value in (("--restore", args.restore), ("--maintain", args.maintain),
                            ("--dry-run", args.dry_run), ("--payload-gc", args.payload_gc),
                            ("--payload-min-size", args.payload_min_size),
                            ("--payload-auto-ratio", args.payload_auto_ratio is not None),
                            ("--no-payload", args.no_payload)):
            if value:
                problems.append(f"{flag} does not apply to --payload-verify")
        if not args.remote:
            problems.append("--payload-verify requires --remote <url>")
        if args.payload_verify != "all" and not args.payload_verify.isdigit():
            problems.append("--payload-verify takes a count or 'all'")
        elif args.payload_verify != "all" and int(args.payload_verify) < 1:
            problems.append("--payload-verify takes a count of at least 1")
    if args.payload_gc:
        for flag, value in (("--restore", args.restore), ("--maintain", args.maintain),
                            ("--dry-run", args.dry_run), ("--payload-min-size", args.payload_min_size),
                            ("--no-payload", args.no_payload),
                            ("--payload-jobs", args.payload_jobs), ("--exclude", args.exclude),
                            ("--no-push", args.no_push)):
            if value:
                problems.append(f"{flag} does not apply to --payload-gc")
        if not args.remote:
            problems.append("--payload-gc requires --remote <url>")
        if args.payload_grace_days < _MIN_GRACE_DAYS:
            problems.append(f"--payload-grace-days must be at least {_MIN_GRACE_DAYS:g}")
        if args.payload_keep is not None and args.payload_keep < 1:
            problems.append("--payload-keep must be at least 1")
        if args.payload_delete and not args.vault and not args.payload_no_vault:
            problems.append(
                "--payload-delete needs --vault <the long-lived vault>: a snapshot that is "
                "only in a vault and was never pushed would look unreferenced. Pass "
                "--payload-no-vault if the remote really is the only copy"
            )
        if not args.payload_delete:
            for flag, value in (("--payload-no-vault", args.payload_no_vault),
                                ("--payload-accept-loss", args.payload_accept_loss)):
                if value:
                    problems.append(f"{flag} only applies to --payload-gc --payload-delete")
        elif args.payload_no_vault and args.vault:
            problems.append("--payload-no-vault and --vault contradict each other")
    else:
        for flag, value in (("--payload-no-vault", args.payload_no_vault),
                            ("--payload-accept-loss", args.payload_accept_loss)):
            if value:
                problems.append(f"{flag} only applies to --payload-gc")
        if args.payload_delete:
            problems.append("--payload-delete only applies to --payload-gc")
        if args.payload_keep is not None:
            problems.append("--payload-keep only applies to --payload-gc")
        if args.payload_grace_days != DEFAULT_GRACE_DAYS:
            problems.append("--payload-grace-days only applies to --payload-gc")
    return problems


class _History(NamedTuple):
    hashes: dict[str, int]       #: every referenced object and its size
    commits: int                 #: commits scanned
    manifests: int               #: distinct manifests found
    tips: dict[str, str]         #: the remote's refs now: ``refs/heads/x`` -> commit
    dropped: list[str]           #: refs seen by the last --payload-delete that lost history
    expired: int                 #: commits --payload-keep leaves out of the count


def _scratch_history_repo(td: str) -> Path:
    """A bare scratch repository in *td* that git will not run maintenance in.

    ``git fetch`` otherwise may start ``gc --auto`` / ``maintenance`` in the
    background, which races with the directory being removed (seen on Linux and
    macOS as "Directory not empty").
    """
    repo = Path(td) / "history.git"
    made = _run(
        ["git", "init", "-q", "--bare", str(repo)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if made.returncode != 0:
        raise SnapshotError(f"could not create a scratch repository: {made.stderr.strip()}")
    for key, value in (("gc.auto", "0"), ("maintenance.auto", "false")):
        _run(
            ["git", f"--git-dir={repo}", "config", key, value],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    return repo


def _hashes_in_history(
    remote: str | None, vault: Path | None, keep: int | None = None,
    seen: dict[str, str] | None = None,
) -> _History:
    """Every payload object referenced from every ref of *remote* and, when given, of
    the local *vault* (which must be one).  *seen* is the ref tips the last
    --payload-delete run saw: any that the remote no longer contains are reported.
    With ``remote=None`` only the vault is read: nothing is downloaded."""
    if vault is not None and not (vault / ".git").exists():
        raise SnapshotError(
            f"--vault {vault} is not a vault (it has no .git); refusing to run without the "
            "un-pushed snapshots it was meant to protect"
        )
    with tempfile.TemporaryDirectory(prefix="grs-payload-gc-") as td:
        # Only the vault to read: use it where it is.  Fetching it into a
        # scratch repository copied the whole vault into the temp directory
        # (with no free-space check) to answer a question git can ask in place.
        in_place = remote is None and vault is not None
        repo = (vault / ".git") if in_place and vault is not None else _scratch_history_repo(td)
        if remote is not None:
            # What is deleted is decided from this fetch, so the helper must not
            # advertise a subset of the refs: a ref it can only warn about and
            # skip makes that branch's objects look unreferenced.
            previous = os.environ.get("SEAFILE_STRICT_REFS")
            os.environ["SEAFILE_STRICT_REFS"] = "1"
            try:
                git(
                    repo, None,
                    ["fetch", "-q", "--no-tags", remote,
                     "+refs/heads/*:refs/remotes/remote/*", "+refs/tags/*:refs/remotes/remote-tags/*"],
                    cwd=Path(td), status=f"fetching the history of {remote}", stall_limit=None,
                )
            finally:
                if previous is None:
                    os.environ.pop("SEAFILE_STRICT_REFS", None)
                else:
                    os.environ["SEAFILE_STRICT_REFS"] = previous
        if vault is not None and not in_place:
            # The vault is read where it lies: with its object store as an
            # alternate the fetch below finds every object already present and
            # transfers none, where it used to copy the whole vault into the
            # temp directory (ROADMAP S7).
            alternates = repo / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text((vault / ".git" / "objects").as_posix() + "\n", encoding="utf-8")
            git(
                repo, None,
                ["fetch", "-q", "--no-tags", str(vault),
                 "+refs/heads/*:refs/remotes/local/*", "+refs/tags/*:refs/remotes/local-tags/*"],
                cwd=Path(td),
            )
        tips: dict[str, str] = {}
        for line in git(
            repo, None, ["for-each-ref", "--format=%(objectname) %(refname)",
                         "refs/remotes/remote/", "refs/remotes/remote-tags/"], cwd=Path(td),
        ).stdout.splitlines():
            oid, _, name = line.partition(" ")
            if name.startswith("refs/remotes/remote-tags/"):
                tips["refs/tags/" + name[len("refs/remotes/remote-tags/"):]] = oid
            elif name.startswith("refs/remotes/remote/"):
                tips["refs/heads/" + name[len("refs/remotes/remote/"):]] = oid
        dropped: list[str] = []
        for ref, old in sorted((seen or {}).items()):
            now = tips.get(ref)
            if now is None:
                dropped.append(f"{ref} was deleted")
            elif now != old and git(
                repo, None, ["merge-base", "--is-ancestor", old, now], cwd=Path(td), check=False,
            ).returncode != 0:
                dropped.append(f"{ref} no longer contains {old[:12]}")
        expired = 0
        if keep is None:
            commits = [c for c in git(repo, None, ["rev-list", "--all"], cwd=Path(td)).stdout.split() if c]
        else:
            refs = git(repo, None, ["for-each-ref", "--format=%(refname)"], cwd=Path(td)).stdout.split()
            kept: set[str] = set()
            for ref in refs:
                kept.update(git(repo, None, ["rev-list", f"--max-count={keep}", ref],
                                cwd=Path(td)).stdout.split())
            commits = sorted(kept)
            everything = set(git(repo, None, ["rev-list", "--all"], cwd=Path(td)).stdout.split())
            # Only a snapshot that has a manifest can become unrestorable: one taken
            # before payload mode is whole in git and stays restorable.
            older = "".join(f"{c}:{PAYLOAD_MANIFEST}\n" for c in sorted(everything - kept))
            if older:
                listed = git(repo, None, ["cat-file", "--batch-check"], cwd=Path(td),
                             stdin=older).stdout.splitlines()
                expired = sum(1 for line in listed
                              if len(line.split()) == 3 and line.split()[1] == "blob")
        if not commits:
            return _History({}, 0, 0, tips, dropped, expired)
        specs = "".join(f"{c}:{PAYLOAD_MANIFEST}\n" for c in commits)
        checked = git(repo, None, ["cat-file", "--batch-check"], cwd=Path(td), stdin=specs).stdout
        blobs = sorted({
            line.split()[0] for line in checked.splitlines()
            if len(line.split()) == 3 and line.split()[1] == "blob"
        })
        hashes: dict[str, int] = {}
        for oid in blobs:
            body = git(repo, None, ["cat-file", "blob", oid], cwd=Path(td)).stdout
            try:
                data = json.loads(body)
            except (ValueError, RecursionError):
                raise SnapshotError(
                    f"a payload manifest in the history ({oid[:12]}) is unreadable; refusing "
                    "to decide what is unreferenced"
                ) from None
            for item in check_manifest_shape(data, f"the payload manifest {oid[:12]}"):
                hashes[item["sha256"]] = item["size"]
        return _History(hashes, len(commits), len(blobs), tips, dropped, expired)


def _remote_tips(remote: str) -> dict[str, str]:
    """``{ref: object id}`` for every branch and tag of *remote*, via the helper's listing.

    Strict, like the history fetch: a listing that would have skipped a ref is
    an error here too.
    """
    previous = os.environ.get("SEAFILE_STRICT_REFS")
    os.environ["SEAFILE_STRICT_REFS"] = "1"
    try:
        proc = _run(
            ["git", "ls-remote", "--heads", "--tags", remote],
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    finally:
        if previous is None:
            os.environ.pop("SEAFILE_STRICT_REFS", None)
        else:
            os.environ["SEAFILE_STRICT_REFS"] = previous
    if proc.returncode != 0:
        raise SnapshotError(
            f"could not list the refs of {remote} to re-check them before deleting: "
            f"{proc.stderr.strip()}"
        )
    tips: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        oid, _, name = line.partition("	")
        if name and not name.endswith("^{}"):
            tips[name] = oid
    return tips


def _refs_the_helper_cannot_see(client: Any, rid: str, repo_path: str) -> list[str]:
    """Names under ``refs/`` that are Seafile conflict copies (invisible to git)."""
    found: list[str] = []
    stack = [f"{repo_path}/refs"]
    while stack:
        directory = stack.pop()
        for entry in client.list_dir(rid, directory):
            name = entry.get("name", "")
            full = f"{directory}/{name}"
            if entry.get("type") == "dir":
                stack.append(full)
            elif "(SFConflict" in name:
                found.append(full)
    return found


def payload_verify(remote: str, vault: Path | None, *, count: str) -> int:
    """Download a sample of the newest snapshot's objects and check their SHA-256."""
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    history = _hashes_in_history(remote, vault, 1)
    sizes, manifests = history.hashes, history.manifests
    if manifests == 0:
        raise SnapshotError("found no payload manifest in the newest snapshot of the remote")
    shas = sorted(sizes)
    chosen = shas if count == "all" else sorted(random.sample(shas, min(int(count), len(shas))))
    print(f"verifying {len(chosen)} of {len(shas)} object(s) of the newest snapshot ...", flush=True)
    bad: list[str] = []
    total_bytes = sum(sizes[sha] for sha in chosen)
    checked = 0
    started = last_pulse = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="grs-payload-verify-") as td:
        for index, sha in enumerate(chosen, 1):
            why = _download_verified(
                client, rid, f"{base}/{object_dir(sha)}/{sha}", Path(td) / sha,
                {"size": sizes[sha], "sha256": sha}, Path(td),
            )
            (Path(td) / sha).unlink(missing_ok=True)
            if why is not None:
                bad.append(f"{sha}: {why}")
            checked += sizes[sha]
            now = time.monotonic()
            if index % 10 == 0 or index == len(chosen):
                print(f"checked [{index}/{len(chosen)}]", flush=True)
            if now - last_pulse >= _PULSE_SECONDS:
                last_pulse = now
                print(_progress_line(checked, total_bytes, now - started, "checked"), flush=True)
    if bad:
        shown = "\n  ".join(bad[:20])
        raise SnapshotError(f"{len(bad)} of {len(chosen)} sampled object(s) are damaged:\n  {shown}")
    print(f"verified {len(chosen)} object(s): every sampled object matches its SHA-256")
    record_verification(client, rid, base, len(chosen), len(shas))
    RUN_FACTS.update(checked=len(chosen), objects=len(shas), damaged=0)
    return 0


def history_fetch_space_problem(
    client: Any, rid: str, repo_path: str, scratch: Path, *, allow_low_disk: bool
) -> str | None:
    """A reason the remote's history would not fit in *scratch*, or ``None``.

    The history is read by fetching the remote into a scratch repository, which
    downloads every pack; a full volume stops the run mid-fetch with whatever
    git says about it.  The packs' listed sizes are what the fetch will write.
    """
    need = sum(
        e.get("size", 0) for e in client.list_dir(rid, f"{repo_path}/objects/pack")
        if e.get("type") == "file" and isinstance(e.get("size"), int)
    )
    try:
        free = shutil.disk_usage(scratch).free
    except OSError:
        return None
    if need + _FREE_SPACE_MARGIN_BYTES <= free or allow_low_disk:
        return None
    return (
        f"refusing to fetch the history: the remote's packs are {human_bytes(need)} and "
        f"{scratch} has {human_bytes(free)} free ({human_bytes(_FREE_SPACE_MARGIN_BYTES)} is "
        "kept in reserve). Point TMPDIR (TEMP on Windows) at a larger volume, or pass "
        "--allow-low-disk to try anyway"
    )


def payload_gc(
    remote: str, vault: Path | None, *, delete: bool, grace_days: float, keep: int | None = None,
    accept_loss: bool = False, from_vault: bool = False, allow_low_disk: bool = False,
) -> int:
    """Report, and with *delete* mark and later remove, unreferenced payload objects.

    ``from_vault`` is for the routine report that follows a push: the history is
    read from the local vault (which a pushing run has just made equal to the
    remote's tip) instead of being downloaded again, and nothing is marked or
    deleted.  It must not be combined with *delete*.
    """
    if from_vault and delete:
        raise SnapshotError("internal error: a vault-only history cannot back a deletion")
    if grace_days < _MIN_GRACE_DAYS:
        raise SnapshotError(f"the grace period must be at least {_MIN_GRACE_DAYS:g} day(s)")
    library, base, server_url = payload_location(remote)
    repo_path = base[: -len("/payload")]
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")

    conflicts = _api(lambda: _refs_the_helper_cannot_see(client, rid, repo_path), "listing refs")
    if conflicts:
        raise SnapshotError(
            f"{len(conflicts)} ref(s) are Seafile conflict copies the history reader cannot "
            f"see (first: {conflicts[0]}); a snapshot reachable only from one would look "
            "unreferenced. Resolve them (keep one ref, delete the copies; git-remote-seafile doctor on the "
            "vault's URL lists every one) and run again"
        )
    if not from_vault:
        problem = _api(
            lambda: history_fetch_space_problem(
                client, rid, repo_path, Path(tempfile.gettempdir()), allow_low_disk=allow_low_disk
            ),
            "listing the remote's packs",
        )
        if problem:
            raise SnapshotError(problem)
    seen = {} if from_vault else read_seen_tips(client, rid, base)
    history = _hashes_in_history(None if from_vault else remote, vault, keep, seen)
    sizes_by_hash, commits, manifests = history.hashes, history.commits, history.manifests
    referenced = set(sizes_by_hash)
    if keep is not None:
        print(f"--payload-keep {keep}: only the newest {keep} snapshot(s) of each branch and "
              f"tag count as using objects; {history.expired} older snapshot(s) become "
              f"unrestorable once their objects are deleted")
    if history.dropped:
        print("Warning: the remote's history changed since the last --payload-delete run:\n  "
              + "\n  ".join(history.dropped)
              + "\n  snapshots only reachable from there no longer count as using objects")
    print(f"history: {commits} commit(s), {manifests} distinct manifest(s), "
          f"{len(referenced)} referenced object(s)")

    stored: dict[str, int] = {}
    others = 0
    for prefix in (f"{n:02x}" for n in range(256)):
        entries = _api(
            lambda p=prefix: client.list_dir(rid, f"{base}/objects/{p}"),
            f"listing {base}/objects/{prefix}",
        )
        for entry in entries:
            name = entry.get("name", "")
            if (entry.get("type") == "dir" or not isinstance(name, str)
                    or not _COLLECTABLE_NAME.match(name)):
                others += 1
                continue
            size = entry.get("size", 0)
            stored[name] = size if isinstance(size, int) and not isinstance(size, bool) else 0

    # A temporary is never referenced; the grace period keeps one that a run in
    # progress is still writing.
    unused = {name for name in stored if name[:64] not in referenced or len(name) > 64}
    missing = referenced - {name for name in stored if len(name) == 64}
    pending_text = _read_pending(client, rid, base)
    try:
        pending_raw = json.loads(pending_text) if pending_text else {}
    except (ValueError, RecursionError):
        raise SnapshotError(f"{base}/{_PENDING_NAME} is unreadable; remove it and run again") from None
    pending = {
        sha: float(ts) for sha, ts in (pending_raw.items() if isinstance(pending_raw, dict) else [])
        if isinstance(sha, str) and _COLLECTABLE_NAME.match(sha) and isinstance(ts, (int, float))
    }
    now = _grace_now(client)
    due = {sha for sha in unused if sha in pending and now - pending[sha] >= grace_days * 86400}
    waiting = unused - due

    def total(shas: Any) -> str:
        return human_bytes(sum(stored[s] for s in shas))

    print(f"stored : {len(stored)} object(s), {total(stored)} under {base}/objects")
    print(f"in use : {len(stored) - len(unused)} object(s), {total(set(stored) - unused)}")
    print(f"unused : {len(unused)} object(s), {total(unused)}")
    print(f"  unreferenced for at least {grace_days:g} day(s) (deletable): {len(due)}, {total(due)}")
    print(f"  not yet unreferenced for that long (kept): {len(waiting)}, {total(waiting)}")
    if others:
        print(f"other  : {others} entry(ies) that are not objects (left alone)")
    if missing:
        print(f"Warning: {len(missing)} object(s) named by a manifest are NOT on the server; "
              f"the snapshots that need them cannot be restored")
    print(verification_note(client, rid, base))

    if not delete:
        # Seed the history-rewrite fence: a report that read the *remote's*
        # history and found no baseline writes the tips it saw, so the first
        # delete run has something to compare against (ROADMAP S2).  Only
        # when none exists — rewriting an existing baseline to current tips
        # would mask a rewrite that happened since the last delete run — and
        # never on the vault-only weekly report, whose tips may be stale.
        if not seen and not from_vault and history.tips:
            write_seen_tips(client, rid, base, history.tips)
            print("fence baseline: recorded the ref tips this report saw (the "
                  "first --payload-delete run compares against them)")
        if unused:
            print("\nnothing changed (report only); --payload-delete marks unused objects now "
                  "and deletes them once they have stayed unused for the grace period")
        return 0
    if manifests == 0:
        raise SnapshotError(
            "found no payload manifest anywhere in the history, so every object would look "
            "unreferenced; refusing to mark or delete (wrong remote, or a fetch that "
            "returned nothing?)"
        )
    if (history.dropped or (keep is not None and history.expired)) and not accept_loss:
        raise SnapshotError(
            "refusing to mark or delete: "
            + ("the remote's history was rewritten since the last --payload-delete run "
               f"({len(history.dropped)} ref(s)); " if history.dropped else "")
            + (f"--payload-keep {keep} would make {history.expired} older snapshot(s) "
               "unrestorable; " if keep is not None and history.expired else "")
            + "if that is intended, run again with --payload-accept-loss. Objects that "
            "only those snapshots need would otherwise be deleted after the grace period"
        )
    if due:
        # The decision above is minutes old.  A snapshot pushed since could name
        # an object that was unreferenced when the history was read, so the refs
        # are read once more immediately before the first deletion and a change
        # stops the run with nothing deleted (the next run re-decides).
        now_tips = _remote_tips(remote)
        if now_tips != {k: v for k, v in history.tips.items()}:
            raise SnapshotError(
                "the remote's branches or tags changed while this run was deciding what is "
                "unused (another machine pushed); nothing was deleted. Run it again"
            )
    deleted = 0
    gone: set[str] = set()
    stopped_early = False
    last_check = time.monotonic()
    for index, sha in enumerate(sorted(due), 1):
        # The loop can run for many minutes.  A push that lands in it is noticed
        # at the next look and stops the run, so the damage is bounded to what
        # was deleted before the push's ref became visible (and a snapshot
        # taken later re-uploads anything missing while its file still exists).
        if index > 1 and (index % _RECHECK_EVERY == 0
                          or time.monotonic() - last_check >= _RECHECK_SECONDS):
            if _remote_tips(remote) != {k: v for k, v in history.tips.items()}:
                stopped_early = True
                break
            last_check = time.monotonic()
        if not client.delete_entry(rid, f"{base}/{object_dir(sha)}/{sha}"):
            raise SnapshotError(f"the server refused to delete {sha}")
        deleted += 1
        gone.add(sha)
        if index % 100 == 0 or index == len(due):
            print(f"deleted [{index}/{len(due)}]", flush=True)
    # The new state: everything still unused keeps its first-seen time (or gets
    # now); anything that became referenced again, or was deleted, drops out.
    fresh = {sha: pending.get(sha, now) for sha in waiting | (due - gone)}
    marked = len(set(fresh) - set(pending))
    if fresh != pending:
        _api(lambda: client.mkdir_p(rid, base), f"creating {base}")
        _api(lambda: client.upload_file(
            rid, base, _PENDING_NAME, json.dumps(fresh, sort_keys=True).encode("ascii")
        ), "writing gc state")
    if stopped_early:
        raise SnapshotError(
            f"the remote's branches or tags changed while objects were being deleted (another "
            f"machine pushed); stopped after {deleted} of {len(due)}. Run it again; if a "
            "restore then names a missing object, take a new snapshot from a tree that still "
            "has the file, or run --payload-verify"
        )
    write_seen_tips(client, rid, base, history.tips)
    print(f"marked {marked} newly unreferenced object(s); deleted {deleted} object(s), "
          f"{total(due)}; quota is released at once, disk after the library's history "
          f"expires and the server's GC runs")
    return 0


# -- Payload restore (see 75_payload.py) -------------------------------------------

# -- restore -------------------------------------------------------------------


def extended_path(path: Path) -> Path:
    """``path`` in Windows' extended-length form, so 260 characters is not a limit.

    Python reaches past MAX_PATH only when the machine opted in
    (``LongPathsEnabled``); the extended-length prefix works everywhere and costs
    nothing elsewhere.  A relative or already-prefixed path is returned unchanged.
    """
    if os.name != "nt":
        return path
    text = str(path)
    bs = chr(92)
    prefix = bs * 2 + "?" + bs
    if text.startswith(prefix) or not os.path.isabs(text):
        return path
    if text.startswith(bs * 2):
        return Path(prefix + "UNC" + bs + text[2:])
    return Path(prefix + text)


def _download_verified(
    client: Any, rid: str, source_path: str, dest: Path, item: dict[str, Any], scratch: Path
) -> str | None:
    """Fetch one object to ``dest`` via a temporary file; ``None`` on success, else why not.

    The temporary file is under ``.git`` (``scratch``), never beside ``dest``: a
    ``<name>.part`` next to the payload file collided with a real file of that
    name that git had just restored, which was overwritten and then renamed
    away -- and the restore went on to certify the tree.
    """
    part = scratch / f"{item['sha256']}.{os.getpid()}.part"
    last = ""
    for _attempt in range(3):
        try:
            if not client.download_file_to(rid, source_path, part, max_bytes=item["size"]):
                part.unlink(missing_ok=True)
                return "object not found on the server"
            if part.stat().st_size != item["size"]:
                last = f"size {part.stat().st_size}, manifest says {item['size']}"
                continue
            if _sha256_file(part) != item["sha256"]:
                last = "SHA-256 does not match the manifest"
                continue
            os.replace(part, dest)
            return None
        except SnapshotError as exc:
            last = str(exc)
        except Exception as exc:  # transient network or API error: try again
            last = f"{type(exc).__name__}: {exc}"
    part.unlink(missing_ok=True)
    return last


def restore_payload(
    remote: str, into: Path, *, allow_low_disk: bool = False, sample: int | None = None
) -> None:
    """Download and verify the payload a restored snapshot's manifest names.

    A security problem in the manifest refuses it whole.  An entry this platform
    cannot hold, one that would overwrite something already there, or one that
    fails to download or verify is skipped and reported -- the rest is restored
    -- and the run then fails.  A file already in place that verifies is kept, so
    a rerun into the same directory picks up where it stopped.  With *sample* only
    that many files, chosen at random, are fetched (the restore drill).
    """
    manifest_path = into / PAYLOAD_MANIFEST
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (ValueError, RecursionError) as exc:
        raise SnapshotError(f"the snapshot's payload manifest is unreadable: {exc}") from None
    files = validate_manifest(manifest, into)
    total_files = len(files)
    if sample is not None and sample < total_files:
        files = random.sample(files, sample)
    problem = restore_space_problem(files, into, allow_low_disk=allow_low_disk)
    if problem is not None:
        raise SnapshotError(problem)
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    of = f" of {total_files}" if len(files) < total_files else ""
    print(f"restoring {len(files)}{of} payload file(s) from {base}/objects ...", flush=True)
    scratch = extended_path(into / ".git" / "grs-restore")
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        _restore_payload_files(client, rid, base, files, into, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _restore_payload_files(
    client: Any, rid: str, base: str, files: list[dict[str, Any]], into: Path, scratch: Path
) -> None:
    problems: list[str] = []
    done = 0
    total_bytes = sum(item["size"] for item in files)
    fetched = 0
    started = last_pulse = time.monotonic()
    for index, item in enumerate(files, 1):
        rel, sha = item["path"], item["sha256"]
        if os.name == "nt":
            why = windows_name_problem(rel)
            if why:
                problems.append(f"{rel}: skipped, the name {why}")
                continue
        dest = extended_path(into / rel)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{rel}: skipped, cannot create its directory ({exc})")
            continue
        if dest.exists() or dest.is_symlink():
            if dest.is_file() and not dest.is_symlink() and _sha256_file(dest) == sha:
                done += 1
                continue
            problems.append(f"{rel}: skipped, something else is already there (a name that "
                            "this filesystem treats as the same as another file's)")
            continue
        print(f"downloading [{index}/{len(files)}] {rel} ({human_bytes(item['size'])})", flush=True)
        why = _download_verified(client, rid, f"{base}/{object_dir(sha)}/{sha}", dest, item, scratch)
        if why is not None:
            problems.append(f"{rel}: {why}")
            continue
        try:
            if item.get("mode") == "100755" and os.name != "nt":
                dest.chmod(dest.stat().st_mode | 0o111)
            os.utime(dest, ns=(item["mtime_ns"], item["mtime_ns"]))
        except (OSError, OverflowError, ValueError):
            pass  # content verified; timestamps and mode are best effort
        done += 1
        fetched += item["size"]
        now = time.monotonic()
        if now - last_pulse >= _PULSE_SECONDS:
            last_pulse = now
            print(_progress_line(fetched, total_bytes, now - started, "downloaded"), flush=True)
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise SnapshotError(
            f"{len(problems)} of {len(files)} payload file(s) were not restored "
            f"({done} were, and verified):\n  {shown}{more}"
        )
    print(f"payload verified: {len(files)} file(s), SHA-256 matches the manifest")


def portable_name_warning(paths: list[str]) -> str | None:
    """A warning for payload paths a Windows restore would skip, or ``None``."""
    found = [(p, windows_name_problem(p)) for p in paths]
    bad = [(p, why) for p, why in found if why]
    if not bad:
        return None
    shown = "\n".join(f"  {p}: {why}" for p, why in bad[:10])
    more = f"\n  ... and {len(bad) - 10} more" if len(bad) > 10 else ""
    return (
        f"\nWarning: {len(bad)} payload path(s) cannot be restored on Windows under that "
        f"name (a restore there skips and reports them; elsewhere they restore):\n{shown}{more}"
    )


# -- Payload command line (see 75_payload.py) --------------------------------------

# -- command line ----------------------------------------------------------------


PAYLOAD_EPILOG = (
    "\npayload mode (large files): on by default ('auto') when the vault has a\n"
    "seafile:// remote. Files of 64 MiB or more, and 4 MiB or more that do not\n"
    "compress, go to Seafile by hash instead of through git; a tree with no such\n"
    "file is snapshotted exactly as without it. --no-payload turns it off, and a\n"
    "complete real run remembers the setting. --restore, --payload-verify,\n"
    "--payload-gc and --payload-view act on a remote's payload; see PAYLOAD.md.\n"
)


PAYLOAD_DEFAULT_NOTICE = (
    "payload: on by default (auto: files of 64 MiB or more, or 4 MiB or more that do not "
    "compress, bypass git); --no-payload turns it off and is remembered"
)


def add_payload_groups(parser: argparse.ArgumentParser) -> None:
    """Every payload option, in two help groups: taking a snapshot, and tools."""
    mode_group = parser.add_argument_group(
        "payload mode: large files stored by hash instead of through git",
        "On by default (auto) when there is a seafile:// remote and the run pushes; "
        "--no-payload turns it off. A complete real run remembers the setting in the "
        "vault. See PAYLOAD.md.",
    )
    add_payload_arguments(mode_group)
    add_payload_jobs_argument(mode_group)
    add_payload_report_argument(mode_group)
    tools_group = parser.add_argument_group(
        "payload tools: act on a remote's payload (no snapshot is taken)",
        "Each needs --remote. Reports and verification change nothing; "
        "--payload-delete and --payload-view write only what they say.",
    )
    add_payload_gc_arguments(tools_group)
    add_payload_view_arguments(tools_group)


def add_payload_arguments(parser: Any) -> None:
    parser.add_argument(
        "--payload-min-size",
        metavar="SIZE|auto",
        nargs="?",
        const="auto",
        default=None,
        help="payload mode: files bypass git; they are hashed (SHA-256) and uploaded to "
        "Seafile by hash under <path>/payload/objects, and the snapshot records a manifest "
        "instead. With no value, or 'auto': files of 64 MiB or more, plus files of 4 MiB or "
        "more that do not compress. With SIZE (e.g. 64M, 1G): files of at least SIZE. "
        "Needs a seafile:// remote; a restore downloads and verifies them. Default: what "
        "the vault remembered, else auto; 0 (or --no-payload) turns it off",
    )
    parser.add_argument(
        "--no-payload",
        action="store_true",
        help="payload mode: off for this run, and remembered (the same as "
        "--payload-min-size 0, without its argument parsing); a vault whose last "
        "snapshot used payload mode keeps its large files out of git until it is "
        "turned back on, so this stages them into git instead",
    )
    parser.add_argument(
        "--payload-auto-ratio",
        type=parse_ratio_argument,
        default=None,
        metavar="R|measure",
        help="payload mode, auto rule: a file of 4-64 MiB goes to payload when a sample of it "
        "keeps at least R of its size under a fast deflate (default 0.9, i.e. only data that "
        "barely compresses). Lower it (e.g. 0.5) to send moderately compressible data to "
        "payload instead of deflating it through git: that is faster when deflate is slow "
        "next to the link, and gives up git's delta compression between snapshots. "
        "0 < R <= 1; remembered by a complete real run like --payload-min-size. A new vault "
        "with a gigabyte or more of such data measures this machine and its link once and "
        "remembers the result; 'measure' does that again on demand",
    )


def add_payload_jobs_argument(parser: Any) -> None:
    parser.add_argument(
        "--payload-rehash",
        action="store_true",
        help="payload mode: ignore the hash cache for this run and hash every payload file "
        "again (default: use the cache), e.g. for a tree whose timestamps were restored by a "
        "copy tool, or as a periodic audit",
    )
    parser.add_argument(
        "--payload-skip-changing",
        action="store_true",
        help="payload mode: a payload file that changes while it is being read or uploaded (a "
        "database, a mailbox, a VM disk) is left out of this snapshot, named in a warning, "
        "instead of failing the run and snapshotting nothing; the previous snapshot keeps its "
        "last version (default: such a file is an error)",
    )
    parser.add_argument(
        "--payload-jobs",
        type=int,
        default=None,
        metavar="N",
        help="payload mode: upload N files at once (default 3; measured on one server: "
        "1 upload ~32 MiB/s, 3 at once ~53 MiB/s, so more rarely helps)",
    )


def add_payload_report_argument(parser: Any) -> None:
    parser.add_argument(
        "--payload-report-days",
        type=float,
        default=None,
        metavar="N",
        help="payload mode: after a run that pushes, print the report-only --payload-gc "
        "summary (what is stored, in use and reclaimable) when none was printed in the last "
        "N days (default 7; 0 never). It changes nothing on the server; --payload-gc "
        "--payload-delete is still the only thing that deletes",
    )


def payload_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.payload_report_days is not None:
        if args.no_payload or (
            args.payload_min_size is not None and not _explicit_size(args.payload_min_size)
        ):
            problems.append("--payload-report-days only applies when payload mode is on")
        if args.dry_run:
            problems.append("--payload-report-days does not apply to --dry-run (nothing is pushed)")
        if args.payload_report_days < 0:
            problems.append("--payload-report-days must be 0 or more")
        if args.restore or args.maintain or args.payload_gc or args.no_push:
            problems.append("--payload-report-days only applies to a snapshot run that pushes")
    if args.no_payload and args.payload_min_size is not None:
        problems.append("--no-payload and --payload-min-size contradict each other")
    # Only a flag typed on this command line is checked here: a default or a
    # remembered setting must never turn a run that worked into an error.
    if args.payload_min_size is not None and args.no_push:
        problems.append(
            "--payload-min-size cannot be combined with --no-push: payload files exist "
            "only on the server, so a snapshot that is not pushed would reference files "
            "that were never uploaded"
        )
    if args.payload_auto_ratio is not None:
        if isinstance(args.payload_auto_ratio, float) and not 0 < args.payload_auto_ratio <= 1:
            problems.append("--payload-auto-ratio must be above 0 and at most 1")
        if args.no_payload or (
            args.payload_min_size is not None and args.payload_min_size.strip().lower() != "auto"
        ):
            problems.append("--payload-auto-ratio only applies to the auto rule "
                            "(--payload-min-size auto)")
        if args.restore or args.maintain or args.payload_gc:
            problems.append("--payload-auto-ratio only applies to a snapshot run")
    if args.payload_jobs is not None:
        if args.payload_jobs < 1:
            problems.append("--payload-jobs must be at least 1")
        if args.no_payload or (
            args.payload_min_size is not None and not _explicit_size(args.payload_min_size)
        ):
            problems.append("--payload-jobs only applies when payload mode is on")
    if args.restore or args.maintain:
        for flag, given in (("--payload-min-size", args.payload_min_size is not None),
                            ("--no-payload", args.no_payload)):
            if given:
                problems.append(f"{flag} does not apply to --restore or --maintain")
    if args.payload_rehash and (args.restore or args.maintain or args.payload_gc):
        problems.append("--payload-rehash only applies to a snapshot run")
    if args.payload_skip_changing and (args.restore or args.maintain or args.payload_gc):
        problems.append("--payload-skip-changing only applies to a snapshot run")
    return problems


def _explicit_size(value: str) -> bool:
    """True unless the value is a size of zero, which means "off"."""
    text = value.strip().lower()
    if text == "auto":
        return True
    try:
        return parse_size(value) > 0
    except SnapshotError:
        return True  # a malformed size is reported by plan_payload with its own message


def payload_by_default_possible(remote: str | None, no_push: bool) -> bool:
    """Whether payload mode may come on by default: the run pushes to a seafile:// remote."""
    if not remote or no_push:
        return False
    # parse_seafile_url is lenient (it reads a plain path as library/path), so the
    # scheme is checked first: a local path or an ssh URL is never a Seafile remote.
    if not remote.lower().startswith("seafile://"):
        return False
    try:
        payload_location(remote)
    except SnapshotError:
        return False
    return True


def resolve_payload_value(
    value: str | None, vault: Path, *, remote: str | None = None, no_push: bool = False,
    no_payload: bool = False,
) -> tuple[str | None, bool]:
    """``(value, from_default)``: the flag as given, else what this vault remembered
    from its last real run, else ``auto`` when the run pushes to a seafile:// remote.

    So a scheduled bare ``seafile_snapshot.py`` keeps using payload mode (or keeps
    deliberately not using it, if ``0`` was remembered) the way the remote and the
    excludes are kept.  The default quietly stays off where payload mode cannot work
    (no remote, a remote that is not seafile://, ``--no-push``): a default must not
    make a run fail that worked before.
    """
    if no_payload:
        return "0", False
    if value is not None:
        return value, False
    possible = payload_by_default_possible(remote, no_push)
    if (vault / ".git").exists():
        remembered = _config(vault, "snapshot.payload")
        # A remembered value is not a request for *this* run: a vault that
        # remembered payload mode still takes a local --no-push snapshot, or one
        # without a seafile:// remote, without an error (explicit flags are
        # checked in payload_conflicts and plan_payload).
        if remembered and possible:
            return remembered, False
        if remembered:
            return None, False
    if possible:
        return "auto", True
    return None, False


def remember_payload_value(vault: Path, value: str | None, *, dry_run: bool) -> None:
    """Write the setting after a *complete* real run, never from a preview."""
    if value is None or dry_run:
        return
    _run(
        ["git", "-C", str(vault), "config", "snapshot.payload", value],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def parse_ratio_argument(text: str) -> float | str:
    """``--payload-auto-ratio``: a ratio above 0 and at most 1, or ``measure``."""
    if text.strip().lower() == "measure":
        return "measure"
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a ratio: {text!r} (a number above 0 and at most 1, or 'measure')"
        ) from None
    return value


def resolve_payload_ratio(value: float | str | None, vault: Path) -> float:
    """The auto rule's compressibility cut-off: the flag, else what this vault
    remembered, else the default.  A remembered value that is not a usable ratio
    (a hand edit) is the default, not an error: a default is not a request.
    ``measure`` asks for a new measurement, so what the vault remembered is only
    where this run starts from."""
    if isinstance(value, float):
        return value
    if (vault / ".git").exists():
        try:
            remembered = float(_config(vault, "snapshot.payloadratio"))
        except ValueError:
            return _AUTO_INCOMPRESSIBLE
        if 0 < remembered <= 1:
            return remembered
    return _AUTO_INCOMPRESSIBLE


def vault_remembers_ratio(vault: Path) -> bool:
    """Whether the vault's own config holds a usable auto cut-off.

    The fresh-vault ratio guard fires only when it does not: a remembered
    value is a deliberate choice, and a default is not a request.
    """
    if not (vault / ".git").exists():
        return False
    try:
        remembered = float(_config(vault, "snapshot.payloadratio"))
    except ValueError:
        return False
    return 0 < remembered <= 1


def remember_payload_ratio(vault: Path, value: float | str | None, *, dry_run: bool) -> None:
    """Write a ratio that was typed, after a *complete* real run; never a default."""
    if not isinstance(value, float) or dry_run:
        return
    for key, text in (("snapshot.payloadratio", repr(value)),
                      ("snapshot.payloadratiosource", "typed")):
        _run(
            ["git", "-C", str(vault), "config", key, text],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )


def plan_payload(
    value: str | None, remote: str | None, source: Path, excludes: list[str],
    ratio: float = _AUTO_INCOMPRESSIBLE, ratio_typed: bool = False,
    survey: dict[str, Any] | None = None, sampled: dict[str, float] | None = None,
) -> tuple[int | None, dict[str, Candidate], str]:
    """``(min_size, candidates, rule)``; ``(None, {}, "size")`` when payload mode is off."""
    auto = value is not None and value.strip().lower() == "auto"
    if ratio_typed and not auto and value is not None and value.strip() != "0":
        # The vault remembers a size rule and this run typed only the ratio: the
        # flag would do nothing, and a flag that does nothing should say so.
        raise SnapshotError(
            "--payload-auto-ratio tunes the auto rule, but this vault uses "
            f"--payload-min-size {value}. Pass --payload-min-size auto to switch deliberately"
        )
    if value is not None and not auto and value.strip().isdigit() and Path(value).is_dir():
        raise SnapshotError(
            f"--payload-min-size took {value!r} as its size, but that is also a directory: "
            "put SOURCE before the flag, or write --payload-min-size=auto"
        )
    min_size = _AUTO_UPPER if auto else (parse_size(value) if value is not None else None)
    rule = "auto" if auto else "size"
    if not min_size:
        return min_size, {}, rule
    if not remote:
        raise SnapshotError(
            "payload mode needs a remote to upload to: pass --remote "
            "seafile://<library>/<path> (or run from a source with a seafile:// remote)"
        )
    return min_size, find_payload_files(source, excludes, min_size, auto, ratio, survey, sampled), rule


def refuse_flagless_payload_vault(
    git_dir: Path, source: Path, branch: str, min_size: int | None
) -> None:
    """Refuse a run without the flag when the local branch's last snapshot used
    payload mode -- before anything outward happens (a ``--code-remote`` push)."""
    if min_size is not None:
        return
    tip = current_tip(git_dir, source, branch)
    previous = load_manifest(git_dir, source, tip)
    if previous is not None:
        raise SnapshotError(
            "this vault's last snapshot used payload mode, so its large files are not in "
            "git; running without --payload-min-size would stage them into git instead. "
            "Repeat the flag (--payload-min-size "
            f"{'auto' if previous.get('rule') == 'auto' else previous.get('min_size', 'SIZE')}), "
            "or pass --no-payload (--payload-min-size 0) to leave payload mode deliberately. A --no-push snapshot cannot use payload mode, since its files exist only on the server: for a local-only snapshot use --no-payload, or drop --no-push"
        )


#: Lines printed again as the last thing a successful run says, so a scheduler's
#: log ends with them (the NOTICE above scrolls past; the end of a log is read).
#: ``main`` clears it at the start of every run.
END_OF_RUN_NOTES: list[str] = []


def conversion_warning(candidates: dict[str, Candidate], dry_run: bool) -> str:
    """Said *before* any hashing or upload, when the default moves an existing vault's
    large files out of git: a scheduled run cannot be asked, but it can be read."""
    total = sum(item[0] for item in candidates.values())
    verb = "would move" if dry_run else "is moving"
    return (
        f"Notice: payload mode is on by default and this vault has earlier snapshots that "
        f"did not use it. This run {verb} {len(candidates)} file(s) "
        f"({human_bytes(total)}) out of git and uploads them as payload objects (one full "
        f"pass).\n"
        f"  - Copies already in the vault's history stay there, so the server holds them "
        f"twice; the vault does not shrink.\n"
        f"  - Snapshots taken before this one restore as before; this one and later ones "
        f"need the payload (0.8.8 cannot restore them completely).\n"
        f"  - To keep this vault as it was: stop now and run with --no-payload (remembered)."
    )


_CLOCK_NOTE_KEY = "snapshot.clocknote"


def _clock_offset_once_noted(git_dir: Path, source: Path) -> tuple[int, bool]:
    """``(offset, unprobed)``, saying "could not be measured" once per vault, not every run."""
    messages: list[str] = []
    offset, unprobed = fs_clock_probe(source, note=messages.append)
    if messages and not _read_config_key(git_dir, _CLOCK_NOTE_KEY):
        print(messages[0], flush=True)
        _run(
            ["git", "--git-dir", str(git_dir), "config", _CLOCK_NOTE_KEY, "1"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    return offset, unprobed


def payload_store_line(remote: str) -> str:
    """One line on what the payload store holds, or ``""`` if it cannot be listed.

    Informational: a run is never failed for it.  Snapshots never delete an object,
    so the number only grows until ``--payload-gc`` / ``--payload-keep`` is used.
    """
    try:
        library, base, server_url = payload_location(remote)
        client = _open_client(server_url)
        rid = client.get_repo_id(library)
        count = total = 0
        for entry in client.list_dir(rid, f"{base}/objects"):
            name = entry.get("name")
            if entry.get("type") != "dir" or not isinstance(name, str):
                continue
            for obj in client.list_dir(rid, f"{base}/objects/{name}"):
                oname, size = obj.get("name"), obj.get("size")
                if (obj.get("type") != "dir" and isinstance(oname, str)
                        and len(oname) == 64 and isinstance(size, int)):
                    count += 1
                    total += size
        checked = verification_note(client, rid, base)
    except Exception:  # informational only
        return ""
    return (f"payload store: {count} object(s), {human_bytes(total)} on the server "
            "(snapshots never delete one; --payload-gc reports what no snapshot uses); "
            + checked)


def publish_payload(
    remote: str,
    source: Path,
    manifest: dict[str, Any],
    *,
    dry_run: bool,
    jobs: int = 3,
    skip_changing: bool = False,
) -> tuple[int, int]:
    """Make the server hold every object the manifest names.

    Uploads the objects (deduplicated by hash) that are missing or the wrong
    size, then lists **every** object the manifest names and requires each at its
    size.  Returns ``(objects_to_upload, bytes_to_upload)``.

    With *skip_changing* a file that changes while it is read is dropped from the
    manifest (and named in a warning) instead of failing the run: one database
    that is always being written otherwise stops every snapshot of everything
    else.  It is opt-in; the default stays an error, because a snapshot that
    quietly lacks a file is the failure a backup cannot afford.

    ``dry_run`` reads the server and changes nothing.
    """
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")

    by_sha: dict[str, dict[str, Any]] = {}
    for item in sorted(manifest["files"], key=lambda e: e["path"]):
        by_sha.setdefault(item["sha256"], item)
    held = _remote_objects(client, rid, base, set(by_sha))
    todo = {sha: item for sha, item in by_sha.items() if held.get(sha) != item["size"]}
    todo_bytes = sum(item["size"] for item in todo.values())
    if todo:
        payload_preflight(client, rid, todo_bytes)
    if dry_run:
        return len(todo), todo_bytes
    left_out = _upload_all(client, rid, server_url, base, source, todo, jobs, skip_changing) if todo else []
    if left_out:
        dropped = set(left_out)
        manifest["files"] = [item for item in manifest["files"] if item["path"] not in dropped]
        manifest["snapshot"] = manifest_snapshot_id(manifest["files"])
        by_sha = {}
        for item in sorted(manifest["files"], key=lambda e: e["path"]):
            by_sha.setdefault(item["sha256"], item)
        todo = {sha: item for sha, item in todo.items() if sha in by_sha}
        todo_bytes = sum(item["size"] for item in todo.values())
        shown = ", ".join(left_out[:5]) + (" ..." if len(left_out) > 5 else "")
        print(
            f"\nWarning: {len(left_out)} payload file(s) changed while they were being read and "
            f"are NOT in this snapshot (--payload-skip-changing): {shown}.  The previous "
            "snapshot still holds their last version; stop whatever writes them, or --exclude "
            "them, to snapshot them again.",
            flush=True,
        )
    after = _remote_objects(client, rid, base, set(by_sha))
    wrong = [sha for sha, item in by_sha.items() if after.get(sha) != item["size"]]
    if wrong:
        raise SnapshotError(
            f"payload objects are missing or the wrong size on the server: {len(wrong)}, "
            f"first: {by_sha[wrong[0]]['path']}"
        )
    return len(todo), todo_bytes


def run_payload_step(
    git_dir: Path,
    source: Path,
    base: str | None,
    remote: str | None,
    min_size: int | None,
    candidates: dict[str, Candidate],
    *,
    dry_run: bool,
    jobs: int | None = None,
    rule: str = "size",
    ratio: float = 0.9,
    ratio_remembered: bool = False,
    ratio_typed: bool = False,
    rehash: bool = False,
    from_default: bool = False,
    skip_changing: bool = False,
) -> tuple[dict[str, Any] | None, tuple[int, int]]:
    """Hash and upload the payload; ``(manifest, (objects, bytes) to upload)``.

    ``ratio`` is this run's auto cut-off; ``ratio_remembered`` says whether
    the vault chose it and ``ratio_typed`` whether this run typed one -- the
    fresh-vault guard fires only when neither is true, because a default is
    not a request.
    """
    warn_if_newer_tool(git_dir, source, base)
    previous = load_manifest(git_dir, source, base)
    if previous is not None and min_size is None:
        raise SnapshotError(
            "this vault's last snapshot used payload mode, so its large files are not "
            "in git; running without --payload-min-size would stage them into git "
            "instead. Repeat the flag (--payload-min-size "
            f"{'auto' if previous.get('rule') == 'auto' else previous.get('min_size', 'SIZE')}), "
            "or pass --no-payload (--payload-min-size 0) to leave payload mode deliberately. A --no-push snapshot cannot use payload mode, since its files exist only on the server: for a local-only snapshot use --no-payload, or drop --no-push"
        )
    if not min_size:
        return None, (0, 0)
    if from_default and previous is not None and previous.get("rule") != "auto":
        # Two machines on different thresholds would move files between git and
        # the payload on every alternate run: the default must not choose one.
        raise SnapshotError(
            "this remote's snapshots use payload rule --payload-min-size "
            f"{previous.get('min_size', 'SIZE')}, but this vault has no setting yet and the "
            "default is auto. Repeat the flag the remote uses, or pass --payload-min-size "
            "auto to switch deliberately"
        )
    if from_default and previous is not None and previous.get("rule") == "auto" \
            and not ratio_remembered and not ratio_typed:
        # The same guard for the auto cut-off, which 0.9.13 started recording
        # in the manifest (ROADMAP T6): a fresh vault whose default (0.9)
        # differs from the cut-off the remote's snapshots were written with
        # would move files in the 4-64 MiB band between git and the payload
        # on alternate runs.  Only a vault with NO remembered ratio and no
        # typed one is refused -- a remembered or typed value is a deliberate
        # choice, and a default is not a request.
        theirs = previous.get("ratio")
        if isinstance(theirs, (int, float)) and not isinstance(theirs, bool) \
                and 0 < theirs <= 1 and abs(ratio - float(theirs)) > 1e-9:
            raise SnapshotError(
                "this remote's snapshots were written with the auto cut-off "
                f"--payload-auto-ratio {float(theirs):g}, but this vault has no ratio of "
                "its own and the default is "
                f"{ratio:g}: the two would move files in the 4-64 MiB band between git "
                "and the payload on alternate runs. Repeat the flag the remote uses, or "
                "pass --payload-auto-ratio <yours> to differ deliberately"
            )
    if not candidates and previous is None:
        # Nothing qualifies and the vault never used payload mode: no manifest,
        # so a tree that merely *could* grow payload files is not locked into
        # needing the flag.
        return None, (0, 0)
    if from_default and previous is None and base is not None:
        print(conversion_warning(candidates, dry_run), flush=True)
        if not dry_run:
            END_OF_RUN_NOTES.append(
                f"Notice: this run moved {len(candidates)} large file(s) of an existing vault "
                "out of git into payload mode (on by default). To stay as before, "
                "run once with --no-payload; it is remembered. See PAYLOAD.md D11."
            )
    # A preview writes nothing -- not even a probe file into a share.
    clock_offset, unprobed = (
        _clock_offset_once_noted(git_dir, source) if candidates and not dry_run else (0, False)
    )
    try:
        root_dev: int | None = os.stat(source).st_dev
    except OSError:
        root_dev = None
    entries, cache_machine = load_machine_hash_cache(git_dir)
    if cache_machine is not None and cache_machine != _machine_id():
        # The cache is documented as per-machine (PAYLOAD D6), but the file
        # lives in the vault, which two machines can share: an entry's
        # validity depends on the writing machine's clock and its probe of
        # the share clock, so another machine's entries cannot vouch for
        # anything (REVIEW.iq3_s D5).
        print(
            f"Note: the payload hash cache was written by another machine ({cache_machine}); "
            "every payload file was hashed again",
            flush=True,
        )
        entries = {}
    manifest, cache = build_manifest(
        candidates, source, {} if rehash else entries, min_size, rule=rule, ratio=ratio,
        clock_offset_ns=clock_offset,
        racy_window_ns=_UNPROBED_WINDOW_NS if unprobed else _RACY_WINDOW_NS,
        root_dev=root_dev,
        checkpoint=None if dry_run else (lambda partial: save_hash_cache(git_dir, partial)),
    )
    try:
        todo = publish_payload(
            remote or "", source, manifest, dry_run=dry_run, jobs=jobs or 3,
            skip_changing=skip_changing,
        )
    finally:
        # Hashing is the long half of a run and it is local and repeatable: keep it
        # when an upload then fails (a live file, a dropped connection), or the next
        # run reads the whole tree again.  The cache is only trusted for a file whose
        # size, mtime, inode and ctime still match, exactly as after a clean run.
        if not dry_run:
            save_hash_cache(git_dir, cache)
    return manifest, todo


def payload_summary(
    manifest: dict[str, Any], todo: tuple[int, int], dry_run: bool,
    *, remote: str | None = None, from_default: bool = False,
) -> str:
    total = sum(item["size"] for item in manifest["files"])
    verb = "to upload" if dry_run else "uploaded"
    line = (
        f"payload: {len(manifest['files'])} file(s) ({human_bytes(total)}) bypass git; "
        f"snapshot {manifest['snapshot']}; {verb} {todo[0]} object(s) ({human_bytes(todo[1])})"
    )
    warning = portable_name_warning([item["path"] for item in manifest["files"]])
    text = line + (warning or "")
    if from_default:
        text = PAYLOAD_DEFAULT_NOTICE + "\n" + text
    if remote and todo[0] and not dry_run:
        # Nothing here ever deletes an object, so say how big the store has become.
        store = payload_store_line(remote)
        if store:
            text += "\n" + store
    return text


_REPORT_KEY = "snapshot.payloadgcreport"
_DEFAULT_REPORT_DAYS = 7.0


def maybe_report_payload_gc(
    git_dir: Path, vault: Path, remote: str, days: float | None, *, now: float | None = None
) -> bool:
    """Print the report-only ``--payload-gc`` summary when one is due; never raises.

    Reclaimable space is invisible until someone asks, and a store that only
    grows is the one thing payload mode cannot fix by itself.  Asking costs a
    listing of the object directories and a history scan, so it is rationed to
    once per *days*, and it runs after the push so a slow or failing report can
    never cost a snapshot.  It deletes nothing and writes nothing but the time
    it ran, on this machine.
    """
    period = _DEFAULT_REPORT_DAYS if days is None else days
    if period <= 0:
        return False
    stamp = _read_config_key(git_dir, _REPORT_KEY)
    current = time.time() if now is None else now
    if not stamp:
        # The first run of a vault has just done the heavy part (hashing and
        # uploading everything), and what a report would say is what that run
        # printed.  Start the clock; the first report comes a period later.
        _run(
            ["git", "--git-dir", str(git_dir), "config", _REPORT_KEY, f"{current:.0f}"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return False
    try:
        if stamp and current - float(stamp) < period * 86400:
            return False
    except ValueError:
        pass  # an unreadable stamp means "due"
    print("\npayload store report (report only; nothing is deleted):", flush=True)
    try:
        payload_gc(remote, vault, delete=False, grace_days=1.0, from_vault=True)
    except Exception as exc:  # a report must never cost the run that already pushed
        print(f"payload report skipped: {exc}", flush=True)
        return False
    _run(
        ["git", "--git-dir", str(git_dir), "config", _REPORT_KEY, f"{current:.0f}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return True


# -- Payload views -------------------------------------------------------------
#
# A snapshot's payload is stored by hash, which is right for the store and useless
# to a person who wants to look.  A *view* is a directory of real file names built
# on the server by copying and renaming objects: no data moves (a server-side copy
# shares blocks; measured at 20-30 ms for 1,500 entries and 0 bytes on disk), and
# it is a convenience that can be deleted and rebuilt at any time -- the objects
# and the manifest stay the source of truth.
#
# Views are *not* byte-faithful restores: they carry no mode and no mtime, and the
# server's name rules apply (NFC, a double quote becomes %22, 255 bytes), so a file
# whose name cannot be held is reported and left out.  `--restore` is the faithful
# path.

_VIEW_NAME_BYTES = 255


def add_payload_view_arguments(parser: Any) -> None:
    parser.add_argument(
        "--payload-view",
        nargs="?",
        const="snapshot",
        default=None,
        metavar="REF",
        help="build a browsable directory of real file names for a snapshot of --remote "
        "(a branch name, or one like snapshot~2; default: the 'snapshot' branch), beside "
        "the vault as <path>-view/<name>, by server-side copy of the stored objects. "
        "No data is transferred; it is a convenience view, not a restore",
    )
    parser.add_argument(
        "--payload-view-name",
        default=None,
        metavar="NAME",
        help="with --payload-view: the directory name under <path>-view (default: the "
        "snapshot id, which is stable for identical content)",
    )
    parser.add_argument(
        "--payload-view-replace",
        action="store_true",
        help="with --payload-view: delete an existing view of that name first, so "
        "'current' can be rebuilt in place",
    )


def payload_view_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.payload_view is not None:
        for flag, value in (("--restore", args.restore), ("--maintain", args.maintain),
                            ("--dry-run", args.dry_run), ("--payload-gc", args.payload_gc),
                            ("--payload-verify", args.payload_verify),
                            ("--payload-min-size", args.payload_min_size),
                            ("--payload-auto-ratio", args.payload_auto_ratio is not None),
                            ("--no-payload", args.no_payload)):
            if value:
                problems.append(f"{flag} does not apply to --payload-view")
        if not args.remote:
            problems.append("--payload-view requires --remote <url>")
        name = args.payload_view_name
        if name is not None and (not name or "/" in name or "\\" in name or name in (".", "..")):
            problems.append("--payload-view-name must be a plain directory name")
    else:
        if args.payload_view_name is not None:
            problems.append("--payload-view-name only applies to --payload-view")
        if args.payload_view_replace:
            problems.append("--payload-view-replace only applies to --payload-view")
    return problems


def _manifest_at(remote: str, ref: str) -> dict[str, Any]:
    """The manifest of snapshot *ref* (``branch`` plus an optional ``~N``/``^``)."""
    cut = min((i for i, ch in enumerate(ref) if ch in "~^@:"), default=len(ref))
    branch, suffix = ref[:cut] or "snapshot", ref[cut:]
    with tempfile.TemporaryDirectory(prefix="grs-payload-view-") as td:
        repo = _scratch_history_repo(td)
        git(
            repo, None,
            ["fetch", "-q", "--no-tags", remote, "+refs/heads/*:refs/remotes/remote/*"],
            cwd=Path(td), status=f"fetching the history of {remote}", stall_limit=None,
        )
        rev = git(
            repo, None,
            ["rev-parse", "--verify", f"refs/remotes/remote/{branch}{suffix}^{{commit}}"],
            cwd=Path(td), check=False,
        )
        if rev.returncode != 0:
            raise SnapshotError(f"cannot resolve {ref!r} in the history of {remote}")
        body = git(
            repo, None, ["show", f"{rev.stdout.strip()}:{PAYLOAD_MANIFEST}"],
            cwd=Path(td), check=False,
        )
        if body.returncode != 0:
            raise SnapshotError(f"{ref!r} is a snapshot with no payload manifest; nothing to view")
        try:
            data = json.loads(body.stdout)
        except (ValueError, RecursionError):
            raise SnapshotError(f"the payload manifest of {ref!r} is unreadable") from None
    check_manifest_shape(data, f"the payload manifest of {ref!r}")
    return data


def _view_name(rel: str) -> str:
    """The name the server will hold for a source path (NFC, quote escaped)."""
    return "/".join(
        unicodedata.normalize("NFC", part).replace('"', "%22") for part in rel.split("/")
    )


def _place_object(client: Any, rid: str, source_dir: str, sha: str, target: str, name: str) -> None:
    """Copy one object into *target* (it keeps its hash name) and rename it."""
    if not client.copy_entries(rid, source_dir, [sha], target):
        raise SnapshotError(f"the server refused to copy object {sha[:12]} into {target}")
    if not client.rename_entry(rid, f"{target}/{sha}", name):
        raise SnapshotError(f"the server would not rename an object to {name!r}")


def payload_view(
    remote: str, ref: str, *, name: str | None, replace: bool, vault: Path | None = None
) -> int:
    manifest = _manifest_at(remote, ref)
    files = validate_manifest(manifest, Path(tempfile.gettempdir()) / "grs-view-validate")
    library, base, server_url = payload_location(remote)
    repo_path = base[: -len("/payload")]
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    view_name = name or manifest["snapshot"]
    root = f"{repo_path}-view/{view_name}"

    held = _remote_objects(client, rid, base, {item["sha256"] for item in files})
    absent = [item["path"] for item in files if held.get(item["sha256"]) != item["size"]]
    if absent:
        raise SnapshotError(
            f"{len(absent)} object(s) of this snapshot are missing from the server (first: "
            f"{absent[0]}); a view of it would be incomplete"
        )
    if _api(lambda: client.dir_exists(rid, root), f"checking {root}"):
        if not replace:
            raise SnapshotError(
                f"{root} already exists; pick another --payload-view-name or pass "
                "--payload-view-replace"
            )
        if not _api(lambda: client.delete_entry(rid, root), f"removing the old {root}"):
            # delete_entry answers False for a refusal the server reports as a
            # status, and _api only converts exceptions: swallowing that built
            # the new view into the old one's remains.
            raise SnapshotError(
                f"the server would not remove the old view {root}; delete it by hand or "
                "pick another --payload-view-name"
            )

    skipped: list[str] = []
    seen: set[str] = set()
    made: list[tuple[str, str, str]] = []   # (directory, shown name, sha)
    for item in files:
        shown = _view_name(item["path"])
        parts = shown.split("/")
        if any(len(part.encode("utf-8")) > _VIEW_NAME_BYTES for part in parts):
            skipped.append(f"{item['path']}: a name over {_VIEW_NAME_BYTES} bytes cannot be held")
            continue
        if shown in seen:
            skipped.append(f"{item['path']}: becomes the same name as another file")
            continue
        seen.add(shown)
        made.append(("/".join(parts[:-1]), parts[-1], item["sha256"]))

    # A server-side copy adds no blocks, but the library's quota counts each copy
    # at its full size (measured, SNAPSHOT.md 15.1, and again on a real server:
    # a view of one 70 MiB object grew the library by 70.0 MiB): a view of a
    # large snapshot can use what the next backup needs.  Said before anything
    # is built, and again in the closing line.
    sizes = {item["sha256"]: item["size"] for item in files}
    counted = sum(sizes[sha] for _d, _n, sha in made)
    payload_preflight(client, rid, counted, action="copies (counted at full size)")
    print(f"building {root}: {len(made)} file(s) ...", flush=True)
    _api(lambda: client.mkdir_p(rid, root), f"creating {root}")
    done_dirs: set[str] = set()
    for index, (directory, shown_name, sha) in enumerate(sorted(made), 1):
        target = root + ("/" + directory if directory else "")
        if target not in done_dirs:
            _api(lambda t=target: client.mkdir_p(rid, t), f"creating {target}")
            done_dirs.add(target)
        source_dir = f"{base}/{object_dir(sha)}"
        _api(
            lambda s=source_dir, h=sha, t=target, n=shown_name:
            _place_object(client, rid, s, h, t, n),
            f"placing {shown_name}",
        )
        if index % 100 == 0 or index == len(made):
            print(f"built [{index}/{len(made)}]", flush=True)
    print(
        f"view ready: {library}:{root} ({len(made)} file(s), {human_bytes(counted)}; the server "
        "copied them without moving data, but the library's quota and size now count each "
        "copy at its full size -- delete the view when you are done with it)"
    )
    if skipped:
        shown = "\n  ".join(skipped[:20])
        more = f"\n  ... and {len(skipped) - 20} more" if len(skipped) > 20 else ""
        print(f"Warning: {len(skipped)} file(s) were left out of the view:\n  {shown}{more}")
    return 0


def payload_command(args: argparse.Namespace) -> int | None:
    """Run a payload sub-command (verify, view, retention); ``None`` for a snapshot run."""
    vault = args.vault.expanduser().resolve() if args.vault else None
    if args.payload_verify is not None:
        return payload_verify(args.remote, vault, count=args.payload_verify)
    if args.payload_view is not None:
        return payload_view(
            args.remote, args.payload_view, name=args.payload_view_name,
            replace=args.payload_view_replace, vault=vault,
        )
    if args.payload_gc:
        return payload_gc(
            args.remote, vault, delete=args.payload_delete,
            grace_days=args.payload_grace_days, keep=args.payload_keep,
            accept_loss=args.payload_accept_loss, allow_low_disk=args.allow_low_disk,
        )
    return None


def source_remote_urls(source: Path) -> list[tuple[str, str]]:
    """``(remote_name, url)`` for every URL the source repository can push to.

    Read with ``--local``: a global ``insteadOf`` or a credential helper is not
    what the user means by "my code remote", and a repository that is not a git
    repository at all simply has no remotes.
    """
    proc = _run(
        [
            "git",
            "-C",
            str(source),
            "config",
            "--local",
            "--get-regexp",
            r"^remote\..*\.(url|pushurl)$",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    found: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        value = value.strip()
        if not value or not key.startswith("remote."):
            continue
        name = key[len("remote.") :].rsplit(".", 1)[0]
        found.append((name, value))
    return found


def normalise_remote(url: str) -> str:
    """Reduce a remote URL to something two spellings of it agree on.

    Deliberately loose.  A trailing slash, a trailing ``.git`` and the case of
    the scheme are all things that differ between two spellings of one remote,
    and the two error directions are not symmetric: a false *match* produces a
    clear message plus an override flag, while a false *miss* publishes the
    secrets.  So it errs towards matching.
    """
    text = url.strip().rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    scheme, sep, rest = text.partition("://")
    if sep:
        text = f"{scheme.lower()}://{rest}"
    return text.rstrip("/").lower()


def check_remote_is_not_a_code_remote(
    source: Path, remote: str, *, allowed: bool
) -> None:
    """Refuse to push the vault to a remote the source repository already uses.

    A separate *local* repository keeps the snapshot out of the code
    repository's object database.  It does **not** keep it off the code remote's
    *server*: point the vault at the same library and one push publishes
    ``.env``, keys and everything else the code repository is careful to
    exclude.  Nothing else in the design prevents that -- this check is the only
    thing standing between "a separate repository" and "the same backup".
    """
    if allowed:
        return
    target = normalise_remote(remote)
    for name, url in source_remote_urls(source):
        if normalise_remote(url) != target:
            continue
        raise SnapshotError(
            f"refusing to push the vault to '{remote}': that is the same place "
            f"as the source repository's '{name}' remote.\n"
            f"A snapshot contains the files .gitignore keeps *out* of the code "
            f"repository -- .env, keys, credentials -- so pushing it there "
            f"would publish them.\n"
            f"Point the vault at a different library, or pass "
            f"--allow-shared-remote if that is genuinely what you want."
        )


def diff_stat(git_dir: Path, work_tree: Path, parent: str, tree: str) -> str:
    proc = git(
        git_dir,
        work_tree,
        ["diff", "--stat", "--stat-count=35", "--no-color", parent, tree],
        cwd=work_tree,
        check=False,
    )
    return proc.stdout.rstrip()


def human_bytes(n: float) -> str:
    """``1536`` as ``1.5 KiB``: the divisor is 1024, so the label says so.

    The only byte formatter in the tool.  A copy of ``git_remote_seafile.units.
    format_bytes`` (this file must run without the package installed);
    ``tests/test_message_style.py`` fails if the two differ.

    It used to print the same numbers as KB/MB/GB while the staging heartbeat
    said GiB, so one run showed ``292.4 GB`` and ``9 GiB`` for the same kind of
    quantity.
    """
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


_PUSHED_KEY = "snapshot.pushed"


def remember_pushed_tip(git_dir: Path, work_tree: Path, branch: str, tip: str) -> None:
    """Record, in the vault, the snapshot the remote last held after one of *our* pushes.

    ``--status`` never contacts the remote, so this is what lets it say whether
    the newest snapshot has left this machine.  Only a successful push (or a
    remote already at our tip) writes it; a failed write is not worth failing a
    backup for.
    """
    git(git_dir, work_tree, ["config", _PUSHED_KEY, f"{branch} {tip}"], cwd=work_tree, check=False)


def recorded_pushed_tip(git_dir: Path, work_tree: Path, branch: str) -> str | None:
    """The tip recorded by :func:`remember_pushed_tip` for *branch*, or ``None``."""
    proc = git(git_dir, work_tree, ["config", "--get", _PUSHED_KEY], cwd=work_tree, check=False)
    name, _sep, tip = proc.stdout.strip().partition(" ")
    return tip if name == branch and tip else None


def push_snapshot(
    git_dir: Path,
    work_tree: Path,
    remote: str,
    branch: str,
    commit: str,
    tree: str,
    base: str | None,
    message: str,
) -> None:
    """Push the snapshot, re-parenting once if the remote moved under us.

    Two machines snapshotting the same vault can race between this run's fetch
    and its push; the helper rejects the loser with a non-fast-forward.  Since
    a snapshot is just a tree, the loser re-commits that tree on top of the new
    remote tip and pushes again -- no data is lost and the chain stays linear.
    Whether the remote actually moved is decided by *fetching the tip again*,
    not by matching words in git's output: the rejection text is
    locale-dependent, and any other failure deserves the same check.

    On any failure the local branch is rolled back to what the remote actually
    has, so the vault never claims a snapshot the remote never took.  That
    rollback is also what makes a plain re-run the correct remedy: the next
    run fetches the true tip and appends to it.  It runs even when the retry
    itself fails -- a fetch dying mid-retry is no reason to leave the branch
    advanced past a commit the server refused.
    """
    spec = f"refs/heads/{branch}:refs/heads/{branch}"
    current = commit
    try:
        for attempt in (1, 2):
            print(f"pushing to {remote} ...")
            # live_stderr: the first push of a large vault runs for hours, and
            # after it succeeds the helper auto-compacts the repository -- its
            # notice goes to stderr, which we stream instead of buffering so a
            # second long silence does not read as a dead run.
            proc = git(
                git_dir,
                work_tree,
                ["push", remote, spec],
                cwd=work_tree,
                check=False,
                status=f"pushing to {remote}",
                live_stderr=True,
                stall_limit=None,
            )
            if proc.returncode == 0:
                print("push complete")
                remember_pushed_tip(git_dir, work_tree, branch, current)
                return

            sys.stdout.write(proc.stdout or "")
            if proc.stderr is not None and proc.stderr:
                sys.stderr.write(proc.stderr)

            if attempt == 1:
                try:
                    tip = fetch_remote_tip(git_dir, work_tree, remote, branch)
                except SnapshotError as fetch_failure:
                    # The remote cannot be re-read; the rollback below still
                    # restores the last state the server is known to have.
                    sys.stderr.write(
                        f"could not re-check the remote tip ({fetch_failure}); "
                        f"rolling the local branch back\n"
                    )
                    break
                if tip and tip != base:
                    print(
                        f"remote moved to {tip[:12]}; re-parenting the snapshot "
                        f"and retrying"
                    )
                    rebased = commit_tree(git_dir, work_tree, tree, tip, message)
                    set_branch(git_dir, work_tree, branch, rebased, current)
                    current, base = rebased, tip
                    continue
            break
    except SnapshotError as retry_failure:
        # A failure while re-parenting (the fetch, commit-tree, or the branch
        # move) must not skip the rollback: the push failed, so the branch is
        # pointing at a commit the remote never took.
        sys.stderr.write(
            f"the push retry failed ({retry_failure}); rolling the local "
            f"branch back\n"
        )

    try:
        if base:
            set_branch(git_dir, work_tree, branch, base, current)
            rolled_back = base[:12]
        else:
            if delete_branch(git_dir, work_tree, branch, current):
                rolled_back = "(deleted)"
            else:
                rolled_back = f"(not deleted: the branch moved and is no longer at {current[:12]})"
    except SnapshotError as rollback_failure:
        raise SnapshotError(
            f"push to {remote} failed, and the rollback failed too "
            f"({rollback_failure}).  The vault branch may point at a commit "
            f"the remote does not have; re-run with --reset-to-remote to "
            f"adopt the remote chain."
        ) from rollback_failure
    raise SnapshotError(
        f"push to {remote} failed.\n"
        f"The local branch was rolled back to {rolled_back} -- the snapshot "
        f"this run attempted was discarded.  The remote may still be ahead "
        f"of that point (it moved during the retry), so re-run to reconcile "
        f"with its tip; the tree itself was never modified."
    )


# -- Where files go: the placement report and its what-if -----------------------------
#
# The auto payload rule (PAYLOAD.md D1) sorts a tree three ways: files under 4 MiB
# stay in git, files of 64 MiB or more go to payload, and the band between is decided
# by how well a sample of the file deflates.  Which side of that last line a band
# file falls on is a *speed* question -- deflating it through git against uploading
# it as it is -- and the answer depends on this machine and this link, so the run
# says what it did and, when a lower cut-off would plainly be faster here, says so
# without doing it.  A default is not a request: nothing moves because of this text.

#: Rates used until a run has measured them (``snapshot.uplink`` / ``snapshot.uplink1``
#: in the vault).  Measured on one server: 3 uploads at once ~53 MiB/s, one ~32 MiB/s;
#: SHA-256 on one core ~2 GiB/s.
_ASSUMED_UPLOAD_MIBS = 53.0
_ASSUMED_GIT_PUSH_MIBS = 32.0
_ASSUMED_HASH_MIBS = 2000.0
#: The cut-off the what-if tries, and how much it must save to be worth a line.
_WHATIF_RATIO = 0.5
_WHATIF_MIN_SECONDS = 60.0
_WHATIF_MIN_BYTES = 1024 ** 3
_DEFLATE_PROBE_FILES = 8


def link_rates(vault_config: dict[str, list[str]]) -> tuple[float, float, bool]:
    """``(upload MiB/s with the default jobs, one stream MiB/s, assumed)``.

    Measured values are what an earlier run stored in the vault; either one
    missing or unusable falls back to the assumed rate for that number.
    """
    def stored(key: str, fallback: float) -> tuple[float, bool]:
        try:
            value = float((vault_config.get(key) or [""])[0])
        except ValueError:
            return fallback, True
        return (value, False) if value > 0 else (fallback, True)

    upload, a1 = stored("snapshot.uplink", _ASSUMED_UPLOAD_MIBS)
    single, a2 = stored("snapshot.uplink1", _ASSUMED_GIT_PUSH_MIBS)
    return upload, single, a1 or a2


def measure_deflate_mibs(source: Path, band: list[tuple[int, float, str]]) -> float | None:
    """Deflate speed (zlib level 6, git's default) on real data from *band*.

    Reads the first 256 KiB of up to eight files spread across the band, so the
    figure is for the data in question and not for a synthetic block; about 2 MiB
    and a few hundredths of a second.  ``None`` when nothing could be read.
    """
    if not band:
        return None
    step = max(1, len(band) // _DEFLATE_PROBE_FILES)
    raw = 0
    spent = 0.0
    for _size, _ratio, rel in band[::step][:_DEFLATE_PROBE_FILES]:
        try:
            with open(source / rel, "rb") as fh:
                chunk = fh.read(_AUTO_SAMPLE_CHUNK)
        except OSError:
            continue
        started = time.perf_counter()
        zlib.compress(chunk, 6)
        spent += time.perf_counter() - started
        raw += len(chunk)
    if not raw or spent <= 0:
        return None
    return raw / 1024 ** 2 / spent


def placement_lines(
    survey: dict[str, Any], payload: dict[str, Candidate], ratio: float, source: Path,
    vault_config: dict[str, list[str]],
) -> list[str]:
    """What the auto rule did with this tree, and a cheaper cut-off if there is one."""
    small_n, small_b = survey["small"]
    band = survey["mid"]
    in_git = [m for m in band if m[1] < ratio]
    git_n = small_n + len(in_git)
    git_b = small_b + sum(m[0] for m in in_git)
    pay_b = sum(item[0] for item in payload.values())
    lines = [
        f"placement: {len(payload)} file(s) ({human_bytes(pay_b)}) go to payload; {git_n} "
        f"({human_bytes(git_b)}) stay in git -- {small_n} under {human_bytes(_AUTO_LOWER)} and "
        f"{len(in_git)} of {human_bytes(_AUTO_LOWER)}-{human_bytes(_AUTO_UPPER)} that deflate "
        f"by more than {1 - ratio:.0%}"
    ]
    if ratio <= _WHATIF_RATIO:
        return lines
    moved = [m for m in band if _WHATIF_RATIO <= m[1] < ratio]
    moved_b = sum(m[0] for m in moved)
    if moved_b < _WHATIF_MIN_BYTES:
        return lines
    deflate = measure_deflate_mibs(source, in_git)
    if not deflate:
        return lines
    upload, single, assumed = link_rates(vault_config)
    mib = 1024 ** 2
    # Per MiB: through git it is deflated and the compressed bytes go up one stream
    # at a time; as payload it is hashed and goes up as it is, jobs at once.
    seconds = sum(
        (size / mib) * (1 / deflate + r / single - 1 / upload - 1 / _ASSUMED_HASH_MIBS)
        for size, r, _rel in moved
    )
    if seconds < _WHATIF_MIN_SECONDS:
        return lines
    extra = sum(size * (1 - r) for size, r, _rel in moved)
    lines.append(
        f"Hint: --payload-auto-ratio {_WHATIF_RATIO:g} would move {len(moved)} file(s) "
        f"({human_bytes(moved_b)}) from git to payload: about {seconds / 60:.0f} min faster "
        f"here ({deflate:.0f} MiB/s deflate, {upload:.0f} MiB/s upload"
        f"{', assumed' if assumed else ''}), for about {human_bytes(int(extra))} more on the "
        f"server and without git's delta between snapshots. Not applied."
    )
    return lines


# -- Measuring this machine and its link, once, for a new vault ------------------------
#
# Which cut-off is faster depends on two speeds the tool can measure: how fast this
# machine deflates the data that would stay in git, and how fast the link takes an
# upload.  A *new* vault with a gigabyte or more of such data measures both on its
# first real run and remembers the result (``snapshot.payloadratio``), so the choice
# is made once and stays: a cut-off that changed from run to run would move files
# between git and payload.  An established vault is never re-tuned on its own -- a
# default is not a request -- and neither is one whose remote already holds payload
# objects, because two machines on different cut-offs would move files back and
# forth.  ``--payload-auto-ratio measure`` measures again on request.

#: Each probe file is this big, and the aggregate probe runs this many at once -- the
#: default ``--payload-jobs``.  On the real server 8 MiB was too short: a transfer of
#: ~0.25 s is mostly request latency, and three streams read *slower* than one.
_PROBE_BYTES = 16 * 1024 ** 2
_PROBE_STREAMS = 3


def tuned_ratio(deflate: float, upload: float, single: float) -> float:
    """The cut-off at which payload and git take equally long, kept in [0.5, 0.9].

    Per MiB, git deflates it (``1/deflate``) and sends the compressed share down
    one stream (``ratio/single``); payload hashes it and sends all of it, *jobs* at
    once (``1/upload``).  Payload is faster when ``ratio`` exceeds
    ``single * (1/upload + 1/hash - 1/deflate)``.  On a fast link that is zero or
    negative, which would send everything of 4 MiB or more to payload; the floor
    keeps the highly compressible files in git, where they cost little to store and
    deltas apply, and the ceiling is the old fixed default.
    """
    breakeven = single * (1 / upload + 1 / _ASSUMED_HASH_MIBS - 1 / deflate)
    return min(_AUTO_INCOMPRESSIBLE, max(_WHATIF_RATIO, breakeven))


def probe_link(remote: str, *, force: bool = False) -> dict[str, Any] | None:
    """Upload speed to the payload store: ``{"single", "upload", "in_use"}`` in MiB/s.

    One stream, then ``_PROBE_STREAMS`` at once, each of ``_PROBE_BYTES`` random
    bytes named ``probe-*`` directly in ``objects/``, outside the 256 fan-out
    directories that gc, verify, view and restore read, so no payload tool ever
    sees one; all are deleted again (a crash can leave one, which is safe to delete).
    ``in_use`` is whether the store already holds objects; unless *force*, that
    is reported without uploading anything.  Any failure is ``None``: a
    measurement must never fail a run.
    """
    try:
        library, base, server_url = payload_location(remote)
        client = _open_client(server_url)
        rid = client.get_repo_id(library)
        directory = f"{base}/objects"
        in_use = any(e.get("type") == "dir" for e in client.list_dir(rid, directory))
        if in_use and not force:
            return {"in_use": True}
        client.mkdir_p(rid, directory)
    except Exception:
        return None
    names: list[str] = []
    mib = _PROBE_BYTES / 1024 ** 2
    try:
        data = os.urandom(_PROBE_BYTES)
        nonce = os.urandom(4).hex()

        def push(index: int, worker: Any) -> bool:
            name = f"probe-{nonce}-{index}{_PART_SUFFIX}{nonce}"
            names.append(name)
            return bool(worker.upload_file(rid, directory, name, data))

        started = time.perf_counter()
        if not push(0, client):
            return None
        single = mib / (time.perf_counter() - started)
        workers = [_open_client(server_url) for _ in range(_PROBE_STREAMS)]
        for worker in workers:
            worker.list_dir(rid, directory)  # connect now, so setup is not timed as upload
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=_PROBE_STREAMS) as pool:
            done = list(pool.map(lambda i: push(i + 1, workers[i]), range(_PROBE_STREAMS)))
        if not all(done):
            return None
        # Never below one stream: if the server serialises uploads, one is what we get.
        upload = max(single, _PROBE_STREAMS * mib / (time.perf_counter() - started))
        return {"single": single, "upload": upload, "in_use": in_use}
    except Exception:
        return None
    finally:
        for name in names:
            try:
                client.delete_entry(rid, f"{directory}/{name}")
            except Exception:
                pass  # a probe file left behind is ignored by every payload tool


def tuning_mode(
    typed: float | str | None, vault: Path, *, dry_run: bool, auto_rule: bool,
    remote: str | None, has_history: bool,
) -> str | None:
    """``"forced"`` (asked for), ``"new"`` (a vault with nothing remembered yet) or None."""
    if dry_run or not auto_rule or not remote:
        return None
    if typed == "measure":
        return "forced"
    if typed is not None or has_history:
        return None
    if _config(vault, "snapshot.payload") or _config(vault, "snapshot.payloadratio"):
        return None
    return "new"


def tune_payload_ratio(
    remote: str, source: Path, survey: dict[str, Any], *, forced: bool
) -> dict[str, Any] | None:
    """Measure and choose, or ``None`` when there is nothing to decide.

    The result carries ``ratio``, the two measured rates and the deflate speed, and
    a ``message`` to print; with no ``ratio`` the message says why the default was
    kept.  Nothing is written here -- :func:`remember_tuning` does that after a
    complete run.
    """
    in_git = [m for m in survey["mid"] if m[1] < _AUTO_INCOMPRESSIBLE]
    if sum(m[0] for m in in_git) < _WHATIF_MIN_BYTES:
        return None  # too little at stake to touch the server for
    deflate = measure_deflate_mibs(source, in_git)
    if not deflate:
        return None
    probe = probe_link(remote, force=forced)
    if probe is None:
        return {"message": "could not measure the link (payload cut-off stays "
                           f"{_AUTO_INCOMPRESSIBLE:g}); --payload-auto-ratio measure tries again"}
    if probe.get("in_use") and "single" not in probe:
        return {"message": "this remote already holds payload objects, so its cut-off "
                           f"({_AUTO_INCOMPRESSIBLE:g} unless set) is kept: machines must agree. "
                           "--payload-auto-ratio measure chooses one for this vault"}
    ratio = tuned_ratio(deflate, probe["upload"], probe["single"])
    verdict = (f"the default cut-off {ratio:.2f} stands" if ratio >= _AUTO_INCOMPRESSIBLE
               else f"cut-off {ratio:.2f}")
    return {
        "ratio": ratio, "deflate": deflate, "upload": probe["upload"], "single": probe["single"],
        "message": (
            f"tuned: {verdict} -- 4-64 MiB files that deflate by less than {1 - ratio:.0%} go "
            f"to payload (deflate {deflate:.0f} MiB/s here; "
            f"uploads {probe['upload']:.0f} MiB/s with {_PROBE_STREAMS} at once, "
            f"{probe['single']:.0f} MiB/s with one). Remembered for this vault; "
            "--payload-auto-ratio R sets it, --payload-auto-ratio measure measures again"
        ),
    }


def remember_tuning(vault: Path, tuning: dict[str, Any] | None, *, dry_run: bool) -> None:
    """Keep a measured cut-off and the link rates after a *complete* real run."""
    if not tuning or tuning.get("ratio") is None or dry_run:
        return
    for key, value in (("snapshot.payloadratio", repr(float(tuning["ratio"]))),
                       ("snapshot.payloadratiosource", "measured"),
                       # Significant figures, not decimals: a link measured at
                       # 0.03 MiB/s (a loaded machine, a bad connection) stored as
                       # "0.0" read back as "unusable" and was replaced by the
                       # assumed 53 MiB/s -- the opposite of what was measured.
                       ("snapshot.uplink", f"{tuning['upload']:.4g}"),
                       ("snapshot.uplink1", f"{tuning['single']:.4g}")):
        _run(
            ["git", "-C", str(vault), "config", key, value],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )


# -- --status: is my backup current? (read-only) ---------------------------------
#
# Answers "is my backup current?" without staging anything (ROADMAP §1.0).  The
# last snapshot's time and size come from the vault; what changed in the source
# since it is a size-and-mtime comparison against the two records the snapshot
# itself wrote into the tip -- the payload manifest for the large files, and
# ``.seafile-payload/mtimes.json`` for the git-stored ones -- so the baseline
# travels with the snapshot and is not one machine's cache.  The comparison is
# explicitly a heuristic: it is what a stat can see, and the snapshot run
# re-hashes precisely because a change that keeps size and mtime can hide.


def add_status_arguments(parser: Any) -> None:
    parser.add_argument(
        "--status",
        action="store_true",
        help="report the last snapshot's time and size, what changed in the "
        "source since it (a size-and-mtime comparison against the records the "
        "snapshot wrote -- no staging, no hashing, nothing uploaded), the "
        "settings the next run would use, and the vault lock",
    )


def status_conflicts(args: argparse.Namespace) -> list[str]:
    if not args.status:
        return []
    problems: list[str] = []
    if args.restore or args.restore_drill is not None or args.maintain:
        problems.append(
            "--status reports and changes nothing, so it does not combine with "
            "--restore, --restore-drill or --maintain"
        )
    if args.payload_gc or args.payload_verify is not None or args.payload_view is not None:
        problems.append(
            "--status reports and changes nothing, so it does not combine with "
            "the payload commands"
        )
    if args.record_mtimes or args.no_record_mtimes:
        problems.append(
            "--record-mtimes and --no-record-mtimes only apply to a snapshot run"
        )
    if args.no_push or args.dry_run:
        problems.append(
            "--status describes the next run as it would be made with the vault's "
            "remembered settings, so it does not combine with --no-push or --dry-run"
        )
    return problems


def _status_examples(paths: list[str], limit: int = 10) -> list[str]:
    """Up to *limit* paths, then one line saying how many more."""
    shown = [p for p in paths[:limit]]
    more = len(paths) - len(shown)
    if more > 0:
        shown.append(f"... and {more} more")
    return shown


def _status_walk_nested(work_tree: Path, nested: str, excludes: list[str]) -> list[str]:
    """The files a snapshot captures under one embedded repository.

    ``stageable_paths`` reports a nested repository as ``nested/`` and the
    snapshot replaces the gitlink with the files under it (``71_embedded``), so
    a change scan that skipped the subtree would call every file inside it
    invisible.  The walk mirrors the capture's skips: the nested ``.git`` and
    the exclude patterns; a junction or symlink is not descended (the capture
    refuses or reports those, and a status that hang-walked a loop would be
    worse than a slightly short answer).
    """
    found: list[str] = []
    root = work_tree / nested
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, work_tree).replace(os.sep, "/")
        keep = []
        for name in dirnames:
            child = name if rel == "." else f"{rel}/{name}"
            if child == f"{nested}/.git" or name == ".git":
                continue
            if any(exclude_matches(child, pat) for pat in excludes):
                continue
            try:
                if getattr(os.lstat(os.path.join(dirpath, name)), "st_file_attributes", 0) & 0x400:
                    continue
            except (OSError, AttributeError):
                pass
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            child = name if rel == "." else f"{rel}/{name}"
            if any(exclude_matches(child, pat) for pat in excludes):
                continue
            found.append(child)
    return found


def _status_junction_filter(source: Path) -> Any:
    """The run's own junction predicate on Windows (git follows a junction, a run leaves it out)."""
    return _junction_filter(source) if os.name == "nt" else None


def _status_inventory(
    git_dir: Path, source: Path, excludes: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """The files a snapshot would capture now: ``(paths, nested_repos, unreadable_dirs)``.

    ``stageable_paths`` is the run's own list (``git ls-files -c -o`` from the
    vault's index point of view, ignored files included), and the excludes are
    applied with the run's own matcher, so a status cannot disagree with a run
    about what belongs.  Embedded repositories are walked separately, as the
    capture does.  A directory git could not open is returned, not dropped: its
    files are missing from the list, and without the name a status would call
    them deleted while the run it forecasts refuses to start.
    """
    nested: list[str] = []
    unreadable_dirs: list[str] = []
    listed = stageable_paths(git_dir, source, nested_out=nested, warnings_out=unreadable_dirs)
    # The three filters a run applies before staging (70_stage): a status that
    # skipped them would call "new" what no run will ever capture.
    behind = _status_junction_filter(source)
    paths = [
        path for path in listed
        if not any(exclude_matches(path, pat) for pat in excludes)
        and path != PAYLOAD_RESERVED_DIR
        and not path.startswith(PAYLOAD_RESERVED_DIR + "/")
        and not is_clock_probe_debris(path)
        and not (behind is not None and behind(path))
    ]
    nested = [d for d in nested if not (behind is not None and behind(d, True))]
    for repo in nested:
        paths.extend(_status_walk_nested(source, repo, excludes))
    return sorted(set(paths)), nested, unreadable_dirs


def _status_tip_facts(git_dir: Path, work_tree: Path, tip: str) -> tuple[str, str, int, int]:
    """``(ISO time, subject, git-stored file count, git-stored bytes)`` of *tip*."""
    meta = git(
        git_dir, work_tree, ["show", "-s", "--format=%cI%x00%s", tip], cwd=work_tree
    ).stdout
    when, _sep, subject = meta.partition("\x00")
    stored = git(
        git_dir, work_tree, ["ls-tree", "-r", "-l", "-z", tip], cwd=work_tree
    ).stdout
    count = 0
    total = 0
    for entry in stored.split("\0"):
        meta_part, _sep, _path = entry.partition("\t")
        parts = meta_part.split()
        if len(parts) == 4 and parts[1] == "blob" and parts[3].isdigit():
            count += 1
            total += int(parts[3])
    return when.strip(), subject.strip(), count, total


def _status_pushed(
    git_dir: Path, source: Path, branch: str, tip: str, remote: str | None
) -> tuple[bool | None, int | None]:
    """Print the ``pushed`` line; ``(pushed?, snapshots taken since)``, ``None`` when unknown.

    What this vault recorded at its last push, not a look at the server: this
    report reads only local state.  A vault with no remote has nothing to say.
    """
    if not remote:
        return None, None
    last = recorded_pushed_tip(git_dir, source, branch)
    if last is None:
        print(
            "pushed        : not recorded -- this vault has not pushed since it began "
            "keeping that record (the next push does)"
        )
        return None, None
    if last == tip:
        print("pushed        : yes -- the newest snapshot is the one this vault last pushed")
        return True, 0
    counted = git(
        git_dir, source, ["rev-list", "--count", f"{last}..{tip}"], cwd=source, check=False
    )
    ahead = int(counted.stdout.strip()) if counted.returncode == 0 and counted.stdout.strip().isdigit() else None
    if ahead:
        print(
            f"pushed        : NO -- {ahead} snapshot(s) were taken after the last push "
            "(a --no-push run, or a push that failed); the next run pushes them"
        )
        return False, ahead
    print("pushed        : NO -- the newest snapshot is not the one this vault last pushed")
    return False, None


def _status_unknown(tip: str | None, **extra: Any) -> dict[str, Any]:
    """The ``--json`` result when nothing can be compared: every key present, the counts null.

    SNAPSHOT.md promises the keys of a mode are only ever added to; a run with no
    vault used to return ``{"snapshots": 0}`` alone, so ``jq -e '.result.status.changed
    == 0'`` read ``null`` and a scheduler could take "no vault" for "all clear".
    Null never equals 0.
    """
    return {
        "tip": tip,
        "comparable": False,
        "changed": None,
        "new": None,
        "deleted": None,
        "unreadable": None,
        "uncompared": None,
        "pushed": None,
        "unpushed": None,
        **extra,
    }


def status_command(
    args: argparse.Namespace,
    source: Path,
    vault: Path,
    remote: str | None,
    code_remote: str | None,
    exclude: list[str],
    branch: str,
) -> int:
    """Print the status report; never stage, hash or upload anything."""
    payload_value, payload_from_default = resolve_payload_value(
        args.payload_min_size, vault, remote=remote, no_push=False,
        no_payload=args.no_payload,
    )
    if payload_value is None:
        payload_mode = "off"
    elif payload_value == "auto":
        payload_mode = f"auto (ratio {resolve_payload_ratio(args.payload_auto_ratio, vault):.2f})"
    else:
        payload_mode = f"files of {payload_value} or more"
    if payload_from_default and payload_mode != "off":
        payload_mode += " -- the default for a run that pushes to a seafile:// remote"
    mtimes_on = resolve_record_mtimes(args, vault, remote=remote, no_push=False)

    print(f"source        : {source}")
    print(f"vault         : {vault} (branch {branch})")
    print(f"remote        : {remote or 'none (a run would be local-only)'}")
    if code_remote:
        print(f"code remote   : {code_remote}")
    print(f"payload mode  : {payload_mode}")
    print(f"excludes      : {', '.join(exclude) if exclude else '(none)'}")
    print(f"mtimes record : {'on' if mtimes_on else 'off'}")

    git_dir = vault / ".git"
    if not git_dir.is_dir():
        print(
            f"no vault at {vault} yet -- nothing has been snapshotted; "
            "a first run creates it"
        )
        RUN_FACTS.update(status=_status_unknown(None, snapshots=0))
        return 0
    holder = VaultLock(git_dir).holder()
    if holder is None:
        print("lock          : free")
    else:
        host, pid, _start = holder
        alive = " (alive)" if host == socket.gethostname() and pid_is_alive(pid) else ""
        print(f"lock          : held by {host}, pid {pid}{alive}")

    tip = current_tip(git_dir, source, branch)
    if tip is None:
        print(f"last snapshot : none -- branch {branch} has no commits yet")
        RUN_FACTS.update(status=_status_unknown(None, snapshots=0))
        return 0

    when, subject, stored_count, stored_bytes = _status_tip_facts(git_dir, source, tip)
    print(f"last snapshot : {when}  \"{subject}\"")
    print(f"  git-stored  : {stored_count:,} file(s), {human_bytes(stored_bytes)}")

    pushed, unpushed = _status_pushed(git_dir, source, branch, tip, remote)
    manifest = load_manifest(git_dir, source, tip)
    payload_by_path: dict[str, dict[str, Any]] = {}
    if manifest is not None:
        for item in manifest.get("files") or []:
            path = item.get("path")
            if isinstance(path, str):
                payload_by_path[path] = item
        payload_bytes = sum(
            int(e.get("size") or 0) for e in payload_by_path.values()
        )
        print(
            f"  payload     : {len(payload_by_path):,} file(s), {human_bytes(payload_bytes)}"
        )
    recorded = _recorded_mtimes(git_dir, source, tip)
    recorded_sizes = _recorded_sizes(git_dir, source, tip)

    if manifest is None and not recorded:
        print(
            "  the last snapshot carries no change records (a snapshot taken by an "
            "older tool, or with --no-record-mtimes and no payload), so what changed "
            "since it cannot be compared; the next snapshot re-establishes them"
        )
        RUN_FACTS.update(status=_status_unknown(tip))
        return 0

    inventory, _nested, unreadable_dirs = _status_inventory(git_dir, source, exclude)
    inventory_set = set(inventory)
    changed: list[str] = []
    new: list[str] = []
    unreadable: list[str] = []
    uncompared = 0
    for path in inventory:
        try:
            st = os.lstat(extended_path(source / path))
        except OSError:
            unreadable.append(path)
            continue
        entry = payload_by_path.get(path)
        if entry is not None:
            if (
                st.st_size != int(entry.get("size") or -1)
                or st.st_mtime_ns != int(entry.get("mtime_ns") or -1)
            ):
                changed.append(path)
        elif path in recorded:
            if st.st_mtime_ns != recorded[path] or (
                path in recorded_sizes and stat.S_ISREG(st.st_mode)
                and st.st_size != recorded_sizes[path]
            ):
                changed.append(path)
        elif recorded:
            new.append(path)
        else:
            # A payload snapshot taken without the mtimes record: no baseline
            # exists for the git-stored files, and "everything is new" would be
            # the report's only possible answer for every one of them.
            uncompared += 1
    known = set(payload_by_path) | set(recorded)
    # What is under an unreadable directory is missing from the listing, not
    # deleted: it stays out of that count and the directory is named instead.
    prefixes = tuple(d.rstrip("/") + "/" for d in unreadable_dirs)
    deleted = sorted(
        p for p in known - inventory_set if not (prefixes and p.startswith(prefixes))
    )
    unreadable.extend(d.rstrip("/") + "/" for d in unreadable_dirs)

    print(
        "Note: the counts below are a size-and-mtime comparison against the records "
        "the last snapshot wrote; they err on both sides -- a change that keeps its "
        "recorded stats hides from this report (size and mtime, for a payload file and for "
        "a git-stored one whose snapshot recorded sizes; mtime alone for an older "
        "snapshot's), and a file "
        "whose time changed without its content shows as changed though the next run "
        "would find nothing new. The snapshot itself re-hashes what it stages. This "
        "report reads only local state and does not contact the remote: the pushed line is "
        "what this vault recorded at its last push, not a look at the server."
    )
    if uncompared:
        print(
            f"  {uncompared:,} git-stored file(s) could not be compared: the last "
            "snapshot has a payload manifest but no mtimes record (--no-record-mtimes); "
            "the next snapshot re-establishes it"
        )
    print(f"  changed since: {len(changed)} file(s), {len(new)} new, {len(deleted)} deleted")
    for line in _status_examples(sorted(changed)):
        print(f"  changed: {line}")
    for line in _status_examples(sorted(new)):
        print(f"  new: {line}")
    for line in _status_examples(deleted):
        print(f"  deleted: {line}")
    for line in _status_examples(sorted(unreadable)):
        print(f"  unreadable: {line}")
    if unreadable_dirs:
        count = len(unreadable_dirs)
        print(
            f"Warning: {count} director{'y' if count == 1 else 'ies'} could not be read, "
            "so what is under it is in none of the counts above; a snapshot run "
            "stops until it can be read or is left out with --exclude"
        )
    print(
        "quota        : not reported -- --status reads only local state; "
        "the library's page on the server names it"
    )
    RUN_FACTS.update(status={
        "tip": tip,
        "comparable": True,
        "last_snapshot": when,
        "changed": len(changed),
        "new": len(new),
        "deleted": len(deleted),
        "unreadable": len(unreadable),
        "uncompared": uncompared,
        "pushed": pushed,
        "unpushed": unpushed,
    })
    return 0


#: The fixed headroom a restore or drill asks of the destination volume
#: before cloning: the vault's git size cannot be known before the transfer,
#: and a checkout roughly doubles the cloned bytes, so this is a floor, not
#: an estimate.
_CLONE_FLOOR_BYTES = 1024 ** 3


def _unrepresentable_paths(repo: Path, rev: str) -> list[str]:
    """Paths in *rev* whose names Windows cannot hold; always empty elsewhere."""
    if os.name != "nt":
        return []
    listing = _run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--name-only", "--full-tree", rev],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if listing.returncode != 0:
        return []  # the checkout below reports whatever is wrong with the revision
    names = (p.decode("utf-8", errors="surrogateescape") for p in listing.stdout.split(b"\0") if p)
    return [name for name in names if windows_name_problem(name) is not None]


def _checkout_around(repo: Path, rev: str, skipped: list[str], *, detach: bool) -> None:
    """Check *rev* out without the paths in *skipped*.

    ``git checkout`` refuses a whole tree for one name Windows cannot hold
    ("error: invalid path 'a:b.txt'") and writes nothing, and relaxing
    ``core.protectNTFS`` to get past it would also lift git's defence against
    hostile NTFS names (8.3 aliases of ``.git``).  So the index is built from
    the revision's own entries minus those paths and written with
    ``checkout-index``; HEAD is pointed at the revision by hand.
    """
    listing = _run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", rev],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if listing.returncode != 0:
        raise SnapshotError(
            f"could not list '{rev}' in {repo} (rc={listing.returncode})\n"
            f"{listing.stderr.decode('utf-8', errors='replace').strip()}"
        )
    drop = set(skipped)
    entries = bytearray()
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        if path.decode("utf-8", errors="surrogateescape") in drop:
            continue
        fields = meta.split()
        entries += fields[0] + b" " + fields[2] + b"\t" + path + b"\0"
    steps = [
        (["read-tree", "--empty"], None),
        (["update-index", "-z", "--add", "--index-info"], bytes(entries)),
        (["update-ref", "--no-deref", "HEAD", rev] if detach
         else ["symbolic-ref", "HEAD", f"refs/heads/{rev}"], None),
        (["-c", "core.longpaths=true", "checkout-index", "-a", "-f"], None),
    ]
    for args, stdin in steps:
        slow = "checkout-index" in args
        proc = _run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            status=f"restoring files into {repo}" if slow else None,
            measure_dir=repo if slow else None,
        )
        if proc.returncode != 0:
            raise SnapshotError(
                f"checkout of '{rev}' failed (rc={proc.returncode})\n"
                f"{proc.stdout.decode('utf-8', errors='replace').strip()}"
            )


def _checkout(repo: Path, rev: str, *, detach: bool = False) -> list[str]:
    """Check *rev* out; return the paths that could not be written (names this OS cannot hold)."""
    if rev.startswith("-"):
        # A remote ref is a file the library writer controls, and a name like
        # "--detach" is a legal one there; handed to git unguarded it parses
        # as the option, and the restore dies with "a branch yet to be born"
        # (REVIEW.sonnet-5.5 E3, reproduced on Windows and Linux).
        raise SnapshotError(
            f"cannot check out {rev!r}: the name reads as a git option. "
            "Restore a differently named branch, or pass a commit id"
        )
    skipped = _unrepresentable_paths(repo, rev)
    if skipped:
        _checkout_around(repo, rev, skipped, detach=detach)
        return skipped
    args = ["git", "-C", str(repo), "-c", "core.longpaths=true", "checkout", "-f"]
    if detach:
        args.append("--detach")
    args.append(rev)
    proc = _run(
        args,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        status=f"restoring files into {repo}",
        measure_dir=repo,
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"checkout of '{rev}' failed (rc={proc.returncode})\n{proc.stdout.strip()}"
        )
    return []


def verify_restore(repo: Path, rev: str, skip: "set[str] | None" = None) -> list[str]:
    """Paths whose bytes on disk disagree with the snapshot's blobs.

    Byte-level on purpose, because the two filter-aware checks can both pass a
    *mangled* restore -- measured on a plain ``git clone`` of the vault:

    * ``git status`` compares *through* the attribute and ``core.autocrlf``
      settings, so a checkout that rewrote every LF as CRLF still reports a
      clean tree;
    * plain ``git hash-object`` applies the clean filter, so it re-normalises
      the very difference being looked for, and matches too.

    ``--no-filters`` hashes the bytes as they lie on disk, which is the only one
    of the three that cannot be fooled.  Our own :func:`restore` happens to pin
    ``core.autocrlf=false`` and ``* -text -filter -ident``, so on *that* tree the
    other two agree as well -- but this check must hold for any checkout,
    including one made by hand, so it does not lean on them.

    Every path is hashed in a single ``hash-object --stdin-paths`` process:
    one subprocess per file made a large restore spend its time in process
    start-up (``ls-tree -z`` emits raw, unquoted paths, which is what the
    NUL-separated stdin expects).  Both sides run in *bytes* end to end, so a
    filename that is not valid UTF-8 is hashed as the filename it actually is
    rather than a ``replace``-decoded approximation of it.
    """
    listing = _run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", rev],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if listing.returncode != 0:
        raise SnapshotError(
            f"could not list '{rev}' in {repo} (rc={listing.returncode})\n"
            f"{listing.stderr.decode('utf-8', errors='replace').strip()}"
        )
    paths: list[bytes] = []
    expected: list[bytes] = []
    symlinks: list[str] = []
    #: Blob SHA for *every* entry, symlinks included.  ``expected`` is parallel
    #: to ``paths``, which deliberately excludes symlinks, so a symlink must not
    #: be looked up by its position in that list: doing so compared the link
    #: against an unrelated file's blob, which reported every correctly-restored
    #: link as a mismatch -- and raised IndexError outright when the tree held
    #: nothing but symlinks.  The SHA is held decoded because it is handed to
    #: :func:`_run` as an element of a ``list[str]`` argument vector.
    blob_sha: dict[str, str] = {}
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) < 3 or fields[1] != b"blob":
            continue
        if skip and path.decode("utf-8", errors="surrogateescape") in skip:
            continue  # a name this OS cannot hold: not restored, and said so
        # A symlink is a blob (mode 120000) holding the link-target
        # string, and the plain hash path cannot verify one: on Windows
        # (core.symlinks=false) git checks it out as a TEXT FILE whose
        # bytes equal the blob, so hash-object would "verify" a file
        # where the source had a link; on POSIX hash-object FOLLOWS the
        # link and hashes the target, so a correct restore would report
        # a spurious mismatch.  Symlinks are verified separately (by
        # reading the link) and never sent through hash-object.
        if fields[0] == b"120000":
            link = path.decode("utf-8", errors="surrogateescape")
            symlinks.append(link)
            blob_sha[link] = fields[2].decode("ascii")
            continue
        paths.append(path)
        expected.append(fields[2])
    if not paths and not symlinks:
        return []
    # --stdin-paths is newline-separated, and git reads a line that starts with
    # a double quote as a C-quoted string -- so a name containing a newline, or
    # starting with `"`, cannot ride in the batch.  Those few are hashed one at
    # a time, as argv, where no quoting rule applies.
    odd = [
        (path, sha) for path, sha in zip(paths, expected, strict=True)
        if b"\n" in path or path.startswith(b'"')
    ]
    if odd:
        keep = [
            (path, sha) for path, sha in zip(paths, expected, strict=True)
            if not (b"\n" in path or path.startswith(b'"'))
        ]
        paths = [path for path, _ in keep]
        expected = [sha for _, sha in keep]
    odd_mismatched: list[str] = []
    for path, sha in odd:
        # `--` and a path relative to -C: a name starting with `-` is a path.
        one = _run(
            ["git", "-C", str(repo), "hash-object", "--no-filters", "--",
             os.fsdecode(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if one.returncode != 0 or one.stdout.strip() != sha:
            odd_mismatched.append(path.decode("utf-8", errors="surrogateescape"))

    # Reported after the batch's own mismatches, with the links.
    mismatched_symlinks: list[str] = list(odd_mismatched)
    for link in symlinks:
        link_path = repo / link
        try:
            actual = os.readlink(str(link_path)).encode("utf-8", errors="surrogateescape")
        except (OSError, ValueError):
            # The entry did not come back as a link at all (the Windows
            # text-file case): that is a difference worth naming.
            mismatched_symlinks.append(link)
            continue
        blob = _run(
            ["git", "-C", str(repo), "cat-file", "blob", blob_sha[link]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if blob.returncode != 0 or blob.stdout != actual:
            mismatched_symlinks.append(link)
    if not paths:
        return mismatched_symlinks
    proc = _run(
        ["git", "-C", str(repo), "hash-object", "--no-filters", "--stdin-paths"],
        input=b"\n".join(paths) + b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        status="verifying restored files (re-hashing every path)",
        # heartbeat-only: this step reads every file and writes nothing, so a
        # stall limit would have no signal to watch.
        stall_limit=None,
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"could not hash the restored files in {repo} (rc={proc.returncode})\n"
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    hashes = proc.stdout.split()
    if len(hashes) != len(expected):
        # git outputs one hash per input path, in order; a short output means
        # something ate a path, and guessing which would be worse than failing.
        # The path it could not open is named on stderr -- append it, or the
        # count mismatch is all the user gets.
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(
            f"hash-object returned {len(hashes)} hashes for {len(paths)} "
            f"paths in {repo}"
            + (f"\n{detail}" if detail else "")
        )
    return [
        path.decode("utf-8", errors="surrogateescape")
        for path, expected_sha, actual_sha in zip(paths, expected, hashes, strict=True)
        if actual_sha != expected_sha
    ] + mismatched_symlinks


def _resolve_assumed_branch(clone: Path, assumed: str) -> str:
    """The branch to restore when the user did not name one.

    The assumed name wins when the remote has it.  Otherwise a remote with
    exactly one branch is unambiguous; with several, naming them beats guessing.
    """
    proc = _run(
        ["git", "-C", str(clone), "for-each-ref", "--format=%(refname)", "refs/remotes/origin"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    prefix = "refs/remotes/origin/"
    names = [
        line[len(prefix):]
        for line in proc.stdout.splitlines()
        if line.startswith(prefix) and line != prefix + "HEAD"
    ]
    if assumed in names or not names:
        return assumed
    if len(names) == 1:
        print(f"Note: the remote has no branch '{assumed}'; restoring its only branch '{names[0]}'")
        return names[0]
    raise SnapshotError(
        f"the remote has no branch '{assumed}' and holds several: {', '.join(sorted(names))}. "
        f"Choose one with --branch."
    )


def restore(
    remote: str,
    into: Path,
    branch: str,
    ref: str | None,
    verify: bool = False,
    branch_explicit: bool = True,
    allow_low_disk: bool = False,
    payload_sample: int | None = None,
) -> None:
    """Materialise a snapshot into a new directory; say what a failure leaves behind.

    A run that fails after the clone leaves a partial tree, and the retry then
    stops with "destination is not empty" -- naming nothing about why it is
    not.  The failure says where the partial restore is and that it has to go
    before trying again.  It is not deleted for the user: a restore that failed
    its byte check is the evidence worth looking at.
    """
    was_empty = not into.exists() or not any(into.iterdir())
    try:
        _restore(remote, into, branch, ref, verify, branch_explicit, allow_low_disk,
                 payload_sample)
    except SnapshotError as exc:
        if was_empty and into.exists() and any(into.iterdir()):
            raise SnapshotError(
                f"{exc}\nThe partial restore was left in {into} for inspection; "
                "delete it before running the restore again."
            ) from exc
        raise


def _restore(
    remote: str,
    into: Path,
    branch: str,
    ref: str | None,
    verify: bool = False,
    branch_explicit: bool = True,
    allow_low_disk: bool = False,
    payload_sample: int | None = None,
) -> None:
    """Materialise a snapshot into a new directory, byte-for-byte.

    ``branch_explicit`` is False when the caller only assumed the default
    branch name.  A restore starts from nothing -- there is no vault here to
    remember a branch from -- so the default may simply be wrong for this
    remote; then the one branch the remote has is used, and several are listed.

    A plain ``git clone`` of the vault is *not* enough on Windows: the clone
    inherits ``core.autocrlf=true`` from Git for Windows' system config, and
    ``info/attributes`` -- which is what makes the vault byte-exact -- is not
    transferred by a clone.  The source's own ``.gitattributes`` *is*
    transferred, so it would re-trigger conversion on checkout.

    So: clone without checking anything out, apply the same byte-exact settings
    to the clone, and only then check out.
    """
    if into.exists() and any(into.iterdir()):
        raise SnapshotError(f"restore destination is not empty: {into}")

    # The payload's need is measured before its download (D13); the git half
    # cannot be — the vault's packed size is unknowable before the transfer —
    # so the destination volume gets a fixed headroom floor instead (the
    # review round's deferred item: the drill and the restore filled temp
    # volumes and kept the partial tree).  A full checkout roughly doubles
    # the cloned bytes, hence the generous floor.
    if not allow_low_disk:
        try:
            free = shutil.disk_usage(into).free
        except OSError:
            free = None
        if free is not None and free < _CLONE_FLOOR_BYTES:
            raise SnapshotError(
                f"the volume holding {into} has {human_bytes(free)} of free space; a restore "
                f"needs at least {human_bytes(_CLONE_FLOOR_BYTES)} of headroom before "
                "the clone, and the vault's git size cannot be known before the "
                "transfer (the payload's need is measured separately, below). "
                "--allow-low-disk tries anyway"
            )

    into.parent.mkdir(parents=True, exist_ok=True)
    print(f"cloning {remote} -> {into} (no checkout) ...")
    # stderr is inherited, not captured: a clone of a large vault transfers for
    # hours, and git's "Receiving objects" progress goes to the terminal only
    # when it is not being piped.  The watchdog adds one line per minute on top.
    proc = _run(
        ["git", "-c", "core.longpaths=true", "clone", "--no-checkout", remote, str(into)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        status=f"cloning {remote}",
        measure_dir=into / ".git",
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"clone failed (rc={proc.returncode})\n{proc.stdout.strip()}"
        )

    ensure_byte_exact(into, into / ".git")

    if not branch_explicit:
        branch = _resolve_assumed_branch(into, branch)

    # A --no-checkout clone has no local branch, so a revision like
    # "snapshot~3" cannot resolve until the branch exists locally.  When the
    # requested revision *is* the branch, checking it out materialises both at
    # once; otherwise the branch ref is created without touching the worktree
    # (``git branch``), and only the requested revision is checked out --
    # checking the branch out first would write the whole tree twice.
    target = branch
    verified_rev = branch
    if ref and ref != branch:
        # Whether the clone already made the branch depends on what the remote
        # advertises as HEAD: a remote whose HEAD is this branch (the helper's
        # case) gets a local branch from `clone --no-checkout`; a bare remote
        # with another HEAD does not.  Creating it unconditionally failed with
        # "a branch named ... already exists" on the first kind.
        exists = _run(
            ["git", "-C", str(into), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).returncode == 0
        if not exists:
            made = _run(
                ["git", "-C", str(into), "branch", branch, f"origin/{branch}"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if made.returncode != 0:
                raise SnapshotError(
                    f"could not materialise '{branch}' in the clone "
                    f"(rc={made.returncode})\n{made.stderr.strip()}"
                )
        proc = _run(
            ["git", "-C", str(into), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise SnapshotError(
                f"cannot resolve '{ref}' in the vault\n{proc.stderr.strip()}"
            )
        sha = proc.stdout.strip()
        unholdable = _checkout(into, sha, detach=True)
        target = f"{ref} ({sha[:12]})"
        verified_rev = sha
    else:
        unholdable = _checkout(into, branch)
    if unholdable:
        shown = "\n".join(f"  {path}" for path in unholdable[:10])
        more = f"\n  ... and {len(unholdable) - 10} more" if len(unholdable) > 10 else ""
        print(
            f"\nWarning: {len(unholdable)} file(s) in this snapshot have names Windows cannot hold "
            f"and were not restored:\n{shown}{more}\nRestore on Linux or macOS to get them; "
            "everything else was restored and is verified below."
        )

    # The snapshot-time warning about embedded repositories is years and
    # machines away by the time someone restores; the restored revision is
    # where the broken directory actually materialises, so repeat it here
    # rather than let the certification below claim byte-exactness for a tree
    # with holes in it.
    restored_gitlinks = find_gitlinks(into / ".git", into, verified_rev)
    if restored_gitlinks:
        print(
            "\nWarning: these paths in the restored tree are embedded git "
            "repositories.  Only\ntheir commit id was recorded at snapshot "
            "time, NOT their contents -- these\ndirectories are empty on "
            "purpose but incomplete as a backup:"
        )
        for path in restored_gitlinks:
            print(f"  {path}")
        print(
            "  Back them up separately, or make them submodules with a "
            "remote that survives."
        )

    # Git stores no timestamps; the record the snapshot wrote (if it wrote one)
    # puts them back.  After the checkout and before the byte check, which
    # reads content only and so is not affected.
    applied, skipped = apply_recorded_mtimes(into)
    if applied or skipped:
        print(f"restored the modification time of {applied} file(s)"
              + (f" ({skipped} could not be set)" if skipped else ""))

    # A restore that differs from the snapshot is not a restore, and the
    # autocrlf class of bug is invisible to `git status` -- so check the bytes.
    mismatched = verify_restore(into, verified_rev, set(unholdable))
    if mismatched:
        shown = "\n".join(f"  {path}" for path in mismatched[:20])
        extra = (
            ""
            if len(mismatched) <= 20
            else f"\n  ... and {len(mismatched) - 20} more"
        )
        raise SnapshotError(
            f"{len(mismatched)} restored file(s) do not match the snapshot "
            f"byte-for-byte:\n{shown}{extra}\n"
            f"The blobs in the vault are intact -- this is a defect in the "
            f"checkout, so please report it rather than trusting the restore."
        )

    if verify:
        proc = _run(
            ["git", "-C", str(into), "fsck", "--no-progress", "--no-dangling"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if proc.returncode != 0:
            raise SnapshotError(
                f"git fsck failed in {into} (rc={proc.returncode})\n"
                f"{proc.stdout.strip()}"
            )
        print("git fsck: object store intact")

    restore_payload(remote, into, allow_low_disk=allow_low_disk, sample=payload_sample)

    files = sum(1 for p in into.rglob("*") if p.is_file() and ".git" not in p.parts)
    print(f"restored {files} files from {target} into {into}")
    print(
        f"verified byte-exact against {verified_rev[:12]}"
        + (f" ({len(unholdable)} unrepresentable name(s) excluded)" if unholdable else "")
    )


def _remove_tree(path: Path) -> None:
    """``rmtree`` that also removes git's read-only object files on Windows."""
    def writable(func: Any, name: str, _exc: Any) -> None:
        os.chmod(name, stat.S_IWRITE)
        func(name)
    shutil.rmtree(extended_path(path), onerror=writable)


def restore_drill(
    remote: str, branch: str, ref: str | None, sample: int, *, branch_explicit: bool,
    allow_low_disk: bool = False, allow_synced: bool = False,
) -> None:
    """Restore into a temporary directory, check it, and delete it.

    The point is the answer to "would a restore work today", asked without the
    operator choosing a destination or cleaning up.  Everything a restore checks
    is checked (every file byte-for-byte, ``git fsck``); only the payload is
    sampled, since it is the part that costs hours.  A failure keeps the
    directory (:func:`restore` names it): a restore that failed is the evidence.
    """
    scratch = Path(tempfile.mkdtemp(prefix="grs-drill-"))
    into = scratch / "restore"
    try:
        check_paths_are_unsynced(("drill directory", scratch), allow_synced=allow_synced)
    except SnapshotError:
        _remove_tree(scratch)
        raise
    started = time.monotonic()
    print(f"restore drill: restoring into {into} (removed afterwards)", flush=True)
    restore(remote, into, branch, ref, verify=True, branch_explicit=branch_explicit,
            allow_low_disk=allow_low_disk, payload_sample=sample)
    seconds = time.monotonic() - started
    _remove_tree(scratch)
    print(f"restore drill passed in {_fmt_elapsed(seconds)}: the snapshot restores byte-exact, "
          "git fsck is clean and every sampled payload file matches its SHA-256", flush=True)
    RUN_FACTS.update(drill_seconds=round(seconds, 1), sample=sample)


def drill_sample(text: str) -> int:
    """The payload files a drill fetches: a positive number, or ``all``."""
    if text == "all":
        return sys.maxsize
    if not text.isdigit() or int(text) < 1:
        raise SnapshotError(f"--restore-drill takes a positive number or 'all', not '{text}'")
    return int(text)


def restore_drill_command(args: argparse.Namespace) -> int:
    if not args.remote:
        raise SnapshotError("--restore-drill requires --remote <url>")
    restore_drill(
        args.remote, args.branch or DEFAULT_BRANCH, args.ref, drill_sample(args.restore_drill),
        branch_explicit=bool(args.branch), allow_low_disk=args.allow_low_disk,
        allow_synced=args.allow_synced,
    )
    return 0


def default_vault_for(source: Path) -> Path:
    """Where the vault lives when ``--vault`` is not given.

    Beside the source, as a sibling: ``/code/myproject`` -> ``/code/myproject-vault``.
    Predictable, easy to find, and inspectable with plain ``git`` -- a vault the
    user cannot locate is a vault they will not trust.
    """
    return source.with_name(source.name + "-vault")


def matched_synced_libraries(path: Path) -> list[dict]:
    """The desktop client's synced libraries whose worktree contains ``path``.

    One dict per containing library, carrying the facts a safety decision needs:
    its name and worktree, whether auto sync is enabled for it (a disable is
    persisted in repo.db as RepoProperty 'auto-sync' = 'false', so this survives
    client restarts -- unlike the global pause, which lives only in the daemon's
    memory), and which seafile-ignore.txt rule covers ``path`` relative to that
    worktree, if any.

    An empty list means "not under a synced folder" -- including when the check
    itself could not run.  Read failures (a corrupt repo.db, an import problem)
    mean "cannot tell", and callers treat empty as safe, so the failure is
    announced on stderr first: silently answering "safe" for a state nobody
    verified is the one thing a fail-closed tool must not do -- the same
    reasoning as safety.check_preflight_safety's transient-failure warning.
    """
    target = path.resolve()
    try:
        import git_remote_seafile.safety as _safety  # sys.path set above
        libraries = _safety.discover_local_synced_libraries()
    except Exception as ex:
        print(
            f"Warning: could not check for Seafile-synced folders ({ex}); "
            "treating the destination as not synced, so the guard could not run. "
            "Check the path is not inside a synced library.",
            file=sys.stderr,
        )
        sys.stderr.flush()
        return []
    matched: list[dict] = []
    for lib in libraries:
        worktree = lib.get("worktree")
        if worktree is None:
            continue
        # Case-insensitive on Windows, identity on POSIX: a library synced to
        # C:\Users\me\Seafile and a vault path spelled c:\users\me\seafile\vault
        # are one directory, and a literal comparison would let the vault
        # escape the guard -- which for the vault is the only guard there is.
        rel = _safety.safe_relative_to(target, Path(worktree))
        if rel is None:
            continue
        # read_seafile_ignore_rules announces an unreadable file on stderr and
        # returns [] for it, so a missing *or* broken ignore file yields no rule:
        # when in doubt the path counts as one the client would touch, and the
        # refusal stands.
        rules = _safety.read_seafile_ignore_rules(Path(worktree))
        matched.append({
            "name": lib.get("name", worktree),
            "worktree": Path(worktree),
            "auto_sync": lib.get("auto_sync", True),
            "ignore_rule": _safety.covering_ignore_rule(rel.as_posix(), rules),
        })
    return matched


def check_paths_are_unsynced(*paths: tuple[str, Path], allow_synced: bool = False) -> None:
    """Refuse to snapshot a source, or write a vault, inside a synced library.

    This is the tool's own version of the helper's Trap 1/Trap 2 guard, and the
    reason it exists is the same: the Seafile client rewrites files underneath
    whatever lives in a synced folder, and it fights Git for control of every
    path Git touches.  The helper cannot see the vault (it is told about the
    *source* via ``GIT_WORK_TREE`` and nothing else), so for the vault this check
    is the only line of defence.

    A containing library is harmless -- and let through with a note on stderr
    saying exactly why -- when its auto sync is disabled (the daemon then
    un-watches that worktree) or the path is covered by that library's
    seafile-ignore.txt (the client never touches it).  Re-enabling auto sync or
    removing the rule makes this guard refuse again.  ``allow_synced`` skips the
    refusal outright; it is the escape hatch for everything else and puts the
    risk on the person passing it, like --allow-shared-remote does.
    """
    for label, path in paths:
        matched = matched_synced_libraries(path)
        if not matched:
            continue

        live = [m for m in matched
                if m["auto_sync"] and m["ignore_rule"] is None]
        names = ", ".join(str(m["name"]) for m in matched)

        if allow_synced:
            print(
                f"Warning: --allow-synced overrides the synced-folder guard for "
                f"{label} ({path}); library {names} can still rewrite files under it",
                file=sys.stderr,
            )
            sys.stderr.flush()
            continue

        if not live:
            for m in matched:
                reason = (f"covered by seafile-ignore.txt rule {m['ignore_rule']!r}"
                          if m["auto_sync"] else "its auto sync is disabled")
                print(
                    f"Note: {label} ({path}) is inside synced library "
                    f"{m['name']} ({m['worktree']}), but the client will not touch it: "
                    f"{reason}. Re-enabling auto sync, or removing that ignore rule, "
                    f"makes this guard refuse again.",
                    file=sys.stderr,
                )
            sys.stderr.flush()
            continue

        raise SnapshotError(
            f"{label} is inside a Seafile-synced library ({names}):\n  {path}\n"
            f"The sync client rewrites files underneath it and competes with "
            f"Git for every path, which is the exact problem this tool "
            f"exists to avoid.\n"
            f"Move it outside the synced folder -- e.g. under C:/code/ -- or "
            f"sync that library with 'auto sync' disabled; pass --allow-synced "
            f"to take responsibility for the risk instead."
        )
def drill_conflicts(args: argparse.Namespace) -> list[str]:
    """What cannot go with ``--restore-drill``: it restores, into a directory it picks."""
    if args.restore_drill is None:
        return []
    problems = [
        f"{flag} does not apply to --restore-drill"
        for flag, value in (
            ("--restore", args.restore), ("--maintain", args.maintain), ("--into", args.into),
            ("--verify", args.verify), ("--vault", args.vault), ("--dry-run", args.dry_run),
            ("--message", args.message), ("--code-remote", args.code_remote),
            ("--exclude", args.exclude), ("--default-excludes", args.default_excludes),
            ("--no-push", args.no_push), ("--source", args.source),
            ("SOURCE (positional)", args.source_positional),
            ("--payload-verify", args.payload_verify is not None),
            ("--payload-gc", args.payload_gc), ("--payload-view", args.payload_view is not None),
            ("--rebuild-index", args.rebuild_index),
        )
        if value
    ]
    try:
        drill_sample(args.restore_drill)
    except SnapshotError as exc:
        problems.append(str(exc))
    return problems


def validate_args(args: argparse.Namespace) -> None:
    """Reject combinations that would otherwise be silently ignored.

    Each of these is a flag that does nothing where it is passed, which is worse
    than a wrong answer: the user believes they asked for something and gets no
    signal either way.  ``--ref`` without ``--restore`` is the sharpest -- a run
    meant to restore an *old* snapshot quietly takes a *new* one instead.
    """
    problems: list[str] = (
        payload_gc_conflicts(args) + payload_conflicts(args) + payload_view_conflicts(args)
        + mtimes_conflicts(args) + status_conflicts(args)
    )

    problems += drill_conflicts(args)
    if args.maintain and args.restore:
        problems.append("--maintain and --restore are different modes")
    if args.maintain:
        # Housekeeping works on the vault's object store alone, so every flag
        # that exists for taking or restoring a snapshot is dead weight here.
        for flag, value in (
            ("--dry-run", args.dry_run),
            ("--message", args.message),
            ("--code-remote", args.code_remote),
            ("--exclude", args.exclude),
            ("--default-excludes", args.default_excludes),
            ("--reset-to-remote", args.reset_to_remote),
            ("--allow-shared-remote", args.allow_shared_remote),
            ("--no-push", args.no_push),
            ("--no-embedded-repos", args.no_embedded_repos),
            ("--verify", args.verify),
            ("--into", args.into),
            ("--ref", args.ref),
            # Housekeeping performs the repack itself; skipping it is a
            # snapshot-run concern.
            ("--no-repack", args.no_repack),
            # There is no staging to re-read in housekeeping.
            ("--rebuild-index", args.rebuild_index),
        ):
            if value:
                problems.append(f"{flag} does not apply to --maintain")
    elif not args.restore:
        for flag, value in (("--verify", args.verify), ("--into", args.into),
                            ("--ref", args.ref)):
            if value and not (flag == "--ref" and args.restore_drill is not None):
                problems.append(f"{flag} only applies to --restore")
    else:
        for flag, value in (
            ("--vault", args.vault),
            ("--dry-run", args.dry_run),
            ("--message", args.message),
            ("--code-remote", args.code_remote),
            ("--exclude", args.exclude),
            ("--default-excludes", args.default_excludes),
            ("--reset-to-remote", args.reset_to_remote),
            ("--allow-shared-remote", args.allow_shared_remote),
            ("--no-push", args.no_push),
            ("--no-embedded-repos", args.no_embedded_repos),
            # A restore fetches and checks out; it packs nothing, so skipping
            # the push-time repack would be silently ignored.
            ("--no-repack", args.no_repack),
            # A restore fetches and checks out; it writes no new objects, so a
            # compression preference here would be silently ignored.
            ("--no-object-compression", args.no_object_compression),
            # The source is meaningless on a restore -- the tree comes from the
            # remote -- and would otherwise be silently ignored.
            ("--source", args.source),
            ("SOURCE (positional)", args.source_positional),
            # A restore stages nothing into a vault index.
            ("--rebuild-index", args.rebuild_index),
        ):
            if value:
                problems.append(f"{flag} does not apply to --restore")

    # Outside --maintain there is no repack to run, so the switch that selects
    # one cannot mean anything (it would otherwise be silently ignored).
    if not args.maintain and args.full_repack:
        problems.append("--full-repack only applies to --maintain")

    if args.no_embedded_repos and (
        args.payload_gc or args.payload_verify is not None or args.payload_view is not None
    ):
        problems.append("--no-embedded-repos only applies to a snapshot run")

    if args.no_push and args.remote:
        problems.append("--no-push and --remote contradict each other")
    if args.no_push and args.code_remote and not (args.maintain or args.restore):
        # Only the typed flag: a remembered code remote under --no-push is a
        # default, and is skipped without complaint.
        problems.append(
            "--code-remote pushes the source repository, which --no-push "
            "forbids; drop one of them"
        )
    if (
        args.source is not None
        and args.source_positional is not None
        and args.source.expanduser().resolve()
        != Path(args.source_positional).expanduser().resolve()
    ):
        problems.append(
            f"two different sources were given: '{args.source_positional}' "
            f"(positional) and '{args.source}' (--source)"
        )

    if problems:
        raise SnapshotError(
            "the arguments do not make sense together:\n  "
            + "\n  ".join(problems)
        )


def resolve_snapshot_settings(
    args: argparse.Namespace,
) -> tuple[Path, Path, str | None, str | None, list[str], str]:
    """Turn a parsed command line into the settings a run actually uses.

    The chain, most specific first:

    1. **the flag you passed** -- always wins;
    2. **what the vault remembered** from its last run;
    3. **the source repository** -- a single ``seafile://`` remote names the
       vault's destination;
    4. **the current directory** for ``--source``, ``'<source>-vault'`` for
       ``--vault``.

    Returns ``(source, vault, remote, code_remote, exclude, branch)``.
    """
    explicit = args.source or args.source_positional
    source = (explicit or Path.cwd()).expanduser().resolve()
    vault = (args.vault or default_vault_for(source)).expanduser().resolve()
    source, vault = resolve_paths(source, vault)

    # A snapshot backs up a repository, as `git push` does: from a subfolder the
    # vault's chain would otherwise be a different tree under the same remote
    # (a 6807-file "deletion" diff on top of the whole repository's vault).
    # A vault that already remembers this very subfolder keeps working -- that
    # is a default, and a default must not turn a run that worked into an error.
    root = repo_toplevel(source)
    if root is not None and root != source and not _vault_remembers(vault, source):
        if explicit:
            raise SnapshotError(
                f"{source} is a subfolder of the repository at {root}.\n"
                f"A snapshot is of the whole repository: give the repository root "
                f"(--source {root}) or run it without --source from inside the "
                f"repository."
            )
        print(f"Note: {source} is inside the repository at {root}; snapshotting "
              f"the whole repository.", flush=True)
        source = root
        vault = (args.vault or default_vault_for(source)).expanduser().resolve()
        source, vault = resolve_paths(source, vault)

    # A vault may not exist yet, so the bottom of the chain is remembered-first:
    # there is nothing to remember until a first run has happened.
    remembered = remembered_settings(vault, source)

    branch = args.branch or str(remembered.get("branch") or DEFAULT_BRANCH)

    remote: str | None
    code_remote: str | None
    if args.no_push:
        # Suppressed for THIS run only.  remember_settings is told (via _UNSET)
        # to leave the vault's memory alone, so a one-off local run does not
        # unhook a scheduled job from its remote; the code repo is not pushed
        # either -- the flag's name promises "do not push at all".
        remote = None
        code_remote = None
    else:
        remote = args.remote or remembered.get("remote")
        if not remote:
            remote = derive_vault_remote(source)
            if remote and not args.dry_run:
                # Said once: after the first complete run the vault remembers it.
                print(
                    f"Note: the vault's destination was not given, so it is {remote}, "
                    "derived from the source's own seafile:// remote -- the same library as "
                    "your code.  Everyone who can read that library can read this snapshot, "
                    ".env files and keys included.  Pass --remote to send it somewhere only "
                    "you can read.",
                    flush=True,
                )
        code_remote = args.code_remote or remembered.get("code_remote")

    exclude = list(args.exclude) if args.exclude is not None else list(
        remembered.get("exclude") or []
    )
    if args.default_excludes:
        tracked: list[str] | None = None
        for pattern in RECOMMENDED_EXCLUDES:
            if pattern in exclude:
                continue
            # The list is a guess about reproducible output; the source's own
            # repository knows better.  "build" must not drop a tracked
            # tools/build/make.py -- a restore would "reconstitute the working
            # directory" without source the user committed.
            if tracked is None:
                tracked = source_tracked_paths(source)
            held = next((path for path in tracked if exclude_matches(path, pattern)), None)
            if held is not None:
                print(
                    f"Note: --default-excludes keeps '{pattern}' for this run: the source's own "
                    f"git repository tracks {held!r}, and a backup that drops committed source "
                    f"is not a backup. Pass --exclude '{pattern}' to leave it out anyway.",
                    flush=True,
                )
                continue
            exclude.append(pattern)
        # Named by what makes it one, not by what it is called (.venv-audit, env).
        for venv in find_virtualenvs(source, exclude):
            if venv not in exclude:
                exclude.append(venv)

    # Everything that can be checked before a single byte is written: a typo in
    # a remote name or a vault inside a synced folder is far cheaper to catch
    # now than after the first push has published a whole tree.
    if remote:
        check_remote_is_not_a_code_remote(
            source, remote, allowed=args.allow_shared_remote
        )
    if code_remote:
        check_code_remote_is_git_remote(source, code_remote)
    check_paths_are_unsynced(
        ("source", source), ("vault", vault), allow_synced=args.allow_synced
    )

    return source, vault, remote, code_remote, exclude, branch


def check_code_remote_is_git_remote(source: Path, name: str) -> None:
    """Fail before the snapshot if ``--code-remote <name>`` is not a remote.

    ``git push origin`` against a name that does not exist fails *after* the
    snapshot is built, which is a confusing half-done state: the vault has a new
    commit for a run the user will read as failed.  A one-line check up front
    turns that into the error it is.
    """
    if not source.is_dir() or not (source / ".git").exists():
        raise SnapshotError(
            f"--code-remote '{name}' was given, but {source} is not a git "
            f"repository, so there is no '{name}' to push to"
        )
    names = {remote_name for remote_name, _url in source_remote_urls(source)}
    if name not in names:
        listing = ", ".join(sorted(names)) or "none"
        raise SnapshotError(
            f"--code-remote '{name}' is not a remote of {source} "
            f"(it has: {listing})"
        )


def _read_config_key(git_dir: Path, key: str) -> str:
    """A single value out of the *vault's own* config, or ''.

    Kept for one-off reads and for tests; the snapshot path itself reads every
    key it needs in a single call via :func:`_read_local_config_map`.
    """
    proc = _run(
        ["git", "--git-dir", str(git_dir), "config", "--local", "--get", key],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return proc.stdout.strip()


def _read_local_config_map(repo: Path) -> dict[str, list[str]]:
    """Every key in a repository's *own* config, in a single git call.

    A run consults five keys in :func:`remembered_settings` and two more in
    :func:`ensure_identity`, and every ``git config`` is a separate process --
    about 125 ms on Windows, nearly all of it process start-up.  One
    ``--local --null --list`` answers them all at once.

    ``--null`` is what makes the output unambiguous: a value may itself contain
    a newline, so each pair is NUL-*terminated* rather than newline-separated.
    ``--local`` keeps the global, system and command-scope values out, which is
    the question every caller here is actually asking -- what does *this*
    repository remember?  ``-C`` rather than ``--git-dir`` because a source
    repository may use a ``.git`` *file* (a submodule or a linked worktree),
    which ``--git-dir`` cannot open.
    """
    proc = _run(
        ["git", "-C", str(repo), "config", "--local", "--null", "--list"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return {}
    found: dict[str, list[str]] = {}
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        key, sep, value = entry.partition("\n")
        if sep:
            found.setdefault(key, []).append(value)
    return found


#: Sentinel for :func:`remember_settings`: "this setting was suppressed for
#: *this* run -- leave whatever the vault remembers alone".  Distinct from
#: ``None``, which means "the setting is empty; clear it".
_UNSET = object()


def _same_config_value(stored: str, desired: str) -> bool:
    """Whether two spellings of the path-valued ``snapshot.source`` agree.

    git stores config values verbatim -- backslashes and all -- so a value
    written by this tool round-trips exactly.  The normalisation covers the
    other writers: a value typed into the config file by hand (or by a shell
    whose path mangling rewrites separators) may spell the same directory with
    ``/`` where this tool wrote ``\\``.  Only ``snapshot.source`` holds a
    filesystem path; for every other key a backslash is data, so they are
    compared literally (see :func:`remember_settings`).
    """
    return stored.replace("\\", "/") == desired.replace("\\", "/")


class _Remembered(TypedDict, total=False):
    """What a vault remembers, keyed exactly as :func:`remember_settings` writes it.

    ``total=False`` because a vault remembers only what it has been told: a
    fresh vault has written nothing, and a key stays absent until a run sets it.
    A ``TypedDict`` rather than ``dict[str, object]`` so that the resolution
    chain reads ``str | None`` / ``list[str] | None`` out of ``.get`` instead of
    ``object``, which is what the defaults are actually made of.
    """

    remote: str
    code_remote: str
    branch: str
    exclude: list[str]


def remembered_settings(vault: Path, source: Path) -> _Remembered:
    """What the vault remembers from its previous run, if anything.

    The vault is the bottom of the resolution chain, and it is also where every
    resolved choice is *written* at the end of a run -- so ``seafile_snapshot.py
    --source C:/code/app`` once, then ``seafile_snapshot.py`` with no arguments
    at all from any directory, does the same thing again.

    A vault belongs to exactly one source directory, so a mismatch here is a
    mistake worth stopping for rather than silently snapshotting the wrong tree
    into it.  Passing ``--vault`` and ``--source`` together *is* that mismatch,
    so the refusal names both and the flag that fixes it.
    """
    git_dir = vault / ".git"
    if not git_dir.is_dir():
        return {}

    config = _read_local_config_map(vault)

    def first(key: str) -> str:
        values = config.get(key)
        return values[0] if values else ""

    remembered_source = first("snapshot.source")
    if remembered_source:
        resolved = Path(remembered_source).expanduser().resolve()
        if resolved != source.resolve():
            raise SnapshotError(
                f"this vault belongs to a different source directory:\n"
                f"  vault    : {vault}\n"
                f"  remembers: {resolved}\n"
                f"  this run : {source.resolve()}\n"
                f"A vault holds one linear chain of snapshots of one tree; "
                f"snapshotting a second tree into it would mix the two.\n"
                f"Point --source at the remembered directory, or give this tree "
                f"its own --vault."
            )

    remembered: _Remembered = {}
    remote = first("snapshot.remote")
    if remote:
        remembered["remote"] = remote
    code_remote = first("snapshot.coderemote")
    if code_remote:
        remembered["code_remote"] = code_remote
    branch = first("snapshot.branch")
    if branch:
        remembered["branch"] = branch
    excludes = [v for v in config.get("snapshot.exclude", []) if v]
    if excludes:
        remembered["exclude"] = excludes
    return remembered


def _vault_remembers(vault: Path, source: Path) -> bool:
    """Whether ``vault`` already exists and records exactly ``source`` as its tree."""
    if not (vault / ".git").is_dir():
        return False
    stored = _read_local_config_map(vault).get("snapshot.source") or []
    return bool(stored) and _same_config_value(stored[0], str(source))


def remember_settings(
    vault: Path,
    source: Path,
    *,
    remote: object,
    code_remote: object,
    exclude: list[str],
    branch: str,
    vault_config: dict[str, list[str]] | None = None,
) -> None:
    """Persist the resolved choices into the vault's own config.

    Written with ``git config --local``, so it lives and travels with the vault
    and nothing is put in the user's global config.

    ``remote`` and ``code_remote`` accept three kinds of value: a string (the
    setting to remember), ``None`` (the setting is empty for this run -- clear
    what was remembered), or :data:`_UNSET` (the setting was *suppressed* for
    this run, e.g. by ``--no-push`` -- leave what the vault remembers alone, so
    a one-off local run does not unhook a scheduled job from its remote).

    Only a value that actually *changed* is written, and a value that is being
    cleared is ``--unset-all``'d.  Re-writing every key on every run would be
    correct but wasteful: each ``git config`` is its own process, and a routine
    run has nothing new to say.  The five-and-more writes a first run makes
    collapse to none once the vault is settled -- measured on this machine, the
    config plumbing was a little over half of a run's subprocess calls.

    A single-valued key is set with a plain ``git config <key> <value>``, which
    replaces whatever was there; only a key that is being *emptied* needs the
    explicit ``--unset-all``.
    """
    settings: list[tuple[str, list[str], bool]] = [
        # (key, values this run, compare path-normalised)
        ("snapshot.source", [str(source)], True),
        ("snapshot.remote", [remote] if isinstance(remote, str) and remote else [], False),
        (
            "snapshot.coderemote",
            [code_remote] if isinstance(code_remote, str) and code_remote else [],
            False,
        ),
        ("snapshot.branch", [branch], False),
        # Repeated --exclude flags would otherwise persist the same pattern
        # twice; the stored list is what --default-excludes and the printed
        # report read back, so keep it a set of unique values in first-seen
        # order (SNAP-12).
        ("snapshot.exclude", list(dict.fromkeys(exclude)), False),
    ]
    suppressed = {key for key, value in (("snapshot.remote", remote),
                                         ("snapshot.coderemote", code_remote))
                  if value is _UNSET}

    if vault_config is None:
        vault_config = _read_local_config_map(vault)

    def unchanged(stored: list[str], values: list[str], path_key: bool) -> bool:
        if len(stored) != len(values):
            return False
        if path_key:
            return all(
                _same_config_value(a, b) for a, b in zip(stored, values, strict=True)
            )
        return all(a == b for a, b in zip(stored, values, strict=True))

    def run(key: str, *args: str, ok_rc: tuple[int, ...] = (0,)) -> None:
        proc = _run(
            ["git", "-C", str(vault), "config", "--local", *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode not in ok_rc:
            # A silent failure here would leave the vault remembering a
            # previous run's settings while this run believes it has updated
            # them -- exactly the divergence the next bare run then trusts.
            raise SnapshotError(
                f"could not update the vault's memory ({key}, "
                f"rc={proc.returncode})\n{proc.stderr.strip()}"
            )

    for key, values, path_key in settings:
        if key in suppressed:
            continue
        stored = vault_config.get(key, [])
        if unchanged(stored, values, path_key):
            continue
        if not values:
            # `--unset-all` exits 5 when the key is absent; that is not an error.
            run(key, "--unset-all", key, ok_rc=(0, 5))
        elif len(values) == 1 and len(stored) <= 1:
            # A single value over a single (or absent) key: a plain set replaces
            # it.  This is the common shape, and one call instead of two.
            run(key, key, values[0])
        else:
            # Either several values now, or a multi-valued key being rewritten
            # -- git refuses to overwrite multiple values with one, so the old
            # values are cleared first or they would linger.
            run(key, "--unset-all", key, ok_rc=(0, 5))
            for value in values:
                run(key, "--add", key, value)


def source_seafile_remote(source: Path) -> str | None:
    """The one ``seafile://`` remote the source repository pushes to, if any.

    Deriving the vault's destination from the code remote is the most useful
    default available, because the two are almost always siblings -- the code
    library and its snapshot library, side by side.  It is only used when the
    *source* has exactly one such remote; two is ambiguous, and guessing wrong
    puts a whole-tree backup somewhere the user did not choose.
    """
    found: list[str] = []
    for _name, url in source_remote_urls(source):
        if url.lower().startswith("seafile://") and url not in found:
            found.append(url)
    if len(found) == 1:
        return found[0]
    return None


def derive_vault_remote(source: Path) -> str | None:
    """The natural Seafile destination for the vault of ``source``.

    ``seafile://code/myproject`` -> ``seafile://code/myproject-vault``: the same
    library and path, suffixed, so the backup sits beside the thing it backs up
    and is recognisably related to it.
    """
    # `git -C <subdir> config --local` finds the enclosing repository's remotes,
    # so a subfolder of /code/app would derive app-vault -- the vault of the
    # WHOLE tree -- adopt its chain as the base and push a snapshot of the
    # subfolder on top of it (seen: a 6807-file "deletion" diff).  The remote
    # names the repository's root, so only the root may derive from it.
    if not (source / ".git").exists():
        return None
    code = source_seafile_remote(source)
    if not code:
        return None
    return f"{code.rstrip('/') or code}-vault"


def repo_toplevel(path: Path) -> Path | None:
    """The root of the git work tree that contains ``path``, or None.

    ``rev-parse --show-toplevel`` rather than looking for ``.git`` upwards: a
    linked worktree and a submodule keep ``.git`` as a file, and for a submodule
    the answer must be the submodule's own root, not the outer repository's.
    """
    proc = _run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    top = proc.stdout.strip() if proc.returncode == 0 else ""
    if not top:
        return None
    return Path(top).resolve()


# -- Run report: exit-code classes and --json (SNAPSHOT.md, "Exit codes and --json") ----

#: The exit codes a script can rely on.  1 and 2 are what the tool always did; 3 is
#: new and opt-in (``--exit-warnings``), because a run that exited 0 with a warning
#: must not start failing a scheduled job that was working.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_WARNINGS = 3

#: Facts a run records for the ``--json`` document, next to ``END_OF_RUN_NOTES``:
#: the code that knows them says them, and nothing reads them unless asked.
RUN_FACTS: dict[str, Any] = {}


_WARNING_LIMIT = 500


class _LineTap:
    """A text stream that passes everything through and remembers some of its lines.

    ``Warning:`` and ``Error:`` are printed from dozens of places, some after a
    blank line and some from a child process's captured output; reading them where
    they leave the process finds all of them without a second path to keep in step.
    """

    def __init__(
        self, stream: Any, warnings: list[str], errors: list[str], seen: set[str]
    ) -> None:
        self._stream = stream
        self._warnings = warnings
        self._errors = errors
        self._seen = seen
        self._partial = ""
        self._open: int | None = None
        self._open_error: int | None = None

    def write(self, text: str) -> int:
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            self._see(line)
        return self._stream.write(text)

    def _see(self, line: str) -> None:
        stripped = line.strip()
        if self._open is not None:
            # A warning is wrapped over several lines (and may list paths); it ends
            # at the blank line that follows it.  Capped, so a stray never swallows
            # the run's output.
            if stripped and len(self._warnings[self._open]) < _WARNING_LIMIT:
                self._warnings[self._open] += " " + stripped
                return
            self._open = None
        if self._open_error is not None:
            # An error is wrapped the same way -- the house style lists the
            # affected paths on the following lines -- and `.error` carrying
            # only the headline loses exactly the part a scheduler would act
            # on (REVIEW.iq3_s E4, reproduced).
            if stripped and len(self._errors[self._open_error]) < _WARNING_LIMIT:
                self._errors[self._open_error] += " " + stripped
                return
            self._open_error = None
        if stripped.startswith("Warning:") and stripped not in self._seen:
            self._seen.add(stripped)
            self._warnings.append(stripped)
            self._open = len(self._warnings) - 1
        elif stripped.startswith("Error:"):
            self._errors.append(stripped)
            self._open_error = len(self._errors) - 1

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def run_mode(args: argparse.Namespace) -> str:
    for flag, mode in (
        ("restore_drill", "restore-drill"), ("restore", "restore"), ("maintain", "maintain"),
        ("payload_verify", "payload-verify"), ("payload_view", "payload-view"),
        ("payload_gc", "payload-gc"), ("status", "status"),
    ):
        if getattr(args, flag, None) not in (None, False):
            return mode
    return "snapshot"


def run_reported(
    args: argparse.Namespace, body: Callable[[argparse.Namespace], int]
) -> int:
    """Run *body*; apply ``--exit-warnings`` and write the ``--json`` document.

    With neither flag this is ``body(args)`` and nothing else, so the default
    behaviour (streams, exit codes) is exactly what it was.  With ``--json`` the
    human text moves to stderr and stdout carries one line: the document.
    """
    if not (args.json or args.exit_warnings):
        return body(args)
    RUN_FACTS.clear()
    warnings: list[str] = []
    errors: list[str] = []
    real_out, real_err = sys.stdout, sys.stderr
    seen: set[str] = set()
    sys.stdout = _LineTap(real_err if args.json else real_out, warnings, errors, seen)
    sys.stderr = _LineTap(real_err, warnings, errors, seen)
    started = time.monotonic()
    rc: int | None = None
    try:
        rc = body(args)
    except KeyboardInterrupt:
        # A scheduler's SIGINT is a run outcome, not a reason to die with a
        # traceback and no document at all: the contract says stdout is then
        # exactly one JSON line, and it is precisely the interrupted runs a
        # scheduler most needs to classify (REVIEW.iq3_s D7/E5, reproduced:
        # rc=-2, empty stdout).
        sys.stderr.write("Error: interrupted by a signal\n")
        sys.stderr.flush()
        rc = EXIT_FAILED
    except BaseException as exc:
        # run_command catches SnapshotError; anything else (an OSError from a
        # raw file operation, a GitError that escaped a wrapper) would leave
        # the same document-less traceback.  Reported, then re-raised: an
        # unexpected exception is a bug, and hiding it behind status "failed"
        # would hide the traceback from the person who can fix it.
        sys.stderr.write(f"Error: {type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        rc = EXIT_FAILED
        raise
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        if rc == EXIT_OK and warnings and args.exit_warnings:
            rc = EXIT_WARNINGS
        if args.json:
            status = (
                "failed" if rc in (EXIT_FAILED, EXIT_USAGE)
                else "warnings" if warnings else "ok"
            )
            document = {
                "schema": 1,
                "tool": "git-seafile-snapshot",
                "version": tool_version(),
                "mode": run_mode(args),
                "status": status,
                "exit_code": rc,
                "seconds": round(time.monotonic() - started, 1),
                "warnings": warnings,
                "error": errors[-1].removeprefix("Error:").strip() if errors else None,
                "result": dict(RUN_FACTS),
            }
            real_out.write(json.dumps(document, sort_keys=True) + "\n")
            real_out.flush()
    return rc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="git-seafile-snapshot",
        description=(
            "Snapshot a working tree (gitignored files included) into a separate "
            "vault repository and push it to Seafile. The source directory is "
            "never modified.\n\n"
            "Every option has a default, so a bare run works: the source is the "
            "current directory, the vault is '<source>-vault' beside it, and the "
            "remote, branch and exclusions are remembered in the vault after the "
            "first run. An explicit flag always wins, and is written back to the "
            "vault for next time."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "defaults, in the order they are tried:\n"
            "  1. the flag you passed\n"
            "  2. what the vault remembered from its last run\n"
            "  3. the source repository (a seafile:// remote names the vault's\n"
            "     destination: seafile://code/app -> seafile://code/app-vault)\n"
            "  4. the current directory (--source) / '<source>-vault' (--vault)\n"
            "     (inside a git repository, the repository root: a snapshot is of the\n"
            "     whole repository, and --source naming a subfolder is refused)\n"
            "\nrecommended exclusions (never applied silently -- this is what\n"
            "--default-excludes adds):\n  "
            + " ".join(f"--exclude '{p}'" for p in RECOMMENDED_EXCLUDES)
            + "\n  plus each Python virtualenv found (a directory holding pyvenv.cfg)"
            "\n\nA pattern with no slash matches that name at ANY depth, so\n"
            "--exclude node_modules also drops packages/*/node_modules.\n"
            "Quote wildcards so your own shell does not expand them first.\n"
            + PAYLOAD_EPILOG
            + "\nNote: neither the source nor the vault may live inside a\n"
            "Seafile-synced folder -- the sync client rewrites files underneath\n"
            "it and competes with git for every path. Checked at startup.\n"
            "The guard lets a synced path through when that library's auto sync\n"
            "is disabled or seafile-ignore.txt covers it; --allow-synced\n"
            "overrides the refusal entirely.\n"
            "\nlong steps (staging large trees, first pushes of large vaults,\n"
            "restores) print one status line to stderr every minute while they\n"
            "run; push and clone also stream git's own progress live.  A step\n"
            "with no measurable progress for 30 minutes is killed as stalled.\n"
            "The remote helper auto-compacts a busy vault after each push --\n"
            "that runs inside the push step and shows up under its status lines.\n"
        ),
    )
    parser.add_argument(
        "source_positional",
        nargs="?",
        type=Path,
        default=None,
        metavar="SOURCE",
        help="directory to snapshot (same as --source, so "
        "'seafile_snapshot.py C:/code/app' works)",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="directory to snapshot (default: the current directory)",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault repository location (default: '<source>-vault' beside the "
        "source)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help=f"branch in the vault holding snapshots "
        f"(default: remembered, else {DEFAULT_BRANCH})",
    )
    parser.add_argument(
        "--remote",
        default=None,
        help="Seafile remote URL to push to, e.g. seafile://code/myproject-vault "
        "(default: remembered, else derived from the source's own seafile:// "
        "remote; pass --no-push to snapshot locally instead)",
    )
    parser.add_argument(
        "--code-remote",
        default=None,
        help="also run a normal 'git push <name>' in the source repo first "
        "(default: remembered, else the code repo is not pushed)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help="path matching this glob is left out of the snapshot (repeatable; "
        "default: remembered, else nothing is excluded)",
    )
    parser.add_argument(
        "--default-excludes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="add the recommended exclusions on top of any --exclude "
        f"({', '.join(RECOMMENDED_EXCLUDES)}) and any Python virtualenv (a "
        "directory holding a pyvenv.cfg, whatever its name). A pattern is kept for the run "
        "when the source's own git repository tracks a file it matches. Not on by default: a snapshot is "
        "a backup, and silently dropping files is how a backup fails at the one "
        "moment it mattered",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="this run pushes nothing -- neither the vault nor the code repo "
        "-- and leaves the vault's remembered remote alone, so a one-off "
        "local run does not unhook a scheduled job",
    )
    parser.add_argument("--message", default=None, help="snapshot commit message")
    parser.add_argument(
        "--restore",
        action="store_true",
        help="restore a snapshot instead of taking one (needs --remote and "
        "--into); byte-exact, unlike a plain git clone",
    )
    parser.add_argument(
        "--into",
        type=Path,
        default=None,
        help="destination directory for --restore",
    )
    parser.add_argument(
        "--ref",
        default=None,
        help=f"snapshot to restore (default: the vault branch, "
        f"{DEFAULT_BRANCH}); any revision works, e.g. 'snapshot~3' to roll back",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the snapshot but do not move the branch or push "
        "(the vault is still initialised if missing, so its location and "
        "config can be inspected; the remote is still fetched, so the diff is "
        "shown against the real base). It is a full run up to the commit: the "
        "git-bound part of the tree is staged into the vault, so it takes the "
        "time and disk a real run would, and it uploads nothing",
    )
    parser.add_argument(
        "--reset-to-remote",
        action="store_true",
        help="if the local vault and the remote have diverged, discard the "
        "local-only snapshots and continue from the remote chain instead of "
        "refusing",
    )
    parser.add_argument(
        "--allow-shared-remote",
        action="store_true",
        help="permit --remote to be a URL the source repository already uses. "
        "The snapshot holds the files .gitignore keeps out of the code "
        "repository, so this publishes them; refused by default",
    )
    parser.add_argument(
        "--allow-synced",
        action="store_true",
        help="proceed even when the source or vault (or a restore destination) "
        "is inside a Seafile-synced library. The guard already lets this through "
        "when that library's auto sync is disabled or the path is covered by its "
        "seafile-ignore.txt; this overrides every remaining refusal and puts the "
        "risk of the client rewriting files under Git on you",
    )
    parser.add_argument(
        "--allow-low-disk",
        action="store_true",
        help="stage even when the estimate says the vault's volume cannot hold "
        "the tree. The estimate is an upper bound (tree size, scaled by the "
        "sampled compressibility, minus what the vault already stores); this "
        "is for data you know is more compressible than the sample suggests. "
        "With --restore: download a payload that does not appear to fit",
    )
    add_payload_groups(parser)
    add_mtimes_arguments(parser)
    add_status_arguments(parser)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="with --restore, also run 'git fsck' on the clone. Byte-exactness "
        "is checked on every restore regardless",
    )
    parser.add_argument(
        "--restore-drill",
        nargs="?",
        const="10",
        default=None,
        metavar="N",
        help="prove the newest snapshot of --remote restores: clone it into a "
        "temporary directory, check every file byte-for-byte and run git fsck, "
        "download N randomly chosen payload files (default 10) and check their "
        "SHA-256, then delete the directory. Needs the disk for the whole vault "
        "(set TMP or TMPDIR to use a bigger volume); --ref drills an older snapshot",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write one JSON document to stdout (status, exit_code, warnings, error, "
        "result) and move the human text to stderr; for scheduled runs",
    )
    parser.add_argument(
        "--exit-warnings",
        action="store_true",
        help="exit 3 instead of 0 when the run completed but printed a Warning:",
    )
    parser.add_argument(
        "--no-object-compression",
        action="store_true",
        help=        "disable deflation for newly written vault objects "
        "(repo-local core.compression=0, stored at full size). For trees "
        "dominated by already-"
        "compressed payloads -- zip/SAFE archives, images, video -- deflation "
        "saves nothing and costs 2-3x the staging time; this flag removes it. "
        "For compressible text-heavy trees it trades some vault size for speed",
    )
    parser.add_argument(
        "--maintain",
        action="store_true",
        help="local vault housekeeping instead of a snapshot: by default just "
        "reports on the object store; with --full-repack it consolidates the "
        "whole store into one packfile and prunes unreachable objects. Never "
        "touches the source tree or any remote",
    )
    parser.add_argument(
        "--full-repack",
        action="store_true",
        help="with --maintain, run a one-shot full repack + prune of the entire "
        "object store. Ordinary snapshot runs already pack the loose tier "
        "incrementally before each push (once it passes roughly 256 MiB); "
        "use this after large deletions to reclaim space",
    )
    parser.add_argument(
        "--no-embedded-repos",
        action="store_true",
        help="record a nested git repository (a directory with its own .git) only as "
        "its commit id, as 0.9.0 did, instead of capturing its files",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="drop the vault's index before staging, so every git-stored file is read and "
        "hashed again. The git-side counterpart of --payload-rehash: an edit that left size "
        "and timestamp untouched (a memory-mapped writer on Windows, a volume with no change "
        "time) is invisible to the stat cache, and this is the audit that re-reads the bytes",
    )
    parser.add_argument(
        "--no-repack",
        action="store_true",
        help="skip the automatic incremental repack that runs before a push",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # A path that is not UTF-8 reaches us as lone surrogates (see git()); the
    # default strict stdout would turn naming it into a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass
    return run_reported(parse_args(argv), run_command)


def run_command(args: argparse.Namespace) -> int:
    END_OF_RUN_NOTES.clear()
    # Set when the push landed but a check *behind* it failed: the run exits
    # non-zero without unwinding past the remember block.
    late_failure = False

    try:
        validate_args(args)
        payload_rc = payload_command(args)
        if payload_rc is not None:
            return payload_rc
        if args.restore_drill is not None:
            return restore_drill_command(args)
        if args.restore:
            if not args.remote:
                raise SnapshotError("--restore requires --remote <url>")
            if not args.into:
                raise SnapshotError("--restore requires --into <directory>")
            into = args.into.expanduser().resolve()
            # The snapshot guard protects where the tool *writes* a tree; a
            # restore writes one too, and a restored tree -- secrets included,
            # by design -- inside a synced library would be uploaded straight
            # back into its original library by the desktop client.
            check_paths_are_unsynced(
                ("restore destination", into), allow_synced=args.allow_synced
            )
            restore(
                args.remote,
                into,
                args.branch or DEFAULT_BRANCH,
                args.ref,
                verify=args.verify,
                branch_explicit=bool(args.branch),
                allow_low_disk=args.allow_low_disk,
            )
            RUN_FACTS.update(into=str(into), ref=args.ref or args.branch or DEFAULT_BRANCH)
            return 0

        source, vault, remote, code_remote, exclude, branch = (
            resolve_snapshot_settings(args)
        )

        # --status shares the resolution ladder so it reports what the next run
        # would use, and stops before anything that writes: the vault, its
        # identity settings and the remembered values are read, never made.
        if args.status:
            return status_command(
                args, source, vault, remote, code_remote, exclude, branch
            )

        # Housekeeping diverges here on purpose: it must not push the code
        # repo, initialise identity settings or remember anything -- only read
        # and tidy the vault's own object store.
        if args.maintain:
            return maintain_vault(vault, full_repack=args.full_repack)

        if args.dry_run and remote:
            print(f"dry run -- would push the vault to {remote}")

        args.payload_min_size, payload_default = resolve_payload_value(
            args.payload_min_size, vault, remote=remote, no_push=args.no_push,
            no_payload=args.no_payload,
        )
        if args.payload_report_days is not None and not payload_by_default_possible(
            remote, args.no_push
        ):
            raise SnapshotError(
                "--payload-report-days needs a run that pushes to a seafile:// remote "
                "(there is no payload store to report on here)"
            )
        payload_ratio = resolve_payload_ratio(args.payload_auto_ratio, vault)
        payload_survey = new_survey()
        payload_min, payload_candidates, payload_rule = plan_payload(
            args.payload_min_size, remote, source, exclude,
            ratio=payload_ratio, ratio_typed=args.payload_auto_ratio is not None,
            survey=payload_survey,
        )
        tuning_kind = tuning_mode(
            args.payload_auto_ratio, vault, dry_run=args.dry_run,
            auto_rule=payload_rule == "auto" and bool(payload_min), remote=remote,
            has_history=(vault / ".git").exists()
            and current_tip(vault / ".git", source, branch) is not None,
        )
        payload_tuning = (
            tune_payload_ratio(remote or "", source, payload_survey, forced=tuning_kind == "forced")
            if tuning_kind else None
        )
        if payload_tuning:
            print(payload_tuning["message"], flush=True)
            if payload_tuning.get("ratio") is not None and payload_tuning["ratio"] != payload_ratio:
                payload_ratio = payload_tuning["ratio"]
                band = {rel: seen for _size, seen, rel in payload_survey["mid"]}
                payload_survey = new_survey()
                payload_min, payload_candidates, payload_rule = plan_payload(
                    args.payload_min_size, remote, source, exclude,
                    ratio=payload_ratio, ratio_typed=args.payload_auto_ratio is not None,
                    survey=payload_survey, sampled=band,
                )
        payload_skip = frozenset(payload_candidates)
        git_dir = ensure_vault(vault, branch)
        # One read of the vault's config serves identity, byte-exactness and
        # the remembered settings below: none of them writes a key another one
        # compares against, so the map cannot go stale between the three.
        vault_config = _read_local_config_map(vault)
        if payload_tuning and payload_tuning.get("upload"):
            # Just measured, not yet remembered (that waits for a complete run).
            vault_config["snapshot.uplink"] = [str(payload_tuning["upload"])]
            vault_config["snapshot.uplink1"] = [str(payload_tuning["single"])]
        if payload_rule == "auto" and payload_min and (payload_candidates or payload_survey["mid"]):
            for line in placement_lines(
                payload_survey, payload_candidates, payload_ratio, source, vault_config
            ):
                print(line, flush=True)
        if not args.dry_run:
            # A preview writes nothing -- not even the byte-exactness config
            # and the inherited identity.  Those writes were safe-looking but
            # a "pure preview" that pins identity and attributes is mutating
            # state a later real run will commit under, without disclosure
            # Reads are harmless and still happen, so a dry run reports the
            # same settings a real run would apply.
            ensure_identity(vault, source, vault_config=vault_config)
            ensure_byte_exact(vault, git_dir, vault_config=vault_config)
            if args.no_object_compression:
                apply_large_payload_profile(vault, git_dir)
            else:
                choose_profile(vault, git_dir, source, exclude, vault_config, write=True,
                               skip=payload_skip or None)
        else:
            # The identity *check* still runs on a preview -- failing
            # early with the actionable message beats a commit-tree
            # traceback -- but read-only: a dry run writes nothing at all
            ensure_identity(vault, source, vault_config=vault_config,
                            write_identity=False)
            if not args.no_object_compression:
                choose_profile(vault, git_dir, source, exclude, vault_config, write=False,
                               skip=payload_skip or None)
        # Free what a killed earlier run left behind first, under the vault
        # lock: it can be tens of GiB, and the free-space check that follows is
        # only honest once it is gone.
        if not args.dry_run:
            with VaultLock(git_dir):
                sweep_temp_packs(git_dir)
                if args.rebuild_index:
                    # The git-side audit: without index entries, staging reads
                    # and re-hashes every file, so an edit that left the stat
                    # cache's fields untouched (a memory-mapped writer, a
                    # volume with no change time) is caught by the bytes.
                    (git_dir / "index").unlink(missing_ok=True)
                    print("rebuilding the vault index: every git-stored file is re-read")
        # Before a byte is staged: a tree the volume cannot hold must be refused
        # now, not discovered when the disk fills (a dry run stages too).
        staging_expect = check_free_space(
            git_dir, vault, source, exclude, vault_config,
            allow_low_disk=args.allow_low_disk, skip=payload_skip or None,
        )
        # The hostname is in the default message because a shared vault is a
        # single chain of snapshots from several machines, and the commit author
        # is the same person on all of them -- so without this, `git log` cannot
        # say which machine saw which tree.  The offset is there because those
        # machines need not share a timezone.
        message = args.message or (
            f"snapshot {datetime.now().astimezone().isoformat(timespec='seconds')} "
            f"on {socket.gethostname()}"
        )

        refuse_flagless_payload_vault(git_dir, source, branch, payload_min)
        remote_tip: str | None = None
        with VaultLock(git_dir):
            # The code push happens here -- identity is settled and no other
            # run holds the vault -- rather than first thing: every check that
            # can refuse this run has already had its say, so a refused run no
            # longer leaves the code pushed and the run reported failed.
            if code_remote:
                if args.dry_run:
                    print(
                        f"dry run -- would push the source repo to "
                        f"'{code_remote}'"
                    )
                else:
                    print(f"pushing source repo to '{code_remote}' ...")
                    proc = _run(
                        ["git", "-C", str(source), "push", code_remote],
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    sys.stdout.write(proc.stdout)
                    if proc.returncode != 0:
                        raise SnapshotError(
                            f"pushing the source repo failed (rc={proc.returncode}); "
                            f"snapshot aborted before the vault was changed"
                        )

            # Decide the base *after* consulting the remote, so a second machine
            # appends to the shared chain rather than building an unrelated one.
            joining = False
            if remote:
                remote_tip = fetch_remote_tip(git_dir, source, remote, branch)
                joining = remote_tip is not None and current_tip(git_dir, source, branch) is None
                base = reconcile_with_remote(
                    git_dir,
                    source,
                    branch,
                    remote_tip,
                    reset_to_remote=args.reset_to_remote,
                    move=not args.dry_run,
                )
            else:
                base = current_tip(git_dir, source, branch)

            # A dry run stops one step before the branch move, so it does not
            # need the commit either -- and building one would need an
            # identity the preview has deliberately not written (the dry run
            # writes nothing).  CI runners have
            # no global identity, which is why passing locally did not mean
            # passing there.  write-tree alone answers every question a
            # preview prints (file count, size, diffstat, secrets, gitlinks).
            manifest, payload_todo = run_payload_step(
                git_dir, source, base, remote, payload_min, payload_candidates,
                dry_run=args.dry_run, jobs=args.payload_jobs, rule=payload_rule,
                ratio=payload_ratio,
                ratio_remembered=vault_remembers_ratio(vault),
                ratio_typed=args.payload_auto_ratio is not None,
                rehash=args.payload_rehash, from_default=payload_default,
                skip_changing=args.payload_skip_changing,
            )
            embedded_report: list[tuple[str, int]] = []
            tree, commit, excluded = build_snapshot(
                git_dir, source, base, exclude, message,
                make_commit=not args.dry_run,
                skip_paths=payload_skip or None, manifest=manifest,
                capture_embedded=not args.no_embedded_repos,
                embedded_report=embedded_report,
                staging_bytes=staging_expect,
                record_mtimes=resolve_record_mtimes(
                    args, vault, remote=remote, no_push=args.no_push,
                    parent_has_record=parent_has_mtimes(git_dir, source, base),
                ),
            )
            count, size = describe_tree(git_dir, source, tree)
            print(f"source : {source}")
            print(f"vault  : {vault}")
            print(f"branch : {branch}")
            print(f"files  : {count} ({human_bytes(size)})")
            RUN_FACTS.update(
                source=str(source), vault=str(vault), branch=branch, files=count, bytes=size,
                dry_run=bool(args.dry_run), base=base, remote=remote,
                payload_objects_to_upload=payload_todo[0] if manifest is not None else 0,
            )
            print(f"base   : {base[:12] if base else '(first snapshot)'}")
            if joining and base and remote:
                warn_if_foreign_chain(git_dir, source, base, tree, remote, branch)
            if manifest is not None:
                print(payload_summary(
                    manifest, payload_todo, args.dry_run,
                    remote=remote, from_default=payload_default,
                ))
            if excluded:
                print(f"excluded: {len(excluded)} path(s) left out")

            suggest_excludes(git_dir, source, excluded)

            if embedded_report:
                print(embedded_note(embedded_report))

            gitlinks = find_gitlinks(git_dir, source, tree)
            if gitlinks:
                print(
                    "\nWarning: these paths are embedded git repositories.  Only "
                    "their commit id is\nrecorded, NOT their contents, so a "
                    "restore will produce a broken directory:"
                )
                for path in gitlinks:
                    print(f"  {path}")
                print(
                    "  Back them up separately, or make them submodules with a "
                    "remote that survives."
                )

            secrets = find_secrets(
                index_paths(git_dir, source)
                + [item["path"] for item in (manifest or {}).get("files", [])]
            )
            if secrets:
                print(
                    f"\nWarning: {len(secrets)} captured path(s) look like "
                    f"credentials.  Git history is\npermanent -- push this vault "
                    f"only to a library only you can read:"
                )
                for path in secrets[:20]:
                    print(f"  {path}")
                if len(secrets) > 20:
                    print(f"  ... and {len(secrets) - 20} more")

            # A real run's ``None`` commit *is* the "tree unchanged" signal:
            # build_snapshot returns one when there is nothing new to record.  A
            # dry run never makes a commit, so there ``None`` says nothing and
            # the tree is compared with the base's instead -- otherwise a first
            # snapshot of 8 GiB was reported as "no changes since the last
            # snapshot".
            if args.dry_run:
                if base:
                    base_tree = git(
                        git_dir, source, ["rev-parse", base + "^{tree}"], cwd=source, check=False
                    ).stdout.strip()
                    changed = tree != base_tree
                else:
                    changed = True
            else:
                changed = commit is not None
            if not changed:
                print("no changes since the last snapshot")
            elif base:
                stat = diff_stat(git_dir, source, base, tree)
                if stat:
                    print(stat)

            if args.dry_run:
                if not changed:
                    print("\ndry run -- nothing to commit, nothing pushed")
                elif commit is None:
                    print(
                        "\ndry run -- branch not moved, nothing pushed "
                        "(a real run would record this tree)"
                    )
                else:
                    print(
                        f"\ndry run -- branch not moved, nothing pushed "
                        f"(would be {commit[:12]})"
                    )
                return 0

            if commit is None:
                print("\nnothing to commit; branch left where it is")
            else:
                set_branch(git_dir, source, branch, commit, base)
                RUN_FACTS["commit"] = commit
                print(f"\nmoved refs/heads/{branch} -> {commit[:12]}")

            if not remote:
                print("no --remote; snapshot kept locally (pass --remote, or set "
                      "one in the source repo, to push)")
            elif commit is None and base == remote_tip:
                print("the remote already has this snapshot; nothing to push")
                if base and not args.dry_run:
                    remember_pushed_tip(git_dir, source, branch, base)
            else:
                if not args.no_repack:
                    auto_incremental_repack(git_dir, source)
                # `commit or base`: when the tree is unchanged but this machine
                # holds snapshots the remote has never seen (taken offline),
                # there is no *new* commit to send -- the existing branch is the
                # thing to push, and `base` is where it points.
                verify_payload_before_push(
                    git_dir, source, remote, remote_tip, commit or base or "",
                    manifest, jobs=args.payload_jobs or 3,
                )
                push_snapshot(
                    git_dir,
                    source,
                    remote,
                    branch,
                    commit or base or "",
                    tree,
                    base,
                    message,
                )
                RUN_FACTS["pushed"] = True
                if manifest is not None:
                    try:
                        verify_payload_after_push(
                            source, remote, manifest, jobs=args.payload_jobs or 3,
                        )
                    except SnapshotError as exc:
                        # The push has landed; this check looks *behind* it, so
                        # failing the run here used to skip the remember block
                        # below -- the vault then forgot its payload setting
                        # and the next bare run was refused by R10's guard for
                        # a decision this run had already made.  The snapshot
                        # is on the server either way: say it as a warning,
                        # remember everything, and still exit non-zero.
                        print(f"Warning: {exc}", file=sys.stderr, flush=True)
                        END_OF_RUN_NOTES.append(
                            "the snapshot was pushed, but a payload object it names could not "
                            "be re-verified afterwards; run --payload-verify and take a new "
                            "snapshot if it names a missing object"
                        )
                        late_failure = True

        # Remember only a run that got all the way through, so the first
        # successful run leaves the next one with nothing to type -- and a typo'd
        # --remote that failed to push does not become the vault's remote for
        # every scheduled run after it.  A preview never gets here.
        remember_payload_value(vault, args.payload_min_size, dry_run=args.dry_run)
        remember_payload_ratio(vault, args.payload_auto_ratio, dry_run=args.dry_run)
        remember_tuning(vault, payload_tuning, dry_run=args.dry_run)
        remember_record_mtimes(vault, args, dry_run=args.dry_run)
        remember_settings(
            vault,
            source,
            remote=_UNSET if args.no_push else remote,
            code_remote=_UNSET if args.no_push else code_remote,
            exclude=exclude,
            branch=branch,
            vault_config=vault_config,
        )
        # Last, after everything the run must keep: the report is optional, and
        # an interrupt during it must not lose the settings this run earned.
        if manifest is not None and remote and not args.dry_run:
            maybe_report_payload_gc(git_dir, vault, remote, args.payload_report_days)
        for note in END_OF_RUN_NOTES:
            print(note, flush=True)
            print(note, file=sys.stderr, flush=True)
        return 1 if late_failure else 0

    except SnapshotError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
