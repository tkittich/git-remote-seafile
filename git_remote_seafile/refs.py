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

from .client import SeafileAPIError, SeafileClient

#: Ref namespaces the helper tracks.  Kept as a tuple so iteration order is
#: stable (heads before tags), which makes the advertised listing deterministic.
REF_NAMESPACES = ("refs/heads", "refs/tags")

#: A ref file is an object id and a newline (at most 65 bytes); anything near
#: this is not one.
_MAX_REF_FILE_BYTES = 4096

_INVALID_REF_CHARS = re.compile(r"[\s\x00-\x1f\x7f~^:?*\[\\]|\.\.|//|\.lock$|@\{")


#: How Seafile names the loser of a concurrent write to one path.  Measured on a
#: real server (CE 13.0.28): twelve simultaneous ``replace=1`` uploads to one
#: path all returned success, and left the path itself plus six copies named
#: ``<name> (SFConflict <user> <date>)`` beside it.
CONFLICT_MARKER = " (SFConflict "


def is_conflict_copy(name: str) -> bool:
    """True for a file Seafile created to hold the loser of a concurrent write."""
    return CONFLICT_MARKER in name


def conflict_copies_of(entries: list, filename: str) -> list[str]:
    """Names in a directory listing that are conflict copies of *filename*.

    Seafile puts the marker before the extension (``notes (SFConflict u 2026-10-12).txt``),
    and a ref such as ``v1.0.0`` or ``release-2.1`` has one as far as it is
    concerned, so both ``<name> (SFConflict ...`` and ``<stem> (SFConflict ...``
    are matched.  A false positive costs a warning; a miss was a silent
    conflict.
    """
    prefixes = {filename + CONFLICT_MARKER}
    stem, dot, _ext = filename.rpartition(".")
    if dot and stem:
        prefixes.add(stem + CONFLICT_MARKER)
    return sorted(
        str(e.get("name"))
        for e in entries
        if isinstance(e, dict) and str(e.get("name", "")).startswith(tuple(prefixes))
    )


def is_valid_ref_name(ref_name: str) -> bool:
    """Return True if ref_name is a valid Git ref name per git-check-ref-format."""
    # Only the bare refname "@" (an alias for HEAD) and the "@{" sequence are
    # forbidden; an @ inside a component -- even "refs/heads/@" itself -- is
    # legal per git-check-ref-format, so it must not be rejected here.
    if not ref_name or ref_name == "@" or _INVALID_REF_CHARS.search(ref_name):
        return False
    parts = ref_name.split("/")
    if any(p.startswith(".") or p.endswith(".lock") for p in parts):
        return False
    if ref_name.endswith("."):
        return False
    # git-check-ref-format: no leading or trailing slash, and at least one slash (the
    # one-level form is only legal under --allow-onelevel, which no ref this
    # helper writes is).  Found by differential testing against git itself.
    if ref_name.startswith("/") or ref_name.endswith("/") or "/" not in ref_name:
        return False
    return True


def _listing(
    client: SeafileClient, repo_id: str, path: str, *, known_dir: bool, strict: bool
) -> list:
    """List *path*; a directory known to exist that lists as missing is not "empty".

    ``list_dir`` answers 404 with ``[]``, which is right for a namespace nobody
    has created yet and wrong for a directory the parent listing just showed:
    there, an empty answer is a transient failure (or a directory removed under
    us), and treating it as "no refs" is how a branch silently falls out of an
    advertisement or out of ``gc``'s view of what is reachable.  An empty answer
    for a known directory is therefore asked again, and a confirmed 404 is an
    error when *strict* and a warning otherwise.
    """
    entries = client.list_dir(repo_id, path)
    if entries or not known_dir:
        return entries
    if client.dir_exists(repo_id, path) is False:
        message = (
            f"{path} was listed by its parent directory but is now missing (404); "
            "the refs under it cannot be read"
        )
        if strict:
            raise SeafileAPIError(message)
        sys.stderr.write(f"Warning: {message}\n")
        sys.stderr.flush()
        return []
    return client.list_dir(repo_id, path)


