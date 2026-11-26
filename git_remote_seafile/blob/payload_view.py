"""Read a git vault's **payload objects** through the blob commands, read-only (PLAN_0.12 D1a, D1b).

A payload object is ``<vault>/payload/objects/<sha256[:2]>/<sha256>`` and its content is exactly the file's bytes (PAYLOAD.md D2),
the same name and the same bytes as a blob object (``<store>/blobs/<sha256[:2]>/<sha256>``).  So the commands that only read
(``get``, ``path``, ``cat``, ``exists``, ``stat``, ``ls``, ``usage``, ``verify``) work on a vault's payload with no copy and no
upload: ``--payload-vault`` opens the store address as the vault.

It is a **separate class** with no write methods, not a flag on ``Store``: nothing here can write, delete or adopt, a vault is never
claimed as a store (STORE.md R9), and the client is never told it is a store's layout (``store_layout``), so no layout rule meant for
a store folder can reach the vault's own folders.  ``records_from_manifest`` turns a snapshot's payload manifest into a record file
(``path<TAB>size<TAB>sha256:...``), which ``get --from`` then restores through the same read-only view.
"""

from __future__ import annotations

from typing import Any

from . import record, refs
from .store import Store, StoreError

READ_ONLY = ("this is a vault's payload, opened read-only (--payload-vault): `{what}` would write to it. Use a blob store for that, "
             "or `get` the object and `put` it there")


class PayloadView(Store):
    """The payload objects of the vault at ``root``, read-only."""

    OBJECTS = "payload/objects"

    def _configure_client(self, client: Any) -> None:
        pass          # never the store's layout rules, upload-link reuse or layout root: this folder is a vault

    def check_root(self, *, create: bool) -> None:
        if self._marker_checked:
            return
        with self._lock:
            entries = self._list(self.root)
            if not any(e.get("name") == "payload" and e.get("type") == "dir" for e in entries):
                raise StoreError(f"{self.root or '/'} has no payload folder: it is not a git vault in payload mode "
                                 "(--payload-vault reads <vault>/payload/objects)")
            self._marker_checked = True

    def require_writable(self) -> None:
        raise StoreError(READ_ONLY.format(what="a write"))

    def put(self, *args: Any, **kwargs: Any) -> Any:
        raise StoreError(READ_ONLY.format(what="put"))

    def would_put(self, *args: Any, **kwargs: Any) -> Any:
        raise StoreError(READ_ONLY.format(what="put"))


def records_from_manifest(manifest: "dict[str, Any]") -> "list[record.Entry]":
    """The record entries of a payload manifest: every payload file as ``path, size, sha256``."""
    entries = []
    for item in manifest.get("files", []):
        entries.append(record.Entry(str(item["path"]), int(item["size"]), refs.full(f"sha256:{item['sha256']}")))
    return entries
