"""refs.py - Recursive discovery of the remote ref namespace on Seafile.

Seafile stores a git ref as a plain file at ``<repo_path>/<ref_name>``.  Git
permits slashes inside ref names ("refs/heads/feature/auth"), and Seafile
represents the intermediate components as nested directories.

A single-level directory listing therefore silently omits every branch or tag
whose name contains a slash.  That omission is not cosmetic:

  * a ref that is never advertised is never fetched, so a clone silently
    produces an incomplete repository;
  * the compactor mirrors refs into a scratch bare repository to decide which
    objects are reachable -- a nested ref it cannot see makes its objects look
    like garbage, and ``git repack -a -d`` deletes them permanently.

Every consumer that enumerates remote refs must go through :func:`iter_refs`.
"""

from __future__ import annotations

import concurrent.futures
import re
import sys
from typing import Iterator

from .client import SeafileClient

#: Ref namespaces the helper tracks.  Kept as a tuple so iteration order is
#: stable (heads before tags), which makes the advertised listing deterministic.
REF_NAMESPACES = ("refs/heads", "refs/tags")

_INVALID_REF_PATTERN = re.compile(r"[\s\x00-\x1f\x7f~^:?*\[\\@]|\.\.|//|\.lock$")


def iter_refs(
    client: SeafileClient,
    repo_id: str,
    base_path: str,
    namespace: str,
    max_workers: int = 8,
) -> Iterator[tuple[str, str]]:
    """Yield ``(ref_name, sha)`` for every ref under ``namespace``.

    ``namespace`` is a git ref prefix such as ``refs/heads``.  The walk is
    recursive, so a nested ref is yielded under its full name
    (``refs/heads/feature/auth``), matching what Git expects to see on the
    wire.

    Entries whose stored content is empty are skipped. Ref file downloads are
    parallelized concurrently across a worker pool up to ``max_workers`` for
    fast enumeration across network latencies, yielding results in deterministic
    sorted order.
    """
    ns = namespace.strip("/")
    root = f"{base_path.rstrip('/')}/{ns}"
    # Depth-first walk.  The stack holds (remote_dir, ref_prefix) pairs, so a
    # directory contributes its own name to the ref name of everything below it.
    stack: list[tuple[str, str]] = [(root, "")]
    ref_entries: list[tuple[str, str]] = []

    while stack:
        dir_path, prefix = stack.pop()
        for entry in client.list_dir(repo_id, dir_path):
            name = entry.get("name") or ""
            if not name:
                continue
            kind = entry.get("type")
            if kind == "dir":
                stack.append((f"{dir_path}/{name}", f"{prefix}{name}/"))
            elif kind == "file":
                ref_name = f"{ns}/{prefix}{name}"
                if _INVALID_REF_PATTERN.search(ref_name):
                    sys.stderr.write(f"Warning: ignoring malformed ref name '{ref_name}'\n")
                    sys.stderr.flush()
                    continue
                ref_entries.append((ref_name, f"{dir_path}/{name}"))

    if not ref_entries:
        return

    # Sort entries by ref name for deterministic output order
    ref_entries.sort(key=lambda item: item[0])

    def _fetch_ref(item: tuple[str, str]) -> tuple[str, str | None]:
        ref_name, ref_path = item
        sha = client.get_file_text(repo_id, ref_path)
        return ref_name, sha.strip() if sha else None

    if len(ref_entries) == 1 or max_workers <= 1:
        for item in ref_entries:
            ref_name, sha = _fetch_ref(item)
            if sha:
                yield ref_name, sha
    else:
        workers = min(max_workers, len(ref_entries))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for ref_name, sha in executor.map(_fetch_ref, ref_entries):
                if sha:
                    yield ref_name, sha


__all__ = ["REF_NAMESPACES", "iter_refs"]
