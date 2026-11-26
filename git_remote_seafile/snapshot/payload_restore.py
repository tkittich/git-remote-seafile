from __future__ import annotations

import json
import os
import random
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from . import common
from . import payload_guards
from . import payload_manifest
from . import payload_upload


# -- Payload restore (see 75_payload.py) -------------------------------------------

# -- restore -------------------------------------------------------------------


def extended_path(path: Path) -> Path:
    """``path`` in Windows' extended-length form, so 260 characters is not a limit.

    Python reaches past MAX_PATH only when the machine opted in
    (``LongPathsEnabled``); the extended-length prefix works everywhere and costs
    nothing elsewhere.  A relative or already-prefixed path is returned unchanged.
    """
    if os.name != "nt":
        return path
    text = str(path)
    bs = chr(92)
    prefix = bs * 2 + "?" + bs
    if text.startswith(prefix) or not os.path.isabs(text):
        return path
    if text.startswith(bs * 2):
        return Path(prefix + "UNC" + bs + text[2:])
    return Path(prefix + text)


def walk_long(root: "Path | str", onerror: "Callable[[OSError], Any] | None" = None) -> Any:
    """``os.walk(root)``, but a directory past 260 characters is entered on every Windows.

    Without ``LongPathsEnabled`` a plain walk treats such a directory as unreadable and
    skips it with no error the caller sees, so a backup left out everything under it
    (AA24).  The walk runs on the extended-length form and the prefix is taken off what
    it yields, so callers keep plain ``dirpath`` values (and still wrap a path in
    ``extended_path`` before a stat, as they did).  Elsewhere it is ``os.walk``.
    """
    if os.name != "nt":
        yield from os.walk(root, onerror=onerror)
        return
    bs = chr(92)
    prefix = bs * 2 + "?" + bs
    long_root = extended_path(Path(os.path.abspath(str(root))))
    for dirpath, dirnames, filenames in os.walk(long_root, onerror=onerror):
        if dirpath.startswith(prefix + "UNC" + bs):
            dirpath = bs + dirpath[len(prefix) + 3:]
        elif dirpath.startswith(prefix):
            dirpath = dirpath[len(prefix):]
        yield dirpath, dirnames, filenames


def _retries_a_held_placement() -> bool:
    """Only Windows holds a file for a scanner or indexer, so only there is a short wait worth it (a function, so a test
    can say "Windows" without patching ``os.name``, which changes how the whole standard library behaves)."""
    return os.name == "nt"


def _download_verified(
    client: Any, rid: str, source_path: str, dest: Path, item: dict[str, Any], scratch: Path
) -> str | None:
    """Fetch one object to ``dest`` via a temporary file; ``None`` on success, else why not.

    The temporary file is under ``.git`` (``scratch``), never beside ``dest``: a
    ``<name>.part`` next to the payload file collided with a real file of that
    name that git had just restored, which was overwritten and then renamed
    away -- and the restore went on to certify the tree.
    """
    part = scratch / f"{item['sha256']}.{os.getpid()}.part"
    last = ""
    verified = False
    part.unlink(missing_ok=True)      # a first attempt starts from nothing; only a retry resumes (RESUME.md D2)
    for _attempt in range(3):
        try:
            # A failed transfer leaves its bytes, and the retry continues them; a partial that hashed wrong was
            # deleted below, so the next attempt starts from byte 0 and never resumes the same bad bytes.
            held = part.stat().st_size if part.exists() else 0
            if not client.download_file_to(rid, source_path, part, max_bytes=item["size"], resume_from=held):
                part.unlink(missing_ok=True)
                return "object not found on the server"
            if part.stat().st_size != item["size"]:
                last = f"size {part.stat().st_size}, manifest says {item['size']}"
                part.unlink(missing_ok=True)
                continue
            if payload_upload._sha256_file(part) != item["sha256"]:
                last = "SHA-256 does not match the manifest"
                part.unlink(missing_ok=True)
                continue
            verified = True
            break
        except common.SnapshotError as exc:
            last = str(exc)
        except Exception as exc:  # transient network or API error: try again, from where it stopped
            last = f"{type(exc).__name__}: {exc}"
    if verified:
        # Placing the file is not the transfer: a name this volume refuses or a read-only folder fails
        # the same way every time, and trying again re-downloads the whole object (fable-S3-6).
        # A scanner or indexer on Windows can hold the file for a moment (WinError 5 or 32): that is the one
        # failure worth a short wait.  The verified bytes are never deleted for failing to move: the scratch
        # folder is removed with the run, and a rerun fetches again either way.
        for tries in range(5):
            try:
                os.replace(part, dest)
                return None
            except PermissionError as exc:
                last = f"could not place the downloaded file: {type(exc).__name__}: {exc}"
                if not _retries_a_held_placement() or tries == 4:
                    break
                time.sleep(0.2 * (tries + 1))
            except OSError as exc:
                last = f"could not place the downloaded file: {type(exc).__name__}: {exc}"
                break
        return last
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass          # a held handle: this one file fails, the restore goes on
    return last


