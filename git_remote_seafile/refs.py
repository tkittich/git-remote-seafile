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
from typing import Callable, Iterator

from .client import SeafileAPIError, SeafileClient

#: Ref namespaces the helper tracks.  Kept as a tuple so iteration order is
#: stable (heads before tags), which makes the advertised listing deterministic.
REF_NAMESPACES = ("refs/heads", "refs/tags")

#: A ref file is an object id and a newline (at most 65 bytes); anything near
#: this is not one.
_MAX_REF_FILE_BYTES = 4096

#: How deep under a namespace the walk will go.  Git itself has no limit, but
#: the walk is driven by listings a hostile server answers, and each level is
#: one more request with a longer path; past this the nesting is not a
#: repository anyone grew by accident (REVIEW.sonnet-5.5 C2: 3,000 nested
#: listings were issued before the probe stopped the script).
_MAX_REF_DEPTH = 32

#: How many directories one walk will list.  The depth cap stops a chain, not a
#: fan-out: a server that answers every directory with three subdirectories
#: sends the walk through ~3**32 listings (REVIEW.sonnet-5.5-v0.9.14 C2, 30,000
#: listings issued before the probe was stopped).  Real repositories have a
#: directory per branch prefix -- dozens, rarely thousands.
_MAX_REF_LISTINGS = 5000

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


#: The server stores names NFC-normalised and turns a double quote into
#: ``%22`` (measured on CE 13.0.28), so two spellings of one name are one file
#: there and two refs here: pushing ``café`` silently overwrote the
#: value ``café`` held (REVIEW.sonnet-5.5 A6, reproduced on the real
#: server through the helper).  A destination must therefore already be in the
#: form the server keeps, and free of the characters it rewrites.
_UNSAFE_DEST_CHARS = re.compile(r'["\x00-\x1f\x7f]')


def is_unsafe_destination_component(component: str) -> str | None:
    """Why *component* may not be written as a ref name on the remote, or None.

    Used on *destinations* (a typed push target), never on names the remote
    already holds -- a listing cannot change what is stored, and refusing to
    advertise an existing ref would make one planted file unfetchable, which is
    a worse failure than the collision this prevents.
    """
    import unicodedata

    if _UNSAFE_DEST_CHARS.search(component):
        return "contains a character the server rewrites in a name (\" or a control character)"
    if unicodedata.normalize("NFC", component) != component:
        return "is not in Unicode Normalization Form C (the server stores names NFC, "                "so two spellings of one name are one file there)"
    if len(component.encode("utf-8")) > 255:
        return "is longer than the 255 bytes of UTF-8 the server accepts"
    return None


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
    error when *strict* and a warning otherwise.  A directory that provably
    exists and answers empty *twice* is a read failure too, not an empty
    namespace: a server that cannot list a directory it holds may be listing it
    short the next time, and every consumer of this walk decides what to
    advertise -- or what to delete -- from the answer.
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
    second = client.list_dir(repo_id, path)
    if second:
        return second
    message = f"{path} exists but twice answered empty; the refs under it cannot be read"
    if strict:
        raise SeafileAPIError(message)
    sys.stderr.write(f"Warning: {message}\n")
    sys.stderr.flush()
    return []


