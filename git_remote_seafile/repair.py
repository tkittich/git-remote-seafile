"""repair.py - Restore remote refs whose objects no remote packfile carries.

A ref can outlive its packfile: something removed a pack out of band, or an
interrupted publish lost one.  Git then fails with "bad object", and the
RUNBOOK's repair is manual -- delete the ref, push it again, per ref, with a
window in between where the branch does not exist.

``repair_remote`` automates the same recovery without touching a ref or
deleting anything: it enumerates the objects the remote's packfiles actually
carry, diffs every ref tip against that set, and uploads -- from the clone it
runs in -- one pack per broken ref holding exactly the objects that ref names
and the remote lacks.  Uploads are proven by size and then by content (the
same two proofs gc pays before it deletes anything), because a repair that
reports success over an unverifiable upload leaves the ref broken while
claiming the opposite.

The primitives are gc's -- the verified pack download, the scratch
enumeration, the proved upload -- applied in the opposite direction: gc
consolidates what the remote has, repair restores what the remote lacks.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from .client import SeafileClient
from .config import RemoteConfig
from .gc import (
    _fetch_and_verify_pack_pair,
    _init_scratch,
    _phase_pulse,
    _upload_new_packs,
)
from .git_util import (
    HEX_SHA_RE,
    PACK_BIG_FILE_THRESHOLD,
    clean_git_env,
    run_git_checked,
)
from .lock import RemoteLock
from .packs import is_valid_pack_name
from .refs import REF_NAMESPACES, is_valid_ref_name, iter_refs

__all__ = ["repair_remote"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_repair_upload(
    client: SeafileClient,
    repo_id: str,
    pack_dir: str,
    new_packs: list[Path],
    probe_dir: Path,
) -> None:
    """Prove the uploaded repair packs are on the remote, whole, or raise.

    The same two proofs gc pays before it deletes -- a fresh listing's sizes,
    then a read-back digest -- worded for an operation that deletes nothing:
    the failure that matters here is reporting a ref as repaired when its
    pack never landed, which is exactly the false success the manual
    procedure's "Everything up-to-date" trap already taught the RUNBOOK to
    distrust.
    """
    expected: dict[str, int] = {}
    for np in new_packs:
        expected[np.name] = np.stat().st_size
        n_idx = np.with_suffix(".idx")
        if n_idx.is_file():
            expected[n_idx.name] = n_idx.stat().st_size

    listing = client.list_dir(repo_id, pack_dir)
    remote_sizes = {
        e.get("name"): e.get("size")
        for e in listing
        if isinstance(e, dict) and e.get("name")
    }
    problems: list[str] = []
    for name, want in sorted(expected.items()):
        try:
            got = int(remote_sizes.get(name) or -1)
        except (TypeError, ValueError):
            got = -1
        if got != want:
            problems.append(f"{name} is not on the remote at its local size ({want} bytes)")
    if problems:
        raise RuntimeError(
            "the repaired packfile could not be verified on the remote ("
            + "; ".join(problems)
            + "); the ref stays broken -- re-run 'git-remote-seafile repair' "
            "once the server is reachable"
        )

    for np in new_packs:
        for local in (np, np.with_suffix(".idx")):
            if not local.is_file():
                continue
            probe = probe_dir / f"verify-{local.name}"
            try:
                present = client.download_file_to(
                    repo_id, f"{pack_dir}/{local.name}", probe,
                    max_bytes=local.stat().st_size,
                )
                same = (
                    present is True
                    and probe.stat().st_size == local.stat().st_size
                    and _sha256(probe) == _sha256(local)
                )
            except Exception as ex:
                raise RuntimeError(
                    f"could not read {local.name} back to check its content ({ex}); "
                    "whether the repair landed is unproven -- re-run "
                    "'git-remote-seafile repair'"
                ) from ex
            finally:
                probe.unlink(missing_ok=True)
            if not same:
                raise RuntimeError(
                    f"{local.name} on the remote has the right size but different "
                    "content from what was uploaded; delete it by hand and re-run "
                    "'git-remote-seafile repair'"
                )


def _int_size(entry: dict[str, Any]) -> int:
    """A listing entry's size, 0 for anything that is not a plain integer."""
    raw = entry.get("size")
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    return int(raw) if isinstance(raw, str) and raw.isdigit() else 0


