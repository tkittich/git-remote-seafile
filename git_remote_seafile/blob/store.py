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
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from ..client import UPLOAD_RESTARTS, HashingReader, SeafileAPIError, UploadCorruptedError, UploadSourceChangedError
from . import cache as cache_mod
from . import refs

MARKER = ".seafile-store"
MARKER_MAX_BYTES = 64 * 1024          # a marker is a few dozen bytes; a planted large file is not read whole
#: Feature names this version understands, by list.  Empty in format 1; a later version adds names here.
KNOWN_FEATURES: "dict[str, tuple[str, ...]]" = {"incompat": (), "ro_compat": (), "compat": ()}
FORMAT = 1
#: How long, **inside one folder run** (``Store.presence_cached``), a listing of a fan-out folder answers "is the object
#: there?" for an upload.  The proofs that follow an upload (the listing after it, the rename's own listing) are always
#: fresh; this only spares the check before it, so a re-run or a resumed run over thousands of objects costs one listing
#: per folder, not one per file.  A single-file ``put`` never uses it (a damaged object must be seen at once, R5), and
#: neither does the pinned form (it lists after its pin is readable, STORE.md D6).
PRESENCE_TTL_SECONDS = 30.0
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


class ReadBackMismatch(StoreError):
    """The bytes read back are not the object's (as opposed to a read that failed): the only proof that justifies a delete."""


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


