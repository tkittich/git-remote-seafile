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
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import record, store
from .store import Store, StoreError


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


def _walk(directory: Path) -> "tuple[list[Path], list[str]]":
    files: "list[Path]" = []
    skipped: "list[str]" = []
    for base, dirs, names in os.walk(directory, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            path = Path(base) / name
            rel = path.relative_to(directory).as_posix()
            if path.is_symlink() or not path.is_file():
                skipped.append(f"{rel}: not a regular file")
            else:
                files.append(path)
        for name in list(dirs):
            if (Path(base) / name).is_symlink():
                dirs.remove(name)
                skipped.append(f"{(Path(base) / name).relative_to(directory).as_posix()}: a link to a folder")
    return files, skipped


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
    except OSError:          # a volume without hard links (FAT, some shares): check, then replace
        if target.exists() or target.is_symlink():
            return False
        os.replace(temp, target)
        return True
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
    files, skipped = _walk(directory)
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
    files = kept
    if not dry_run:
        base.check_root(create=True)       # once, before any thread: two first puts would race on the marker

    def one(path: Path) -> "tuple[Path, store.Put | None, str | None]":
        worker = base if jobs <= 1 else base.clone()
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
                results = list(pool.map(one, files))
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
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)

    def one(entry: record.Entry) -> "tuple[str, str | None]":
        target = destination / entry.path
        if target.exists() or target.is_symlink():
            if _in_place(target, entry):
                return "in place", None
            return "problem", f"{entry.path}: something else is already there; not overwritten"
        if dry_run:
            return "restored", None          # it would be fetched
        worker = base if jobs <= 1 else base.clone()
        # A name no one else can have: the temporary is deleted on every path out, so it must be ours alone
        # (a user's `<name>.sfblob-part` was deleted by a predictable name, round 5).
        temp = target.with_name(f"{target.name}.sfblob-part-{os.urandom(4).hex()}")
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
            temp.unlink(missing_ok=True)

    if jobs <= 1:
        outcomes = [one(e) for e in entries]
    else:
        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            outcomes = list(pool.map(one, entries))
    for kind, problem in outcomes:
        if kind == "restored":
            report.restored += 1
        elif kind == "in place":
            report.in_place += 1
        elif problem:
            report.problems.append(problem)
    return report