def iter_refs(
    client: SeafileClient,
    repo_id: str,
    base_path: str,
    namespace: str,
    max_workers: int = 8,
    *,
    strict: bool = False,
    strict_listing: bool = False,
    renew: Callable[[], None] | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield ``(ref_name, sha)`` for every ref under ``namespace``.

    *renew*, when given, is called before every directory listing and between
    batches of ref reads, never while a read is in flight.  A caller that holds
    the push lock passes its lease renewal: reading is ~33 ms a ref on a real
    server, so a few thousand refs outlast a 60 s lease that nothing renewed
    (REVIEW.sonnet-5.5-v0.9.14 B5).

    ``namespace`` is a git ref prefix such as ``refs/heads``.  The walk is
    recursive, so a nested ref is yielded under its full name
    (``refs/heads/feature/auth``), matching what Git expects to see on the
    wire.

    A ref whose content cannot be read is never dropped *silently*: it is
    reported on stderr, and with ``strict`` it is an error.  ``gc`` passes
    ``strict`` because there an omitted ref means its objects are deleted.  A
    listing for fetch passes ``strict_listing``: a directory the parent listed
    that then answers 404 is an error (the caller may retry), while a ref file
    with unusable *content* stays a warning, so one planted file cannot make a
    repository unfetchable.  Ref file downloads are
    parallelized concurrently across a worker pool up to ``max_workers`` for
    fast enumeration across network latencies, yielding results in deterministic
    sorted order.
    """
    ns = namespace.strip("/")
    root = f"{base_path.rstrip('/')}/{ns}"
    # A namespace root the repository's own ``refs`` directory lists is known to
    # exist.  Asked when strict or strict_listing: it is one more request, worth
    # it wherever the answer decides what is deleted or advertised -- the fetch
    # advertisement gains the same "an existing directory that answers empty is
    # a read failure" guard gc already had.
    root_known = False
    if (strict or strict_listing) and "/" in ns:
        parent, _, leaf = root.rpartition("/")
        root_known = any(
            isinstance(e, dict) and e.get("name") == leaf and e.get("type") == "dir"
            for e in client.list_dir(repo_id, parent)
        )
    # Depth-first walk.  The stack holds (remote_dir, ref_prefix) pairs, so a
    # directory contributes its own name to the ref name of everything below it.
    stack: list[tuple[str, str, bool, int]] = [(root, "", root_known, 0)]
    ref_entries: list[tuple[str, str]] = []
    depth_capped = False
    listings = 0

    while stack:
        dir_path, prefix, known, depth = stack.pop()
        listings += 1
        if listings > _MAX_REF_LISTINGS:
            message = (
                f"more than {_MAX_REF_LISTINGS} directories under {ns} were listed; "
                "the server's answers do not describe a repository, so the walk stops"
            )
            if strict or strict_listing:
                raise SeafileAPIError(message)
            sys.stderr.write(f"Warning: {message}; the refs not reached are not advertised\n")
            sys.stderr.flush()
            break
        if depth > _MAX_REF_DEPTH:
            if strict:
                raise SeafileAPIError(
                    f"refs nested deeper than {_MAX_REF_DEPTH} levels under {ns} cannot be "
                    "walked, so the refs below them cannot be seen"
                )
            if not depth_capped:
                sys.stderr.write(
                    f"Warning: refs nested deeper than {_MAX_REF_DEPTH} levels under "
                    f"{ns} are not walked; the deeper names are not advertised\n"
                )
                sys.stderr.flush()
                depth_capped = True
            continue
        if renew is not None:
            renew()
        for entry in _listing(
            client, repo_id, dir_path, known_dir=known, strict=strict or strict_listing
        ):
            name = entry.get("name") or ""
            if not name:
                continue
            kind = entry.get("type")
            if kind == "dir":
                stack.append((f"{dir_path}/{name}", f"{prefix}{name}/", True, depth + 1))
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
                    if strict:
                        raise SeafileAPIError(
                            f"'{ref_name}' is not a ref name git accepts, so the ref cannot be "
                            "listed; rename or remove it"
                        )
                    sys.stderr.write(f"Warning: ignoring malformed ref name '{ref_name}'\n")
                    sys.stderr.flush()
                    continue
                ref_entries.append((ref_name, f"{dir_path}/{name}"))
            else:
                # Seafile returns only dir/file today; an unknown type (or a
                # missing field from an unexpected server response) must not
                # vanish silently -- a skipped entry here is an unadvertised
                # ref, which is how silent ref loss starts.  A walk that decides
                # what is deleted (``strict``) stops instead of warning.
                if strict:
                    raise SeafileAPIError(
                        f"the entry '{name}' under {dir_path} has the unknown type '{kind}', "
                        "so a ref may be hidden behind it"
                    )
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
            if renew is not None:
                renew()
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
        #
        # The reads go out in batches so that *renew* (an upload) runs between
        # them, with no read in flight, rather than beside them on the shared
        # session.  Results still come back in sorted order.
        batch = workers * 8
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for start in range(0, len(ref_entries), batch):
                if renew is not None:
                    renew()
                for ref_name, sha in executor.map(_fetch_ref, ref_entries[start:start + batch]):
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
