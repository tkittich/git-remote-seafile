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
* A vault-local lock file keeps two runs from interleaving.
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

Do **not** simply ``git clone`` the vault and check out.  On Windows the clone
inherits ``core.autocrlf=true`` from Git for Windows' system config, and the
vault's ``info/attributes`` -- the thing that makes it byte-exact -- is not
carried by a clone, while the source's own ``.gitattributes`` is.  The result is
a restore with LF rewritten to CRLF.  ``--restore`` applies the byte-exact
settings to the clone *before* checking out, so the files match the originals.

Usage
=====
::

    # snapshot only, no push -- see what would be captured
    python tools/seafile_snapshot.py --source C:/code/myproject --dry-run

    # snapshot + push to Seafile
    python tools/seafile_snapshot.py --source C:/code/myproject \
        --vault C:/code/myproject-vault \
        --remote seafile://code/myproject-vault

    # also push the code repo itself first, in one command
    python tools/seafile_snapshot.py --source C:/code/myproject \
        --code-remote origin \
        --remote seafile://code/myproject-vault

Recommended exclusions (reproducible junk you do not want in a backup)::

    --exclude node_modules --exclude venv --exclude .venv \
    --exclude __pycache__ --exclude '*.pyc' --exclude dist --exclude build

Secrets
=======
This tool will happily back up ``.env``, keys and credentials, and Git history
is permanent.  Push the vault to a library only you can read.  Never point the
vault at a public remote.
"""

from __future__ import annotations

import argparse
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


class SnapshotError(RuntimeError):
    """A failure the operator can act on."""


def git(
    git_dir: Path,
    work_tree: Path | None,
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
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


def _local_config(path: Path, key: str) -> str:
    """Read a value from the repository's *own* config only.

    ``git config --get`` reads through to the global and system files, so it
    cannot answer "does this repository have an identity of its own?" -- and
    that is exactly the question that decides whether to inherit one.  Using it
    there made the vault inherit nothing whenever a global identity existed.
    """
    proc = subprocess.run(
        ["git", "-C", str(path), "config", "--local", "--get", key],
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
    """
    for key in ("user.name", "user.email"):
        if _local_config(vault, key):
            continue
        inherited = _local_config(source, key)
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
    """
    git(git_dir, vault, ["config", "core.autocrlf", "false"], cwd=vault)
    git(git_dir, vault, ["config", "core.safecrlf", "false"], cwd=vault)

    attributes = git_dir / "info" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    existing = attributes.read_text(encoding="utf-8") if attributes.exists() else ""
    # Checked on the marker line, not on "-text": a vault created by an earlier
    # version carries only "* -text", and appending is safe because a later line
    # overrides an earlier one within the same attributes file.
    if BYTE_EXACT_MARKER not in existing:
        attributes.write_text(existing + BYTE_EXACT_ATTRIBUTES, encoding="utf-8")


class VaultLock:
    """An exclusive lock so two snapshots cannot interleave."""

    def __init__(self, git_dir: Path) -> None:
        self.path = git_dir / "seafile-snapshot.lock"
        self._fd: int | None = None

    def __enter__(self) -> "VaultLock":
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SnapshotError(
                f"another snapshot appears to be running (lock: {self.path}).\n"
                f"If that run died, delete the lock file and retry."
            ) from None
        os.write(self._fd, str(os.getpid()).encode())
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


def build_snapshot(
    git_dir: Path,
    work_tree: Path,
    parent: str | None,
    excludes: list[str],
    message: str,
) -> tuple[str, str | None]:
    """Stage the whole tree into the vault index and write a commit object.

    Returns ``(tree, commit)``, where ``commit`` is ``None`` when the tree is
    identical to ``parent`` -- an unchanged run should not pile up empty
    commits.  ``parent`` is passed in rather than read from the branch, because
    the caller may have just re-based on the remote tip.  The branch is *not*
    moved here: that is the caller's job, so a dry run can stop one step short.
    """
    git(git_dir, work_tree, ["add", "-A", "-f", "."], cwd=work_tree)

    for pattern in excludes:
        git(
            git_dir,
            work_tree,
            ["rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", pattern],
            cwd=work_tree,
            check=False,
        )

    tree = git(git_dir, work_tree, ["write-tree"], cwd=work_tree).stdout.strip()

    if parent:
        parent_tree = git(
            git_dir, work_tree, ["rev-parse", f"{parent}^{{tree}}"], cwd=work_tree
        ).stdout.strip()
        if parent_tree == tree:
            return tree, None

    return tree, commit_tree(git_dir, work_tree, tree, parent, message)


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


def restore(remote: str, into: Path, branch: str, ref: str | None) -> None:
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

    files = sum(1 for p in into.rglob("*") if p.is_file() and ".git" not in p.parts)
    print(f"restored {files} files from {target} into {into}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seafile_snapshot",
        description=(
            "Snapshot a working tree (gitignored files included) into a separate "
            "vault repository and push it to Seafile. The source directory is "
            "never modified."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "recommended exclusions:\n  "
            + " ".join(f"--exclude {p}" for p in RECOMMENDED_EXCLUDES)
            + "\n\nnote: the vault is a git repository and must NOT live inside a\n"
            "Seafile-synced folder, for the same reason the source must not.\n"
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.cwd(),
        help="directory to snapshot (default: current directory)",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault repository location (default: <source>-vault)",
    )
    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help=f"branch in the vault holding snapshots (default: {DEFAULT_BRANCH})",
    )
    parser.add_argument(
        "--remote",
        default=None,
        help="Seafile remote URL to push to, e.g. seafile://code/myproject-vault "
        "(omit to snapshot without pushing)",
    )
    parser.add_argument(
        "--code-remote",
        default=None,
        help="also run a normal 'git push <name>' in the source repo first",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="pathspec to leave out of the snapshot (repeatable)",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        if args.restore:
            if not args.remote:
                raise SnapshotError("--restore requires --remote <url>")
            if not args.into:
                raise SnapshotError("--restore requires --into <directory>")
            restore(
                args.remote,
                args.into.expanduser().resolve(),
                args.branch,
                args.ref,
            )
            return 0

        source = args.source.expanduser().resolve()
        vault = args.vault or source.with_name(source.name + "-vault")
        source, vault = resolve_paths(source, vault)

        if args.code_remote:
            print(f"pushing source repo to '{args.code_remote}' ...")
            proc = subprocess.run(
                ["git", "-C", str(source), "push", args.code_remote],
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

        git_dir = ensure_vault(vault, args.branch)
        ensure_identity(vault, source)
        ensure_byte_exact(vault, git_dir)
        # The hostname is in the default message because a shared vault is a
        # single chain of snapshots from several machines, and the commit author
        # is the same person on all of them -- so without this, `git log` cannot
        # say which machine saw which tree.
        message = args.message or (
            f"snapshot {datetime.now().isoformat(timespec='seconds')} "
            f"on {socket.gethostname()}"
        )

        with VaultLock(git_dir):
            # Decide the base *after* consulting the remote, so a second machine
            # appends to the shared chain rather than building an unrelated one.
            if args.remote:
                tip = fetch_remote_tip(git_dir, source, args.remote, args.branch)
                base = reconcile_with_remote(
                    git_dir,
                    source,
                    args.branch,
                    tip,
                    reset_to_remote=args.reset_to_remote,
                    move=not args.dry_run,
                )
            else:
                base = current_tip(git_dir, source, args.branch)

            tree, commit = build_snapshot(
                git_dir, source, base, args.exclude, message
            )
            count, size = describe_tree(git_dir, source, tree)

            print(f"source : {source}")
            print(f"vault  : {vault}")
            print(f"branch : {args.branch}")
            print(f"files  : {count} ({human_bytes(size)})")
            print(f"base   : {base[:12] if base else '(first snapshot)'}")

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
                set_branch(git_dir, source, args.branch, commit, base)
                print(f"\nmoved refs/heads/{args.branch} -> {commit[:12]}")

            if args.remote:
                push_snapshot(
                    git_dir,
                    source,
                    args.remote,
                    args.branch,
                    commit,
                    tree,
                    base,
                    message,
                )
            else:
                print("no --remote given; snapshot kept locally")
        return 0

    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