def restore_payload(
    remote: str, into: Path, *, allow_low_disk: bool = False, sample: int | None = None,
    rev: "str | None" = None, only: Any = None,
) -> None:
    """Download and verify the payload a restored snapshot's manifest names.

    A security problem in the manifest refuses it whole.  An entry this platform
    cannot hold, one that would overwrite something already there, or one that
    fails to download or verify is skipped and reported -- the rest is restored
    -- and the run then fails.  A file already in place that verifies is kept, so
    a rerun into the same directory picks up where it stopped.  With *sample* only
    that many files, chosen at random, are fetched (the restore drill).  With *only* (a
    ``restore_only.Selection``) the manifest is read from *rev* of the clone -- a partial restore
    does not write it -- validated whole, and then cut to the selection.
    """
    if only is not None:
        from . import restore_only
        manifest = restore_only.read_manifest(into, rev or "HEAD")
        if manifest is None:
            return
    else:
        manifest_path = into / payload_upload.PAYLOAD_MANIFEST
        if not manifest_path.is_file():
            return
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (ValueError, RecursionError) as exc:
            raise common.SnapshotError(f"the snapshot's payload manifest is unreadable: {exc}") from None
    files = payload_manifest.validate_manifest(manifest, into)
    in_manifest = len(files)
    if only is not None:
        files = [item for item in files if only.contains_path(item["path"])]
    total_files = len(files)
    if sample is not None and sample < total_files:
        files = random.sample(files, sample)
    problem = payload_guards.restore_space_problem(files, into, allow_low_disk=allow_low_disk)
    if problem is not None:
        raise common.SnapshotError(problem)
    library, base, server_url = payload_upload.payload_location(remote)
    client = payload_upload._api(lambda: payload_upload._open_client(server_url), "opening the Seafile client")
    rid = payload_upload._api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    of = f" of {in_manifest}" if len(files) < in_manifest else ""
    print(f"restoring {len(files)}{of} payload file(s) from {base}/objects ...", flush=True)
    scratch = extended_path(into / ".git" / "grs-restore")
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        _restore_payload_files(client, rid, base, files, into, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _claim(owners: dict[tuple[int, int], str], dest: Path, rel: str) -> str | None:
    """Record that *rel* owns the file at *dest*; the path that already owned it, if another did."""
    try:
        info = os.stat(extended_path(dest))
    except OSError:
        return None
    if not info.st_ino:
        return None      # a volume with no file index: nothing to tell two names apart by
    key = (info.st_dev, info.st_ino)
    first = owners.setdefault(key, rel)
    return None if first == rel else first


def _spelled_differently(listings: "dict[Path, set[str]]", dest: Path, rel: str) -> str | None:
    """The name a file already in place goes by, when it is not the manifest's spelling.

    The files git checked out are not in ``owners``, so on a folding volume a manifest name that differs
    from one of them only by case found "its" file already there with the right bytes and was reported
    verified while only the other spelling sat on disk (review c305b0a gemini-S3-5 a).  The directory's own
    listing says which spelling the disk holds; a rerun into the same directory sees the manifest's own.
    """
    names = listings.get(dest.parent)
    if names is None:
        try:
            names = {unicodedata.normalize("NFC", name) for name in os.listdir(dest.parent)}
        except OSError:
            names = set()
        listings[dest.parent] = names
    wanted = unicodedata.normalize("NFC", dest.name)
    if not names or wanted in names:
        return None
    folded = wanted.casefold()
    return next((name for name in sorted(names) if name.casefold() == folded), "another file")


def _restore_payload_files(
    client: Any, rid: str, base: str, files: list[dict[str, Any]], into: Path, scratch: Path
) -> None:
    from . import remote_ops, restore_names
    problems: list[str] = []
    done = 0
    #: (device, file index) -> the manifest path that owns that file on disk: two entries that
    #: a folding volume maps to one file must not both count as restored (review dfd2269,
    #: gemini-4-hydrogen-2-G1.1: identical bytes made the second one "verified" in silence).
    owners: dict[tuple[int, int], str] = {}
    listings: dict[Path, set[str]] = {}
    # What the volume under *into* refuses, on every platform: a FAT volume mounted on Linux drops a trailing
    # dot (the file then sat under another name and was reported verified) or refuses a colon after the whole
    # object was downloaded (review c305b0a gemini-S3-5 b).  Windows keeps its fixed rules.
    name_problem = restore_names.destination_name_problem(into)
    total_bytes = sum(item["size"] for item in files)
    fetched = 0
    started = last_pulse = time.monotonic()
    for index, item in enumerate(files, 1):
        rel, sha = item["path"], item["sha256"]
        why = name_problem(rel) if name_problem is not None else None
        if why:
            problems.append(f"{rel}: skipped, the name {why}")
            continue
        dest = extended_path(into / rel)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{rel}: skipped, cannot create its directory ({exc})")
            continue
        if dest.exists() or dest.is_symlink():
            if dest.is_file() and not dest.is_symlink() and payload_upload._sha256_file(dest) == sha:
                other = _claim(owners, dest, rel) or _spelled_differently(listings, dest, rel)
                if other is not None:
                    problems.append(f"{rel}: skipped, this filesystem treats the name as the same "
                                    f"file as {other}, which was restored first")
                    continue
                done += 1
                continue
            problems.append(f"{rel}: skipped, something else is already there (a name that "
                            "this filesystem treats as the same as another file's)")
            continue
        print(f"downloading [{index}/{len(files)}] {rel} ({remote_ops.human_bytes(item['size'])})", flush=True)
        why = _download_verified(client, rid, f"{base}/{payload_upload.object_dir(sha)}/{sha}", dest, item, scratch)
        if why is not None:
            problems.append(f"{rel}: {why}")
            continue
        if not dest.is_file():       # a placement that reported success must have left the file (round 4, DeepSeek N4)
            problems.append(f"{rel}: downloaded and verified, but the file is not in place")
            continue
        try:
            if item.get("mode") == "100755" and os.name != "nt":
                dest.chmod(dest.stat().st_mode | 0o111)
            os.utime(dest, ns=(item["mtime_ns"], item["mtime_ns"]))
        except (OSError, OverflowError, ValueError):
            pass  # content verified; timestamps and mode are best effort
        _claim(owners, dest, rel)
        done += 1
        fetched += item["size"]
        now = time.monotonic()
        if now - last_pulse >= payload_upload._PULSE_SECONDS:
            last_pulse = now
            print(payload_upload._progress_line(fetched, total_bytes, now - started, "downloaded"), flush=True)
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise common.SnapshotError(
            f"{len(problems)} of {len(files)} payload file(s) were not restored "
            f"({done} were, and verified):\n  {shown}{more}"
        )
    print(f"payload verified: {len(files)} file(s), SHA-256 matches the manifest")


def portable_name_warning(paths: list[str]) -> str | None:
    """A warning for payload paths a Windows restore would skip, or ``None``."""
    found = [(p, payload_manifest.windows_name_problem(p)) for p in paths]
    bad = [(p, why) for p, why in found if why]
    if not bad:
        return None
    shown = "\n".join(f"  {p}: {why}" for p, why in bad[:10])
    more = f"\n  ... and {len(bad) - 10} more" if len(bad) > 10 else ""
    return (
        f"\nWarning: {len(bad)} payload path(s) cannot be restored on Windows under that "
        f"name (a restore there skips and reports them; elsewhere they restore):\n{shown}{more}"
    )
