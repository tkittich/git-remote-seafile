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
import shutil
import socket
import stat
import subprocess
import sys
import time
import unicodedata
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

# Running as documented from a bare source checkout -- `python
# tools/seafile_snapshot.py` -- puts only tools/ on sys.path, so the
# git_remote_seafile package (whose safety module this tool consults below)
# would be unimportable and the synced-folder guard silently off.  The doctor
# solves the same problem the same way; do this before any library import.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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


def _fmt_bytes(num: float) -> str:
    if num >= 1073741824:
        return f"{num / 1073741824:.1f} GiB"
    if num >= 1048576:
        return f"{num / 1048576:.0f} MiB"
    return f"{int(num)} B"


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
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` for git, failing as a ``SnapshotError`` when it hangs.

    Without ``status`` this is a plain run bounded by ``GIT_TIMEOUT``.

    With ``status`` the child runs under a *no-progress* watchdog instead of a
    wall-clock one, driven from this thread by retrying
    ``communicate(timeout=STATUS_INTERVAL)`` -- which keeps stdin, stdout and
    stderr intact across the timeouts, so no helper threads are needed.  Each
    time the interval passes without the child exiting:

    * one line goes to stderr ("still staging the tree ... 4m12s (9 GiB written
      to objects)"), because nothing else speaks during such a step;
    * bytes newly present under ``measure_dir`` count as progress and restart the
      stall clock;
    * if nothing has grown for ``STALL_LIMIT`` seconds the child (and, on
      Windows, its process tree) is killed and a ``SnapshotError`` raised.

    ``stall_limit=None`` is heartbeat-only mode, for steps with no measurable
    progress (a push's silent upload, a verification that re-reads every file
    and writes nothing): the lines keep coming and nothing is ever killed.
    """
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
            line += f" ({_fmt_bytes(growth)} written to {measure_dir.name})"
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
    ``live_stderr`` the child's stderr is inherited, so a push shows git's and
    the remote helper's own output as it happens -- including the helper's
    notices.  Callers must tolerate ``proc.stderr`` being ``None`` on failure
    (the text already reached the user directly).
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
        errors="replace",
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        stdout=subprocess.PIPE,
        stderr=None if live_stderr else subprocess.PIPE,
        status=status,
        measure_dir=measure_dir,
        stall_limit=stall_limit,
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


BYTE_EXACT_MARKER = "* -text -filter -ident"

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
    "* -text -filter -ident\n"
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

    The two config values are only written when they are not already correct,
    so a vault that has been snapshotted before costs no *writes* here (one
    config read -- see :func:`_read_local_config_map`).  Reading the *local*
    config is what makes that safe: a global ``core.autocrlf`` is irrelevant,
    because the vault has to pin its own.
    """
    if vault_config is None:
        vault_config = _read_local_config_map(vault)
    for key in ("core.autocrlf", "core.safecrlf"):
        have = (vault_config.get(key) or [""])[0]
        if have == "false":
            continue
        git(git_dir, vault, ["config", key, "false"], cwd=vault)

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
    print(f"consolidating {count} loose object(s), {_fmt_bytes(loose_bytes)}, "
          f"before the push ...")
    try:
        git(git_dir, None, ["repack", "-d"], cwd=work_tree,
            status="packing the vault's loose objects", measure_dir=git_dir / "objects")
        git(git_dir, None, ["prune"], cwd=work_tree)
    except SnapshotError as exc:
        print(f"note: incremental repack failed ({str(exc).splitlines()[0]}); "
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
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
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
    second-guessed.  A ``default`` verdict is final unless *recheck* is set,
    which :func:`main` does when the tree has since grown past the point where
    the decision matters (a vault that began small and became a 20 GB payload
    would otherwise stay on the slow settings forever).  Under ``--dry-run``
    (``write=False``) the sample is still taken and reported, but nothing is
    written.
    """
    profile = (vault_config.get("snapshot.profile") or [""])[0]
    if profile == "raw" or vault_config.get("core.compression"):
        return
    if profile and not recheck:
        return
    measure = sample_payload(source, excludes, skip)
    verdict = decide_profile(measure)
    if verdict == "raw":
        what = (
            f"large raw payload detected ({_fmt_bytes(measure['payload'])} in big files of "
            f"{_fmt_bytes(measure['total'])}; the sample keeps {measure['ratio']:.0%} of its "
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
) -> None:
    """Refuse to start staging a tree the vault's volume cannot hold.

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
        return
    raise SnapshotError(
        f"refusing to stage: the tree is {_fmt_bytes(tree)} and staging it needs about "
        f"{_fmt_bytes(need)} of new object storage in {vault}, but only {_fmt_bytes(free)} is free "
        f"on that volume (and {_fmt_bytes(_FREE_SPACE_MARGIN_BYTES)} is kept in reserve). "
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
              f"({_fmt_bytes(freed)} freed)")
    if failed:
        print("warning: could not remove leftover temporary pack file(s): " + "; ".join(failed))
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
          f"{_fmt_bytes(loose_kb * 1024)} loose, {_fmt_bytes(packed_kb * 1024)} "
          f"in {stats.get('packs', 0)} packfile(s)")
    if stats.get("garbage", 0):
        print(f"note: {stats['garbage']} leftover temporary file(s) from an interrupted run "
              f"hold {_fmt_bytes(stats.get('size-garbage', 0) * 1024)} in objects/pack; "
              "a snapshot run (or --maintain --full-repack) removes them")

    profile = _read_config_key(git_dir, "snapshot.profile")
    if profile:
        print(f"storage profile: {profile}"
              + (" (core.compression=0, core.bigFileThreshold="
                 f"{_PROFILE_BIG_FILE_THRESHOLD})" if profile == "raw" else ""))

    if not full_repack:
        need = _fmt_bytes(store_bytes)
        have = _fmt_bytes(free)
        verdict = "fits" if free >= store_bytes else "does NOT fit -- free space first"
        print(f"a full repack would need roughly {need} of headroom; this volume has {have} ({verdict}).")
        print("re-run with --maintain --full-repack to consolidate the whole store.")
        return 0

    if free < store_bytes:
        raise SnapshotError(
            f"refusing to repack: the object store is {_fmt_bytes(store_bytes)} but only "
            f"{_fmt_bytes(free)} is free on this volume; a repack keeps the old packfiles "
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
          f"{_fmt_bytes(after.get('size-pack', 0) * 1024)} in "
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
        _KERNEL32 = kernel32
    return _KERNEL32


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

    def _holder(self) -> tuple[str, int] | None:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        host, _, pid_text = text.partition(" ")
        try:
            return host, int(pid_text)
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
        host, pid = holder
        if host != socket.gethostname() or pid_is_alive(pid):
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
        os.write(self._fd, f"{socket.gethostname()} {os.getpid()}".encode())
        os.fsync(self._fd)
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


def delete_branch(git_dir: Path, work_tree: Path, branch: str, expected: str) -> None:
    git(
        git_dir,
        work_tree,
        ["update-ref", "-d", f"refs/heads/{branch}", expected],
        cwd=work_tree,
        check=False,
    )


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
    """
    # The pattern travels the same road as the index: forward slashes, no
    # trailing slash, and -- so one command line means one snapshot -- a case
    # comparison that does not depend on the OS (T-11/SNAP-06; index paths are
    # case-sensitive data, and fnmatch's normcase would make Windows behave
    # differently from every other platform).
    pattern = pattern.strip().replace("\\", "/").rstrip("/")
    if not pattern:
        return False
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


def stageable_paths(git_dir: Path, work_tree: Path) -> list[str]:
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
    listing = git(git_dir, work_tree, ["ls-files", "-z", "-c", "-o"], cwd=work_tree).stdout
    deleted = {
        path.rstrip("/")
        for path in git(
            git_dir, work_tree, ["ls-files", "-z", "--deleted"], cwd=work_tree
        ).stdout.split("\0")
        if path
    }
    return [
        path
        for path in (entry.rstrip("/") for entry in listing.split("\0") if entry)
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
) -> tuple[str, str | None, list[str]]:
    """Stage the tree into the vault index and write a commit object.

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
    """
    if excludes or skip_paths:
        candidates = stageable_paths(git_dir, work_tree)
        excluded = [
            path
            for path in candidates
            if any(exclude_matches(path, pat) for pat in excludes)
        ]
        excluded_set = set(excluded)
        # Payload files are not excluded in the user's sense -- they are
        # recorded in the manifest and live on the server -- so they are
        # kept out of the index without being reported as excluded.
        kept = [
            path for path in candidates
            if path not in excluded_set and not (skip_paths and path in skip_paths)
        ]
        kept_set = set(kept)
        if kept:
            git(
                git_dir,
                work_tree,
                ["add", "-A", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
                cwd=work_tree,
                stdin="\0".join(kept) + "\0",
                status="staging the tree",
                measure_dir=git_dir / "objects",
            )
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
    else:
        git(
            git_dir,
            work_tree,
            ["add", "-A", "-f", "."],
            cwd=work_tree,
            status="staging the tree",
            measure_dir=git_dir / "objects",
        )
        excluded = []

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

    return tree, commit_tree(git_dir, work_tree, tree, parent, message), excluded


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
    """
    found: list[str] = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
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
    if not present:
        return []
    listing = git(
        git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree
    ).stdout
    paths = [p for p in listing.split("\0") if p]
    found: list[str] = []
    for pattern in sorted(present):
        hit = any(exclude_matches(path, pattern) for path in paths)
        if hit:
            found.append(pattern)
    if not found:
        return []
    flags = " ".join(f"--exclude {p!r}" for p in found)
    print(
        f"\nWARNING: the snapshot includes reproducible build output that is "
        f"usually not worth\nbacking up.  Leave it out next time with "
        f"--default-excludes, or explicitly:\n  {flags}"
    )
    return found


# -- Payload mode --------------------------------------------------------------
#
# Large immutable files do not go through git at all.  Each one is stream-hashed
# (SHA-256) and uploaded to Seafile as an ordinary file at its real relative path,
# and the vault commit carries a *manifest* -- path, mode, size, SHA-256, mtime --
# so git keeps what git is good at (the file list, the checksums, the history of
# both) and Seafile keeps what Seafile is good at (big files, shared blocks).
#
# Layout, beside the vault's own remote directory:
#
#     <path>.payload/current/<relative path>        working mirror, updated in place
#     <path>.payload/snapshots/<id>/<relative ...>  one server-side copy of current/
#
# A snapshot is a single copy operation (metadata only: the copy shares blocks),
# so it is browsable, consistent, and costs nothing on disk beyond what changed.
# Design, measurements and open items: SNAPSHOT.md section 15.1.

#: Where the manifest lives inside the snapshot tree.
PAYLOAD_MANIFEST = ".seafile-payload/manifest.json"
PAYLOAD_FORMAT = 1
_PAYLOAD_HASH_BLOCK = 8 * 1024 * 1024
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}


def parse_size(text: str) -> int:
    """``"64M"``, ``"1g"``, ``"500k"`` or ``"1048576"`` as a byte count."""
    raw = text.strip().lower().rstrip("b").replace("i", "")
    digits = raw.rstrip("kmgt")
    unit = raw[len(digits):]
    try:
        value = float(digits)
        return int(value * _SIZE_UNITS[unit])
    except (ValueError, KeyError):
        raise SnapshotError(
            f"not a size: {text!r} (examples: 64M, 1G, 500k, 1048576)"
        ) from None


#: What this server was measured to do to names (CE 13.0.28): a name is stored
#: NFC-normalised, a double quote becomes ``%22``, and a name longer than 255
#: *bytes* of UTF-8 is refused.  Everything else tried -- ``: * ? < > |``, a
#: trailing space or dot, reserved Windows names, emoji, Thai, a path of 4,200
#: characters -- was stored as given.
_MAX_NAME_BYTES = 255


def remote_rel(rel: str) -> str:
    """The path the server will hold for source path *rel*."""
    return "/".join(
        unicodedata.normalize("NFC", part).replace('"', "%22") for part in rel.split("/")
    )


def check_payload_names(paths: list[str]) -> None:
    """Refuse, before anything is uploaded, names the server cannot hold or would merge."""
    too_long = [
        p for p in paths
        if any(len(part.encode("utf-8")) > _MAX_NAME_BYTES for part in remote_rel(p).split("/"))
    ]
    if too_long:
        raise SnapshotError(
            f"{len(too_long)} payload path(s) have a name longer than {_MAX_NAME_BYTES} bytes "
            f"(UTF-8), which the server refuses; first: {too_long[0]}"
        )
    seen: dict[str, str] = {}
    for p in paths:
        other = seen.setdefault(remote_rel(p), p)
        if other != p:
            raise SnapshotError(
                f"payload paths {other!r} and {p!r} become the same name on the server "
                "(Unicode normalisation or quote escaping); rename one or exclude it"
            )


def payload_location(remote: str) -> tuple[str, str, str | None]:
    """``(library, payload_dir, server_url)`` for a vault remote URL.

    The payload directory is a *sibling* of the vault's own directory, so the
    helper's listings of the vault never see it.
    """
    from git_remote_seafile.url import parse_seafile_url

    try:
        parsed = parse_seafile_url(remote)
    except ValueError as exc:
        raise SnapshotError(f"payload mode needs a seafile:// remote: {exc}") from None
    return parsed.library_name, parsed.repo_path.rstrip("/") + ".payload", parsed.server_url


def _open_client(server_url: str | None) -> Any:
    try:
        from git_remote_seafile.client import SeafileClient
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SnapshotError(f"payload mode needs the git_remote_seafile package: {exc}") from None
    return SeafileClient(server_url=server_url)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_PAYLOAD_HASH_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def find_payload_files(
    source: Path, excludes: list[str], min_size: int
) -> dict[str, tuple[int, int, str]]:
    """``{relative path: (size, mtime_ns, mode)}`` for every payload file.

    A payload file is a *regular* file of at least ``min_size`` bytes that no
    ``--exclude`` removes.  Symlinks stay with git (they are a few bytes and git
    stores them exactly); so does everything smaller.
    """
    found: dict[str, tuple[int, int, str]] = {}
    for size, rel in _walk_sizes(source, excludes):
        if size < min_size:
            continue
        try:
            st = os.lstat(source / rel)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        found[rel] = (st.st_size, st.st_mtime_ns, mode)
    return found


def load_manifest(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, Any] | None:
    """The manifest recorded in snapshot ``rev``, or ``None`` when it has none."""
    if not rev:
        return None
    proc = git(
        git_dir, work_tree, ["show", f"{rev}:{PAYLOAD_MANIFEST}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def build_manifest(
    candidates: dict[str, tuple[int, int, str]],
    source: Path,
    previous: dict[str, Any] | None,
    min_size: int,
    *,
    quiet: bool = False,
) -> dict[str, Any]:
    """Hash every payload file (reusing the previous run's hash when size and
    mtime are unchanged) and return the manifest, snapshot id included."""
    known: dict[str, dict[str, Any]] = {}
    for entry in (previous or {}).get("files", []):
        if isinstance(entry, dict) and "path" in entry:
            known[entry["path"]] = entry
    entries: list[dict[str, Any]] = []
    hashed = reused = 0
    total = len(candidates)
    for index, rel in enumerate(sorted(candidates), 1):
        size, mtime_ns, mode = candidates[rel]
        old = known.get(rel)
        if old and old.get("size") == size and old.get("mtime_ns") == mtime_ns and old.get("sha256"):
            digest = old["sha256"]
            reused += 1
        else:
            if not quiet:
                print(f"hashing [{index}/{total}] {rel} ({human_bytes(size)})", flush=True)
            digest = _sha256_file(source / rel)
            hashed += 1
        record: dict[str, Any] = {
            "path": rel, "size": size, "mode": mode, "sha256": digest, "mtime_ns": mtime_ns,
        }
        if remote_rel(rel) != rel:
            record["remote"] = remote_rel(rel)  # what the server actually holds
        entries.append(record)
    ident = hashlib.sha256()
    for item in entries:
        ident.update(f"{item['path']}\0{item['size']}\0{item['mode']}\0{item['sha256']}\n".encode())
    return {
        "format": PAYLOAD_FORMAT,
        "snapshot": "p-" + ident.hexdigest()[:16],
        "min_size": min_size,
        "files": entries,
    }


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    # One line, ASCII, no trailing newline: nothing between here and the object
    # store (text-mode pipes, filters) has a newline to translate.
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("ascii")


def add_manifest_to_index(
    git_dir: Path, work_tree: Path, manifest: dict[str, Any]
) -> None:
    """Record the manifest in the vault index (and so in the snapshot tree)."""
    blob = git(
        git_dir, work_tree, ["hash-object", "-w", "--no-filters", "--stdin"],
        cwd=work_tree, stdin=manifest_bytes(manifest).decode("ascii"),
    ).stdout.strip()
    git(
        git_dir, work_tree,
        ["update-index", "--add", "--cacheinfo", f"100644,{blob},{PAYLOAD_MANIFEST}"],
        cwd=work_tree,
    )


def _remote_index(client: Any, rid: str, directory: str) -> dict[str, int]:
    """``{relative path: size}`` of every file under *directory* on the server."""
    found: dict[str, int] = {}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        for entry in client.list_dir(rid, directory + ("/" + rel_dir if rel_dir else "")):
            name = entry.get("name")
            if not name:
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if entry.get("type") == "dir":
                stack.append(rel)
            else:
                found[rel] = int(entry.get("size", -1))
    return found


def publish_payload(
    remote: str,
    source: Path,
    manifest: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """Make the server hold exactly this payload, then snapshot it.

    Uploads what is missing or differs in size into ``current/``, removes what
    the manifest no longer lists, verifies the listing, and makes one
    server-side copy of ``current/`` as ``snapshots/<id>/`` unless that
    snapshot already exists.  Returns ``(files_to_upload, bytes_to_upload)``.

    ``dry_run`` reads the server and changes nothing.
    """
    library, base, server_url = payload_location(remote)
    client = _open_client(server_url)
    rid = client.get_repo_id(library)
    current = f"{base}/current"
    snap_id = manifest["snapshot"]
    wanted = {item["path"]: item for item in manifest["files"]}
    check_payload_names(list(wanted))
    remote_of = {rel: item.get("remote", rel) for rel, item in wanted.items()}
    held = set(remote_of.values())

    # The listing can only prove a file is *there at the right size*.  A file
    # edited in place to the same length would pass that test while holding the
    # old bytes, so a path whose hash differs from the last committed manifest
    # is uploaded regardless.
    committed = {
        entry.get("path"): entry.get("sha256")
        for entry in (previous or {}).get("files", [])
        if isinstance(entry, dict)
    }
    remote_files = _remote_index(client, rid, current)
    todo = [
        rel for rel, item in wanted.items()
        if remote_files.get(remote_of[rel]) != item["size"] or committed.get(rel) != item["sha256"]
    ]
    stale = [rel for rel in remote_files if rel not in held]
    todo_bytes = sum(wanted[rel]["size"] for rel in todo)
    if dry_run:
        return len(todo), todo_bytes

    for index, rel in enumerate(sorted(todo), 1):
        parent, _, name = remote_of[rel].rpartition("/")
        client.mkdir_p(rid, current + ("/" + parent if parent else ""))
        print(
            f"uploading [{index}/{len(todo)}] {rel} ({human_bytes(wanted[rel]['size'])})",
            flush=True,
        )
        if not client.upload_file(
            rid, current + ("/" + parent if parent else ""), name, source / rel
        ):
            raise SnapshotError(f"uploading {rel} to {remote} failed")
    for rel in stale:
        client.delete_entry(rid, f"{current}/{rel}")
    if todo or stale:
        after = _remote_index(client, rid, current)
        wrong = [
            rel for rel, item in wanted.items() if after.get(remote_of[rel]) != item["size"]
        ]
        if wrong:
            raise SnapshotError(
                f"payload upload did not land: {len(wrong)} file(s) missing or the wrong "
                f"size on the server, first: {wrong[0]}"
            )

    snapshots = f"{base}/snapshots"
    existing = {e.get("name") for e in client.list_dir(rid, snapshots)}
    if snap_id not in existing and wanted:
        client.mkdir_p(rid, f"{snapshots}/{snap_id}")
        names = [e["name"] for e in client.list_dir(rid, current) if e.get("name")]
        if not client.copy_entries(rid, current, names, f"{snapshots}/{snap_id}"):
            raise SnapshotError(
                f"the server refused to copy {current} to {snapshots}/{snap_id}"
            )
        listed = _remote_index(client, rid, f"{snapshots}/{snap_id}")
        if len(listed) != len(wanted):
            raise SnapshotError(
                f"snapshot {snap_id} holds {len(listed)} file(s) but the manifest lists "
                f"{len(wanted)}"
            )
    return len(todo), todo_bytes


def restore_payload(remote: str, into: Path) -> None:
    """Download and verify the payload a restored snapshot's manifest names."""
    manifest_path = into / PAYLOAD_MANIFEST
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except ValueError as exc:
        raise SnapshotError(f"the snapshot's payload manifest is unreadable: {exc}") from None
    files = manifest.get("files", [])
    library, base, server_url = payload_location(remote)
    client = _open_client(server_url)
    rid = client.get_repo_id(library)
    snap_dir = f"{base}/snapshots/{manifest['snapshot']}"
    print(f"restoring {len(files)} payload file(s) from {snap_dir} ...", flush=True)
    problems: list[str] = []
    for index, item in enumerate(files, 1):
        rel = item["path"]
        dest = into / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading [{index}/{len(files)}] {rel} ({human_bytes(item['size'])})", flush=True)
        if not client.download_file_to(rid, f"{snap_dir}/{item.get('remote', rel)}", dest):
            problems.append(f"{rel}: not found in {snap_dir}")
            continue
        if dest.stat().st_size != item["size"]:
            problems.append(f"{rel}: size {dest.stat().st_size}, manifest says {item['size']}")
            continue
        if _sha256_file(dest) != item["sha256"]:
            problems.append(f"{rel}: SHA-256 does not match the manifest")
            continue
        if item.get("mode") == "100755" and os.name != "nt":
            dest.chmod(dest.stat().st_mode | 0o111)
        try:
            os.utime(dest, ns=(item["mtime_ns"], item["mtime_ns"]))
        except OSError:
            pass
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise SnapshotError(
            f"{len(problems)} payload file(s) failed verification:\n  {shown}{more}"
        )
    print(f"payload verified: {len(files)} file(s), SHA-256 matches the manifest")


def add_payload_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--payload-min-size",
        metavar="SIZE",
        default=None,
        help="payload mode: files of at least SIZE (e.g. 64M, 1G) bypass git; they are "
        "hashed (SHA-256) and uploaded to Seafile as plain files beside the vault, and the "
        "snapshot records a manifest instead. Needs a remote; a restore downloads and "
        "verifies them. 0 turns it off. A vault that has used it needs the flag on every "
        "run (SNAPSHOT.md section 15.1)",
    )


def payload_conflicts(args: argparse.Namespace) -> list[str]:
    if args.payload_min_size is not None and args.no_push:
        return [
            "--payload-min-size cannot be combined with --no-push: payload files exist "
            "only on the server, so a snapshot that is not pushed would reference files "
            "that were never uploaded"
        ]
    return []


def plan_payload(
    value: str | None, remote: str | None, source: Path, excludes: list[str]
) -> tuple[int | None, dict[str, tuple[int, int, str]]]:
    """``(min_size, candidates)`` for this run; ``(None, {})`` when payload mode is off."""
    min_size = parse_size(value) if value is not None else None
    if not min_size:
        return min_size, {}
    if not remote:
        raise SnapshotError(
            "payload mode needs a remote to upload to: pass --remote "
            "seafile://<library>/<path> (or run from a source with a seafile:// remote)"
        )
    return min_size, find_payload_files(source, excludes, min_size)


def run_payload_step(
    git_dir: Path,
    source: Path,
    base: str | None,
    remote: str | None,
    min_size: int | None,
    candidates: dict[str, tuple[int, int, str]],
    *,
    dry_run: bool,
) -> tuple[dict[str, Any] | None, tuple[int, int]]:
    """Hash, upload and snapshot the payload; ``(manifest, (files, bytes) to upload)``."""
    previous = load_manifest(git_dir, source, base)
    if previous is not None and min_size is None:
        raise SnapshotError(
            "this vault's last snapshot used payload mode, so its large files are not "
            "in git; running without --payload-min-size would stage them into git "
            f"instead. Repeat the flag (--payload-min-size {previous.get('min_size', 'SIZE')}), "
            "or pass --payload-min-size 0 to leave payload mode deliberately"
        )
    if not min_size:
        return None, (0, 0)
    manifest = build_manifest(candidates, source, previous, min_size)
    todo = publish_payload(remote or "", source, manifest, previous, dry_run=dry_run)
    return manifest, todo


def payload_summary(manifest: dict[str, Any], todo: tuple[int, int], dry_run: bool) -> str:
    total = sum(item["size"] for item in manifest["files"])
    verb = "to upload" if dry_run else "uploaded"
    return (
        f"payload: {len(manifest['files'])} file(s) ({human_bytes(total)}) bypass git; "
        f"snapshot {manifest['snapshot']}; {verb} {todo[0]} file(s) ({human_bytes(todo[1])})"
    )


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
        ["diff", "--stat", "--no-color", parent, tree],
        cwd=work_tree,
        check=False,
    )
    return proc.stdout.rstrip()


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


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
            delete_branch(git_dir, work_tree, branch, current)
            rolled_back = "(deleted)"
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


def _checkout(repo: Path, rev: str, *, detach: bool = False) -> None:
    args = ["git", "-C", str(repo), "checkout", "-f"]
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


def verify_restore(repo: Path, rev: str) -> list[str]:
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
    if any(b"\n" in path for path in paths):
        # --stdin-paths is newline-separated and cannot express a path that
        # contains one; refuse loudly rather than verify a subset silently.
        raise SnapshotError(
            "a filename in the snapshot contains a newline, so the byte "
            "verification cannot hash every path; refusing to certify the "
            "restore"
        )

    mismatched_symlinks: list[str] = []
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
        print(f"note: the remote has no branch '{assumed}'; restoring its only branch '{names[0]}'")
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

    into.parent.mkdir(parents=True, exist_ok=True)
    print(f"cloning {remote} -> {into} (no checkout) ...")
    # stderr is inherited, not captured: a clone of a large vault transfers for
    # hours, and git's "Receiving objects" progress goes to the terminal only
    # when it is not being piped.  The watchdog adds one line per minute on top.
    proc = _run(
        ["git", "clone", "--no-checkout", remote, str(into)],
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
        _checkout(into, sha, detach=True)
        target = f"{ref} ({sha[:12]})"
        verified_rev = sha
    else:
        _checkout(into, branch)

    # The snapshot-time warning about embedded repositories is years and
    # machines away by the time someone restores; the restored revision is
    # where the broken directory actually materialises, so repeat it here
    # rather than let the certification below claim byte-exactness for a tree
    # with holes in it.
    restored_gitlinks = find_gitlinks(into / ".git", into, verified_rev)
    if restored_gitlinks:
        print(
            "\nWARNING: these paths in the restored tree are embedded git "
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

    # A restore that differs from the snapshot is not a restore, and the
    # autocrlf class of bug is invisible to `git status` -- so check the bytes.
    mismatched = verify_restore(into, verified_rev)
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

    restore_payload(remote, into)

    files = sum(1 for p in into.rglob("*") if p.is_file() and ".git" not in p.parts)
    print(f"restored {files} files from {target} into {into}")
    print(f"verified byte-exact against {verified_rev[:12]}")


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
                f"warning: --allow-synced overrides the synced-folder guard for "
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
                    f"note: {label} ({path}) is inside synced library "
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
    code = source_seafile_remote(source)
    if not code:
        return None
    return f"{code.rstrip('/') or code}-vault"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seafile_snapshot",
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
            "\nrecommended exclusions (never applied silently -- this is what\n"
            "--default-excludes adds):\n  "
            + " ".join(f"--exclude '{p}'" for p in RECOMMENDED_EXCLUDES)
            + "\n\nA pattern with no slash matches that name at ANY depth, so\n"
            "--exclude node_modules also drops packages/*/node_modules.\n"
            "Quote wildcards so your own shell does not expand them first.\n"
            "\nnote: neither the source nor the vault may live inside a\n"
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
        f"({', '.join(RECOMMENDED_EXCLUDES)}). Not on by default: a snapshot is "
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
        "shown against the real base)",
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
        "is for data you know is more compressible than the sample suggests",
    )
    add_payload_arguments(parser)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="with --restore, also run 'git fsck' on the clone. Byte-exactness "
        "is checked on every restore regardless",
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
        "--no-repack",
        action="store_true",
        help="skip the automatic incremental repack that runs before a push",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    """Reject combinations that would otherwise be silently ignored.

    Each of these is a flag that does nothing where it is passed, which is worse
    than a wrong answer: the user believes they asked for something and gets no
    signal either way.  ``--ref`` without ``--restore`` is the sharpest -- a run
    meant to restore an *old* snapshot quietly takes a *new* one instead.
    """
    problems: list[str] = []

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
            ("--verify", args.verify),
            ("--into", args.into),
            ("--ref", args.ref),
            # Housekeeping performs the repack itself; skipping it is a
            # snapshot-run concern.
            ("--no-repack", args.no_repack),
        ):
            if value:
                problems.append(f"{flag} does not apply to --maintain")
    elif not args.restore:
        for flag, value in (("--verify", args.verify), ("--into", args.into),
                            ("--ref", args.ref)):
            if value:
                problems.append(f"{flag} only applies to --restore")
        problems.extend(payload_conflicts(args))
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
        ):
            if value:
                problems.append(f"{flag} does not apply to --restore")

    # Outside --maintain there is no repack to run, so the switch that selects
    # one cannot mean anything (it would otherwise be silently ignored).
    if not args.maintain and args.full_repack:
        problems.append("--full-repack only applies to --maintain")

    if args.no_push and args.remote:
        problems.append("--no-push and --remote contradict each other")
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
        remote = args.remote or remembered.get("remote") or derive_vault_remote(source)
        code_remote = args.code_remote or remembered.get("code_remote")

    exclude = list(args.exclude) if args.exclude is not None else list(
        remembered.get("exclude") or []
    )
    if args.default_excludes:
        for pattern in RECOMMENDED_EXCLUDES:
            if pattern not in exclude:
                exclude.append(pattern)

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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        validate_args(args)
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
            )
            return 0

        source, vault, remote, code_remote, exclude, branch = (
            resolve_snapshot_settings(args)
        )

        # Housekeeping diverges here on purpose: it must not push the code
        # repo, initialise identity settings or remember anything -- only read
        # and tidy the vault's own object store.
        if args.maintain:
            return maintain_vault(vault, full_repack=args.full_repack)

        if args.dry_run and remote:
            print(f"dry run -- would push the vault to {remote}")

        payload_min, payload_candidates = plan_payload(
            args.payload_min_size, remote, source, exclude
        )
        payload_skip = frozenset(payload_candidates)
        git_dir = ensure_vault(vault, branch)
        # One read of the vault's config serves identity, byte-exactness and
        # the remembered settings below: none of them writes a key another one
        # compares against, so the map cannot go stale between the three.
        vault_config = _read_local_config_map(vault)
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
        # Before a byte is staged: a tree the volume cannot hold must be refused
        # now, not discovered when the disk fills (a dry run stages too).
        check_free_space(
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
            if remote:
                remote_tip = fetch_remote_tip(git_dir, source, remote, branch)
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
                dry_run=args.dry_run,
            )
            tree, commit, excluded = build_snapshot(
                git_dir, source, base, exclude, message,
                make_commit=not args.dry_run,
                skip_paths=payload_skip or None, manifest=manifest,
            )
            count, size = describe_tree(git_dir, source, tree)
            # The profile was decided from a sample taken before the first
            # staging.  A vault that began small and has since become a large
            # payload gets one more look -- for the *next* run, since this one
            # has already staged -- once per doubling of the tree.
            if (not args.dry_run and not args.no_object_compression
                    and profile_recheck_due(vault_config, size)):
                choose_profile(vault, git_dir, source, exclude, vault_config,
                               write=True, recheck=True, skip=payload_skip or None)

            print(f"source : {source}")
            print(f"vault  : {vault}")
            print(f"branch : {branch}")
            print(f"files  : {count} ({human_bytes(size)})")
            print(f"base   : {base[:12] if base else '(first snapshot)'}")
            if manifest is not None:
                print(payload_summary(manifest, payload_todo, args.dry_run))
            if excluded:
                print(f"excluded: {len(excluded)} path(s) left out")

            suggest_excludes(git_dir, source, excluded)

            gitlinks = find_gitlinks(git_dir, source, tree)
            if gitlinks:
                print(
                    "\nWARNING: these paths are embedded git repositories.  Only "
                    "their commit id is\nrecorded, NOT their contents, so a "
                    "restore will produce a broken directory:"
                )
                for path in gitlinks:
                    print(f"  {path}")
                print(
                    "  Back them up separately, or make them submodules with a "
                    "remote that survives."
                )

            secrets = find_secrets(index_paths(git_dir, source))
            if secrets:
                print(
                    f"\nWARNING: {len(secrets)} captured path(s) look like "
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
                print(f"\nmoved refs/heads/{branch} -> {commit[:12]}")

            if not remote:
                print("no --remote; snapshot kept locally (pass --remote, or set "
                      "one in the source repo, to push)")
            elif commit is None and base == remote_tip:
                print("the remote already has this snapshot; nothing to push")
            else:
                if not args.no_repack:
                    auto_incremental_repack(git_dir, source)
                # `commit or base`: when the tree is unchanged but this machine
                # holds snapshots the remote has never seen (taken offline),
                # there is no *new* commit to send -- the existing branch is the
                # thing to push, and `base` is where it points.
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

        # Remember only a run that got all the way through, so the first
        # successful run leaves the next one with nothing to type -- and a typo'd
        # --remote that failed to push does not become the vault's remote for
        # every scheduled run after it.  A preview never gets here.
        remember_settings(
            vault,
            source,
            remote=_UNSET if args.no_push else remote,
            code_remote=_UNSET if args.no_push else code_remote,
            exclude=exclude,
            branch=branch,
            vault_config=vault_config,
        )
        return 0

    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