def place_new(temp: Path, target: Path) -> bool:
    """Give *temp* the name *target* unless something already has it; one act, so nothing is replaced in a window.

    A hard link refuses a taken name atomically.  On a volume without hard links (FAT, some shares): Windows renames,
    which also refuses a taken name and is atomic; elsewhere the name is claimed with ``O_EXCL`` and the bytes are
    copied into it (a kill in the copy leaves a short file under the name: the restore reports it, never overwrites it).
    """
    try:
        os.link(temp, target)
    except FileExistsError:
        return False
    except OSError:
        if os.name == "nt":
            try:
                os.rename(temp, target)
            except FileExistsError:
                return False
            return True
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as dst, open(temp, "rb") as src:
                shutil.copyfileobj(src, dst, 1 << 20)
                dst.flush()
                os.fsync(dst.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    temp.unlink()
    return True


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
        self._read_only_because: "list[str]" = []          # ``ro_compat`` features of the marker this version lacks
        #: fan-out folder -> (when it was listed, {object name: size}); shared with the clones a folder run makes
        self._presence: "dict[str, tuple[float, dict[str, int]]]" = {}
        self._presence_ttl = 0.0          # 0: every check asks the server
        try:
            client.reuse_upload_links = True          # one upload link per folder, not one per object
        except AttributeError:
            pass
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
        other._read_only_because = self._read_only_because
        other._presence = self._presence
        other._presence_ttl = self._presence_ttl
        return other

    @contextmanager
    def presence_cached(self) -> "Iterator[None]":
        """For one folder run: an upload's pre-check may use a listing up to ``PRESENCE_TTL_SECONDS`` old."""
        self._presence.clear()
        self._presence_ttl = PRESENCE_TTL_SECONDS
        try:
            yield
        finally:
            self._presence_ttl = 0.0
            self._presence.clear()

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
            self._refuse_unfit_root(entries, names)
            self._write_marker()
            self._marker_checked = True

    def _refuse_unfit_root(self, entries: "list[dict[str, Any]]", names: "set[str]") -> None:
        """Every reason (STORE.md D2) a folder with no marker may not become a store; changes nothing.

        ``check_root`` calls it before writing the marker and ``would_put`` (``put --dry-run``) calls it so that a dry
        run refuses what the real run refuses (review R7).
        """
        holds_git = sorted(names & _GIT_ENTRIES)
        if holds_git:
            raise StoreError(
                f"{self.root or '/'} looks like a vault or a git remote (it holds {', '.join(holds_git)}); "
                "choose another folder")
        if entries:
            raise StoreError(
                f"{self.root or '/'} is not empty and is not a blob store: it is never adopted. Use an empty "
                "or new folder")
        self._refuse_vault_neighbours()
        parent = self.root.rpartition("/")[0]
        while True:          # up to and including the library root, which is the empty path
            above = {str(e.get("name", "")) for e in self._list(parent or "/")}
            if above & _GIT_ENTRIES:
                raise StoreError(f"{parent or '/'} looks like a vault or a git remote; a store is not created under it")
            if MARKER in above:
                raise StoreError(f"{parent or '/'} is a blob store; a store is not created inside another one")
            if not parent:
                break
            parent = parent.rpartition("/")[0]

    def _refuse_vault_neighbours(self) -> None:
        """D2: not ``<vault>/payload`` (the folder payload mode owns), and not under ``<x>-view`` beside a vault ``<x>``
        (a payload view replaces it, and the store with it)."""
        parts = [p for p in self.root.split("/") if p]
        if parts and parts[-1] == "payload":
            raise StoreError(f"{self.root} is named payload, the folder a payload-mode remote owns; choose another folder")
        for depth, part in enumerate(parts):
            if part.endswith("-view") and len(part) > len("-view"):
                sibling = "/" + "/".join(parts[:depth] + [part[: -len("-view")]])
                names = {str(e.get("name", "")) for e in self._list(sibling)}
                if names & _GIT_ENTRIES:
                    raise StoreError(f"{self.root} is under {'/' + '/'.join(parts[:depth + 1])}, the view folder of the "
                                     f"vault {sibling}; a payload view replaces it. Choose another folder")

    def require_writable(self) -> None:
        """Refuse a write to a store whose marker asks for features this version lacks (``ro_compat``)."""
        if self._read_only_because:
            raise StoreError(f"{self.root}/{MARKER} needs features this version does not have "
                             f"({', '.join(self._read_only_because)}): it may read this store but not write to it; use a newer sfblob")

    def _write_marker(self) -> None:
        body = json.dumps({"format": FORMAT, "id": uuid.uuid4().hex}).encode("utf-8")
        try:
            self.client.upload_file(self.repo_id, self.root or "/", MARKER, body, replace=False)
        except SeafileAPIError as exc:
            raise StoreError(f"could not write the store marker: {exc}") from None
        # Two first puts race (D18 row 6): the loser's bytes land as a ``(N)`` copy and the name holds the winner's marker, so
        # the proof is "a valid marker is there", never "my bytes are" (review R7 Gemini P3, DeepSeek P1).  A marker stored
        # one byte long is a CR, which is JSON whitespace and reads fine; anything that does not parse fails here.
        self._read_marker()

    def _read_marker(self) -> None:
        try:
            raw = self.client.get_file_bytes(self.repo_id, f"{self.root}/{MARKER}", max_bytes=MARKER_MAX_BYTES)
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (SeafileAPIError, ValueError, AttributeError, UnicodeDecodeError):
            raise StoreError(f"{self.root}/{MARKER} cannot be read as a store marker") from None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise StoreError(f"{self.root}/{MARKER} is a marker of another format; this version reads format {FORMAT}")
        # Feature lists (as ext4's): a later version that must stop older ones can say so without a new format number,
        # which would stop them *reading* as well.  Absent means empty; an unknown ``compat`` name is ignored.
        for key in ("incompat", "ro_compat", "compat"):
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
                # A feature value this version cannot read as a list of names must not read as "no features": that is how a
                # later version's lever against older writers would fail open (review R8 Opus P2).
                raise StoreError(f"{self.root}/{MARKER} has a {key!r} this version cannot read (not a list of names); "
                                 "use a newer sfblob")
        unknown = {key: [n for n in data.get(key, []) if n not in KNOWN_FEATURES.get(key, ())]
                   for key in ("incompat", "ro_compat")}
        if unknown.get("incompat"):
            raise StoreError(f"{self.root}/{MARKER} needs features this version does not have ({', '.join(unknown['incompat'])}); "
                             "use a newer sfblob")
        self._read_only_because = unknown.get("ro_compat") or []

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
        self.check_root(create=False)          # a full reference skips ``resolve``'s check: the marker is read here
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
        if pin:
            from . import pins
            pins.check_name(pin)          # a bad name is refused before anything is written, the marker included
        self.check_root(create=True)
        self.require_writable()
        try:
            hex_digest, size = _sha256_and_size(path)
        except OSError as exc:
            raise StoreError(f"cannot read {path}: {exc}") from None
        ref = refs.full(f"sha256:{hex_digest}")
        if pin:
            pins.write(self, pin, [ref])
        have = self._listed_size(ref, fresh=bool(pin))
        if have == size:
            return Put(ref, size, uploaded=False)
        directory = self.blob_dir(ref.hex)
        from ..snapshot import payload_upload      # the one commit rule payload and LFS share (RESUME.md D1)
        temp = ""
        try:
            temp, reader = self._upload_temporary(path, size, ref, directory, progress)
            vouched = (_sha256_and_size(path)[0] == ref.hex) if reader.rewound else (
                reader.complete and reader.hexdigest() == ref.hex)
            if not vouched:
                self._drop(directory, temp)
                raise StoreError(f"{path} changed while it was being uploaded; nothing was stored, run again")
            if not payload_upload._commit_object(self.client, self.repo_id, directory, temp, ref.hex, size):
                self._drop(directory, temp)
                self._drop_wrong_size(directory, ref, size)
                raise StoreError(f"the server would not give the object its name ({ref}); run again")
        except SeafileAPIError as exc:
            self._drop(directory, temp)
            raise StoreError(f"upload of {path} failed: {exc}") from None
        if self._listed_size(ref, fresh=True) != size:
            raise StoreError(f"the server lists {ref} at another size than {size} after the upload; run again")
        if self.verify_by_download:
            try:
                self._prove_by_reading(ref, size)
            except ReadBackMismatch:
                # Wrong bytes under the hash name at the right size: the next ``put`` would see it present and skip it.
                # Only a *proven* mismatch deletes: a read that merely failed proves nothing, and the object under the name
                # may be another writer's, already verified (review R7 P1).
                self._drop_object(directory, ref)
                raise
        return Put(ref, size, uploaded=True)

    def _upload_temporary(self, path: Path, size: int, ref: refs.Ref, directory: str,
                          progress: "Callable[[int, int], None] | None") -> "tuple[str, HashingReader]":
        """Upload *path* under a fresh temporary name; start again under another when the server stored a chunk wrongly.

        The server can store a chunk of a chunked upload one byte too long (its multipart parser, measured on CE 13.0);
        the client notices at once, and the object cannot be repaired in place, so it is uploaded again.
        """
        for restart in range(UPLOAD_RESTARTS + 1):
            temp = f"{ref.hex}{_PART}{os.urandom(4).hex()}"
            try:
                with open(path, "rb") as source:
                    reader = HashingReader(source, size)
                    try:
                        self.client.upload_file(self.repo_id, directory, temp, reader, replace=True, progress_callback=progress)
                    except UploadSourceChangedError:
                        self._drop(directory, temp)
                        raise StoreError(f"{path} changed while it was being uploaded; nothing was stored, run again") from None
                return temp, reader
            except UploadCorruptedError as exc:
                self._drop(directory, temp)
                if restart == UPLOAD_RESTARTS:
                    raise StoreError(f"the server stored {path} wrongly {restart + 1} times in a row ({exc})") from None
            except SeafileAPIError:
                self._drop(directory, temp)
                raise
        raise AssertionError("unreachable")

    def would_put(self, path: Path) -> Put:
        """What ``put`` would do, changing nothing (not even the marker): ``uploaded`` says it would upload."""
        try:
            hex_digest, size = _sha256_and_size(path)
        except OSError as exc:
            raise StoreError(f"cannot read {path}: {exc}") from None
        ref = refs.full(f"sha256:{hex_digest}")
        entries = self._list(self.root)
        names = {str(e.get("name", "")) for e in entries}
        if MARKER not in names:
            self._refuse_unfit_root(entries, names)          # what the real put would refuse, said without writing
        else:
            self.check_root(create=False)          # reads the marker: a store this version may not write is refused here too
            self.require_writable()
        have = self._listed_size(ref) if MARKER in names else None
        return Put(ref, size, uploaded=have != size)

    def _listed_size(self, ref: refs.Ref, *, fresh: bool = False) -> "int | None":
        """The listed size of an object, or ``None``.  ``fresh=False`` may answer from a listing up to
        ``PRESENCE_TTL_SECONDS`` old; ``fresh=True`` always asks the server (and refreshes the shared listing)."""
        folder = self.blob_dir(ref.hex)
        held = self._presence.get(folder)
        if not fresh and held is not None and time.monotonic() - held[0] < self._presence_ttl:
            return held[1].get(ref.hex)
        sizes: "dict[str, int]" = {}
        for entry in self._list(folder):
            name, size = entry.get("name"), entry.get("size")
            if entry.get("type") != "dir" and isinstance(name, str) and isinstance(size, int) and not isinstance(size, bool):
                sizes[name] = size
        self._presence[folder] = (time.monotonic(), sizes)
        return sizes.get(ref.hex)

    def _drop_wrong_size(self, directory: str, ref: refs.Ref, size: int) -> None:
        """Remove a *file* under the hash name that is listed at another size than the object has.

        A failed commit can leave one (the rename landed on the free name and the server stored other bytes); such an
        object is damage that no reader is ever given (R3), and ``exists`` would say yes to it until the next ``put``
        replaced it (R5).  A directory under the name is never deleted.
        """
        try:
            for entry in self._list(directory):
                if entry.get("name") == ref.hex and entry.get("type") != "dir" and entry.get("size") != size:
                    self.client.delete_entry(self.repo_id, f"{directory}/{ref.hex}")
        except Exception:
            pass          # the next put replaces a wrong-size object (R5)

    def _drop_object(self, directory: str, ref: refs.Ref) -> None:
        """Remove the file under the hash name (a directory there is never deleted); best effort."""
        try:
            for entry in self._list(directory):
                if entry.get("name") == ref.hex and entry.get("type") != "dir":
                    self.client.delete_entry(self.repo_id, f"{directory}/{ref.hex}")
        except Exception:
            pass

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
                raise StoreError(f"could not read {ref} back to prove it ({exc}); the object was left as it is: "
                                 f"run `sfblob verify --deep {ref}`") from None
            if not found:
                raise StoreError(f"{ref} was not there to read back; run again")
            if probe.stat().st_size != size or _sha256_and_size(probe)[0] != ref.hex:
                raise ReadBackMismatch(f"{ref} did not read back as the bytes that were sent; run again")

    # -- get --------------------------------------------------------------------------------------------------------

    def get(self, ref: refs.Ref, *, keep: bool = True, use_cache: bool = True,
            progress: "Callable[[int, int], None] | None" = None) -> "tuple[Path, bool]":
        """The cached, verified file for *ref* and whether it came from the cache.

        A hit costs no request.  A miss downloads into the cache's partial folder, hashes, and only then renames
        into the cache (R3, R6).  With ``keep=False`` a fetched object is not admitted (``cat``): the caller deletes
        the returned temporary file.  With ``use_cache=False`` (``verify --deep``, which must judge the server's
        bytes and not a copy that was good once) the cache is not looked at: the object is always downloaded, and
        ``keep`` must be ``False``.
        """
        if not use_cache:
            if keep:
                raise ValueError("use_cache=False reads the server's copy only to judge it: keep must be False")
            self.check_root(create=False)
            full = self.resolve(ref)
            size = self.stat(full)
            if size is None:
                raise StoreError(f"{full} is not in the store")
            return self._fetch(full, size, keep=False, progress=progress), False
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
        # One name per object for a kept fetch, so a later run continues what an interrupted one left (RESUME.md D4);
        # the lock makes it one writer.  ``cat`` takes a private name in the system temporary folder, outside the cache,
        # so ``cache --clear`` and ``cache --size`` neither delete nor count it (review R6 G17).
        if keep:
            partial_dir = self.cache.partial_dir()
            partial_dir.mkdir(parents=True, exist_ok=True)
            part = partial_dir / f"{full.hex}.part"
        else:
            part = Path(tempfile.gettempdir()) / f"sfblob-cat-{full.hex[:12]}.{uuid.uuid4().hex}.tmp"
        try:
            return self._fetch_into(full, size, part, keep=keep, progress=progress)
        except BaseException:
            if not keep:
                part.unlink(missing_ok=True)          # also on Ctrl-C, which is not an ``Exception``
            raise

    def _fetch_into(self, full: refs.Ref, size: int, part: Path, *, keep: bool,
                    progress: "Callable[[int, int], None] | None") -> Path:
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
            if not keep:
                part.unlink(missing_ok=True)
            # A kept partial stays when only the transfer failed, so the next run continues from it (RESUME.md D4);
            # a size or hash mismatch removed it above.
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
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise StoreError(f"{destination} already exists; get does not overwrite")
        # Staged under a name of our own beside the destination, then placed in one act that refuses a taken name: a
        # failed copy, a kill or a power cut leaves a stage file (ours, deleted on every path out we control), never half a
        # file under the name the caller asked for (review R7 DeepSeek P3, Gemini).
        stage = destination.with_name(f".sfblob-part-{os.urandom(8).hex()}")
        try:
            with open(source, "rb") as src, open(stage, "xb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
                dst.flush()
                try:
                    os.fsync(dst.fileno())
                except OSError:
                    pass
            if not place_new(stage, destination):
                raise StoreError(f"{destination} already exists; get does not overwrite")
        except OSError as exc:
            raise StoreError(f"cannot write {destination}: {exc}") from None
        finally:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass
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
