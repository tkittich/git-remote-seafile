"""One store on one Seafile library: ``put``, ``get``, ``cat``, ``exists``, ``stat`` (STORE.md D1-D5, D8).

Objects live at ``<root>/blobs/<sha[:2]>/<sha>``.  An upload goes through the verified-upload primitive
(RESUME.md D1): a random temporary name, a reader that hashes the bytes as they are sent, the listed size checked, a
rename onto the hash name only if the digest is the name.  A download is hashed, and a mismatch fails and leaves
nothing in the cache (R3).  The store root owns itself with a marker written before any blob (D2).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from ..client import HashingReader, SeafileAPIError, UploadSourceChangedError
from . import cache as cache_mod
from . import refs

MARKER = ".seafile-store"
FORMAT = 1
#: Names that say a folder is a vault or a helper remote, never a store root (D2).
_GIT_ENTRIES = frozenset({"payload", "refs", "HEAD", "objects"})
_PART = ".part-"
_LOCKS: "dict[str, threading.Lock]" = {}
_LOCKS_GUARD = threading.Lock()


def _hash_lock(hex_digest: str) -> threading.Lock:
    """One lock per object in this process; a lock across processes is RESUME.md D4 (not built)."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(hex_digest, threading.Lock())


class StoreError(Exception):
    """A store problem a person can act on (the message says what and what to do)."""


@dataclass(frozen=True)
class Put:
    ref: refs.Ref
    size: int
    uploaded: bool          # False: the object was already there at the right size (R5)


def _sha256_and_size(path: Path) -> "tuple[str, int]":
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