def iter_refs(
    client: SeafileClient,
    repo_id: str,
    base_path: str,
    namespace: str,
    max_workers: int = 8,
    *,
    strict: bool = False,
) -> Iterator[tuple[str, str]]:
    """Yield ``(ref_name, sha)`` for every ref under ``namespace``.

    ``namespace`` is a git ref prefix such as ``refs/heads``.  The walk is
    recursive, so a nested ref is yielded under its full name
    (``refs/heads/feature/auth``), matching what Git expects to see on the
    wire.

    A ref whose content cannot be read is never dropped *silently*: it is
    reported on stderr, and with ``strict`` it is an error.  ``gc`` passes
    ``strict`` because there an omitted ref means its objects are deleted;
    advertising for a fetch can afford a warning.  Ref file downloads are
    parallelized concurrently across a worker pool up to ``max_workers`` for
    fast enumeration across network latencies, yielding results in deterministic
    sorted order.
    """
    ns = namespace.strip("/")
    root = f"{base_path.rstrip('/')}/{ns}"
    # A namespace root the repository's own ``refs`` directory lists is known to
    # exist.  Asked only when strict: it is one more request, worth it where the
    # answer decides what is deleted.
    root_known = False
    if strict and "/" in ns:
        parent, _, leaf = root.rpartition("/")
        root_known = any(
            isinstance(e, dict) and e.get("name") == leaf and e.get("type") == "dir"
            for e in client.list_dir(repo_id, parent)
        )
    # Depth-first walk.  The stack holds (remote_dir, ref_prefix) pairs, so a
    # directory contributes its own name to the ref name of everything below it.
    stack: list[tuple[str, str, bool]] = [(root, "", root_known)]
    ref_entries: list[tuple[str, str]] = []

    while stack:
        dir_path, prefix, known = stack.pop()
        for entry in _listing(client, repo_id, dir_path, known_dir=known, strict=strict):
            name = entry.get("name") or ""
            if not name:
                continue
            kind = entry.get("type")
            if kind == "dir":
                stack.append((f"{dir_path}/{name}", f"{prefix}{name}/", True))
            elif kind == "file":
                ref_name = f"{ns}/{prefix}{name}"
                size = entry.get("size")
                if isinstance(size, int) and not isinstance(size, bool) and size > _MAX_REF_FILE_BYTES:
                    # A ref holds one object id.  Reading a file this large
                    # only to reject it as "not a SHA" is the memory cost a
                    # hostile library is after.
                    message = (
                        f"the ref {ref_name} is {size} bytes; a ref holds one object id, so it "
                        "is not read"
                    )
                    if strict:
                        raise SeafileAPIError(message)
                    sys.stderr.write(f"Warning: {message}\n")
                    sys.stderr.flush()
                    continue
                if is_conflict_copy(name):
                    # Not a malformed name: a *symptom*.  Two clients wrote one
                    # ref at once and the server kept both.  Say what it is and
                    # what to do, because "malformed ref name" sends the reader
                    # looking for a typo.
                    sys.stderr.write(
                        f"Warning: '{ref_name}' is a Seafile conflict copy -- two clients wrote the "
                        f"same ref at once. It is ignored; delete it once you have decided which "
                        f"commit is right (RUNBOOK.md, \"Conflict Artifacts\").\n"
                    )
                    sys.stderr.flush()
                    continue
                if not is_valid_ref_name(ref_name):
                    sys.stderr.write(f"Warning: ignoring malformed ref name '{ref_name}'\n")
                    sys.stderr.flush()
                    continue
                ref_entries.append((ref_name, f"{dir_path}/{name}"))
            else:
                # Seafile returns only dir/file today; an unknown type (or a
                # missing field from an unexpected server response) must not
                # vanish silently -- a skipped entry here is an unadvertised
                # ref, which is how silent ref loss starts.
                sys.stderr.write(
                    f"Warning: ignoring entry '{name}' of unknown type '{kind}' under {dir_path}\n"
                )
                sys.stderr.flush()

    if not ref_entries:
        return

    # Sort entries by ref name for deterministic output order
    ref_entries.sort(key=lambda item: item[0])

    def _fetch_ref(item: tuple[str, str]) -> tuple[str, str | None]:
        ref_name, ref_path = item
        sha = client.get_file_text(repo_id, ref_path)
        return ref_name, sha.strip() if sha else None

    def _unreadable(ref_name: str) -> None:
        message = (
            f"the ref {ref_name} is listed but its content could not be read "
            "(missing or empty)"
        )
        if strict:
            raise SeafileAPIError(message)
        sys.stderr.write(f"Warning: {message}; it is not advertised\n")
        sys.stderr.flush()

    if len(ref_entries) == 1 or max_workers <= 1:
        for item in ref_entries:
            ref_name, sha = _fetch_ref(item)
            if sha:
                yield ref_name, sha
            else:
                _unreadable(ref_name)
    else:
        workers = min(max_workers, len(ref_entries))
        # The pool shares one requests.Session.  urllib3's connection pool is
        # thread-safe and the only session mutation during these reads is the
        # Date-header hook appending to a list (atomic under the GIL), but
        # requests itself does not guarantee Session thread safety -- keep the
        # concurrent work strictly read-only; do not widen it to calls that
        # mutate session state (cookies, auth, headers).
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for ref_name, sha in executor.map(_fetch_ref, ref_entries):
                if sha:
                    yield ref_name, sha
                else:
                    _unreadable(ref_name)


__all__ = [
    "CONFLICT_MARKER",
    "REF_NAMESPACES",
    "conflict_copies_of",
    "is_conflict_copy",
    "is_valid_ref_name",
    "iter_refs",
]