def _list_packs(
    client: SeafileClient, repo_id: str, pack_dir: str, *, has_refs: bool
) -> tuple[dict[str, int], dict[str, int]]:
    """``(pack sizes, idx sizes)`` of the remote, by name.

    A listing that answers empty while refs exist is asked again once: a
    transient empty answer would otherwise make every ref look broken (and a
    real run re-upload the whole history).  If it is still empty the remote
    may genuinely have lost every pack -- the case repair exists for -- so it
    goes on, saying so.
    """
    entries = client.list_dir(repo_id, pack_dir)
    if not entries and has_refs:
        entries = client.list_dir(repo_id, pack_dir)
        if not entries:
            sys.stderr.write(
                f"Warning: {pack_dir} lists no packfiles at all, twice; every ref will be "
                "treated as broken. Repair only adds packs, so this is safe, but check "
                "that the server is listing the directory correctly\n"
            )
            sys.stderr.flush()
    pack_sizes: dict[str, int] = {}
    idx_sizes: dict[str, int] = {}
    for e in entries:
        name = str(e.get("name", "")) if isinstance(e, dict) else ""
        if name.endswith(".idx"):
            idx_sizes[name] = _int_size(e)
        elif name.endswith(".pack"):
            # The name becomes a local path in the scratch directory.  gc and
            # fetch validate it before any download; a library writer who can
            # plant "..\\..\\x.pack" must not get a write outside it here.
            if not is_valid_pack_name(name):
                sys.stderr.write(f"Warning: ignoring pack file with an invalid name '{name}'\n")
                sys.stderr.flush()
                continue
            pack_sizes[name] = _int_size(e)
    return pack_sizes, idx_sizes


def _read_tips(
    client: SeafileClient, repo_id: str, repo_path: str, renew: Callable[[], None] | None = None
) -> list[tuple[str, str]]:
    """Every ref tip the remote holds.

    Strict, as ``gc`` reads its mirror: a ref that is listed but cannot be
    read is an error naming it.  Skipping it with a warning made
    ``repair --dry-run`` answer "clean" over the very ref that was broken.
    """
    tips: list[tuple[str, str]] = []
    for namespace in REF_NAMESPACES:
        for ref_name, sha in iter_refs(
            client, repo_id, repo_path, namespace, strict=True, strict_listing=True,
            renew=renew,
        ):
            if not is_valid_ref_name(ref_name):
                sys.stderr.write(f"Warning: ignoring malformed ref name '{ref_name}'\n")
                sys.stderr.flush()
                continue
            if not HEX_SHA_RE.fullmatch(sha):
                sys.stderr.write(f"Warning: ignoring invalid SHA '{sha}' for ref '{ref_name}'\n")
                sys.stderr.flush()
                continue
            tips.append((ref_name, sha))
    return tips


