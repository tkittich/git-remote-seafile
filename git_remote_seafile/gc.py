"""gc.py - Remote packfile compaction and garbage collection for git-remote-seafile."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import SeafileAPIError, SeafileClient
from .config import RemoteConfig
from .git_util import GIT_TIMEOUT, clean_git_env
from .lock import RemoteLock, describe_ownership_failure
from .packs import fetch_pack_artifact, is_valid_pack_name
from .refs import REF_NAMESPACES, is_valid_ref_name, iter_refs

#: Ref prefixes the mirror in :func:`compact_repository` carries into the
#: scratch repository -- the only namespaces whose objects ``repack -a -d`` is
#: told are reachable.
_MIRRORED_PREFIXES = tuple(f"{ns}/" for ns in REF_NAMESPACES)


def _run_git(
    args: list[str], env: dict[str, str], **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """Run git against the scratch repository, bounded by ``GIT_TIMEOUT``.

    Compaction holds the remote lease for the whole operation, so a git child
    that never returns is not merely a stuck command -- it is a stuck *lease*,
    and the queue will lawfully hand the lock to another client while this
    process still believes it owns it.  A timeout is therefore converted into a
    RuntimeError so the caller's ordinary failure path (which releases the
    lease) runs, rather than the process blocking until someone kills it.
    """
    try:
        return subprocess.run(["git", *args], env=env, timeout=GIT_TIMEOUT, **kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"git {' '.join(args)} did not finish within {GIT_TIMEOUT:g}s; aborting compaction."
        ) from None


def refs_outside_namespaces(client: SeafileClient, repo_id: str, repo_path: str) -> list[str]:
    """Ref files under ``<repo_path>/refs`` the mirror would not carry.

    The remote is a plain file store, so refs outside ``refs/heads`` and
    ``refs/tags`` can exist -- hand-created, or written by another tool.  The
    mirror walks only the two known namespaces, so such a ref's objects look
    unreachable in the scratch repository and step 7 would delete them
    permanently while the ref file itself survived, broken.  Returns one
    human-readable entry per problem (off-namespace refs, and files whose
    names git could not treat as refs); empty means "safe to mirror".
    """
    root = f"{repo_path.rstrip('/')}/refs"
    problems: list[str] = []
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        dir_path, prefix = stack.pop()
        for entry in client.list_dir(repo_id, dir_path):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or ""
            if not name:
                continue
            full = f"refs/{prefix}{name}"
            if entry.get("type") == "dir":
                stack.append((f"{dir_path}/{name}", f"{prefix}{name}/"))
            elif entry.get("type") == "file":
                if not full.startswith(_MIRRORED_PREFIXES):
                    problems.append(f"{full} (outside refs/heads and refs/tags)")
                elif not is_valid_ref_name(full):
                    problems.append(f"{full} (not a valid git ref name)")
            else:
                problems.append(f"{full} (entry of unknown type '{entry.get('type')}')")
    return problems


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

        def lock_still_ours() -> bool:
            """Whether we can still prove we hold the lock.

            gc prefers verify_ownership() and falls back to renew() for a lock
            object supplied by a caller that predates ownership fencing.  A lock
            that offers no way to ask at all is treated as "do not block": the
            fence exists to stop a *known* loss of ownership, and a caller that
            never supplied a fenced lock is not evidence of one.
            """
            if hasattr(lock, "verify_ownership"):
                return bool(lock.verify_ownership())
            if hasattr(lock, "renew"):
                return bool(lock.renew())
            return True

        def ownership_reason() -> str:
            state = lock.ownership_state() if hasattr(lock, "ownership_state") else ""
            return describe_ownership_failure(state)

        def on_upload_progress(transferred: int, total: int) -> None:
            maybe_renew()

        def on_download_progress(transferred: int, total: int) -> None:
            # A multi-gigabyte pack download is the longest phase gc holds the
            # lock across; without this callback the lease can lapse mid-
            # download and another client may lawfully take over.
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

        # Refuse before anything is downloaded: the mirror below carries only
        # refs/heads and refs/tags, so a ref outside those namespaces -- or
        # one whose name git could not honour -- would make its objects look
        # unreachable and step 7 would delete them for good.  The remote is a
        # plain file store; nothing stops such refs from existing.
        stray = refs_outside_namespaces(client, repo_id, clean_repo)
        if stray:
            return {
                "status": "error",
                "message": (
                    f"Refusing to compact {clean_repo}: refs outside the mirrored "
                    "namespaces exist, and repacking would delete their objects:\n  "
                    + "\n  ".join(sorted(stray))
                    + "\nMove them into refs/heads or refs/tags, or delete them "
                    "from the remote first."
                ),
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

            # The scratch repository's object format must match the remote's.
            # A SHA-256 remote's packs carry 64-hex object names, and a default
            # (SHA-1) scratch repo cannot read them: index-pack rejects the pack
            # ("pack is corrupted (SHA1 mismatch)") and repack then dies with
            # "bad object refs/heads/main" on the mirrored 64-hex refs.  The
            # pack *names* are the only format signal available before any pack
            # is read -- a SHA-256 pack is named pack-<64 hex>.pack.
            obj_format = "sha1"
            for p_name in old_packs:
                hex_part = p_name.removeprefix("pack-").removesuffix(".pack")
                if len(hex_part) == 64:
                    obj_format = "sha256"
                    break
            init_cmd = ["git", "init", "--bare"]
            if obj_format == "sha256":
                init_cmd.append("--object-format=sha256")
            init_cmd.append(str(bare_repo))
            _run_git(init_cmd[1:], scratch_env, check=True, capture_output=True)

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
                try:
                    reason = fetch_pack_artifact(
                        client, repo_id, f"{pack_dir}/{pack_name}", pack_file,
                        expected_size=expected_size, progress_callback=on_download_progress,
                    )
                except SeafileAPIError as exc:
                    # The in-memory-fallback ceiling: convert to the same
                    # fail-closed error dict as every other download failure.
                    return {"status": "error", "message": str(exc)}
                if reason == "truncated":
                    return {
                        "status": "error",
                        "message": f"Packfile {pack_name} downloaded size ({pack_file.stat().st_size}) does not match expected size ({expected_size}); aborting compaction.",
                    }
                if reason is not None:
                    return {
                        "status": "error",
                        "message": f"Failed to download {pack_name} during compaction; aborting compaction to prevent data loss.",
                    }

                downloaded_packs.append(pack_name)
                total_old_bytes += pack_file.stat().st_size

                # A missing or truncated index is regenerable locally; only a
                # size mismatch is worth telling the user about.
                try:
                    idx_reason = fetch_pack_artifact(
                        client, repo_id, f"{pack_dir}/{idx_name}", idx_file,
                        expected_size=pack_sizes.get(idx_name), progress_callback=on_download_progress,
                    )
                except SeafileAPIError as exc:
                    return {"status": "error", "message": str(exc)}
                if idx_reason == "truncated":
                    sys.stderr.write(f"Warning: downloaded {idx_name} size mismatch; will regenerate locally.\n")
                    sys.stderr.flush()
                    try:
                        idx_file.unlink(missing_ok=True)
                    except Exception:
                        pass

                # verify-pack/index-pack run *inside* the scratch repo (-C) so
                # they inherit its object format; the scrubbed env removes
                # GIT_DIR, and without a repo context they default to SHA-1 and
                # reject a SHA-256 pack.  -C is safe with the absolute paths used
                # here.
                verified = False
                if idx_file.is_file():
                    res = _run_git(
                        ["-C", str(bare_repo), "verify-pack", "-v", str(idx_file)],
                        scratch_env,
                        capture_output=True,
                    )
                    verified = (res.returncode == 0)

                if not verified:
                    try:
                        idx_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    _run_git(
                        ["-C", str(bare_repo), "index-pack", "-o", str(idx_file), str(pack_file)],
                        scratch_env,
                        check=True,
                        capture_output=True,
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
            res = _run_git(
                ["-C", str(bare_repo), "repack", "-a", "-d", "-l"],
                scratch_env,
                capture_output=True,
                text=True,
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

            # Fencing check: verify lock ownership before touching anything
            # else.  It comes before the upload verification below so that a
            # lost lease is reported as a lost lease -- "we are not allowed to
            # finish this" is a different problem from "the bytes are not
            # there", and they send the user to different places.
            if not lock_still_ours():
                raise RuntimeError(
                    f"refusing to delete obsolete packfiles: {ownership_reason()}. "
                    "Compacted packfiles were uploaded, but old packs were left untouched to prevent data corruption."
                )

            # 6b. Prove the consolidated packfiles are actually there, and
            # whole, before a single old pack is deleted.  This is the step that
            # deserves the most paranoia in the whole tool: what gc deletes are
            # the packfiles the repository is *currently* served from, and an
            # upload that returned True is not evidence that the bytes are on
            # the server -- a truncated write, a quota error that a proxy
            # answered 2xx, or an .idx that never landed all look identical from
            # here.  Comparing the remote listing's sizes against the local
            # files is the cheapest check that catches all three, and it fails
            # closed: an upload that cannot be verified leaves the old packs
            # alone, so the repository keeps the copy it already had.
            expected_sizes: dict[str, int] = {}
            for np in new_packs:
                expected_sizes[np.name] = np.stat().st_size
                n_idx = np.with_suffix(".idx")
                if n_idx.is_file():
                    expected_sizes[n_idx.name] = n_idx.stat().st_size

            try:
                verified_listing = client.list_dir(repo_id, pack_dir)
            except Exception as ex:
                raise RuntimeError(
                    "refusing to delete obsolete packfiles: could not list the remote pack "
                    f"directory to verify the upload ({ex}). Nothing was deleted."
                ) from ex
            remote_sizes: dict[Any, Any] = {
                e.get("name"): e.get("size")
                for e in verified_listing
                if isinstance(e, dict) and e.get("name")
            }
            unverified: list[str] = []
            # Sorted by str() because the keys are normally names but a caller
            # may hand us path-like objects; the order is only for a stable
            # message.
            for name, expected in sorted(expected_sizes.items(), key=lambda kv: str(kv[0])):
                try:
                    raw_size = remote_sizes.get(name)
                    if raw_size is None:
                        # Not listed at all -- distinct from a listed-but-junk
                        # size, and the same verdict either way.
                        raise TypeError("not listed on the remote")
                    got = int(raw_size)
                except (TypeError, ValueError):
                    unverified.append(f"{name} is not listed on the remote with a readable size")
                    continue
                if got != expected:
                    unverified.append(f"{name} is {got} bytes on the remote but {expected} locally")
            if unverified:
                raise RuntimeError(
                    "refusing to delete obsolete packfiles: the consolidated packfiles could not "
                    "be verified on the remote (" + "; ".join(unverified) + "). The old packfiles "
                    "were left untouched -- until the new ones are proven present, they are still "
                    "the repository's only complete copy."
                )

            # 7. Delete obsolete old packs from Seafile
            if verbose:
                sys.stderr.write("  Cleaning up obsolete remote packfiles...\n")

            deleted_count = 0
            failed_deletions: list[str] = []
            cleanup_stopped: str = ""
            for old_p in downloaded_packs:
                if old_p in new_pack_names:
                    continue
                # Fence *every* deletion, not just the first.  maybe_renew()
                # cannot stand in here: its False also means "the renewal
                # interval has not elapsed", which is what most of its calls
                # return, so a lease lost mid-loop would read as an ordinary
                # no-op and the loop would keep deleting packfiles inside
                # another client's push.  verify_ownership() answers exactly one
                # question, and it also renews, so the lease cannot lapse while
                # the loop runs.
                if not lock_still_ours():
                    cleanup_stopped = (
                        f"stopped deleting obsolete packfiles: {ownership_reason()}. "
                        "The consolidated packfiles are verified and in place, so nothing was lost; "
                        "the leftovers are unreferenced and a later gc will remove them."
                    )
                    break
                old_idx = old_p.removesuffix(".pack") + ".idx"
                if client.delete_entry(repo_id, f"{pack_dir}/{old_p}"):
                    deleted_count += 1
                else:
                    failed_deletions.append(old_p)
                if not client.delete_entry(repo_id, f"{pack_dir}/{old_idx}"):
                    # An orphaned .idx is invisible to fetch (which keys on
                    # .pack names) but is clutter; name it so a later gc or
                    # a manual cleanup can remove it.
                    failed_deletions.append(old_idx)

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
                if failed_deletions:
                    sys.stderr.write(
                        "Warning: could not delete obsolete remote files: "
                        + ", ".join(failed_deletions)
                        + ". They are unreferenced and can be removed by a later gc or by hand.\n"
                    )
                sys.stderr.flush()

            if cleanup_stopped:
                # Outside the verbose block deliberately: a compaction that
                # stopped short of finishing its cleanup is not a detail worth
                # mentioning only when the user asked for detail.
                sys.stderr.write(f"Warning: {cleanup_stopped}\n")
                sys.stderr.flush()

            return {
                "status": "ok",
                "old_packs": len(old_packs),
                "new_packs": len(new_packs),
                "saved_kb": saved_kb,
                "deleted_packs": deleted_count,
                "deletion_failures": failed_deletions,
                "cleanup_stopped": cleanup_stopped,
            }