class Store:
    def __init__(
        self, client: Any, repo_id: str, root: str, cache: "cache_mod.Cache | None" = None,
        *, verify_by_download: bool = False, factory: "Callable[[], Store] | None" = None,
    ) -> None:
        self.client = client
        self.repo_id = repo_id
        self.root = "/" + root.strip("/") if root.strip("/") else ""
        self.cache = cache if cache is not None else cache_mod.Cache()
        #: Over plain http a stored object is proved only by reading it back (D8), never by the hash the upload computed.
        self.verify_by_download = verify_by_download
        self._factory = factory
        self._marker_checked = False
        try:
            client.layout_root = self.root          # a racing mkdir_p knows the store's own folders (STORE.md D2)
            client.store_layout = True
        except AttributeError:
            pass
        self._lock = threading.Lock()

    # -- layout -----------------------------------------------------------------------------------------------

    def blob_dir(self, hex_digest: str) -> str:
        return f"{self.root}/blobs/{hex_digest[:2]}"

    def blob_path(self, ref: refs.Ref) -> str:
        return f"{self.blob_dir(ref.hex)}/{ref.hex}"

    def clone(self) -> "Store":
        """A store for another thread (a client is not shared across threads)."""
        if self._factory is None:
            return self
        other = self._factory()
        other._marker_checked = self._marker_checked
        return other

    def _list(self, path: str) -> "list[dict[str, Any]]":
        try:
            entries = self.client.list_dir(self.repo_id, path or "/")
        except SeafileAPIError as exc:
            raise StoreError(f"could not list {path or '/'} in the library: {exc}") from None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    # -- the marker (D2) --------------------------------------------------------------------------------------

    def _marker_names(self, entries: "list[dict[str, Any]]") -> "list[str]":
        return [str(e.get("name")) for e in entries if str(e.get("name", "")) == MARKER]

    def check_root(self, *, create: bool) -> None:
        """A root is used only when it carries a store's marker; ``put`` may adopt an empty or missing folder."""
        if self._marker_checked:
            return
        with self._lock:
            if self._marker_checked:
                return
            entries = self._list(self.root)
            names = {str(e.get("name", "")) for e in entries}
            if MARKER in names:
                self._read_marker()
                self._marker_checked = True
                return
            if not create:
                raise StoreError(
                    f"{self.root or '/'} is not a blob store (no {MARKER}); put something in it first with `put`")
            holds_git = sorted(names & _GIT_ENTRIES)
            if holds_git:
                raise StoreError(
                    f"{self.root or '/'} looks like a vault or a git remote (it holds {', '.join(holds_git)}); "
                    "choose another folder")
            if entries:
                raise StoreError(
                    f"{self.root or '/'} is not empty and is not a blob store: it is never adopted. Use an empty "
                    "or new folder")
            parent = self.root.rpartition("/")[0]
            while parent:
                above = {str(e.get("name", "")) for e in self._list(parent)}
                if above & _GIT_ENTRIES:
                    raise StoreError(f"{parent} looks like a vault or a git remote; a store is not created under it")
                if MARKER in above:
                    raise StoreError(f"{parent} is a blob store; a store is not created inside another one")
                parent = parent.rpartition("/")[0]
            self._write_marker()
            self._marker_checked = True

    def _write_marker(self) -> None:
        body = json.dumps({"format": FORMAT, "id": uuid.uuid4().hex}).encode("utf-8")
        try:
            self.client.upload_file(self.repo_id, self.root or "/", MARKER, body, replace=False)
        except SeafileAPIError as exc:
            raise StoreError(f"could not write the store marker: {exc}") from None

    def _read_marker(self) -> None:
        try:
            raw = self.client.get_file_bytes(self.repo_id, f"{self.root}/{MARKER}")
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (SeafileAPIError, ValueError, AttributeError, UnicodeDecodeError):
            raise StoreError(f"{self.root}/{MARKER} cannot be read as a store marker") from None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise StoreError(f"{self.root}/{MARKER} is a marker of another format; this version reads format {FORMAT}")

    # -- references -------------------------------------------------------------------------------------------

    def resolve(self, ref: refs.Ref) -> refs.Ref:
        """The full reference a short one names, from one listing of its fan-out folder."""
        if ref.is_full:
            return ref
        self.check_root(create=False)          # an unmarked folder says so, not "no object starts with"
        names = [str(e.get("name", "")) for e in self._list(f"{self.root}/blobs/{refs.fanout(ref)}")
                 if e.get("type") != "dir"]      # a folder named like an object is not one
        try:
            return refs.resolve(ref, names)
        except refs.RefError as exc:
            raise StoreError(str(exc)) from None

    def stat(self, ref: refs.Ref) -> "int | None":
        """The size of the stored object, or ``None`` when it is not there (a folder under the name is not an object)."""
        full = self.resolve(ref)
        for entry in self._list(self.blob_dir(full.hex)):
            if entry.get("name") == full.hex and entry.get("type") != "dir":
                size = entry.get("size")
                return size if isinstance(size, int) and not isinstance(size, bool) else None
        return None

    def exists(self, ref: refs.Ref) -> bool:
        return self.stat(ref) is not None

    def objects(self) -> "list[tuple[refs.Ref, int]]":
        """Every object in the store with its size: one listing of ``blobs/``, then one per fan-out folder that exists."""
        self.check_root(create=False)
        found: "list[tuple[refs.Ref, int]]" = []
        for fan in sorted(str(e.get("name", "")) for e in self._list(f"{self.root}/blobs") if e.get("type") == "dir"):
            if len(fan) != 2:
                continue
            for entry in self._list(f"{self.root}/blobs/{fan}"):
                name, size = str(entry.get("name", "")), entry.get("size")
                if (entry.get("type") != "dir" and len(name) == 64 and name.startswith(fan)
                        and isinstance(size, int) and not isinstance(size, bool)):
                    try:
                        found.append((refs.full(f"sha256:{name}"), size))
                    except refs.RefError:
                        continue          # a name that is not an object (a conflict copy, a user's file): not listed
        return found

    # -- put ------------------------------------------------------------------------------------------------------

    def put(self, path: Path, progress: "Callable[[int, int], None] | None" = None, pin: "str | None" = None) -> Put:
        """Hash, upload if absent, and prove it: the object is listed at its size under its hash name.

        With *pin* the pin is written (and read back) **before** the object is listed: a pinner lists after its pin is
        readable, which is what a later ``gc`` relies on (STORE.md D6).
        """
        self.check_root(create=True)
        try:
            hex_digest, size = _sha256_and_size(path)
        except OSError as exc:
            raise StoreError(f"cannot read {path}: {exc}") from None
        ref = refs.full(f"sha256:{hex_digest}")
        if pin:
            from . import pins
            pins.write(self, pin, [ref])
        have = self._listed_size(ref)
        if have == size:
            return Put(ref, size, uploaded=False)
        directory = self.blob_dir(ref.hex)
        temp = f"{ref.hex}{_PART}{os.urandom(4).hex()}"
        from ..snapshot import payload_upload      # the one commit rule payload and LFS share (RESUME.md D1)
        try:
            with open(path, "rb") as source:
                reader = HashingReader(source, size)
                try:
                    self.client.upload_file(self.repo_id, directory, temp, reader, replace=True, progress_callback=progress)
                except UploadSourceChangedError:
                    self._drop(directory, temp)
                    raise StoreError(f"{path} changed while it was being uploaded; nothing was stored, run again") from None
            vouched = (_sha256_and_size(path)[0] == ref.hex) if reader.rewound else (
                reader.complete and reader.hexdigest() == ref.hex)
            if not vouched:
                self._drop(directory, temp)
                raise StoreError(f"{path} changed while it was being uploaded; nothing was stored, run again")
            if not payload_upload._commit_object(self.client, self.repo_id, directory, temp, ref.hex, size):
                self._drop(directory, temp)
                raise StoreError(f"the server would not give the object its name ({ref}); run again")
        except SeafileAPIError as exc:
            self._drop(directory, temp)
            raise StoreError(f"upload of {path} failed: {exc}") from None
        if self._listed_size(ref) != size:
            raise StoreError(f"the server lists {ref} at another size than {size} after the upload; run again")
        if self.verify_by_download:
            self._prove_by_reading(ref, size)
        return Put(ref, size, uploaded=True)

    def would_put(self, path: Path) -> Put:
        """What ``put`` would do, changing nothing (not even the marker): ``uploaded`` says it would upload."""
        try:
            hex_digest, size = _sha256_and_size(path)
        except OSError as exc:
            raise StoreError(f"cannot read {path}: {exc}") from None
        ref = refs.full(f"sha256:{hex_digest}")
        names = {str(e.get("name", "")) for e in self._list(self.root)}
        have = self._listed_size(ref) if MARKER in names else None
        return Put(ref, size, uploaded=have != size)

    def _listed_size(self, ref: refs.Ref) -> "int | None":
        for entry in self._list(self.blob_dir(ref.hex)):
            if entry.get("name") == ref.hex and entry.get("type") != "dir":
                size = entry.get("size")
                return size if isinstance(size, int) and not isinstance(size, bool) else None
        return None

    def _drop(self, directory: str, temp: str) -> None:
        try:
            if any(e.get("name") == temp for e in self._list(directory)):
                self.client.delete_entry(self.repo_id, f"{directory}/{temp}")
        except Exception:
            pass          # a leftover temporary is harmless: payload's collector and the grace period remove it

    def _prove_by_reading(self, ref: refs.Ref, size: int) -> None:
        """Over http the bytes are proved only by reading them back (D8)."""
        with tempfile.TemporaryDirectory(prefix="sfblob-verify-") as scratch:
            probe = Path(scratch) / "object"
            try:
                found = self.client.download_file_to(self.repo_id, self.blob_path(ref), probe, max_bytes=size)
            except SeafileAPIError as exc:
                raise StoreError(f"could not read {ref} back to prove it: {exc}") from None
            if not found or probe.stat().st_size != size or _sha256_and_size(probe)[0] != ref.hex:
                raise StoreError(f"{ref} did not read back as the bytes that were sent; run again")

    # -- get --------------------------------------------------------------------------------------------------------

    def get(self, ref: refs.Ref, *, keep: bool = True,
            progress: "Callable[[int, int], None] | None" = None) -> "tuple[Path, bool]":
        """The cached, verified file for *ref* and whether it came from the cache.

        A hit costs no request.  A miss downloads into the cache's partial folder, hashes, and only then renames
        into the cache (R3, R6).  With ``keep=False`` a fetched object is not admitted (``cat``): the caller deletes
        the returned temporary file.
        """
        if ref.is_full:
            hit = self.cache.lookup(ref)
            if hit is not None:
                return hit, True
        self.check_root(create=False)
        full = self.resolve(ref)
        hit = self.cache.lookup(full)
        if hit is not None:
            return hit, True
        size = self.stat(full)
        if size is None:
            raise StoreError(f"{full} is not in the store")
        hit = self.cache.lookup(full, size)
        if hit is not None:
            return hit, True
        if not keep:
            return self._fetch(full, size, keep=False, progress=progress), False
        with _hash_lock(full.hex), self.cache.fetch_lock(full.hex):    # one fetch at a time: threads, then processes
            hit = self.cache.lookup(full, size)
            if hit is not None:
                return hit, True
            return self._fetch(full, size, keep=True, progress=progress), False

    def _fetch(self, full: refs.Ref, size: int, *, keep: bool,
               progress: "Callable[[int, int], None] | None" = None) -> Path:
        """Download, check size and hash, and admit into the cache (or, for ``cat``, hand back the proved temporary)."""
        partial_dir = self.cache.partial_dir()
        partial_dir.mkdir(parents=True, exist_ok=True)
        # One name per object for a kept fetch, so a later run continues what an interrupted one left (RESUME.md D4);
        # the lock makes it one writer.  ``cat`` takes a private name and deletes it.
        part = partial_dir / (f"{full.hex}.part" if keep else f"{full.hex}.{uuid.uuid4().hex}.tmp")
        if keep:
            self.cache.sweep_partials()
            if part.exists() and part.stat().st_size > size:
                part.unlink()          # not a prefix of this object: start again
        else:
            part.unlink(missing_ok=True)
        last = ""
        for _attempt in range(3):
            try:
                held = part.stat().st_size if part.exists() else 0
                if not self.client.download_file_to(self.repo_id, self.blob_path(full), part, progress_callback=progress,
                                                 max_bytes=size, resume_from=held):
                    part.unlink(missing_ok=True)
                    raise StoreError(f"{full} is not in the store")
                if part.stat().st_size != size:
                    last = f"size {part.stat().st_size}, the store says {size}"
                    part.unlink(missing_ok=True)
                    continue
                if _sha256_and_size(part)[0] != full.hex:
                    last = "the bytes do not hash to the reference"
                    part.unlink(missing_ok=True)
                    continue
                break
            except StoreError:
                raise
            except Exception as exc:       # a dropped transfer: the next attempt continues the partial
                last = f"{type(exc).__name__}: {exc}"
        else:
            part.unlink(missing_ok=True)
            raise StoreError(f"could not fetch {full}: {last}")
        if not keep:
            return part
        path = self.cache.admit(full, part)
        self.cache.evict()
        return path

    def get_to(self, ref: refs.Ref, destination: Path,
               progress: "Callable[[int, int], None] | None" = None) -> Path:
        """Copy the verified object to *destination*, which must not exist (R4: ``get`` never overwrites)."""
        source, _ = self.get(ref, progress=progress)
        try:
            with open(source, "rb") as src, open(destination, "xb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        except FileExistsError:
            raise StoreError(f"{destination} already exists; get does not overwrite") from None
        except OSError as exc:
            raise StoreError(f"cannot write {destination}: {exc}") from None
        return destination

    def cat(self, ref: refs.Ref, out: BinaryIO, progress: "Callable[[int, int], None] | None" = None) -> int:
        """Write the verified object to *out*; nothing is written before the whole object has been proved."""
        path, cached = self.get(ref, keep=False, progress=progress)
        try:
            written = 0
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    out.write(block)
                    written += len(block)
            return written
        finally:
            if not cached:
                path.unlink(missing_ok=True)
