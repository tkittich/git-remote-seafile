"""git_util.py - Git subprocess utilities for git-remote-seafile."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


class GitError(Exception):
    pass


def run_git(args: list[str], input_bytes: bytes | None = None, cwd: Path | None = None) -> tuple[bytes, bytes, int]:
    """Execute a git command and return (stdout, stderr, returncode)."""
    proc = subprocess.Popen(
        ["git"] + args,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )
    out, err = proc.communicate(input=input_bytes)
    return out, err, proc.returncode


def get_git_dir() -> Path:
    """Return the absolute path to the .git directory of the current repository."""
    out, err, code = run_git(["rev-parse", "--git-dir"])
    if code != 0:
        raise GitError(f"Not inside a git repository: {err.decode('utf-8', errors='replace')}")
    p = Path(out.decode("utf-8").strip())
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


#: Variables git uses to decide which repository it is operating on.  A remote
#: helper is launched *by* git, which exports these pointing at the caller's
#: repository; they take precedence over -C, so a command meant for a
#: *different* repository (the scratch bare repo used by compaction) must not
#: inherit them.
_GIT_REPO_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
)


def clean_git_env() -> dict[str, str]:
    """Return a copy of the environment with git's repo-locating vars removed.

    Use this for git commands that must operate on a repository other than the
    one this process was launched in.
    """
    env = dict(os.environ)
    for var in _GIT_REPO_ENV_VARS:
        env.pop(var, None)
    return env


def rev_parse(ref: str) -> str | None:
    """Resolve a reference to its SHA-1 hash."""
    out, _, code = run_git(["rev-parse", "--verify", ref])
    if code != 0:
        return None
    return out.decode("utf-8").strip()


def is_ancestor(ancestor_sha: str, descendant_sha: str) -> bool:
    """Check if ancestor_sha is an ancestor of descendant_sha (fast-forward check)."""
    _, err, code = run_git(["merge-base", "--is-ancestor", ancestor_sha, descendant_sha])
    if code == 0:
        return True
    if code == 1:
        return False
    raise GitError(
        f"git merge-base failed (exit {code}): {err.decode('utf-8', errors='replace').strip()}"
    )


HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_HEX_SHA_RE = HEX_SHA_RE


def filter_existing_objects(shas: list[str] | str | None) -> list[str]:
    """Return those SHAs that exist in the local object store, in order.

    Checked in a single git process, because one call per ref would be
    needlessly slow on a repository with many branches.
    """
    if not shas:
        return []
    if isinstance(shas, str):
        shas = [shas]
    candidates = [s for s in dict.fromkeys(shas) if s and _HEX_SHA_RE.match(s)]
    if not candidates:
        return []

    try:
        git_dir = get_git_dir()
    except Exception:
        git_dir = None

    cmd = ["git"]
    if git_dir is not None:
        cmd.extend(["--git-dir", str(git_dir)])
    cmd.extend(["cat-file", "--batch-check"])

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = proc.communicate(("\n".join(candidates) + "\n").encode("utf-8"))
        if proc.returncode != 0:
            return []
    except Exception:
        return []

    existing = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        # "<sha> <type> <size>" when present, "<sha> missing" when absent.
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "missing":
            existing.append(parts[0])
    return existing


def get_objects_to_push(local_sha: list[str] | str, exclude_shas: list[str] | str | None = None) -> list[str]:
    """Find all git object lines reachable from local_sha but not in exclude_shas.

    local_sha may be a single SHA string or a list of SHAs (for multi-spec pushes).
    Preserves full '<sha> <path>' lines emitted by 'rev-list --objects' so that
    'pack-objects' receives file path hints for optimal delta compression windows.

    Exclusions that are not in the local object store are ignored rather than
    handed to git.  Over diverged history the remote tip can be a commit this
    clone has never fetched, and "rev-list --not <unknown>" fails with
    "bad object" instead of simply excluding nothing -- which made a legitimate
    force-push impossible.
    """
    if isinstance(local_sha, str):
        shas = [local_sha]
    else:
        shas = list(local_sha)
    unique_shas = list(dict.fromkeys(s for s in shas if s))
    if not unique_shas:
        return []

    args = ["rev-list", "--objects"] + unique_shas
    for ex in filter_existing_objects(exclude_shas):
        args.append(f"^{ex}")

    out, err, code = run_git(args)
    if code != 0:
        raise GitError(f"Failed to list objects to push: {err.decode('utf-8', errors='replace')}")

    lines = out.decode("utf-8", errors="surrogateescape").splitlines()
    objects = [line.strip() for line in lines if line.strip()]
    return objects


def _generate_pack_in_dir(tmp_dir: Path, object_shas: list[str]) -> tuple[str, Path, Path]:
    """Helper to generate packfile and index in a target directory."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pack_prefix = tmp_dir / "pack"

    input_data = ("\n".join(object_shas) + "\n").encode("utf-8", errors="surrogateescape")
    out, err, code = run_git(
        ["pack-objects", str(pack_prefix)],
        input_bytes=input_data,
    )
    if code != 0:
        err_msg = err.decode("utf-8", errors="replace").strip()
        raise GitError(f"git pack-objects failed: {err_msg}")

    pack_sha = out.decode("utf-8").strip()
    pack_file = tmp_dir / f"pack-{pack_sha}.pack"
    idx_file = tmp_dir / f"pack-{pack_sha}.idx"

    if not pack_file.is_file():
        raise GitError(f"Expected packfile not created: {pack_file}")

    # If .idx was not automatically generated, create it
    if not idx_file.is_file():
        _, err, code = run_git(["index-pack", "-o", str(idx_file), str(pack_file)])
        if code != 0:
            err_msg = err.decode("utf-8", errors="replace").strip()
            raise GitError(f"git index-pack failed: {err_msg}")

    return pack_sha, pack_file, idx_file


