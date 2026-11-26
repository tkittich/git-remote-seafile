"""``gc`` in this version: a **report**, and nothing else (STORE.md D3, D6; removal of objects is a later version).

It reads every pin (failing closed on one it cannot read), lists the objects, and says what no pin names, which upload
temporaries and duplicate copies exist, and what it does not recognise.  It writes nothing and deletes nothing, so it can be
run at any time, by anyone who can read the library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import pins
from .store import MARKER

if TYPE_CHECKING:
    from .store import Store

_MARKER_COPY = re.compile(re.escape(MARKER) + r" \((\d+|SFConflict [^)]*)\)")
_OBJECT = re.compile(r"[0-9a-f]{64}")
_TEMPORARY = re.compile(r"([0-9a-f]{64})\.part-[0-9a-f]+")
_COPY = re.compile(r"([0-9a-f]{64}) \((\d+|SFConflict [^)]*)\)")


@dataclass
class Report:
    objects: "dict[str, int]" = field(default_factory=dict)         # hex -> size
    pinned: "set[str]" = field(default_factory=set)
    pins: "dict[str, int]" = field(default_factory=dict)            # pin name -> references
    unused: "list[str]" = field(default_factory=list)
    temporaries: "list[tuple[str, int]]" = field(default_factory=list)
    copies: "list[tuple[str, int]]" = field(default_factory=list)
    foreign: "list[str]" = field(default_factory=list)
    missing: "list[str]" = field(default_factory=list)              # named by a pin, not in the store

    def bytes_of(self, hexes: "list[str]") -> int:
        return sum(self.objects[h] for h in hexes)


def scan(store: "Store") -> Report:
    store.check_root(create=False)
    report = Report()
    held = pins.read_all(store)
    for name, wanted in held.items():
        report.pins[name] = len({r.hex for r in wanted})          # as ``pin ls`` counts: a reference named twice is one
        report.pinned.update(r.hex for r in wanted)
    # D2: whatever else is in the store's own folders is reported once and left alone.
    for entry in store._list(store.root or "/"):
        name = str(entry.get("name", ""))
        if name not in ("blobs", "pins", MARKER) and not _MARKER_COPY.fullmatch(name):
            report.foreign.append(name)
    for entry in store._list(f"{store.root}/pins"):
        name = str(entry.get("name", ""))
        if entry.get("type") != "dir":
            report.foreign.append(f"pins/{name}")
        else:
            for inner in store._list(f"{store.root}/pins/{name}"):
                if inner.get("type") == "dir":
                    report.foreign.append(f"pins/{name}/{inner.get('name', '')}")
    for fan in sorted(store._list(f"{store.root}/blobs"), key=lambda e: str(e.get("name", ""))):
        fan_name = str(fan.get("name", ""))
        if fan.get("type") != "dir" or len(fan_name) != 2:
            report.foreign.append(f"blobs/{fan_name}")
            continue
        for entry in store._list(f"{store.root}/blobs/{fan_name}"):
            name, size = str(entry.get("name", "")), entry.get("size")
            size = size if isinstance(size, int) and not isinstance(size, bool) else 0
            if entry.get("type") == "dir":
                report.foreign.append(f"blobs/{fan_name}/{name}")
            elif _OBJECT.fullmatch(name) and name.startswith(fan_name):
                report.objects[name] = size
            elif _TEMPORARY.fullmatch(name):
                report.temporaries.append((name, size))
            elif _COPY.fullmatch(name):
                report.copies.append((name, size))
            else:
                report.foreign.append(f"blobs/{fan_name}/{name}")
    report.unused = sorted(h for h in report.objects if h not in report.pinned)
    report.missing = sorted(h for h in report.pinned if h not in report.objects)
    return report
