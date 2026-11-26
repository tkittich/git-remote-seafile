"""Folders: ``put -r DIR --record FILE`` and ``get --from FILE --to DIR`` (STORE.md D3, "Ease of use").

A folder is uploaded file by file and its ``path -> reference`` list written as a record file (record.py: one sorted
line per entry, meant for git).  ``get --from`` restores every path, verified.  Both are safe to run again: an upload
of an object the store holds does nothing (R5), and a path already in place with the right bytes is left alone.  A path
that holds *other* bytes is never overwritten (R4): it is reported and the run fails, after everything else was done.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import os
import shutil
import stat
import sys
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import record, store
from .store import Store, StoreError


class _Workers:
    """One store (one client, one set of connections and upload links) per worker thread, closed at the end of the run.

    A client is not shared across threads, but one per *file* (the first version) meant a new login, new sessions and a
    new upload link for every file at ``--jobs`` above 1 (review R6 N5: 601 clients and 601 link requests for 600 files).
    """

    def __init__(self, base: Store, jobs: int) -> None:
        self._base, self._jobs = base, jobs
        self._local = threading.local()
        self._made: "list[Store]" = []
        self._lock = threading.Lock()

    def get(self) -> Store:
        if self._jobs <= 1:
            return self._base
        worker = getattr(self._local, "store", None)
        if worker is None:
            worker = self._local.store = self._base.clone()
            with self._lock:
                self._made.append(worker)
        return worker

    def close(self) -> None:
        for worker in self._made:
            if worker is not self._base:
                try:
                    worker.client.close()
                except Exception:
                    pass


@dataclass
class PutReport:
    entries: "list[record.Entry]" = field(default_factory=list)
    uploaded: int = 0
    present: int = 0
    skipped: "list[str]" = field(default_factory=list)       # not regular files, or unreadable: said, never silent
    new: "set[str]" = field(default_factory=set)             # paths whose object was (or would be) uploaded
    problems: "list[str]" = field(default_factory=list)
    excluded: int = 0
    dry_run: bool = False


@dataclass
class GetReport:
    restored: int = 0
    in_place: int = 0
    problems: "list[str]" = field(default_factory=list)
    warnings: "list[str]" = field(default_factory=list)      # said, but the file was placed (a size the hash overrules)
    excluded: int = 0
    dry_run: bool = False


def excluded_by(path: str, patterns: "tuple[str, ...] | list[str]") -> bool:
    """True when a relative path matches any ``--exclude`` pattern (shell-style, ``/`` separators, NFC both sides)."""
    text = unicodedata.normalize("NFC", path.replace("\\", "/"))
    return any(fnmatch.fnmatchcase(text, unicodedata.normalize("NFC", pattern)) for pattern in patterns)


def is_link_or_junction(path: Path) -> bool:
    """A symbolic link, or a Windows junction (a reparse-point folder, which ``is_symlink`` does not report)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _walk(directory: Path) -> "tuple[list[Path], list[str], list[str]]":
    """``(files, skipped, problems)``: a folder that cannot be read is a problem, never a silent omission."""
    files: "list[Path]" = []
    skipped: "list[str]" = []
    problems: "list[str]" = []

    def unreadable(error: OSError) -> None:
        try:
            where = Path(error.filename).relative_to(directory).as_posix()
        except (TypeError, ValueError):
            where = str(error.filename or "?")
        problems.append(f"{where}: cannot be read ({error.strerror or error})")

    for base, dirs, names in os.walk(directory, followlinks=False, onerror=unreadable):
        dirs.sort()
        for name in sorted(names):
            path = Path(base) / name
            rel = path.relative_to(directory).as_posix()
            if is_link_or_junction(path) or not path.is_file():
                skipped.append(f"{rel}: not a regular file")
            else:
                files.append(path)
        for name in list(dirs):
            if is_link_or_junction(Path(base) / name):
                dirs.remove(name)
                skipped.append(f"{(Path(base) / name).relative_to(directory).as_posix()}: a link to a folder")
    return files, skipped, problems


def _write_atomically(path: Path, text: str) -> None:
    # A name only this call can have made: a predictable one would overwrite, then delete, a user's file.
    temp = path.with_name(f"{path.name}.sfblob-tmp-{os.urandom(4).hex()}")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _place(temp: Path, target: Path) -> bool:
    """Give *temp* the name *target* unless something already has it; one act, so nothing is replaced in a window."""
    try:
        os.link(temp, target)
    except FileExistsError:
        return False
    except OSError:
        # A volume without hard links (FAT, some shares): claim the name with O_EXCL, which refuses a name that exists,
        # then copy into it.  Never ``os.replace`` onto a name this run has not proved free (review R6 D1).
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as dst, open(temp, "rb") as src:
                shutil.copyfileobj(src, dst, 1 << 20)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    temp.unlink()
    return True


