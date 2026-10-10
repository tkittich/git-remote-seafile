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

Updates
=======
Each run is a commit on top of the previous one, so the vault is a linear chain
of snapshots.  Only changed blobs are uploaded: Git is content-addressed, so a
snapshot that changed one file in a 486 KB tree pushed 31 KB, and a snapshot
that changed nothing pushes nothing at all (no commit is created).  The vault's
index keeps a stat cache, so unchanged files are not re-hashed either.

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


def ensure_identity(vault: Path, source: Path) -> None:
    """Give the vault a committer identity, inheriting the source repo's.

    A vault created by ``git init`` has none of its own, so it falls back to the
    global identity.  When that is missing too, ``commit-tree`` dies with a bare
    "Author identity unknown" -- so resolve it up front and prefer the source
    repository's own identity, which is whose code this is.
    """
    for key in ("user.name", "user.email"):
        if _config(vault, key):
            continue
        inherited = _config(source, key)
        if not inherited:
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


def build_snapshot(
    git_dir: Path,
    work_tree: Path,
    branch: str,
    excludes: list[str],
    message: str,
) -> tuple[str, str | None, str | None]:
    """Stage the whole tree into the vault index and write a commit object.

    Returns ``(tree, commit, parent)``, where ``commit`` is ``None`` when the
    tree is identical to the previous snapshot -- an unchanged run should not
    pile up empty commits.  The branch is *not* moved here: that is the caller's
    job, so a dry run can stop one step short.
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
    parent = current_tip(git_dir, work_tree, branch)

    if parent:
        parent_tree = git(
            git_dir, work_tree, ["rev-parse", f"{parent}^{{tree}}"], cwd=work_tree
        ).stdout.strip()
        if parent_tree == tree:
            return tree, None, parent

    commit_args = ["commit-tree", tree]
    if parent:
        commit_args += ["-p", parent]
    commit_args += ["-m", message]
    commit = git(git_dir, work_tree, commit_args, cwd=work_tree).stdout.strip()
    return tree, commit, parent


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
        "config can be inspected)",
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
        message = args.message or f"snapshot {datetime.now().isoformat(timespec='seconds')}"

        with VaultLock(git_dir):
            tree, commit, parent = build_snapshot(
                git_dir, source, args.branch, args.exclude, message
            )
            count, size = describe_tree(git_dir, source, tree)

            print(f"source : {source}")
            print(f"vault  : {vault}")
            print(f"branch : {args.branch}")
            print(f"files  : {count} ({human_bytes(size)})")
            print(f"base   : {parent[:12] if parent else '(first snapshot)'}")

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
            elif parent:
                stat = diff_stat(git_dir, source, parent, tree)
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
                ref_args = ["update-ref", f"refs/heads/{args.branch}", commit]
                if parent:
                    ref_args.append(parent)
                git(git_dir, source, ref_args, cwd=source)
                print(f"\nmoved refs/heads/{args.branch} -> {commit[:12]}")

            if args.remote:
                print(f"pushing to {args.remote} ...")
                git(
                    git_dir,
                    source,
                    [
                        "push",
                        args.remote,
                        f"refs/heads/{args.branch}:refs/heads/{args.branch}",
                    ],
                    cwd=source,
                )
                print("push complete")
            else:
                print("no --remote given; snapshot kept locally")
        return 0

    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
