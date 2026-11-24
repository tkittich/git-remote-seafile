"""The record file: ``path -> reference`` for a folder, one sorted line per entry (STORE.md D3).

Meant to be committed to git.  One entry per line and sorted by path, so two branches that add different files merge
line by line, and two that give one path different references are a visible git conflict, not a silent loss.
A line is ``<path>\t<size>\t<reference>``: the path in NFC with ``/`` separators, the size in bytes, the full reference.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from . import refs


class RecordError(ValueError):
    """A record file or an entry that cannot be read or written."""


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    ref: refs.Ref


def normalise_path(path: str) -> str:
    """The form a path is stored and compared in: NFC, ``/`` separators, relative, no ``..``, no control characters."""
    text = unicodedata.normalize("NFC", path.replace("\\", "/"))
    if not text or text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise RecordError(f"not a relative path: {path!r}")
    parts = text.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise RecordError(f"not a plain relative path: {path!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise RecordError(f"a control character in a path: {path!r}")
    return text


def render(entries: "list[Entry]") -> str:
    """The text of a record file: sorted by path, one line each, a path stored once."""
    seen: "dict[str, Entry]" = {}
    for entry in entries:
        key = normalise_path(entry.path)
        normal = Entry(key, entry.size, refs.full(str(entry.ref)))
        if key in seen and seen[key] != normal:
            raise RecordError(f"two different entries for {key!r}")
        seen[key] = normal
    lines = [f"{e.path}\t{e.size}\t{e.ref}" for e in sorted(seen.values(), key=lambda e: e.path.encode("utf-8"))]
    return "".join(line + "\n" for line in lines)


def parse(text: str) -> "list[Entry]":
    """The entries of a record file; a bad line, a duplicated path or a path that is not normal is an error that names it."""
    entries: "dict[str, Entry]" = {}
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        fields = raw.split("\t")
        if len(fields) != 3:
            raise RecordError(f"line {number}: expected <path>, <size>, <reference> separated by tabs")
        path, size_text, ref_text = fields
        if not size_text.isdigit():
            raise RecordError(f"line {number}: the size is not a whole number of bytes: {size_text!r}")
        try:
            key = normalise_path(path)
            ref = refs.full(ref_text)
        except (RecordError, refs.RefError) as exc:
            raise RecordError(f"line {number}: {exc}") from None
        if key != path:
            raise RecordError(f"line {number}: the path is not in normal form (NFC, '/'): {path!r}")
        if key in entries:
            raise RecordError(f"line {number}: {key!r} appears twice")
        entries[key] = Entry(key, int(size_text), ref)
    return list(entries.values())