def put_tree(
    base: Store, directory: Path, record_path: "Path | None", jobs: int = 1,
    on_file: "Callable[[str, store.Put], None] | None" = None,
    *, dry_run: bool = False, exclude: "tuple[str, ...] | list[str]" = (), pin: "str | None" = None,
) -> PutReport:
    """Upload every regular file under *directory*; write the record file when one is named.

    With *dry_run* nothing is changed: no marker, no upload, no record file; the report says what would happen.
    *exclude* patterns leave paths out (and so out of the record).
    """
    if not directory.is_dir():
        raise StoreError(f"{directory} is not a folder")
    files, skipped, walk_problems = _walk(directory)
    if record_path is not None:
        # The record sits in the folder it describes (`put -r . --record blobs.txt`): it is not part of the data,
        # and uploading its previous bytes under its own name would make it disagree with itself forever.
        own = os.path.normcase(os.path.abspath(record_path))
        leave = [p for p in files if os.path.normcase(os.path.abspath(p)) == own
                 or os.path.normcase(os.path.abspath(p)).startswith(own + ".sfblob-tmp")]
        for p in leave:
            skipped.append(f"{p.relative_to(directory).as_posix()}: the record file itself")
        files = [p for p in files if p not in leave]
    kept = [p for p in files if not excluded_by(p.relative_to(directory).as_posix(), exclude)]
    report = PutReport(skipped=skipped, excluded=len(files) - len(kept), dry_run=dry_run)
    report.problems.extend(walk_problems)
    if os.name != "nt":
        # ``record.normalise_path`` turns a backslash into a slash, so on POSIX a file ``a\b`` would be recorded as ``a/b``.
        slashed = [p for p in kept if "\\" in p.relative_to(directory).as_posix()]
        report.problems.extend(f"{p.relative_to(directory).as_posix()}: a backslash in a name cannot be recorded"
                               for p in slashed)
        kept = [p for p in kept if p not in slashed]
    files = kept
    if pin:
        from . import pins
        pins.check_name(pin)
    if not dry_run:
        base.check_root(create=True)       # once, before any thread: two first puts would race on the marker

    workers = _Workers(base, jobs)

    def one(path: Path) -> "tuple[Path, store.Put | None, str | None]":
        worker = workers.get()
        try:
            if dry_run:
                return path, worker.would_put(path), None
            return path, (worker.put(path, pin=pin) if pin else worker.put(path)), None
        except StoreError as exc:
            return path, None, str(exc)

    with base.presence_cached():
        if jobs <= 1:
            results = [one(p) for p in files]
        else:
            with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
                try:
                    results = list(pool.map(one, files))
                finally:
                    workers.close()
    spelled: "dict[str, str]" = {}
    for path, done, problem in results:
        rel = path.relative_to(directory).as_posix()
        if problem is not None or done is None:
            report.problems.append(f"{rel}: {problem}")
            continue
        try:
            normal = record.normalise_path(rel)
        except record.RecordError as exc:
            report.problems.append(f"{rel}: {exc}")
            continue
        if normal in spelled:
            report.problems.append(
                f"{rel}: the same path as {spelled[normal]} once Unicode forms are made equal; a record names a path once")
            continue
        spelled[normal] = rel
        report.entries.append(record.Entry(rel, done.size, done.ref))
        report.uploaded += 1 if done.uploaded else 0
        if done.uploaded:
            report.new.add(rel)
        report.present += 0 if done.uploaded else 1
        if on_file is not None:
            on_file(rel, done)
    if record_path is not None and not report.problems and not dry_run:
        # A record of a run that failed would claim files are stored that are not: it is written only on success.
        _write_atomically(record_path, record.render(report.entries))
    return report


def _in_place(target: Path, entry: record.Entry) -> bool:
    try:
        if target.is_symlink() or not target.is_file() or target.stat().st_size != entry.size:
            return False
        from .cache import _sha256_file
        return _sha256_file(target) == entry.ref.hex
    except OSError:
        return False


def _folds_case(dest: Path, dry_run: bool) -> bool:
    """Does the volume under *dest* treat names that differ only in case as one?  Found by creating a name there."""
    probe = f"sfblob-case-{os.urandom(4).hex()}A"
    try:
        if dest.is_dir():
            (dest / probe).write_bytes(b"")
            try:
                return (dest / probe.lower()).exists()
            finally:
                (dest / probe).unlink(missing_ok=True)
    except OSError:
        pass
    return dry_run and (os.name == "nt" or sys.platform == "darwin")


