"""git_util.py - Git subprocess utilities for git-remote-seafile."""

from __future__ import annotations

import os
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


def rev_parse(ref: str) -> str | None:
    """Resolve a reference to its SHA-1 hash."""
    out, _, code = run_git(["rev-parse", "--verify", ref])
    if code != 0:
        return None
    return out.decode("utf-8").strip()


def is_ancestor(ancestor_sha: str, descendant_sha: str) -> bool:
    """Check if ancestor_sha is an ancestor of descendant_sha (fast-forward check)."""
    _, _, code = run_git(["merge-base", "--is-ancestor", ancestor_sha, descendant_sha])
    return code == 0


def get_objects_to_push(local_sha: str, exclude_shas: list[str] | str | None = None) -> list[str]:
    """Find all git object SHAs reachable from local_sha but not in exclude_shas."""
    args = ["rev-list", "--objects", local_sha]
    if exclude_shas:
        if isinstance(exclude_shas, str):
            exclude_shas = [exclude_shas]
        for ex in exclude_shas:
            if ex:
                args.extend(["--not", ex])

    out, err, code = run_git(args)
    if code != 0:
        raise GitError(f"Failed to list objects to push: {err.decode('utf-8', errors='replace')}")

    lines = out.decode("utf-8").splitlines()
    objects = []
    for line in lines:
        parts = line.strip().split()
        if parts:
            objects.append(parts[0])
    return objects


def create_packfile(object_shas: list[str]) -> tuple[str, bytes, bytes]:
    """Generate a .pack file and corresponding .idx file for the given object SHAs.
    
    Returns (pack_sha, pack_bytes, idx_bytes).
    """
    if not object_shas:
        return "", b"", b""

    with tempfile.TemporaryDirectory(prefix="git-seaf-pack-") as td:
        tmp_dir = Path(td)
        pack_prefix = tmp_dir / "pack"

        # 1. Generate packfile
        input_data = ("\n".join(object_shas) + "\n").encode("utf-8")
        out, err, code = run_git(
            ["pack-objects", str(pack_prefix)],
            input_bytes=input_data,
        )
        if code != 0:
            raise GitError(f"git pack-objects failed: {err.decode('utf-8', errors='replace')}")

        pack_sha = out.decode("utf-8").strip()
        pack_file = tmp_dir / f"pack-{pack_sha}.pack"
        idx_file = tmp_dir / f"pack-{pack_sha}.idx"

        if not pack_file.is_file():
            raise GitError(f"Expected packfile not created: {pack_file}")

        # If .idx was not automatically generated, create it
        if not idx_file.is_file():
            _, err, code = run_git(["index-pack", "-o", str(idx_file), str(pack_file)])
            if code != 0:
                raise GitError(f"git index-pack failed: {err.decode('utf-8', errors='replace')}")

        pack_bytes = pack_file.read_bytes()
        idx_bytes = idx_file.read_bytes()
        return pack_sha, pack_bytes, idx_bytes


def install_packfile(pack_name: str, pack_bytes: bytes, idx_bytes: bytes | None = None) -> None:
    """Save a packfile into the local repository's .git/objects/pack/ directory."""
    git_dir = get_git_dir()
    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)

    base_name = pack_name.removesuffix(".pack").removesuffix(".idx")
    target_pack = pack_dir / f"{base_name}.pack"
    target_idx = pack_dir / f"{base_name}.idx"

    target_pack.write_bytes(pack_bytes)

    if idx_bytes:
        target_idx.write_bytes(idx_bytes)
    else:
        # Generate index locally
        _, err, code = run_git(["index-pack", "-o", str(target_idx), str(target_pack)])
        if code != 0:
            raise GitError(f"git index-pack failed on installed pack: {err.decode('utf-8', errors='replace')}")


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

