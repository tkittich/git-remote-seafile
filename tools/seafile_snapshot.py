#!/usr/bin/env python3
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

    # what that run would do, without writing anything
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
is written back to the vault for next time.  ``--no-push`` overrides any
remembered or derived remote; ``--dry-run`` previews without writing anything,
including the remembered settings.

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
  startup, before anything is written.
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
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_BRANCH = "snapshot"

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


def git(
    git_dir: Path,
    work_tree: Path | None,
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git against an explicit --git-dir/--work-tree pair.

    Passing both explicitly is what keeps this out of the source repository's
    own ``.git``: git never consults it, and never writes to it.
    """
    cmd = ["git", f"--git-dir={git_dir}"]
    if work_tree is not None:
        cmd.append(f"--work-tree={work_tree}")
    cmd.extend(args)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SnapshotError(
            f"git {' '.join(args)} failed (rc={proc.returncode})\n{detail}"
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
    proc = subprocess.run(
        ["git", "init", "-q", "-b", branch, str(vault)],
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
    return git_dir


def _config(path: Path, key: str) -> str:
    """Read a git config value, with the usual global/system fallbacks."""
    proc = subprocess.run(
        ["git", "-C", str(path), "config", "--get", key],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return proc.stdout.strip()


def ensure_identity(vault: Path, source: Path) -> None:
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
    after the first run is that there is nothing to do.
    """
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
        proc = subprocess.run(
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


def ensure_byte_exact(vault: Path, git_dir: Path) -> None:
    """Stop git from rewriting line endings inside the vault.

    Git for Windows ships ``core.autocrlf=true`` in its *system* config, so a
    freshly initialised vault converts LF to CRLF on checkout even though
    ``git config --global core.autocrlf`` reports nothing.  The stored blob is
    correct; the *restored* file is not -- and a backup whose restore differs
    from the original is not a backup.

    Both levers are pulled: the config, and an ``info/attributes`` override that
    outranks any ``.gitattributes`` in the source tree.

    The two config values are only written when they are not already correct,
    so a vault that has been snapshotted before costs no subprocesses here.
    Reading the *local* config is what makes that safe: a global
    ``core.autocrlf`` is irrelevant, because the vault has to pin its own.
    """
    current = _read_local_config_map(vault)
    for key in ("core.autocrlf", "core.safecrlf"):
        have = (current.get(key) or [""])[0]
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

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
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
            return False
        host, pid = holder
        if host != socket.gethostname() or pid_is_alive(pid):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
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
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


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
    """True iff ``older`` is an ancestor of ``newer`` (equal counts)."""
    proc = git(
        git_dir,
        work_tree,
        ["merge-base", "--is-ancestor", older, newer],
        cwd=work_tree,
        check=False,
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
        blob = f"{proc.stderr}\n{proc.stdout}".lower()
        if "couldn't find remote ref" in blob or "remote ref does not exist" in blob:
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

    * ``fnmatch`` against the whole path, which is what makes a wildcard work
      (``*.pyc`` matches ``a/b/c.pyc``: fnmatch's ``*`` crosses ``/``); or
    * any single path *component* equal to the pattern, which is what makes a
      bare name mean "a directory by that name, at any depth".
    """
    pattern = pattern.strip().rstrip("/")
    if not pattern:
        return False
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern in path.split("/")


def apply_excludes(git_dir: Path, work_tree: Path, excludes: list[str]) -> list[str]:
    """Drop excluded paths from the vault index; return what was dropped.

    ``update-index --force-remove`` with NUL-separated paths on stdin, rather
    than one ``git rm`` per pattern: the matching is ours (see
    :func:`exclude_matches`), and batching keeps the argument list short.
    """
    if not excludes:
        return []
    listing = git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout
    dropped = [
        path
        for path in listing.split("\0")
        if path and any(exclude_matches(path, pat) for pat in excludes)
    ]
    for start in range(0, len(dropped), _INDEX_BATCH):
        batch = dropped[start : start + _INDEX_BATCH]
        git(
            git_dir,
            work_tree,
            ["update-index", "--force-remove", "-z", "--stdin"],
            cwd=work_tree,
            stdin="\0".join(batch) + "\0",
        )
    return dropped


def build_snapshot(
    git_dir: Path,
    work_tree: Path,
    parent: str | None,
    excludes: list[str],
    message: str,
) -> tuple[str, str | None, list[str]]:
    """Stage the whole tree into the vault index and write a commit object.

    Returns ``(tree, commit, excluded)``, where ``commit`` is ``None`` when the
    tree is identical to ``parent`` -- an unchanged run should not pile up empty
    commits.  ``parent`` is passed in rather than read from the branch, because
    the caller may have just re-based on the remote tip.  The branch is *not*
    moved here: that is the caller's job, so a dry run can stop one step short.
    """
    git(git_dir, work_tree, ["add", "-A", "-f", "."], cwd=work_tree)
    excluded = apply_excludes(git_dir, work_tree, excludes)

    tree = git(git_dir, work_tree, ["write-tree"], cwd=work_tree).stdout.strip()

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
    """
    found: list[str] = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        for pattern in SECRET_PATTERNS:
            subject = path if "/" in pattern else name
            if fnmatch.fnmatch(subject, pattern):
                found.append(path)
                break
    return found


def index_paths(git_dir: Path, work_tree: Path) -> list[str]:
    """Every path staged in the vault's index."""
    listing = git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout
    return [path for path in listing.split("\0") if path]


def source_remote_urls(source: Path) -> list[tuple[str, str]]:
    """``(remote_name, url)`` for every URL the source repository can push to.

    Read with ``--local``: a global ``insteadOf`` or a credential helper is not
    what the user means by "my code remote", and a repository that is not a git
    repository at all simply has no remotes.
    """
    proc = subprocess.run(
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


def _looks_like_non_fast_forward(output: str) -> bool:
    """Whether a push failed because the remote moved ahead of us."""
    low = output.lower()
    return (
        "non-fast-forward" in low
        or "fetch first" in low
        or "[rejected]" in low
    )


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

    On any other failure the local branch is rolled back to what the remote
    actually has, so the vault never claims a snapshot the remote never took.
    That rollback is also what makes a plain re-run the correct remedy: the next
    run fetches the true tip and appends to it.
    """
    spec = f"refs/heads/{branch}:refs/heads/{branch}"
    current = commit
    for attempt in (1, 2):
        print(f"pushing to {remote} ...")
        proc = git(
            git_dir, work_tree, ["push", remote, spec], cwd=work_tree, check=False
        )
        if proc.returncode == 0:
            print("push complete")
            return

        sys.stdout.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")

        if attempt == 1 and _looks_like_non_fast_forward(
            f"{proc.stdout}\n{proc.stderr}"
        ):
            tip = fetch_remote_tip(git_dir, work_tree, remote, branch)
            if tip and tip != base:
                print(
                    f"remote moved to {tip[:12]}; re-parenting the snapshot and "
                    f"retrying"
                )
                rebased = commit_tree(git_dir, work_tree, tree, tip, message)
                set_branch(git_dir, work_tree, branch, rebased, current)
                current, base = rebased, tip
                continue
        break

    if base:
        set_branch(git_dir, work_tree, branch, base, current)
        rolled_back = base[:12]
    else:
        delete_branch(git_dir, work_tree, branch, current)
        rolled_back = "(deleted)"
    raise SnapshotError(
        f"push to {remote} failed.\n"
        f"The local branch was rolled back to {rolled_back} so the vault still "
        f"matches the remote; nothing was lost.  Re-run to retry."
    )


def _checkout(repo: Path, rev: str, *, detach: bool = False) -> None:
    args = ["git", "-C", str(repo), "checkout", "-f"]
    if detach:
        args.append("--detach")
    args.append(rev)
    proc = subprocess.run(
        args,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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
    """
    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", rev],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if listing.returncode != 0:
        raise SnapshotError(
            f"could not list '{rev}' in {repo} (rc={listing.returncode})\n"
            f"{listing.stderr.strip()}"
        )
    mismatched: list[str] = []
    for entry in listing.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) < 3 or fields[1] != "blob":
            continue
        proc = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "--no-filters", "--", path],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.stdout.strip() != fields[2]:
            mismatched.append(path)
    return mismatched


def restore(
    remote: str,
    into: Path,
    branch: str,
    ref: str | None,
    verify: bool = False,
) -> None:
    """Materialise a snapshot into a new directory, byte-for-byte.

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
    proc = subprocess.run(
        ["git", "clone", "--no-checkout", remote, str(into)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"clone failed (rc={proc.returncode})\n{proc.stdout.strip()}"
        )

    ensure_byte_exact(into, into / ".git")

    # A --no-checkout clone has no local branch, so a revision like
    # "snapshot~3" cannot resolve until the branch exists locally.  Materialise
    # the branch first, then move to the requested revision.
    _checkout(into, branch)
    target = branch
    verified_rev = branch
    if ref and ref != branch:
        proc = subprocess.run(
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
        proc = subprocess.run(
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


def directory_is_seafile_synced(path: Path) -> bool:
    """Whether ``path`` is inside one of the desktop client's synced libraries.

    Only meaningful where the Seafile client is installed; on a machine without
    it there are no synced libraries and this is simply ``False``.  Imported
    lazily so the tool keeps working if the package layout ever changes -- a
    failed import means "cannot tell", not "safe".
    """
    try:
        from git_remote_seafile.safety import discover_local_synced_libraries
    except Exception:
        return False
    target = path.resolve()
    try:
        libraries = discover_local_synced_libraries()
    except Exception:
        return False
    for lib in libraries:
        worktree = lib.get("worktree")
        if worktree is None:
            continue
        try:
            target.relative_to(Path(worktree).resolve())
            return True
        except ValueError:
            continue
    return False


def check_paths_are_unsynced(*paths: tuple[str, Path]) -> None:
    """Refuse to snapshot a source, or write a vault, inside a synced library.

    This is the tool's own version of the helper's Trap 1/Trap 2 guard, and the
    reason it exists is the same: the Seafile client rewrites files underneath
    whatever lives in a synced folder, and it fights Git for control of every
    path Git touches.  The helper cannot see the vault (it is told about the
    *source* via ``GIT_WORK_TREE`` and nothing else), so for the vault this check
    is the only line of defence.
    """
    for label, path in paths:
        if directory_is_seafile_synced(path):
            raise SnapshotError(
                f"{label} is inside a Seafile-synced library:\n  {path}\n"
                f"The sync client rewrites files underneath it and competes with "
                f"Git for every path, which is the exact problem this tool "
                f"exists to avoid.\n"
                f"Move it outside the synced folder -- e.g. under C:/code/ -- "
                f"or sync that library with 'auto sync' disabled."
            )


def _read_config_key(git_dir: Path, key: str) -> str:
    """A single value out of the *vault's own* config, or ''.

    Kept for one-off reads and for tests; the snapshot path itself reads every
    key it needs in a single call via :func:`_read_local_config_map`.
    """
    proc = subprocess.run(
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
    proc = subprocess.run(
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


def _same_config_value(stored: str, desired: str) -> bool:
    """Whether two spellings of a config value are the same setting.

    Git rewrites a Windows path's backslashes to forward slashes as it stores
    it, so a literal ``str(source)`` never compares equal to what comes back --
    and without normalising, every run would look changed and be rewritten.
    """
    return stored.replace("\\", "/") == desired.replace("\\", "/")


def remembered_settings(vault: Path, source: Path) -> dict[str, object]:
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

    remembered: dict[str, object] = {}
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
    remote: str | None,
    code_remote: str | None,
    exclude: list[str],
    branch: str,
) -> None:
    """Persist the resolved choices into the vault's own config.

    Written with ``git config --local``, so it lives and travels with the vault
    and nothing is put in the user's global config.

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
    settings: list[tuple[str, list[str]]] = [
        ("snapshot.source", [str(source)]),
        ("snapshot.remote", [remote] if remote else []),
        ("snapshot.coderemote", [code_remote] if code_remote else []),
        ("snapshot.branch", [branch]),
        ("snapshot.exclude", list(exclude)),
    ]

    current = _read_local_config_map(vault)

    def unchanged(stored: list[str], values: list[str]) -> bool:
        if len(stored) != len(values):
            return False
        return all(
            _same_config_value(a, b) for a, b in zip(stored, values, strict=True)
        )

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "--git-dir", str(vault / ".git"), "config", "--local", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    for key, values in settings:
        stored = current.get(key, [])
        if unchanged(stored, values):
            continue
        if not values:
            # `--unset-all` exits 5 when the key is absent; that is not an error.
            run("--unset-all", key)
        elif len(values) == 1 and len(stored) <= 1:
            # A single value over a single (or absent) key: a plain set replaces
            # it.  This is the common shape, and one call instead of two.
            run(key, values[0])
        else:
            # Either several values now, or a multi-valued key being rewritten
            # -- git refuses to overwrite multiple values with one, so the old
            # values are cleared first or they would linger.
            run("--unset-all", key)
            for value in values:
                run("--add", key, value)


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
        help="snapshot locally and do not push at all, ignoring any remembered "
        "or derived --remote",
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
        "--verify",
        action="store_true",
        help="with --restore, also run 'git fsck' on the clone. Byte-exactness "
        "is checked on every restore regardless",
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

    if not args.restore:
        for flag, value in (("--verify", args.verify), ("--into", args.into),
                            ("--ref", args.ref)):
            if value:
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
        ):
            if value:
                problems.append(f"{flag} does not apply to --restore")

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

    if args.no_push:
        remote: str | None = None
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
    check_paths_are_unsynced(("source", source), ("vault", vault))

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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        validate_args(args)
        if args.restore:
            if not args.remote:
                raise SnapshotError("--restore requires --remote <url>")
            if not args.into:
                raise SnapshotError("--restore requires --into <directory>")
            restore(
                args.remote,
                args.into.expanduser().resolve(),
                args.branch or DEFAULT_BRANCH,
                args.ref,
                verify=args.verify,
            )
            return 0

        source, vault, remote, code_remote, exclude, branch = (
            resolve_snapshot_settings(args)
        )

        if args.dry_run and remote:
            print(f"dry run -- would push the vault to {remote}")

        if code_remote:
            if args.dry_run:
                print(
                    f"dry run -- would push the source repo to "
                    f"'{code_remote}'"
                )
            else:
                print(f"pushing source repo to '{code_remote}' ...")
                proc = subprocess.run(
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
                        f"snapshot aborted before touching the vault"
                    )

        git_dir = ensure_vault(vault, branch)
        ensure_identity(vault, source)
        ensure_byte_exact(vault, git_dir)
        # Remember *after* the vault exists, so the first run leaves the next one
        # with nothing to type.  Not on a dry run: a preview must not mutate.
        if not args.dry_run:
            remember_settings(
                vault,
                source,
                remote=remote,
                code_remote=code_remote,
                exclude=exclude,
                branch=branch,
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

            tree, commit, excluded = build_snapshot(
                git_dir, source, base, exclude, message
            )
            count, size = describe_tree(git_dir, source, tree)

            print(f"source : {source}")
            print(f"vault  : {vault}")
            print(f"branch : {branch}")
            print(f"files  : {count} ({human_bytes(size)})")
            print(f"base   : {base[:12] if base else '(first snapshot)'}")
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

            unchanged = commit is None
            if unchanged:
                print("no changes since the last snapshot")
            elif base:
                stat = diff_stat(git_dir, source, base, tree)
                if stat:
                    print(stat)

            if args.dry_run:
                if unchanged:
                    print("\ndry run -- nothing to commit, nothing pushed")
                else:
                    print(
                        f"\ndry run -- branch not moved, nothing pushed "
                        f"(would be {commit[:12]})"
                    )
                return 0

            if unchanged:
                print("\nnothing to commit; branch left where it is")
            else:
                set_branch(git_dir, source, branch, commit, base)
                print(f"\nmoved refs/heads/{branch} -> {commit[:12]}")

            if not remote:
                print("no --remote; snapshot kept locally (pass --remote, or set "
                      "one in the source repo, to push)")
            elif unchanged and base == remote_tip:
                print("the remote already has this snapshot; nothing to push")
            else:
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
        return 0

    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
