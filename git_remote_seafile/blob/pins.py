"""Pins: what keeps an object from garbage collection (STORE.md D6).

A pin is a **new write-once file** ``pins/<pin>/<random hex>.refs``, one reference a line, never rewritten: Seafile has no
atomic read-modify-write (two writers of one pin file left one reference, measured), so a pin is the union of every file in
its folder, conflict copies included.  ``unpin`` removes the folder.  A pin that cannot be read or is not valid **stops**
whoever reads it (``gc``), it never counts as "no pin".

A writer lists the object *after* its pin is readable (``Store.put(..., pin=)`` does), which is what lets ``gc`` (0.12) rename
an object away, re-read the pins, and put it back without a clock.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from ..client import SeafileAPIError
from . import refs
from .store import StoreError

if TYPE_CHECKING:
    from .store import Store

PINS = "pins"
#: A pin name is a plain file name (R7): it becomes a folder, so no separator, no dot-only name, no control character.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")
MAX_PIN_FILE_BYTES = 8 * 1024 * 1024


def check_name(name: str) -> str:
    if name == "ls":
        raise StoreError("`ls` is not a pin name: `pin ls` lists the pins")
    if not _NAME.fullmatch(name) or name.endswith((" ", ".")):
        raise StoreError(f"{name!r} is not a pin name: use letters, digits, '.', '_', '-' or a space, starting with a letter "
                         "or digit, up to 64 characters")
    return name


def pin_folder(store: "Store", name: str) -> str:
    return f"{store.root}/{PINS}/{check_name(name)}"


def write(store: "Store", name: str, wanted: "list[refs.Ref]") -> str:
    """Write one pin file naming *wanted* and prove it is readable; returns the file's name."""
    folder = pin_folder(store, name)
    store.require_writable()
    full = sorted({str(r) for r in wanted})
    if not full:
        raise StoreError("nothing to pin")
    body = "".join(f"{r}\n" for r in full).encode("ascii")
    filename = f"{os.urandom(8).hex()}.refs"
    try:
        store.client.upload_file(store.repo_id, folder, filename, body, replace=False)
        back = store.client.get_file_bytes(store.repo_id, f"{folder}/{filename}", max_bytes=MAX_PIN_FILE_BYTES)
    except SeafileAPIError as exc:
        raise StoreError(f"could not write the pin {name!r}: {exc}") from None
    if back != body:
        raise StoreError(f"the pin {name!r} was written but could not be read back as written; run again")
    return filename


def remove(store: "Store", name: str) -> bool:
    """Remove a pin's folder; ``False`` when there was none (a pin another run removed is not an error)."""
    folder = pin_folder(store, name)
    store.require_writable()
    present = [e for e in store._list(f"{store.root}/{PINS}") if e.get("name") == name]
    if not present:
        return False
    if present[0].get("type") != "dir":
        raise StoreError(f"{PINS}/{name} is not a pin folder; it is left alone")
    try:
        store.client.delete_entry(store.repo_id, folder)
    except SeafileAPIError as exc:
        raise StoreError(f"could not remove the pin {name!r}: {exc}") from None
    return True


def parse(text: str, where: str) -> "list[refs.Ref]":
    found: "list[refs.Ref]" = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            ref = refs.parse(line.strip())
        except refs.RefError as exc:
            raise StoreError(f"{where}, line {number}: {exc}") from None
        if not ref.is_full:
            raise StoreError(f"{where}, line {number}: a pin names full references, not {line.strip()!r}")
        found.append(ref)
    return found


def read_all(store: "Store") -> "dict[str, list[refs.Ref]]":
    """Every pin with the references it names.  A file that is listed but cannot be read or is not valid raises."""
    result: "dict[str, list[refs.Ref]]" = {}
    for folder in sorted(store._list(f"{store.root}/{PINS}"), key=lambda e: str(e.get("name", ""))):
        name = str(folder.get("name", ""))
        if folder.get("type") != "dir":
            continue          # not ours: reported by the caller as foreign, never read
        wanted: "list[refs.Ref]" = []
        for entry in store._list(f"{store.root}/{PINS}/{name}"):
            if entry.get("type") == "dir":
                continue
            where = f"{PINS}/{name}/{entry.get('name')}"
            try:
                data = store.client.get_file_bytes(store.repo_id, f"{store.root}/{where}", max_bytes=MAX_PIN_FILE_BYTES)
            except SeafileAPIError as exc:
                raise StoreError(f"cannot read the pin file {where} ({exc}); nothing is decided from a pin that cannot be read") from None
            if data is None:
                raise StoreError(f"the pin file {where} is listed but cannot be read; nothing is decided from it")
            listed = entry.get("size")
            if isinstance(listed, int) and not isinstance(listed, bool) and listed != len(data):
                # A proxy that cut the body at a line end would give a valid pin with fewer references: fail-open for gc.
                raise StoreError(f"the pin file {where} read back at {len(data)} bytes, the listing says {listed}; "
                                 "nothing is decided from it")
            try:
                found = parse(data.decode("utf-8"), where)
            except UnicodeDecodeError:
                raise StoreError(f"the pin file {where} is not text; nothing is decided from it") from None
            if not found:
                # ``write`` never writes an empty pin, so an empty body is a read that lost its bytes (a proxy, a
                # truncated reply): reading it as "pins nothing" would let a later gc delete what the pin keeps.
                raise StoreError(f"the pin file {where} is empty; nothing is decided from it")
            wanted.extend(found)
        result[name] = wanted
    return result
