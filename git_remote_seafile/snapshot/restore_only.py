"""``restore --only PATH``: which part of a snapshot a restore writes (RESTORE_ONLY.md).

A value names a file or a folder of the snapshot **literally**: compared byte for byte, as whole path
components, with no glob and no case folding.  :func:`value_problems` checks the spelling before anything
is cloned (exit 2); :func:`select` checks the values against the snapshot after the clone and before the
checkout or any download (R3), and returns the :class:`Selection` every reader of the tree is handed.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import NamedTuple

from . import common
from . import gitrun
from . import payload_manifest
from . import payload_upload

_RESERVED = (".git", payload_upload.PAYLOAD_RESERVED_DIR)


class Value(NamedTuple):
    path: str
    folder: bool  # typed with a trailing slash


def _split(text: str) -> "tuple[str, bool]":
    folder = text.endswith("/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/"), folder


def parse(values: "list[str]") -> "tuple[list[Value], list[str]]":
    """``(the values, the problems)``: ``--only`` values that could leave the destination are refused (R4)."""
    import os
    found: list[Value] = []
    problems: list[str] = []
    for text in values:
        path, folder = _split(text)
        why = None
        if not text:
            why = "is empty"
        elif text.startswith("/") or re.match(r"^[A-Za-z]:", text) or re.match(r"^[A-Za-z]:", path) or text.startswith("\\\\"):
            why = "is an absolute path; write it relative to the snapshot's root"
        elif os.name == "nt" and "\\" in text:
            why = "uses a backslash; write the path with /"
        elif not path or any(part in ("", ".", "..") for part in path.split("/")):
            why = "does not name a file or folder of the snapshot"
        elif path.split("/")[0].casefold() in _RESERVED:
            why = f"is inside {path.split('/')[0]}, which a restore never takes from the snapshot"
        if why:
            problems.append(f"--only {text!r} {why}")
        else:
            found.append(Value(path, folder))
    return found, problems


def value_problems(values: "list[str] | None") -> "list[str]":
    return parse(values or [])[1]


def _nfc_fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _inside(value: str, path: str) -> bool:
    return path == value or path.startswith(value + "/")


class Selection:
    """The paths of one snapshot that a restore writes."""

    def __init__(self, values: "list[Value]", tree: "dict[str, str]", payload: "list[str]") -> None:
        self.values = values
        self.tree = tree                    # path -> mode, for every entry of the snapshot's git tree
        self.git_selected = {p for p in tree if self.contains_path(p)}
        self.unselected = set(tree) - self.git_selected
        self.payload_selected = [p for p in payload if self.contains_path(p)]
        self.payload_total = len(payload)

    def contains_path(self, path: str) -> bool:
        return any(_inside(value.path, path) for value in self.values)

    @property
    def file_count(self) -> int:
        return len([p for p in self.git_selected if self.tree[p] != "160000"]) + len(self.payload_selected)


def _list_tree(repo: Path, rev: str) -> "dict[str, str]":
    listing = gitrun._run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", rev],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if listing.returncode != 0:
        raise common.SnapshotError(
            f"could not list '{rev}' in {repo} (rc={listing.returncode})\n"
            f"{listing.stderr.decode('utf-8', errors='replace').strip()}"
        )
    tree: dict[str, str] = {}
    for entry in listing.stdout.split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) >= 3:
            tree[path.decode("utf-8", errors="surrogateescape")] = fields[0].decode("ascii")
    return tree


def read_blob(repo: Path, rev: str, path: str) -> "bytes | None":
    """The bytes of *path* in *rev*; ``None`` when the snapshot has no such file, an error when git cannot read it."""
    spec = f"{rev}:{path}"
    if gitrun._run(["git", "-C", str(repo), "cat-file", "-e", spec],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode != 0:
        return None
    shown = gitrun._run(["git", "-C", str(repo), "show", spec], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if shown.returncode != 0:
        raise common.SnapshotError(
            f"could not read {path} of '{rev}' in {repo} (rc={shown.returncode})\n"
            f"{shown.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return shown.stdout


def read_manifest(repo: Path, rev: str) -> "dict | None":
    """The payload manifest of *rev*, ``None`` when the snapshot has none, an error when it cannot be read."""
    raw = read_blob(repo, rev, payload_upload.PAYLOAD_MANIFEST)
    if raw is None:
        return None
    try:
        manifest = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise common.SnapshotError(f"the snapshot's payload manifest is unreadable: {exc}") from None
    return manifest


def select(repo: Path, rev: str, values: "list[str]") -> Selection:
    """Check every value against the snapshot (R3) and return what they select."""
    parsed, problems = parse(values)
    if problems:
        raise common.SnapshotError("; ".join(problems))
    tree = _list_tree(repo, rev)
    manifest = read_manifest(repo, rev)
    payload = [item["path"] for item in payload_manifest.check_manifest_shape(manifest)] if manifest else []
    paths = list(tree) + payload
    folders = {"/".join(p.split("/")[:i]) for p in paths for i in range(1, len(p.split("/")))}
    gitlinks = [p for p, mode in tree.items() if mode == "160000"]
    for value in parsed:
        if any(value.path.startswith(link + "/") for link in gitlinks):
            raise common.SnapshotError(
                f"--only {value.path!r} is inside an embedded repository; its files are not in this snapshot"
            )
        if value.folder and value.path in set(paths) - folders:
            raise common.SnapshotError(
                f"--only '{value.path}/' is a file, not a folder; write '{value.path}'"
            )
        if value.path in folders or any(_inside(value.path, p) for p in paths):
            continue
        near = sorted({c for c in [*paths, *folders] if _nfc_fold(c) == _nfc_fold(value.path)})
        hint = f" (the snapshot spells it {near[0]!r})" if near else ""
        raise common.SnapshotError(f"--only {value.path!r} matches nothing in the snapshot{hint}")
    return Selection(parsed, tree, payload)
