"""Folders: ``put -r DIR --record FILE`` and ``get --from FILE --to DIR`` (STORE.md D3, "Ease of use").

A folder is uploaded file by file and its ``path -> reference`` list written as a record file (record.py: one sorted
line per entry, meant for git).  ``get --from`` restores every path, verified.  Both are safe to run again: an upload
of an object the store holds does nothing (R5), and a path already in place with the right bytes is left alone.  A path
that holds *other* bytes is never overwritten (R4): it is reported and the run fails, after everything else was done.
"""

from __future__ import annotations

import concurrent.futures
import os
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
    problems: "list[str]" = field(default_factory=list)


@dataclass
class GetReport:
    restored: int = 0
    in_place: int = 0
    problems: "list[str]" = field(default_factory=list)


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
    temp = path.with_name(path.name + ".sfblob-tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def put_tree(
    base: Store, directory: Path, record_path: "Path | None", jobs: int = 1,
    on_file: "Callable[[str, store.Put], None] | None" = None,
) -> PutReport:
    """Upload every regular file under *directory*; write the record file when one is named."""
    if not directory.is_dir():
        raise StoreError(f"{directory} is not a folder")
    files, skipped = _walk(directory)
    report = PutReport(skipped=skipped)
    base.check_root(create=True)           # once, before any thread: two first puts would race on the marker

    def one(path: Path) -> "tuple[Path, store.Put | None, str | None]":
        worker = base if jobs <= 1 else base.clone()
        try:
            return path, worker.put(path), None
        except StoreError as exc:
            return path, None, str(exc)

    if jobs <= 1:
        results = [one(p) for p in files]
    else:
        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            results = list(pool.map(one, files))
    for path, done, problem in results:
        rel = path.relative_to(directory).as_posix()
        if problem is not None or done is None:
            report.problems.append(f"{rel}: {problem}")
            continue
        report.entries.append(record.Entry(rel, done.size, done.ref))
        report.uploaded += 1 if done.uploaded else 0
        report.present += 0 if done.uploaded else 1
        if on_file is not None:
            on_file(rel, done)
    if record_path is not None and not report.problems:
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


def get_tree(base: Store, record_path: Path, destination: Path, jobs: int = 1) -> GetReport:
    """Restore every path of a record file under *destination*; nothing already there with other bytes is touched."""
    try:
        entries = record.parse(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read the record file {record_path}: {exc}") from None
    except record.RecordError as exc:
        raise StoreError(f"{record_path}: {exc}") from None
    report = GetReport()
    destination.mkdir(parents=True, exist_ok=True)

    def one(entry: record.Entry) -> "tuple[str, str | None]":
        target = destination / entry.path
        if target.exists() or target.is_symlink():
            if _in_place(target, entry):
                return "in place", None
            return "problem", f"{entry.path}: something else is already there; not overwritten"
        worker = base if jobs <= 1 else base.clone()
        temp = target.with_name(target.name + ".sfblob-part")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            worker.get_to(entry.ref, temp)
            if temp.stat().st_size != entry.size:
                temp.unlink(missing_ok=True)
                return "problem", f"{entry.path}: the object is {entry.ref} but the record says {entry.size} bytes"
            if target.exists() or target.is_symlink():          # appeared while we fetched: never replace it
                temp.unlink(missing_ok=True)
                return "problem", f"{entry.path}: something appeared there while it was fetched; not overwritten"
            os.replace(temp, target)
            return "restored", None
        except StoreError as exc:
            temp.unlink(missing_ok=True)
            return "problem", f"{entry.path}: {exc}"
        except OSError as exc:
            temp.unlink(missing_ok=True)
            return "problem", f"{entry.path}: {exc}"

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
