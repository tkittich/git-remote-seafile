"""gc.py - Remote packfile compaction and garbage collection for git-remote-seafile."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import SeafileClient
from .config import RemoteConfig
from .git_util import clean_git_env
from .lock import RemoteLock
from .packs import PACK_NAME_RE, is_valid_pack_name, MAX_IN_MEMORY_PACK_BYTES
from .refs import REF_NAMESPACES, iter_refs

_PACK_NAME_RE = PACK_NAME_RE


def describe_size_delta(saved_kb: int) -> str:
    """Phrase a compaction's size change, including the negative case.

    Consolidating packfiles usually shrinks a repository, but not always:
    repacking can produce a slightly *larger* pack when the originals were
    already tightly packed, or when the new pack happens to delta less well.
    The delta is reported as a signed number because that is the honest value,
    but printing it raw produced ``(saved -12 KB)`` -- which reads like a bug,
    and calls a growth a saving.  So the negative case says what happened.
    """
    if saved_kb < 0:
        return (
            f"grew by ~{abs(saved_kb)} KB "
            "(repacking can do this when the existing packs were already tight)"
        )
    return f"saved ~{saved_kb} KB"


def compact_repository(
    client: SeafileClient,
    repo_id: str,
    repo_path: str,
    min_packs: int = 2,
    verbose: bool = True,
    config: RemoteConfig | None = None,
) -> dict[str, Any]:
    """Consolidate multiple small packfiles on Seafile into a single optimized packfile."""
    clean_repo = repo_path.rstrip("/")
    pack_dir = f"{clean_repo}/objects/pack"

    with RemoteLock(client, repo_id, clean_repo, config=config) as lock:
        def maybe_renew() -> None:
            lock.maybe_renew(20.0)

        def on_upload_progress(transferred: int, total: int) -> None:
            maybe_renew()

        # 1. Discover all remote packfiles
        entries = client.list_dir(repo_id, pack_dir)
        old_packs = [e["name"] for e in entries if e["name"].endswith(".pack")]

        if len(old_packs) < min_packs:
            if verbose:
                sys.stderr.write(
                    f"Repository has {len(old_packs)} packfile(s); compaction requires at least {min_packs}.\n"
                )
            return {
                "status": "skipped",
                "count": len(old_packs),
                "message": f"Only {len(old_packs)} packfile(s) present.",
            }

        if verbose:
            sys.stderr.write(f"Starting compaction for {len(old_packs)} packfiles...\n")

        with tempfile.TemporaryDirectory(prefix="git-seaf-gc-") as td:
            tmp = Path(td)
            bare_repo = tmp / "bare.git"

            # 2. Initialize temporary bare Git repository.  Every git command
            # below targets this scratch repo, not the caller's, so it must run
            # with a scrubbed environment: when a helper is launched by git,
            # GIT_DIR points at the caller's repository and overrides -C.
            scratch_env = clean_git_env()
            subprocess.run(
                ["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True, env=scratch_env
            )

            local_pack_dir = bare_repo / "objects" / "pack"
            local_pack_dir.mkdir(parents=True, exist_ok=True)

            pack_sizes = {
                e.get("name"): e.get("size")
                for e in entries
                if isinstance(e, dict) and e.get("name")
            }

            # 3. Download all packfiles and indices
            downloaded_packs: list[str] = []
            total_old_bytes = 0
            for pack_name in old_packs:
                if not is_valid_pack_name(pack_name):
                    return {
                        "status": "error",
                        "message": f"Invalid packfile name '{pack_name}' on remote; aborting compaction to prevent data loss.",
                    }
                maybe_renew()
                if verbose:
                    sys.stderr.write(f"  Downloading {pack_name}...\n")
                idx_name = pack_name.removesuffix(".pack") + ".idx"
                pack_file = local_pack_dir / pack_name
                idx_file = local_pack_dir / idx_name
                expected_size = pack_sizes.get(pack_name)

                # Stream packfile directly to disk, falling back to get_file_bytes if needed.
                downloaded = False
                if hasattr(client, "download_file_to"):
                    try:
                        downloaded = bool(client.download_file_to(repo_id, f"{pack_dir}/{pack_name}", pack_file)) and pack_file.is_file()
                    except Exception as exc:
                        sys.stderr.write(f"Warning: streaming download of {pack_name} failed: {exc}\n")
                        downloaded = False
                if not downloaded:
                    if expected_size is not None and expected_size > MAX_IN_MEMORY_PACK_BYTES:
                        return {
                            "status": "error",
                            "message": f"Packfile {pack_name} ({expected_size} bytes) exceeds in-memory buffer limit ({MAX_IN_MEMORY_PACK_BYTES} bytes) and streaming failed.",
                        }
                    pack_bytes = client.get_file_bytes(repo_id, f"{pack_dir}/{pack_name}")
                    if not pack_bytes:
                        return {
                            "status": "error",
                            "message": f"Failed to download {pack_name} during compaction; aborting compaction to prevent data loss.",
                        }
                    pack_file.write_bytes(pack_bytes)

                if expected_size is not None and pack_file.stat().st_size != expected_size:
                    return {
                        "status": "error",
                        "message": f"Packfile {pack_name} downloaded size ({pack_file.stat().st_size}) does not match expected size ({expected_size}); aborting compaction.",
                    }

                downloaded_packs.append(pack_name)
                total_old_bytes += pack_file.stat().st_size

                idx_downloaded = False
                if hasattr(client, "download_file_to"):
                    try:
                        idx_downloaded = bool(client.download_file_to(repo_id, f"{pack_dir}/{idx_name}", idx_file)) and idx_file.is_file()
                    except Exception as exc:
                        sys.stderr.write(f"Warning: streaming download of {idx_name} failed: {exc}\n")
                        idx_downloaded = False
                if not idx_downloaded:
                    idx_bytes = client.get_file_bytes(repo_id, f"{pack_dir}/{idx_name}")
                    if idx_bytes:
                        idx_file.write_bytes(idx_bytes)
                        idx_downloaded = True

                verified = False
                if idx_downloaded and idx_file.is_file():
                    res = subprocess.run(
                        ["git", "verify-pack", "-v", str(idx_file)],
                        capture_output=True,
                        env=scratch_env,
                    )
                    verified = (res.returncode == 0)

                if not verified:
                    try:
                        idx_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    subprocess.run(
                        ["git", "index-pack", "-o", str(idx_file), str(pack_file)],
                        check=True,
                        capture_output=True,
                        env=scratch_env,
                    )

            # 4. Mirror remote refs so git repack knows all roots are reachable.
            # This walk must be recursive: a nested ref that is not mirrored
            # here makes its objects look unreachable, and "repack -a -d" would
            # then delete them from the consolidated packfile -- silently and
            # irreversibly.
            mirrored_refs = 0
            for namespace in REF_NAMESPACES:
                for ref_name, sha in iter_refs(client, repo_id, clean_repo, namespace):
                    if not sha or not sha.strip():
                        raise RuntimeError(
                            f"refusing to compact {clean_repo}: empty SHA for ref '{ref_name}'."
                        )
                    ref_file = bare_repo / ref_name
                    ref_file.parent.mkdir(parents=True, exist_ok=True)
                    ref_file.write_text(f"{sha.strip()}\n", encoding="utf-8")
                    mirrored_refs += 1

            # Compaction is only safe when we know what is reachable.  With no
            # refs mirrored, "git repack -a -d" treats every object as
            # unreachable and step 7 deletes the lot.  An empty ref listing is
            # far more likely to be a failed request than a genuinely empty
            # repository, so refuse rather than guess: a skipped compaction is
            # cheap, deleted history is not.
            if mirrored_refs == 0:
                raise RuntimeError(
                    f"refusing to compact {clean_repo}: no refs could be read, so no object can be "
                    "proven reachable. Nothing was deleted."
                )

            # 5. Run git repack -a -d -l
            maybe_renew()
            if verbose:
                sys.stderr.write("  Repacking objects with delta compression...\n")
            res = subprocess.run(
                ["git", "-C", str(bare_repo), "repack", "-a", "-d", "-l"],
                capture_output=True,
                text=True,
                env=scratch_env,
            )
            maybe_renew()
            if res.returncode != 0:
                raise RuntimeError(f"git repack failed: {res.stderr}")

            # 6. Locate the consolidated packfile
            new_packs = list(local_pack_dir.glob("pack-*.pack"))
            if not new_packs:
                # Repacking produced nothing, so the existing packfiles cannot
                # be proven superseded.  Deleting them at step 7 would destroy
                # history, so stop here and leave the remote untouched.
                raise RuntimeError(
                    "refusing to compact: repacking produced no packfile, so the existing "
                    "packfiles cannot be proven superseded. Nothing was deleted."
                )

            if verbose:
                sys.stderr.write("  Uploading consolidated packfile(s) to Seafile...\n")

            new_pack_names = set()
            for np in new_packs:
                new_pack_names.add(np.name)
                n_idx = np.with_suffix(".idx")
                client.upload_file(
                    repo_id, pack_dir, np.name, np, replace=True, progress_callback=on_upload_progress
                )
                if n_idx.is_file():
                    new_pack_names.add(n_idx.name)
                    client.upload_file(
                        repo_id, pack_dir, n_idx.name, n_idx, replace=True, progress_callback=on_upload_progress
                    )

            # 7. Delete obsolete old packs from Seafile
            if verbose:
                sys.stderr.write("  Cleaning up obsolete remote packfiles...\n")

            # Fencing check (N-4): verify lock ownership before deleting any remote packs
            lock_valid = False
            if hasattr(lock, "verify_ownership"):
                lock_valid = lock.verify_ownership()
            elif hasattr(lock, "renew"):
                lock_valid = lock.renew()
            else:
                lock_valid = True

            if not lock_valid:
                raise RuntimeError(
                    "refusing to delete obsolete packfiles: lock lease lapsed or was lost. "
                    "Compacted packfiles were uploaded, but old packs were left untouched to prevent data corruption."
                )

            deleted_count = 0
            for old_p in downloaded_packs:
                maybe_renew()
                if old_p not in new_pack_names:
                    old_idx = old_p.removesuffix(".pack") + ".idx"
                    client.delete_entry(repo_id, f"{pack_dir}/{old_p}")
                    client.delete_entry(repo_id, f"{pack_dir}/{old_idx}")
                    deleted_count += 1

            new_total_bytes = sum(np.stat().st_size for np in new_packs)
            # Signed on purpose: compaction can grow a repository, and the
            # callers need to be able to tell.  `describe_size_delta` is what
            # turns it into a sentence.
            saved_kb = (total_old_bytes - new_total_bytes) // 1024

            if verbose:
                sys.stderr.write(
                    f"Compaction complete: consolidated {len(old_packs)} packfiles into {len(new_packs)} "
                    f"({describe_size_delta(saved_kb)}).\n"
                )

            return {
                "status": "ok",
                "old_packs": len(old_packs),
                "new_packs": len(new_packs),
                "saved_kb": saved_kb,
            }
