"""The local cache of fetched objects (STORE.md D4).

A directory of ``<sha256[:2]>/<sha256>`` files.  The invariant everything else relies on: **a file under its final
name is a complete, verified object**.  So an object is admitted only from a verified temporary file, fsynced and
renamed (a crash leaves no object under the final name, R3, R6); cache files are read-only, opened without
following symlinks and refused unless regular; and one that could have been written through (hard-linked, or its
modification time moved) is hashed again before it is served.

How "its modification time moved" is known without a database: an admitted file's mtime is set to a **fixed
value**, and recency is kept in the access time.  Any write moves the mtime off the fixed value; a hit only touches
the access time.  A cache is a copy: deleting it loses nothing.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import refs

#: The modification time of every admitted object (2001-09-09): anything else means the file was written to.
ADMITTED_MTIME_NS = 1_000_000_000 * 1_000_000_000
#: A path just handed out is not evicted for this long, so ``gdalinfo "$(sfblob path ...)"`` keeps its file.
GRACE_SECONDS = 60
PARTIAL_DIR = "partial"
#: An abandoned partial or lock file is removed after this long (RESUME.md D4: one expiry).
PARTIAL_EXPIRY_SECONDS = 7 * 24 * 3600


def default_dir() -> Path:
    """``$SFBLOB_CACHE``, else the user's cache directory."""
    env = os.environ.get("SFBLOB_CACHE")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "git-remote-seafile" / "blobs"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "git-remote-seafile" / "blobs"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return          # a directory cannot be opened for fsync on Windows; the rename itself is the commit
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _try_lock(fd: int) -> None:
    """Take an exclusive lock on the whole file without waiting; ``OSError`` when another holder has it."""
    os.lseek(fd, 0, 0)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, 0)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class Cache:
    def __init__(self, root: "Path | None" = None, size_limit: "int | None" = None) -> None:
        self.root = Path(root) if root is not None else default_dir()
        self.size_limit = size_limit

    def path_of(self, ref: refs.Ref) -> Path:
        if not ref.is_full:
            raise refs.RefError("the cache is addressed by a full reference")
        return self.root / ref.hex[:2] / ref.hex

    def partial_dir(self) -> Path:
        return self.root / PARTIAL_DIR

    # -- reading ---------------------------------------------------------------------------------------------

    def lookup(self, ref: refs.Ref, size: "int | None" = None) -> "Path | None":
        """The cached file for *ref*, or ``None``; a damaged or doubtful one is dropped or checked first (R3)."""
        path = self.path_of(ref)
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None                       # a symlink or a folder under an object's name is never served
        if size is not None and st.st_size != size:
            self._drop(path)                  # not the size the store says: not this object
            return None
        doubtful = st.st_mtime_ns != ADMITTED_MTIME_NS or st.st_nlink > 1
        if doubtful:
            try:
                good = _sha256_file(path) == ref.hex
            except OSError:
                return None
            if not good:
                self._drop(path)
                return None
            self._seal(path)
        self.touch(path)
        return path

    def touch(self, path: Path) -> None:
        """Recency: the access time, best effort (the modification time is what proves nobody wrote to it)."""
        try:
            os.utime(path, ns=(time.time_ns(), ADMITTED_MTIME_NS))
        except OSError:
            pass

    # -- writing ---------------------------------------------------------------------------------------------

    def admit(self, ref: refs.Ref, verified: Path) -> Path:
        """Move a **verified** temporary file into the cache under its hash: fsync, rename, read-only, fixed mtime."""
        final = self.path_of(ref)
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(verified, "r+b") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass          # the verification already read every byte; a platform that cannot fsync still renames
        os.replace(verified, final)
        _fsync_dir(final.parent)
        self._seal(final)
        return final

    def _seal(self, path: Path) -> None:
        try:
            os.chmod(path, stat.S_IREAD | (0 if os.name == "nt" else stat.S_IRGRP | stat.S_IROTH))
        except OSError:
            pass
        self.touch(path)

    def _drop(self, path: Path) -> None:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(path)
        except OSError:
            pass          # Windows refuses an open file; the next run tries again

    # -- partials and their lock (RESUME.md D4) -----------------------------------------------------------------

    @contextmanager
    def fetch_lock(self, hex_digest: str, timeout: float = 900.0) -> Iterator[None]:
        """One fetch of an object at a time across processes: a **kernel lock** on ``partial/<sha>.lock``.

        A crash or ``kill -9`` releases it (an ``O_EXCL`` file would block every later fetch of that object after
        the very event resume exists for).  The file itself is left in place and reaped by ``sweep_partials``:
        unlinking a lock file races a process that is opening the same name, and fails on Windows while held.
        """
        folder = self.partial_dir()
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / f"{hex_digest}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"another process has been fetching {hex_digest} for {int(timeout)} s") from None
                    time.sleep(0.1)
            try:
                yield
            finally:
                try:
                    _unlock(fd)
                except OSError:
                    pass
        finally:
            os.close(fd)

    def sweep_partials(self, max_age: float = PARTIAL_EXPIRY_SECONDS) -> int:
        """Remove partial, temporary and lock files not touched for *max_age* seconds; a file in use is skipped."""
        folder = self.partial_dir()
        if not folder.is_dir():
            return 0
        removed = 0
        now = time.time()
        for item in folder.iterdir():
            try:
                if now - item.stat().st_mtime < max_age:
                    continue
                item.unlink()
                removed += 1
            except OSError:
                pass          # held open (Windows) or already gone: the next sweep tries again
        return removed

    # -- the size bound ----------------------------------------------------------------------------------------

    def objects(self) -> "list[tuple[Path, int, float]]":
        """Every admitted object as ``(path, size, access time)``; the partial folder is not among them."""
        found: "list[tuple[Path, int, float]]" = []
        if not self.root.is_dir():
            return found
        for fan in self.root.iterdir():
            if fan.name == PARTIAL_DIR or not fan.is_dir() or len(fan.name) != 2:
                continue
            for item in fan.iterdir():
                try:
                    st = os.lstat(item)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and len(item.name) == 64:
                    found.append((item, st.st_size, st.st_atime))
        return found

    def partial_bytes(self) -> int:
        folder = self.partial_dir()
        if not folder.is_dir():
            return 0
        total = 0
        for item in folder.iterdir():
            try:
                total += item.stat().st_size
            except OSError:
                pass
        return total

    def used(self) -> int:
        return sum(size for _, size, _ in self.objects()) + self.partial_bytes()

    def evict(self, limit: "int | None" = None) -> "list[Path]":
        """Remove the least recently used objects until the cache fits; returns what was removed.

        A file just handed out (within ``GRACE_SECONDS``) is skipped, and so is one that cannot be unlinked
        (Windows refuses an open file): both are tried again next time.
        """
        limit = self.size_limit if limit is None else limit
        if limit is None:
            return []
        removed: "list[Path]" = []
        total = self.used()
        now = time.time()
        for path, size, atime in sorted(self.objects(), key=lambda item: item[2]):
            if total <= limit:
                break
            if now - atime < GRACE_SECONDS:
                continue
            self._drop(path)
            if not path.exists():
                total -= size
                removed.append(path)
        return removed

    def clear(self) -> int:
        """Remove every cached object (and the partials); returns the number of objects removed."""
        count = 0
        for path, _, _ in self.objects():
            self._drop(path)
            count += 0 if path.exists() else 1
        folder = self.partial_dir()
        if folder.is_dir():
            for item in folder.iterdir():
                try:
                    item.unlink()
                except OSError:
                    pass
        return count
