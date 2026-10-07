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

from typing import Iterator

from .client import SeafileClient

#: Ref namespaces the helper tracks.  Kept as a tuple so iteration order is
#: stable (heads before tags), which makes the advertised listing deterministic.
REF_NAMESPACES = ("refs/heads", "refs/tags")


def iter_refs(
    client: SeafileClient,
    repo_id: str,
    base_path: str,
    namespace: str,
) -> Iterator[tuple[str, str]]:
    """Yield ``(ref_name, sha)`` for every ref under ``namespace``.

    ``namespace`` is a git ref prefix such as ``refs/heads``.  The walk is
    recursive, so a nested ref is yielded under its full name
    (``refs/heads/feature/auth``), matching what Git expects to see on the
    wire.

    Entries whose stored content is empty are skipped, mirroring the previous
    single-level behaviour.
    """
    ns = namespace.strip("/")
    root = f"{base_path.rstrip('/')}/{ns}"
    # Depth-first walk.  The stack holds (remote_dir, ref_prefix) pairs, so a
    # directory contributes its own name to the ref name of everything below it.
    stack: list[tuple[str, str]] = [(root, "")]
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
                sha = client.get_file_text(repo_id, f"{dir_path}/{name}")
                if sha:
                    yield f"{ns}/{prefix}{name}", sha.strip()


__all__ = ["REF_NAMESPACES", "iter_refs"]
