"""gc.py - Remote packfile compaction and garbage collection for git-remote-seafile."""

from __future__ import annotations

import hashlib
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import SeafileAPIError, SeafileClient
from .config import RemoteConfig
from .git_util import PACK_BIG_FILE_THRESHOLD, clean_git_env, run_git_checked
from .lock import RemoteLock, describe_ownership_failure
from .packs import fetch_pack_artifact, is_valid_pack_name
from .refs import REF_NAMESPACES, is_conflict_copy, is_valid_ref_name, iter_refs
from .units import format_bytes

#: Ref prefixes the mirror in :func:`compact_repository` carries into the
#: scratch repository -- the only namespaces whose objects ``repack -a -d`` is
#: told are reachable.
_MIRRORED_PREFIXES = tuple(f"{ns}/" for ns in REF_NAMESPACES)


#: The object-name hash of a pack, taken from the *end* of its filename.  A name
#: is ``pack-<hash>.pack`` for packs written by git and ``pack-tail-<hash>.pack``
#: for the ones tail merge writes; the hash is the only part whose length says
#: which object format the pack is in.
_PACK_HASH_RE = re.compile(r"([0-9a-f]{64}|[0-9a-f]{40})\.pack$")


def detect_object_format(pack_names: list[str]) -> str:
    """``"sha256"`` or ``"sha1"``, from the names of a remote's packfiles.

    The pack *names* are the only format signal available before any pack is
    downloaded: a SHA-256 pack ends in a 64-hex hash, a SHA-1 pack in a 40-hex
    one.  Measured from the end of the name rather than by stripping a fixed
    ``pack-`` prefix, because tail merge writes ``pack-tail-<hash>.pack`` -- and
    a detector that counted what was left after the prefix saw ``tail-<64 hex>``
    (69 characters), answered SHA-1, and made every manual ``gc`` of a SHA-256
    remote fail once tail merge had been its only writer.
    """
    for name in pack_names:
        m = _PACK_HASH_RE.search(name)
        if m and len(m.group(1)) == 64:
            return "sha256"
    return "sha1"


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
                elif is_conflict_copy(name):
                    problems.append(
                        f"{full} (a Seafile conflict copy -- see RUNBOOK.md, \"Conflict Artifacts\")"
                    )
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
    but printing it raw produced ``(saved -12.0 KiB)`` -- which reads like a bug,
    and calls a growth a saving.  So the negative case says what happened.
    """
    if saved_kb < 0:
        return (
            f"grew by ~{format_bytes(abs(saved_kb) * 1024)} "
            "(repacking can do this when the existing packs were already tight)"
        )
    return f"saved ~{format_bytes(saved_kb * 1024)}"


def _fetch_and_verify_pack_pair(
    client: SeafileClient,
    repo_id: str,
    pack_dir: str,
    pack_name: str,
    expected_size: Any,
    idx_expected_size: Any,
    local_pack_dir: Path,
    bare_repo: Path,
    scratch_env: dict[str, str],
    on_download_progress,
    op: str = "tail merge",
) -> str | None:
    """Download one remote .pack/.idx pair into the scratch object store.

    Returns ``None`` when both files are usable -- a missing or corrupt index
    is regenerated locally with ``index-pack`` and counts as usable, because it
    is derived from the pack bytes that were verified by size -- or a
    human-readable reason this pack cannot be trusted (truncated download,
    unreadable API response).  The pair lands in ``local_pack_dir`` under its
    remote name, so the scratch repository reads it like any installed pack.

    verify-pack and index-pack run *inside* the scratch repo (-C) so they
    inherit its object format; a SHA-256 pack would be rejected by a default
    (SHA-1) git with no repo context.
    """
    idx_name = pack_name.removesuffix(".pack") + ".idx"
    pack_file = local_pack_dir / pack_name
    idx_file = local_pack_dir / idx_name

    try:
        reason = fetch_pack_artifact(
            client, repo_id, f"{pack_dir}/{pack_name}", pack_file,
            expected_size=expected_size, progress_callback=on_download_progress,
        )
    except SeafileAPIError as exc:
        return str(exc)
    if reason == "truncated":
        return (
            f"Packfile {pack_name} downloaded size ({pack_file.stat().st_size}) does not "
            f"match expected size ({expected_size}); aborting {op}."
        )
    if reason is not None:
        return f"Failed to download {pack_name} during {op}; aborting {op} to prevent data loss."

    # A missing or truncated index is regenerable locally; only an unrepairable
    # packfile itself is worth failing over.
    try:
        idx_reason = fetch_pack_artifact(
            client, repo_id, f"{pack_dir}/{idx_name}", idx_file,
            expected_size=idx_expected_size, progress_callback=on_download_progress,
        )
    except SeafileAPIError as exc:
        return str(exc)
    if idx_reason == "truncated":
        sys.stderr.write(f"Warning: downloaded {idx_name} size mismatch; will regenerate locally.\n")
        sys.stderr.flush()
        try:
            idx_file.unlink(missing_ok=True)
        except Exception:
            pass

    verified = False
    if idx_file.is_file():
        res = run_git_checked(
            ["-C", str(bare_repo), "verify-pack", str(idx_file)],
            scratch_env,
            capture_output=True,
        )
        verified = (res.returncode == 0)

    if not verified:
        try:
            idx_file.unlink(missing_ok=True)
        except Exception:
            pass
        run_git_checked(
            ["-C", str(bare_repo), "index-pack", "-o", str(idx_file), str(pack_file)],
            scratch_env,
            check=True,
            capture_output=True,
        )
    return None


def _init_scratch(tmp: Path, pack_names: list[str], scratch_env: dict[str, str]) -> tuple[Path, Path]:
    """Create the scratch bare repository the merge runs in.

    Returns ``(bare_repo, local_pack_dir)``.  Every git command that follows
    targets this repo, not the caller's, so it must run with the scrubbed
    *scratch_env*: when a helper is launched by git, GIT_DIR points at the
    caller's repository and overrides ``-C``.

    The object format must match the remote's.  A SHA-256 remote's packs carry
    64-hex object names, and a default (SHA-1) scratch repo cannot read them:
    index-pack rejects the pack ("pack is corrupted (SHA1 mismatch)") and repack
    then dies with "bad object refs/heads/main".  The pack *names* are the only
    format signal available before any pack is read.
    """
    bare_repo = tmp / "bare.git"
    init_cmd = ["init", "--bare"]
    if detect_object_format(pack_names) == "sha256":
        init_cmd.append("--object-format=sha256")
    run_git_checked(init_cmd + [str(bare_repo)], scratch_env, check=True, capture_output=True)
    local_pack_dir = bare_repo / "objects" / "pack"
    local_pack_dir.mkdir(parents=True, exist_ok=True)
    return bare_repo, local_pack_dir


def _upload_new_packs(
    client: SeafileClient,
    repo_id: str,
    pack_dir: str,
    new_packs: list[Path],
    on_upload_progress,
) -> set[str]:
    """Upload each new pack and its index; return the remote names written."""
    uploaded: set[str] = set()
    for np in new_packs:
        uploaded.add(np.name)
        client.upload_file(
            repo_id, pack_dir, np.name, np, replace=True, progress_callback=on_upload_progress
        )
        n_idx = np.with_suffix(".idx")
        if n_idx.is_file():
            uploaded.add(n_idx.name)
            client.upload_file(
                repo_id, pack_dir, n_idx.name, n_idx, replace=True, progress_callback=on_upload_progress
            )
    return uploaded


def _verify_upload(
    client: SeafileClient,
    repo_id: str,
    pack_dir: str,
    new_packs: list[Path],
    *,
    old_adj: str,
    new_desc: str,
    plural: bool,
) -> None:
    """Prove the new packfiles are on the remote, whole, or raise RuntimeError.

    This is the step that deserves the most paranoia in the whole tool: what
    gc deletes are the packfiles the repository is *currently* served from, and
    an upload that returned True is not evidence that the bytes are on the
    server -- a truncated write, a quota error that a proxy answered 2xx, or an
    .idx that never landed all look identical from here.  Comparing the remote
    listing's sizes against the local files is the cheapest check that catches
    all three, and it fails closed: what cannot be verified leaves the old packs
    alone, so the repository keeps the copy it already had.
    """
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
            f"refusing to delete {old_adj} packfiles: could not list the remote pack "
            f"directory to verify the upload ({ex}). Nothing was deleted."
        ) from ex
    remote_sizes: dict[Any, Any] = {
        e.get("name"): e.get("size")
        for e in verified_listing
        if isinstance(e, dict) and e.get("name")
    }
    unverified: list[str] = []
    # Sorted by str() because the keys are normally names but a caller may hand
    # us path-like objects; the order is only for a stable message.
    for name, expected in sorted(expected_sizes.items(), key=lambda kv: str(kv[0])):
        try:
            raw_size = remote_sizes.get(name)
            if raw_size is None:
                # Not listed at all -- distinct from a listed-but-junk size,
                # and the same verdict either way.
                raise TypeError("not listed on the remote")
            got = int(raw_size)
        except (TypeError, ValueError):
            unverified.append(f"{name} is not listed on the remote with a readable size")
            continue
        if got != expected:
            unverified.append(f"{name} is {got} bytes on the remote but {expected} locally")
    if unverified:
        raise RuntimeError(
            f"refusing to delete {old_adj} packfiles: the {new_desc} could not "
            "be verified on the remote (" + "; ".join(unverified) + "). The old packfiles "
            f"were left untouched -- until the new {'ones are' if plural else 'one is'} proven "
            "present, they are still the repository's only complete copy."
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_content(
    client: SeafileClient,
    repo_id: str,
    pack_dir: str,
    new_packs: list[Path],
    scratch: Path,
    *,
    old_adj: str,
    new_desc: str,
) -> None:
    """Read the new packfiles back and compare them with what was uploaded.

    The listing proves a *size*, and the server reports no hash.  A same-length
    substitution -- a padded partial write, a damaged block, a hostile operator
    -- passes it, and the deletions that follow would remove the only intact
    copy.  Downloading the new pack once and comparing digests turns the proof
    into a content proof, at the cost of one more transfer of what was just
    uploaded (about 0.015 s per MiB at the measured 64 MiB/s), under the lock
    already held.  Anything that cannot be read back leaves the old packs alone.
    """
    for np in new_packs:
        if not isinstance(np, Path) or not np.is_file():
            continue
        for local in (np, np.with_suffix(".idx")):
            if not local.is_file():
                continue
            probe = scratch / f"verify-{local.name}"
            try:
                present = client.download_file_to(repo_id, f"{pack_dir}/{local.name}", probe)
                same = (
                    present is True
                    and probe.stat().st_size == local.stat().st_size
                    and _file_sha256(probe) == _file_sha256(local)
                )
            except Exception as ex:
                raise RuntimeError(
                    f"refusing to delete {old_adj} packfiles: could not read the {new_desc} "
                    f"back to check its content ({ex}). Nothing was deleted."
                ) from ex
            finally:
                probe.unlink(missing_ok=True)
            if not same:
                raise RuntimeError(
                    f"refusing to delete {old_adj} packfiles: {local.name} on the remote has "
                    f"the right size but different content from what was uploaded. The old "
                    f"packfiles were left untouched; delete the damaged {local.name} by hand "
                    "and run gc again."
                )


def _delete_superseded(
    client: SeafileClient,
    repo_id: str,
    pack_dir: str,
    candidates: list[str],
    keep: set[str],
    lock_still_ours,
    ownership_reason,
    *,
    old_adj: str,
    new_desc: str,
    plural: bool,
) -> tuple[int, list[str], str]:
    """Delete *candidates* (minus *keep*) with the ownership fence on each one.

    Returns ``(deleted_count, failed_deletions, cleanup_stopped)``;
    *cleanup_stopped* is a sentence when the lease was lost mid-loop, else "".
    """
    deleted_count = 0
    failed_deletions: list[str] = []
    cleanup_stopped = ""
    for old_p in candidates:
        if old_p in keep:
            continue
        # Fence *every* deletion, not just the first.  maybe_renew() cannot
        # stand in here: its False also means "the renewal interval has not
        # elapsed", which is what most of its calls return, so a lease lost
        # mid-loop would read as an ordinary no-op and the loop would keep
        # deleting packfiles inside another client's push.  verify_ownership()
        # answers exactly one question, and it also renews, so the lease cannot
        # lapse while the loop runs.
        if not lock_still_ours():
            cleanup_stopped = (
                f"stopped deleting {old_adj} packfiles: {ownership_reason()}. "
                f"The {new_desc} {'are' if plural else 'is'} verified and in place, so nothing "
                "was lost; the leftovers are unreferenced and a later gc will remove them."
            )
            break
        old_idx = old_p.removesuffix(".pack") + ".idx"
        if client.delete_entry(repo_id, f"{pack_dir}/{old_p}"):
            deleted_count += 1
        else:
            failed_deletions.append(old_p)
        if not client.delete_entry(repo_id, f"{pack_dir}/{old_idx}"):
            # An orphaned .idx is invisible to fetch (which keys on .pack
            # names) but is clutter; name it so a later gc or a manual cleanup
            # can remove it.
            failed_deletions.append(old_idx)
    return deleted_count, failed_deletions, cleanup_stopped


def tail_merge(
    client: SeafileClient,
    repo_id: str,
    repo_path: str,
    config: RemoteConfig | None = None,
    verbose: bool = True,
    respect_budget: bool = True,
) -> dict[str, Any]:
    """Merge the remote's small ("movable") packfiles.

    The automatic tier of compaction (DESIGN.md §4.5).  Unlike
    :func:`compact_repository` it never moves the big packs: a pack at or above
    ``config.gc_max_pack_mib`` is frozen where it stands -- size alone decides,
    so no state records what was last touched -- and only the smaller ones are
    downloaded, unioned, re-uploaded and removed.  The merged pack is a pure bag
    union of every object id stored in the movable packs, with **no
    reachability semantics**: nothing ever decides an object's fate by what
    references it, so objects whose refs were deleted cannot be filtered out,
    and delta bases living in frozen packs are duplicated rather than referenced
    away.  File count stays bounded at *frozen packs + a small constant*; cost
    per cycle is 2×tail bytes, not 2×store.

    ``respect_budget=False`` is for a run the user asked for by hand
    (``gc --tail``): the download budget exists so an *automatic* merge cannot
    hold the push lock for minutes, and a person who started one has already
    accepted the time.

    Returns a dict whose ``status`` is one of:

    ``"ok"``
        Merged, and every superseded packfile removed.
    ``"incomplete"``
        Merged and verified, but ownership of the remote lock was lost while
        deleting; the deletions after that point were skipped.  Nothing is lost
        -- the new packs are verified and in place, and the leftovers are
        unreferenced clutter a later run removes.
    ``"skipped"``
        Nothing to do: fewer than ``config.gc_threshold`` movable packfiles --
        or their total exceeds the download budget (``config.gc_max_mib``, 2 x ``gc_max_pack_mib``), when
        auto-GC defers to a manual off-hours run instead of round-tripping that
        much mid-push.  The message says which case applied.
    ``"error"``
        Aborted before any deletion: a pack failed to download or verify, an
        index could not be rebuilt, repacking produced nothing, or the upload
        could not be proven present on the remote.
    """
    cfg = config if config is not None else RemoteConfig.load()
    clean_repo = repo_path.rstrip("/")
    pack_dir = f"{clean_repo}/objects/pack"

    with RemoteLock(client, repo_id, clean_repo, config=cfg) as lock:
        def maybe_renew() -> None:
            lock.maybe_renew(20.0)

        def lock_still_ours() -> bool:
            return bool(lock.verify_ownership())

        def ownership_reason() -> str:
            return describe_ownership_failure(lock.ownership_state())

        def on_upload_progress(transferred: int, total: int) -> None:
            maybe_renew()

        def on_download_progress(transferred: int, total: int) -> None:
            # A multi-gigabyte tail download is the longest phase this runs hold
            # the lock across; without this callback the lease can lapse mid-
            # download and another client may lawfully take over.
            maybe_renew()

        # 1. Discover remote packfiles and split them into movable and frozen.
        freeze_at = cfg.freeze_bytes
        entries = client.list_dir(repo_id, pack_dir)
        pack_sizes: dict[Any, Any] = {
            e.get("name"): e.get("size")
            for e in entries if isinstance(e, dict) and e.get("name")
        }
        # (name, size-from-listing); the size may be None when the listing
        # omitted it.
        movable: list[tuple[str, Any]] = []
        frozen_count = 0
        for e in entries:
            name = e.get("name") if isinstance(e, dict) else None
            if not name or not name.endswith(".pack"):
                continue
            if not is_valid_pack_name(name):
                return {
                    "status": "error",
                    "message": f"Invalid packfile name '{name}' on remote; aborting tail merge to prevent data loss.",
                }
            # A listing that omits the size says nothing; treat it as small and
            # let the download's own size check be the gate for that pack.
            size = int(pack_sizes.get(name) or 0)
            if size < freeze_at:
                movable.append((name, pack_sizes.get(name)))
            else:
                frozen_count += 1

        # The trigger is the *movable* count, not the total: frozen packs are
        # part of the bounded floor, so they must not re-arm the threshold.
        if len(movable) < cfg.gc_threshold:
            return {
                "status": "skipped",
                "movable_packs": len(movable),
                "frozen_packs": frozen_count,
                "message": (
                    f"Only {len(movable)} movable packfile(s); the tail-merge threshold is "
                    f"{cfg.gc_threshold}."
                ),
            }

        total_movable = sum(int(size or 0) for _, size in movable)
        budget = cfg.gc_max_mib * 1024 * 1024
        if respect_budget and total_movable > budget:
            return {
                "status": "skipped",
                "movable_packs": len(movable),
                "frozen_packs": frozen_count,
                "total_bytes": total_movable,
                "message": (
                    f"Movable packs total {total_movable} bytes across {len(movable)} packfile(s); "
                    f"the automatic download budget is {budget}. Tail merge was deferred -- run "
                    "'git-remote-seafile gc' manually when the time cost suits."
                ),
            }

        if verbose:
            sys.stderr.write(
                f"Tailing remote repository ({len(movable)} movable packfiles, "
                f"{frozen_count} frozen)...\n"
            )

        with tempfile.TemporaryDirectory(prefix="git-seaf-tailmerge-") as td:
            tmp = Path(td)
            bare_repo = tmp / "bare.git"

            # 2. Scratch repository, scrubbed and object-format matched.
            scratch_env = clean_git_env()
            bare_repo, local_pack_dir = _init_scratch(
                tmp, [name for name, _ in movable], scratch_env
            )

            # 3. Download and verify exactly the movable packs.
            for pack_name, expected_size in movable:
                maybe_renew()
                if verbose:
                    sys.stderr.write(f"  Downloading {pack_name}...\n")
                idx_expected = pack_sizes.get(pack_name.removesuffix(".pack") + ".idx")
                err = _fetch_and_verify_pack_pair(
                    client, repo_id, pack_dir, pack_name, expected_size, idx_expected,
                    local_pack_dir, bare_repo, scratch_env, on_download_progress,
                )
                if err is not None:
                    return {"status": "error", "message": err}

            # 4. Enumerate every object id stored in the movable packs -- a pure
            # bag union with no reachability semantics, which is what makes this
            # safe to run while branches are being created and deleted.
            maybe_renew()
            res = run_git_checked(
                ["-C", str(bare_repo), "cat-file", "--batch-all-objects", "--batch-check"],
                scratch_env,
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"git cat-file failed while enumerating the tail: {res.stderr}")
            shas = sorted(
                line.split(" ", 1)[0]
                for line in res.stdout.splitlines()
                if line.strip()
            )
            if not shas:
                # The movable packs held no objects at all; there is nothing to
                # merge and nothing that may be deleted.
                return {
                    "status": "skipped",
                    "movable_packs": len(movable),
                    "frozen_packs": frozen_count,
                    "message": "The movable packfiles contain no objects; nothing to merge.",
                }

            # 5. Pack the union into one self-contained packfile.  Deltas are
            # computed only against objects in this set: a base that lives in a
            # frozen pack is duplicated, never referenced away, so the merged
            # pack stands on its own once the originals go.
            maybe_renew()
            if verbose:
                sys.stderr.write("  Merging movable packs...\n")
            # The base name becomes "<base>-<hash>.pack"; starting it with the
            # store's own "pack-" prefix keeps the result indistinguishable from
            # every other pack in /objects/pack.
            before = {p.name for p in local_pack_dir.glob("pack-*.pack")}
            res = run_git_checked(
                ["-C", str(bare_repo), "-c", f"core.bigFileThreshold={PACK_BIG_FILE_THRESHOLD}",
                 "pack-objects", "-q", f"--max-pack-size={cfg.max_pack_bytes}",
                 str(local_pack_dir / "pack-tail")],
                scratch_env,
                input="\n".join(shas) + "\n",
                text=True,
                capture_output=True,
            )
            maybe_renew()
            if res.returncode != 0:
                raise RuntimeError(f"git pack-objects failed while merging the tail: {res.stderr}")
            new_pack_names = sorted(
                {p.name for p in local_pack_dir.glob("pack-*.pack")} - before
            )
            if not new_pack_names:
                # Repacking produced nothing, so the existing packfiles cannot
                # be proven superseded.  Deleting them would destroy history,
                # so stop here and leave the remote untouched.
                raise RuntimeError(
                    "refusing to tail-merge: pack-objects produced no packfile, so the "
                    "movable packfiles cannot be proven superseded. Nothing was deleted."
                )

            if verbose:
                sys.stderr.write("  Uploading merged packfile to Seafile...\n")

            new_pack_paths = [local_pack_dir / n for n in new_pack_names]
            uploaded = _upload_new_packs(
                client, repo_id, pack_dir, new_pack_paths, on_upload_progress
            )

            # Fencing check: verify lock ownership before touching anything else.
            # It comes before the upload verification below so that a lost lease
            # is reported as a lost lease -- "we are not allowed to finish this"
            # is a different problem from "the bytes are not there".
            if not lock_still_ours():
                raise RuntimeError(
                    f"refusing to delete superseded packfiles: {ownership_reason()}. "
                    "The merged packfile was uploaded, but old packs were left untouched to prevent data corruption."
                )

            # 6. Prove the merged packfile is there, and whole, before a single
            # old pack is deleted.
            _verify_upload(
                client, repo_id, pack_dir, new_pack_paths,
                old_adj="superseded", new_desc="merged packfile", plural=False,
            )
            _verify_content(
                client, repo_id, pack_dir, new_pack_paths, Path(td),
                old_adj="superseded", new_desc="merged packfile",
            )

            # 7. Delete the superseded movable packs.  Frozen packs were never
            # downloaded and are never touched here.
            if verbose:
                sys.stderr.write("  Cleaning up superseded remote packfiles...\n")
            deleted_count, failed_deletions, cleanup_stopped = _delete_superseded(
                client, repo_id, pack_dir, [n for n, _ in movable], uploaded,
                lock_still_ours, ownership_reason,
                old_adj="superseded", new_desc="merged packfile", plural=False,
            )

            new_total_bytes = sum(np.stat().st_size for np in new_pack_paths)

            if verbose:
                sys.stderr.write(
                    f"Tail merge complete: consolidated {len(movable)} movable packfiles into "
                    f"{len(new_pack_names)} ({describe_size_delta((total_movable - new_total_bytes) // 1024)}); "
                    f"{frozen_count} frozen packfile(s) left in place.\n"
                )
                if failed_deletions:
                    sys.stderr.write(
                        "Warning: could not delete superseded remote files: "
                        + ", ".join(failed_deletions)
                        + ". They are unreferenced and can be removed by a later gc or by hand.\n"
                    )
                sys.stderr.flush()

            if cleanup_stopped:
                # Outside the verbose block deliberately: a merge that stopped
                # short of finishing its cleanup is not a detail worth mentioning
                # only when the user asked for detail.
                sys.stderr.write(f"Warning: {cleanup_stopped}\n")
                sys.stderr.flush()

            return {
                "status": "incomplete" if cleanup_stopped else "ok",
                "merged_packs": len(movable),
                "frozen_packs": frozen_count,
                "new_pack_bytes": new_total_bytes,
                "tail_bytes": total_movable,
                "deleted_packs": deleted_count,
                "deletion_failures": failed_deletions,
                "cleanup_stopped": cleanup_stopped,
            }


def _verify_ref_mirror(
    bare_repo: Path, expected: dict[str, str], env: dict[str, str], clean_repo: str
) -> None:
    """Refuse to repack unless git itself reads back exactly the refs we mirrored.

    The mirror is only a means: what ``repack -a -d`` keeps is whatever *git*
    sees as a root.  Asking git, rather than trusting what was written, is what
    catches a file that did not take (line endings, a name git will not honour)
    before a single object is pruned.
    """
    listed = run_git_checked(
        ["-C", str(bare_repo), "for-each-ref", "--format=%(objectname) %(refname)"],
        env, capture_output=True, text=True, check=True,
    ).stdout
    seen = {line for line in str(listed).splitlines() if line.strip()}
    want = {f"{sha} {name}" for name, sha in expected.items()}
    if seen != want:
        missing = sorted(want - seen)
        raise RuntimeError(
            f"refusing to compact {clean_repo}: git reads back {len(seen)} of "
            f"{len(want)} mirrored refs (first missing: {missing[0] if missing else '?'}). "
            "Nothing was deleted."
        )


def compact_repository(
    client: SeafileClient,
    repo_id: str,
    repo_path: str,
    min_packs: int = 2,
    verbose: bool = True,
    config: RemoteConfig | None = None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    """Consolidate multiple small packfiles on Seafile into a single optimized packfile.

    Returns a dict whose ``status`` is one of:

    ``"ok"``
        Consolidated, and every superseded packfile removed.
    ``"incomplete"``
        Consolidated and verified, but the cleanup stopped short: ownership of
        the remote lock was lost while deleting, so the deletions after that
        point were skipped.  Nothing is lost -- the new packs are verified and in
        place, and the leftovers are unreferenced clutter a later gc removes --
        but it is a distinct status, because "did everything asked" and "did
        most of it" are not the same answer to a scheduled job.
    ``"skipped"``
        Fewer than *min_packs* packfiles, so there was nothing worth doing --
        or the remote packs total more than *max_total_bytes*, when given: a
        compaction costs one full download and re-upload of that history, so
        above a cap it is deferred to a manual off-hours run instead of
        blocking whatever triggered it.  The message says which case applied.
    ``"error"``
        Aborted: a download failed, a verification failed, or a pack name was
        not valid.
    """
    clean_repo = repo_path.rstrip("/")
    pack_dir = f"{clean_repo}/objects/pack"

    cfg = config if config is not None else RemoteConfig.load()
    with RemoteLock(client, repo_id, clean_repo, config=config) as lock:
        def maybe_renew() -> None:
            lock.maybe_renew(20.0)

        def lock_still_ours() -> bool:
            """Whether we can still prove we hold the lock.

            ``verify_ownership()`` re-reads the ticket *and* scans the queue, so
            a lapsed lease and a preemption both answer False.  There is no
            fallback for a lock object that cannot be asked: this module always
            constructs a ``RemoteLock``, and a fence that passes when the check
            is missing is the opposite of fail-closed.
            """
            return bool(lock.verify_ownership())

        def ownership_reason() -> str:
            return describe_ownership_failure(lock.ownership_state())

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

        if max_total_bytes is not None:
            total = sum(int(e.get("size") or 0) for e in entries if e["name"].endswith(".pack"))
            if total > max_total_bytes:
                return {
                    "status": "skipped",
                    "count": len(old_packs),
                    "total_bytes": total,
                    "message": (
                        f"Remote packs total {total} bytes across {len(old_packs)} packfile(s); "
                        f"the automatic cap is {max_total_bytes}. Compaction of a history this size "
                        "downloads and re-uploads it in full, so it was deferred -- run 'git-remote-seafile gc' "
                        "manually when the time cost suits."
                    ),
                }

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

            # 2. Scratch repository, scrubbed and object-format matched.
            scratch_env = clean_git_env()
            bare_repo, local_pack_dir = _init_scratch(tmp, old_packs, scratch_env)

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
                err = _fetch_and_verify_pack_pair(
                    client, repo_id, pack_dir, pack_name, pack_sizes.get(pack_name),
                    pack_sizes.get(pack_name.removesuffix(".pack") + ".idx"),
                    local_pack_dir, bare_repo, scratch_env, on_download_progress,
                    op="compaction",
                )
                if err is not None:
                    return {"status": "error", "message": err}
                downloaded_packs.append(pack_name)
                total_old_bytes += (local_pack_dir / pack_name).stat().st_size

            # 4. Mirror remote refs so git repack knows all roots are reachable.
            # This walk must be recursive: a nested ref that is not mirrored
            # here makes its objects look unreachable, and "repack -a -d" would
            # then delete them from the consolidated packfile -- silently and
            # irreversibly.
            #
            # The mirror is ONE ``packed-refs`` file, not a loose file per ref.
            # A ref name is a path on the remote, but not a safe local one:
            # ``Feature`` and ``feature`` are one file on Windows and macOS,
            # ``NUL`` is a device and ``x./y`` loses its dot, so a loose mirror
            # silently held fewer refs than the remote -- and the branch it
            # lost had its commits pruned and its packs deleted.  A single file
            # has no filesystem to collide on.  ``strict`` makes a ref that
            # cannot be read an error: skipping it is the same loss.
            def read_mirror() -> dict[str, str]:
                found: dict[str, str] = {}
                for namespace in REF_NAMESPACES:
                    for ref_name, sha in iter_refs(
                        client, repo_id, clean_repo, namespace, strict=True
                    ):
                        if not sha or not sha.strip():
                            raise RuntimeError(
                                f"refusing to compact {clean_repo}: empty SHA for ref '{ref_name}'."
                            )
                        found[ref_name] = sha.strip()
                return found

            mirrored = read_mirror()
            mirrored_refs = len(mirrored)
            if mirrored:
                (bare_repo / "packed-refs").write_bytes(
                    "".join(f"{sha} {name}\n" for name, sha in sorted(mirrored.items()))
                    .encode("utf-8")
                )
                _verify_ref_mirror(bare_repo, mirrored, scratch_env, clean_repo)

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
            res = run_git_checked(
                ["-C", str(bare_repo), "repack", "-a", "-d", "-l",
                 f"--max-pack-size={cfg.max_pack_bytes}"],
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

            new_pack_names = _upload_new_packs(
                client, repo_id, pack_dir, new_packs, on_upload_progress
            )

            # Fencing check: verify lock ownership before touching anything
            # else.  It comes before the upload verification so that a lost
            # lease is reported as a lost lease -- "we are not allowed to finish
            # this" is a different problem from "the bytes are not there", and
            # they send the user to different places.
            if not lock_still_ours():
                raise RuntimeError(
                    f"refusing to delete obsolete packfiles: {ownership_reason()}. "
                    "Compacted packfiles were uploaded, but old packs were left untouched to prevent data corruption."
                )

            # 6b. Prove the consolidated packfiles are there, and whole, before
            # a single old pack is deleted.
            _verify_upload(
                client, repo_id, pack_dir, new_packs,
                old_adj="obsolete", new_desc="consolidated packfiles", plural=True,
            )
            _verify_content(
                client, repo_id, pack_dir, new_packs, Path(td),
                old_adj="obsolete", new_desc="consolidated packfiles",
            )

            # 6c. The mirror was read before a repack that can take minutes.  A
            # writer that ignores the lock (a pre-0.8.4 client, a script) may
            # have pushed meanwhile: its objects were not in the mirror, so the
            # consolidated pack does not carry them, and step 7 would delete the
            # only pack that does -- leaving its ref naming nothing.  Read the
            # refs again, and delete nothing if they are not what the
            # reachability was computed from (the payload retention run does the
            # same before its first deletion).
            if read_mirror() != mirrored:
                raise RuntimeError(
                    "refusing to delete obsolete packfiles: the remote's refs changed while "
                    "the repository was being compacted (a writer that did not take the lock). "
                    "The consolidated packfiles were uploaded and the old ones left untouched; "
                    "run gc again."
                )

            # 7. Delete obsolete old packs from Seafile
            if verbose:
                sys.stderr.write("  Cleaning up obsolete remote packfiles...\n")
            deleted_count, failed_deletions, cleanup_stopped = _delete_superseded(
                client, repo_id, pack_dir, downloaded_packs, new_pack_names,
                lock_still_ours, ownership_reason,
                old_adj="obsolete", new_desc="consolidated packfiles", plural=True,
            )

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
                # Not "ok" when the cleanup stopped short: the run did not do
                # everything it was asked to, and a scheduled job that sees only
                # "ok" cannot tell a complete compaction from one that left
                # orphans behind.  The packs are verified either way.
                "status": "incomplete" if cleanup_stopped else "ok",
                "old_packs": len(old_packs),
                "new_packs": len(new_packs),
                "saved_kb": saved_kb,
                "deleted_packs": deleted_count,
                "deletion_failures": failed_deletions,
                "cleanup_stopped": cleanup_stopped,
            }