def repair_remote(
    client: SeafileClient,
    repo_id: str,
    repo_path: str,
    dry_run: bool = False,
    verbose: bool = True,
    config: RemoteConfig | None = None,
    local_repo: Path | None = None,
) -> dict[str, Any]:
    """Restore every ref whose tip no remote packfile carries.

    Returns a dict whose ``status`` is one of:

    ``"clean"``
        Every ref tip is in a remote packfile; nothing to do.
    ``"broken"``
        *dry_run* only: the broken refs are named in ``broken_refs`` and
        nothing was uploaded.
    ``"ok"``
        Every broken ref was repaired: its objects left this clone in packs
        proven present on the remote.
    ``"partial"``
        Some refs repaired, some not; ``unrepaired`` names each ref and why
        (most often: this clone does not hold its objects either).
    ``"error"``
        Fatal -- a remote pack could not be downloaded and verified, so the
        coverage scan is not trustworthy enough to repair against.

    The lock is held throughout, as for a gc: the repair uploads, and the
    download-enumerate-upload sequence must not interleave with a concurrent
    compaction reshaping the very pack set the coverage answer came from.
    """
    clean_repo = repo_path.rstrip("/")
    pack_dir = f"{clean_repo}/objects/pack"
    cfg = config if config is not None else RemoteConfig.load()

    with RemoteLock(client, repo_id, clean_repo, config=cfg) as lock:
        def maybe_renew() -> None:
            lock.maybe_renew(20.0)

        # Listed under the lock, like the rest of the sequence: a pack list taken
        # before it can be reshaped by a compaction that finishes while we wait.
        # Reading the refs is minutes on a repository with thousands of tags, so
        # the lease is renewed from inside the walk.
        tips = _read_tips(client, repo_id, repo_path, renew=maybe_renew)
        pack_sizes, idx_sizes = _list_packs(client, repo_id, pack_dir, has_refs=bool(tips))

        def on_upload_progress(transferred: int, total: int) -> None:
            maybe_renew()

        def on_download_progress(transferred: int, total: int) -> None:
            maybe_renew()

        with tempfile.TemporaryDirectory(prefix="git-seaf-repair-") as td:
            tmp = Path(td)
            scratch_env = clean_git_env()
            bare_repo, scratch_pack_dir = _init_scratch(tmp, list(pack_sizes), scratch_env)

            for pack_name, expected in sorted(pack_sizes.items()):
                maybe_renew()
                if verbose:
                    sys.stderr.write(f"  Reading {pack_name}...\n")
                    sys.stderr.flush()
                err = _fetch_and_verify_pack_pair(
                    client, repo_id, pack_dir, pack_name, expected,
                    idx_sizes.get(pack_name.removesuffix(".pack") + ".idx"),
                    scratch_pack_dir, bare_repo, scratch_env, on_download_progress,
                    op="repair",
                )
                if err is not None:
                    return {"status": "error", "message": err}

            res = run_git_checked(
                ["-C", str(bare_repo), "cat-file", "--batch-all-objects", "--batch-check"],
                scratch_env,
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"git cat-file failed while enumerating the remote packs: {res.stderr}")
            covered = {
                line.split(" ", 1)[0]
                for line in res.stdout.splitlines()
                if line.strip()
            }

            broken = [(name, sha) for name, sha in tips if sha not in covered]
            if not broken:
                return {"status": "clean", "refs": len(tips), "packs": len(pack_sizes)}
            if dry_run:
                return {"status": "broken", "broken_refs": broken, "packs": len(pack_sizes)}

            local_env = clean_git_env()
            work_tree = local_repo if local_repo is not None else Path.cwd()
            out_dir = tmp / "out"
            out_dir.mkdir()
            pulse = _phase_pulse(sys.stderr, "building the repair pack")

            def pack_pulse() -> None:
                maybe_renew()
                pulse()

            repaired: list[str] = []
            unrepaired: list[tuple[str, str]] = []
            uploaded_names: list[str] = []
            uploaded_bytes = 0

            for ref_name, tip in sorted(broken):
                maybe_renew()
                res = run_git_checked(
                    ["-C", str(work_tree), "rev-list", "--objects", tip],
                    local_env,
                    capture_output=True,
                    text=True,
                )
                if res.returncode != 0:
                    first_line = res.stderr.strip().splitlines()[0] if res.stderr.strip() else "git failed"
                    unrepaired.append(
                        (ref_name, f"this clone cannot enumerate its objects ({first_line})")
                    )
                    continue
                wanted = {
                    line.split(" ", 1)[0]
                    for line in res.stdout.splitlines()
                    if line.strip()
                } - covered

                before = {p.name for p in out_dir.glob("pack-*.pack")}
                res = run_git_checked(
                    ["-C", str(work_tree), "-c", f"core.bigFileThreshold={PACK_BIG_FILE_THRESHOLD}",
                     "pack-objects", "-q", f"--max-pack-size={cfg.max_pack_bytes}",
                     str(out_dir / "pack-repair")],
                    local_env,
                    input="\n".join(sorted(wanted)) + "\n",
                    text=True,
                    capture_output=True,
                    renew=pack_pulse,
                )
                maybe_renew()
                if res.returncode != 0:
                    first_line = res.stderr.strip().splitlines()[0] if res.stderr.strip() else "git failed"
                    unrepaired.append((ref_name, f"git pack-objects failed ({first_line})"))
                    continue
                new_packs = [
                    out_dir / n
                    for n in sorted({p.name for p in out_dir.glob("pack-*.pack")} - before)
                ]
                if not new_packs:
                    unrepaired.append(
                        (ref_name, "pack-objects produced no packfile for its object set")
                    )
                    continue

                if verbose:
                    sys.stderr.write(f"  Uploading the pack that repairs {ref_name}...\n")
                    sys.stderr.flush()
                _upload_new_packs(client, repo_id, pack_dir, new_packs, on_upload_progress)
                _verify_repair_upload(client, repo_id, pack_dir, new_packs, tmp)
                # The next ref shares what this one uploaded: the coverage set
                # grows here, so two broken branches on one commit pay once.
                covered |= wanted
                repaired.append(ref_name)
                uploaded_names.extend(p.name for p in new_packs)
                uploaded_bytes += sum(p.stat().st_size for p in new_packs)

            if not unrepaired:
                status = "ok"
            elif repaired:
                status = "partial"
            else:
                status = "error"
            return {
                "status": status,
                "repaired": repaired,
                "unrepaired": unrepaired,
                "packs_uploaded": uploaded_names,
                "uploaded_bytes": uploaded_bytes,
                "refs": len(tips),
            }