def _case_collisions(entries: "list[record.Entry]", dest: Path, dry_run: bool) -> "dict[str, str]":
    """``path -> reason`` for every path that shares a name with another once case is folded, on a volume that folds it.

    The restore would place one and call the other "something else is already there" (or, with equal bytes, count it as
    in place and lose it silently), so neither is restored and both are named (review R6 G4, D7, Q1).
    """
    groups: "dict[str, list[str]]" = {}
    for entry in entries:
        groups.setdefault(unicodedata.normalize("NFC", entry.path).casefold(), []).append(entry.path)
    clashing = {key: paths for key, paths in groups.items() if len(paths) > 1}
    if not clashing or not _folds_case(dest, dry_run):
        return {}
    found: "dict[str, str]" = {}
    for paths in clashing.values():
        for path in paths:
            others = ", ".join(repr(p) for p in paths if p != path)
            found[path] = f"differs only in case from {others}, and this volume treats them as one name"
    return found


def get_tree(
    base: Store, record_path: Path, destination: Path, jobs: int = 1,
    *, dry_run: bool = False, exclude: "tuple[str, ...] | list[str]" = (),
) -> GetReport:
    """Restore every path of a record file under *destination*; nothing already there with other bytes is touched.

    With *dry_run* nothing is changed (no folder, no download, not even the cache): ``restored`` counts what would be
    fetched.  *exclude* patterns leave paths of the record out.
    """
    try:
        entries = record.parse(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read the record file {record_path}: {exc}") from None
    except record.RecordError as exc:
        raise StoreError(f"{record_path}: {exc}") from None
    kept = [e for e in entries if not excluded_by(e.path, exclude)]
    report = GetReport(excluded=len(entries) - len(kept), dry_run=dry_run)
    entries = kept
    # The guards the snapshot restore already has (SNAPSHOT.md): names this volume refuses, a link on the way, and paths past
    # Windows' 260 characters.  Nothing is written through a link, and a name the volume would refuse is reported, not half-made.
    from ..snapshot import payload_restore, restore_names
    dest = Path(os.path.abspath(destination))
    if not dry_run:
        payload_restore.extended_path(dest).mkdir(parents=True, exist_ok=True)
    colliding = _case_collisions(entries, dest, dry_run)
    name_problem = (restore_names.destination_name_problem(dest) if not dry_run and dest.is_dir()
                    else (restore_names.destination_name_problem(dest.parent) if os.name == "nt" else None))

    def link_on_the_way(rel: str) -> "str | None":
        here = dest
        for part in rel.split("/")[:-1]:
            here = here / part
            if is_link_or_junction(payload_restore.extended_path(here)):
                return f"{part} is a link; nothing is written through a link"
        return None

    workers = _Workers(base, jobs)

    def one(entry: record.Entry) -> "tuple[str, str | None]":
        if entry.path in colliding:
            return "problem", f"{entry.path}: {colliding[entry.path]}; not restored"
        reason = (name_problem(entry.path) if name_problem is not None else None) or link_on_the_way(entry.path)
        if reason:
            return "problem", f"{entry.path}: {reason}; not restored"
        target = payload_restore.extended_path(dest / entry.path)
        if target.exists() or target.is_symlink():
            if _in_place(target, entry):
                return "in place", None
            return "problem", f"{entry.path}: something else is already there; not overwritten"
        if dry_run:
            return "restored", None          # it would be fetched
        worker = workers.get()
        # A name no one else can have: the temporary is deleted on every path out, so it must be ours alone
        # (a user's `<name>.sfblob-part` was deleted by a predictable name, round 5).
        # A fixed length, whatever the name: ``<name>.sfblob-part-..`` made a 235-character name too long to stage.
        temp = target.with_name(f".sfblob-part-{os.urandom(8).hex()}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            worker.get_to(entry.ref, temp)
            if not _place(temp, target):          # appeared while we fetched: never replace it
                return "problem", f"{entry.path}: something appeared there while it was fetched; not overwritten"
            size = target.stat().st_size
            if size != entry.size:
                # The hash proved the object; the record's size is the weaker witness and does not unmake it.
                report.warnings.append(f"{entry.path}: the record says {entry.size} bytes, the object is {size}; restored")
            return "restored", None
        except (StoreError, OSError) as exc:
            return "problem", f"{entry.path}: {exc}"
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    if jobs <= 1:
        outcomes = [one(e) for e in entries]
    else:
        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            try:
                outcomes = list(pool.map(one, entries))
            finally:
                workers.close()
    for kind, problem in outcomes:
        if kind == "restored":
            report.restored += 1
        elif kind == "in place":
            report.in_place += 1
        elif problem:
            report.problems.append(problem)
    return report
