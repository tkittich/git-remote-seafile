"""packs.py - Packfile transfer, verification and staging for git-remote-seafile."""

from __future__ import annotations

from pathlib import Path
import inspect
import re
import shutil
import sys
import tempfile
from typing import Any

from .client import SeafileClient, SeafileAPIError
from .git_util import install_packfile

PACK_NAME_RE = re.compile(r"^pack-[0-9a-zA-Z._-]+\.pack$")
MAX_IN_MEMORY_PACK_BYTES = 16 * 1024 * 1024


def is_valid_pack_name(pack_name: str) -> bool:
    """Validate remote packfile name against pattern and directory traversal."""
    if not PACK_NAME_RE.match(pack_name):
        return False
    if ".." in pack_name or "/" in pack_name or "\\" in pack_name:
        return False
    return True


def check_remote_has_packs(client: SeafileClient, repo_id: str, remote_pack_dir: str) -> bool:
    """True if the remote already holds at least one packfile.

    Used to tell a genuinely empty repository apart from one whose ref
    listing failed.  A missing pack directory is the genuine-empty answer;
    any *other* listing failure propagates, so the caller's empty-repository
    guard cannot be silenced by the same transient error that emptied the ref
    listing.
    """
    entries = client.list_dir(repo_id, remote_pack_dir)  # 404 -> []; failures raise
    if not isinstance(entries, list):
        return False
    return any(e.get("name", "").endswith(".pack") for e in entries if isinstance(e, dict))


def fetch_pack_artifact(
    client: SeafileClient,
    repo_id: str,
    remote_path: str,
    dest: Path,
    expected_size: int | None = None,
    *,
    progress_callback: Any = None,
) -> str | None:
    """Download one remote pack artifact (a .pack or .idx) to *dest*.

    Streams via download_file_to when the client supports it, else falls back
    to an in-memory GET capped at MAX_IN_MEMORY_PACK_BYTES -- a larger artifact
    raises instead, since that path means streaming already failed and
    buffering is the only option left.

    Returns None when *dest* holds a non-empty file that matches
    *expected_size* whenever the listing provided one; otherwise returns the
    failure reason -- 'missing' (nothing downloaded), 'empty' (zero bytes), or
    'truncated' (size mismatch) -- and the caller decides whether that is
    fatal (a pack) or regenerable (an index).

    Residual: when the listing omitted the size entirely (``expected_size is
    None``), the in-memory fallback cannot be size-checked up front, and a
    huge artifact would be buffered before it could be refused.  Every
    current caller passes the size from the listing; keep that contract for
    any future caller.
    """
    downloaded = False
    if hasattr(client, "download_file_to"):
        try:
            downloaded = bool(
                client.download_file_to(repo_id, remote_path, dest, progress_callback=progress_callback)
            ) and dest.is_file()
        except Exception as exc:
            sys.stderr.write(f"Warning: streaming download of {dest.name} failed: {exc}\n")
            sys.stderr.flush()
            downloaded = False
    if not downloaded:
        if expected_size is not None and expected_size > MAX_IN_MEMORY_PACK_BYTES:
            raise SeafileAPIError(
                f"{dest.name} streaming download failed and its size ({expected_size} bytes) "
                f"exceeds in-memory buffer limit ({MAX_IN_MEMORY_PACK_BYTES} bytes)."
            )
        data = client.get_file_bytes(repo_id, remote_path)
        if data:
            dest.write_bytes(data)
            downloaded = True
    if not downloaded:
        return "missing"
    actual = dest.stat().st_size
    if actual == 0:
        return "empty"
    if expected_size is not None and actual != expected_size:
        return "truncated"
    return None


def fetch_and_install_pack(
    client: SeafileClient,
    repo_id: str,
    remote_pack_dir: str,
    pack_name: str,
    git_dir: Path | None = None,
    expected_size: int | None = None,
    expected_idx_size: int | None = None,
    installer: Any = install_packfile,
) -> None:
    """Download a remote packfile and its index, verify integrity, and install atomically."""
    idx_name = pack_name.removesuffix(".pack") + ".idx"
    sys.stderr.write(f"Downloading {pack_name} from Seafile...\n")
    sys.stderr.flush()

    staging_dir = Path(tempfile.mkdtemp(prefix="grs-fetch-", dir=str(git_dir) if git_dir else None))
    try:
        staged_pack = staging_dir / pack_name
        staged_idx = staging_dir / idx_name

        reason = fetch_pack_artifact(
            client, repo_id, f"{remote_pack_dir}/{pack_name}", staged_pack, expected_size=expected_size
        )
        if reason == "truncated":
            raise SeafileAPIError(
                f"Packfile {pack_name} download was truncated: expected {expected_size} bytes, "
                f"got {staged_pack.stat().st_size} bytes."
            )
        if reason is not None:
            raise SeafileAPIError(
                f"Packfile {pack_name} is listed at {remote_pack_dir} but could not be "
                "downloaded -- the local repository would be missing objects. Retry the "
                "fetch, or run 'git-remote-seafile test <url>' to diagnose."
            )

        # A missing or truncated index is regenerable locally; only a size
        # mismatch is worth telling the user about.
        idx_reason = fetch_pack_artifact(
            client, repo_id, f"{remote_pack_dir}/{idx_name}", staged_idx, expected_size=expected_idx_size
        )
        idx_downloaded = idx_reason is None
        if idx_reason == "truncated":
            sys.stderr.write(f"Warning: downloaded {idx_name} size mismatch; will regenerate locally.\n")
            sys.stderr.flush()
            idx_downloaded = False

        target = installer or install_packfile
        # Custom installer callbacks may predate the ``move`` parameter; probe
        # the signature instead of retrying on TypeError -- a bare except would
        # mask a genuine failure occurring *after* the staged files were moved.
        try:
            accepts_move = "move" in inspect.signature(target).parameters
        except (TypeError, ValueError):
            accepts_move = False
        if accepts_move:
            target(pack_name, staged_pack, staged_idx if idx_downloaded else None, move=True)
        else:
            target(pack_name, staged_pack, staged_idx if idx_downloaded else None)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
