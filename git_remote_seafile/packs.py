"""packs.py - Packfile transfer, verification and staging for git-remote-seafile."""

from __future__ import annotations

from pathlib import Path
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
    listing failed. Returns False when the question cannot be answered.
    """
    try:
        entries = client.list_dir(repo_id, remote_pack_dir)
        if not isinstance(entries, list):
            return False
    except Exception:
        return False
    return any(e.get("name", "").endswith(".pack") for e in entries if isinstance(e, dict))


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

        downloaded = False
        if hasattr(client, "download_file_to"):
            try:
                downloaded = bool(client.download_file_to(
                    repo_id, f"{remote_pack_dir}/{pack_name}", staged_pack
                )) and staged_pack.is_file()
            except Exception as exc:
                sys.stderr.write(f"Warning: streaming download of {pack_name} failed: {exc}\n")
                downloaded = False

        if not downloaded:
            if expected_size is not None and expected_size > MAX_IN_MEMORY_PACK_BYTES:
                raise SeafileAPIError(
                    f"Packfile {pack_name} streaming download failed and size ({expected_size} bytes) "
                    "exceeds in-memory buffer limit (16MB)."
                )
            pack_bytes = client.get_file_bytes(repo_id, f"{remote_pack_dir}/{pack_name}")
            if pack_bytes:
                staged_pack.write_bytes(pack_bytes)
                downloaded = True

        if not downloaded or not staged_pack.is_file() or staged_pack.stat().st_size == 0:
            raise SeafileAPIError(
                f"Packfile {pack_name} is listed at {remote_pack_dir} but could not be "
                "downloaded -- the local repository would be missing objects. Retry the "
                "fetch, or run 'git-remote-seafile test <url>' to diagnose."
            )

        if expected_size is not None and staged_pack.stat().st_size != expected_size:
            raise SeafileAPIError(
                f"Packfile {pack_name} download was truncated: expected {expected_size} bytes, "
                f"got {staged_pack.stat().st_size} bytes."
            )

        # Download idx
        idx_downloaded = False
        if hasattr(client, "download_file_to"):
            try:
                idx_downloaded = bool(client.download_file_to(
                    repo_id, f"{remote_pack_dir}/{idx_name}", staged_idx
                )) and staged_idx.is_file()
            except Exception as exc:
                sys.stderr.write(f"Warning: streaming download of {idx_name} failed: {exc}\n")
                idx_downloaded = False

        if not idx_downloaded:
            idx_bytes = client.get_file_bytes(repo_id, f"{remote_pack_dir}/{idx_name}")
            if idx_bytes:
                staged_idx.write_bytes(idx_bytes)
                idx_downloaded = True

        if idx_downloaded and expected_idx_size is not None and staged_idx.stat().st_size != expected_idx_size:
            sys.stderr.write(f"Warning: downloaded {idx_name} size mismatch; will regenerate locally.\n")
            sys.stderr.flush()
            try:
                staged_idx.unlink(missing_ok=True)
            except Exception:
                pass
            idx_downloaded = False

        try:
            (installer or install_packfile)(
                pack_name, staged_pack, staged_idx if idx_downloaded else None, move=True
            )
        except TypeError:
            (installer or install_packfile)(
                pack_name, staged_pack, staged_idx if idx_downloaded else None
            )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
