"""gc.py - Remote packfile compaction and garbage collection for git-remote-seafile."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import SeafileClient
from .lock import RemoteLock
from .refs import REF_NAMESPACES, iter_refs


def compact_repository(
    client: SeafileClient,
    repo_id: str,
    repo_path: str,
    min_packs: int = 2,
    verbose: bool = True,
) -> dict[str, Any]:
    """Consolidate multiple small packfiles on Seafile into a single optimized packfile."""
    clean_repo = repo_path.rstrip("/")
    pack_dir = f"{clean_repo}/objects/pack"

    with RemoteLock(client, repo_id, clean_repo):
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

            # 2. Initialize temporary bare Git repository
            subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True)

            local_pack_dir = bare_repo / "objects" / "pack"
            local_pack_dir.mkdir(parents=True, exist_ok=True)

            # 3. Download all packfiles and indices
            total_old_bytes = 0
            for pack_name in old_packs:
                if verbose:
                    sys.stderr.write(f"  Downloading {pack_name}...\n")
                idx_name = pack_name.removesuffix(".pack") + ".idx"

                pack_bytes = client.get_file_bytes(repo_id, f"{pack_dir}/{pack_name}")
                idx_bytes = client.get_file_bytes(repo_id, f"{pack_dir}/{idx_name}")

                if not pack_bytes:
                    continue
                total_old_bytes += len(pack_bytes)

                (local_pack_dir / pack_name).write_bytes(pack_bytes)
                if idx_bytes:
                    (local_pack_dir / idx_name).write_bytes(idx_bytes)
                else:
                    subprocess.run(
                        ["git", "index-pack", "-o", str(local_pack_dir / idx_name), str(local_pack_dir / pack_name)],
                        check=True,
                        capture_output=True,
                    )

            # 4. Mirror remote refs so git repack knows all roots are reachable.
            # This walk must be recursive: a nested ref that is not mirrored
            # here makes its objects look unreachable, and "repack -a -d" would
            # then delete them from the consolidated packfile -- silently and
            # irreversibly.
            for namespace in REF_NAMESPACES:
                for ref_name, sha in iter_refs(client, repo_id, clean_repo, namespace):
                    ref_file = bare_repo / ref_name
                    ref_file.parent.mkdir(parents=True, exist_ok=True)
                    ref_file.write_text(f"{sha}\n", encoding="utf-8")

            # 5. Run git repack -a -d -l
            if verbose:
                sys.stderr.write("  Repacking objects with delta compression...\n")
            res = subprocess.run(
                ["git", "-C", str(bare_repo), "repack", "-a", "-d", "-l"],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"git repack failed: {res.stderr}")

            # 6. Locate the single consolidated packfile
            new_packs = list(local_pack_dir.glob("pack-*.pack"))
            if len(new_packs) != 1:
                # If there are still multiple packs (e.g. unreachable objects), keep them all
                pass

            if verbose:
                sys.stderr.write("  Uploading consolidated packfile(s) to Seafile...\n")

            new_pack_names = set()
            for np in new_packs:
                new_pack_names.add(np.name)
                n_idx = np.with_suffix(".idx")
                client.upload_file(repo_id, pack_dir, np.name, np.read_bytes(), replace=True)
                if n_idx.is_file():
                    new_pack_names.add(n_idx.name)
                    client.upload_file(repo_id, pack_dir, n_idx.name, n_idx.read_bytes(), replace=True)

            # 7. Delete obsolete old packs from Seafile
            if verbose:
                sys.stderr.write("  Cleaning up obsolete remote packfiles...\n")
            deleted_count = 0
            for old_p in old_packs:
                if old_p not in new_pack_names:
                    old_idx = old_p.removesuffix(".pack") + ".idx"
                    client.delete_entry(repo_id, f"{pack_dir}/{old_p}")
                    client.delete_entry(repo_id, f"{pack_dir}/{old_idx}")
                    deleted_count += 1

            new_total_bytes = sum(np.stat().st_size for np in new_packs)
            saved_kb = (total_old_bytes - new_total_bytes) // 1024

            if verbose:
                sys.stderr.write(
                    f"Compaction complete: consolidated {len(old_packs)} packfiles into {len(new_packs)} "
                    f"(saved ~{saved_kb} KB).\n"
                )

            return {
                "status": "ok",
                "old_packs": len(old_packs),
                "new_packs": len(new_packs),
                "saved_kb": saved_kb,
            }
