"""References: ``sha256:<64 lowercase hex>``, and short forms of at least six hex characters (STORE.md D3, D11).

A hostile string never reaches a path: only lowercase hex is accepted, nothing else (R7).  A short reference is
resolved against the names a listing of ``blobs/<first two hex>/`` returned; an unknown or ambiguous prefix is an
error that lists the candidates, never a guess, and anything that is stored (a record file, a script) is the full form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PREFIX = "sha256:"
MIN_SHORT = 6
_HEX = re.compile(r"[0-9a-f]+")


class RefError(ValueError):
    """A reference that is not one, is unknown, or is ambiguous."""


@dataclass(frozen=True)
class Ref:
    hex: str

    @property
    def is_full(self) -> bool:
        return len(self.hex) == 64

    def __str__(self) -> str:
        return PREFIX + self.hex


def parse(text: str) -> Ref:
    """``sha256:<hex>`` with 6 to 64 lowercase hex characters; the result says whether it is whole."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise RefError(f"not a reference: {text!r} (a reference is sha256:<hex>)")
    digits = text[len(PREFIX):]
    if not _HEX.fullmatch(digits):
        raise RefError(f"not a reference: {text!r} (lowercase hex only after sha256:)")
    if len(digits) > 64:
        raise RefError(f"not a reference: {text!r} (a SHA-256 is 64 hex characters)")
    if len(digits) < MIN_SHORT:
        raise RefError(f"too short to name one object: {text!r} (at least {MIN_SHORT} hex characters)")
    return Ref(digits)


def full(text: str) -> Ref:
    """A reference that must be whole (what a record file or a script stores)."""
    ref = parse(text)
    if not ref.is_full:
        raise RefError(f"not a full reference: {text!r} (64 hex characters are needed here)")
    return ref


def resolve(ref: Ref, names: "list[str]") -> Ref:
    """The one full reference a short one names among *names* (the object names of one fan-out folder)."""
    if ref.is_full:
        return ref
    found = sorted({n for n in names if len(n) == 64 and _HEX.fullmatch(n) and n.startswith(ref.hex)})
    if not found:
        raise RefError(f"no object in the store starts with {PREFIX}{ref.hex}")
    if len(found) > 1:
        shown = ", ".join(PREFIX + n for n in found[:5]) + (" ..." if len(found) > 5 else "")
        raise RefError(f"{PREFIX}{ref.hex} is ambiguous: {shown}")
    return Ref(found[0])


def fanout(ref: Ref) -> str:
    """The two hex characters of the folder ``blobs/<xx>`` holds the object (or the candidates) in."""
    return ref.hex[:2]