def create_packfile(
    object_shas: list[str],
    staged_dir: Path | str | None = None,
) -> tuple[str, bytes | Path, bytes | Path]:
    """Generate a .pack file and corresponding .idx file for the given object SHAs.

    If *staged_dir* is provided, the pack and idx are created directly in that
    directory and their Path objects are returned, avoiding buffering the packfile
    into memory. Otherwise, returns (pack_sha, pack_bytes, idx_bytes).
    """
    if not object_shas:
        return "", b"", b""

    # Use .git directory as parent for temporary pack directory to guarantee
    # it resides on the same filesystem/drive, preventing cross-device 'Improper link' (EXDEV)
    # errors during git pack-objects atomic rename on Windows and multi-volume systems.
    try:
        tmp_parent = get_git_dir()
    except Exception:
        tmp_parent = None

    if staged_dir is not None:
        return _generate_pack_in_dir(Path(staged_dir), object_shas)

    with tempfile.TemporaryDirectory(prefix="git-seaf-pack-", dir=str(tmp_parent) if tmp_parent else None) as td:
        pack_sha, pack_file, idx_file = _generate_pack_in_dir(Path(td), object_shas)
        pack_bytes = pack_file.read_bytes()
        idx_bytes = idx_file.read_bytes()
        return pack_sha, pack_bytes, idx_bytes


def install_packfile(pack_name: str, pack_bytes: bytes | Path | str, idx_bytes: bytes | Path | str | None = None) -> None:
    """Install a packfile into the local repository's .git/objects/pack/.

    Accepts pack content either in-memory as bytes, or as a Path/str pointing
    to a staged file on disk, avoiding in-memory buffering for large packs.

    The pack and its index are staged in a scratch directory and only moved to
    their final names once both are complete, so ``os.replace`` publishes them
    atomically.  This matters because cmd_fetch decides whether to download a
    pack by checking whether a file of that *name* already exists: a truncated
    .pack written straight to the final path would never be retried and would
    poison the object store permanently.  Nothing may appear at the final path
    until it is known-good.
    """
    git_dir = get_git_dir()
    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)

    base_name = pack_name.removesuffix(".pack").removesuffix(".idx")
    target_pack = pack_dir / f"{base_name}.pack"
    target_idx = pack_dir / f"{base_name}.idx"

    # Stage beside objects/ rather than inside objects/pack: git scans that
    # directory for pack-*.pack / pack-*.idx, and "git index-pack -o" insists
    # that the index it writes is named *.idx -- so a staged file cannot be
    # renamed out of the way there.  A sibling of objects/ is still on the same
    # filesystem, which is what keeps the final os.replace atomic.
    staging_dir = Path(tempfile.mkdtemp(prefix="grs-staging-", dir=str(git_dir)))
    try:
        staged_pack = staging_dir / f"{base_name}.pack"
        staged_idx = staging_dir / f"{base_name}.idx"

        if isinstance(pack_bytes, (str, Path, os.PathLike)):
            shutil.copyfile(pack_bytes, staged_pack)
        else:
            staged_pack.write_bytes(pack_bytes)

        if idx_bytes:
            if isinstance(idx_bytes, (str, Path, os.PathLike)):
                shutil.copyfile(idx_bytes, staged_idx)
            else:
                staged_idx.write_bytes(idx_bytes)
            # Verify the index matches the packfile (N-5)
            _, err, code = run_git(["verify-pack", "-v", str(staged_idx)])
            if code != 0:
                try:
                    staged_idx.unlink(missing_ok=True)
                except Exception:
                    pass
                _, err, code = run_git(["index-pack", "-o", str(staged_idx), str(staged_pack)])
                if code != 0:
                    raise GitError(f"git index-pack failed on installed pack: {err.decode('utf-8', errors='replace')}")
        else:
            # Generate index locally
            _, err, code = run_git(["index-pack", "-o", str(staged_idx), str(staged_pack)])
            if code != 0:
                raise GitError(f"git index-pack failed on installed pack: {err.decode('utf-8', errors='replace')}")

        # Both artefacts are complete; publish them.
        os.replace(staged_idx, target_idx)
        os.replace(staged_pack, target_pack)
    finally:
        # After a successful publish the staged files have been renamed away;
        # after a failure this is what keeps the final path clean for a retry.
        shutil.rmtree(staging_dir, ignore_errors=True)


def get_git_config(key: str, default: str | None = None) -> str | None:
    """Read a git configuration value."""
    out, _, code = run_git(["config", "--get", key])
    if code != 0 or not out:
        return default
    return out.decode("utf-8").strip()


def get_git_config_bool(key: str, default: bool = False) -> bool:
    """Read a boolean git configuration value."""
    val = get_git_config(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def get_git_config_int(key: str, default: int) -> int:
    """Read an integer git configuration value."""
    val = get_git_config(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default

