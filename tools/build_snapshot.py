#!/usr/bin/env python3
"""Build ``git_remote_seafile/snapshot_tool.py`` from the parts in ``tools/_snapshot/``.

Why this exists
===============
The snapshot tool is distributed as **one file**: a user can ``curl`` it, drop it
next to nothing, and run it.  That property is worth keeping.  A 3,000-line
single source file is not worth keeping -- it is past what a reader can hold, and
every change lands in the same place.

So the two are separated.  The *source* is a handful of cohesive parts in
``tools/_snapshot/`` (``10_setup.py``, ``20_gitrun.py``, ...), and the file users
run is **generated** by concatenating them in name order.  Nothing else happens:
no import rewriting, no minification, no bundler.  A part is an ordinary slice of
the module, which is what keeps this auditable -- the generated file is exactly
the parts, in order, and ``--check`` proves it.

Rules for the parts
===================
* They are *fragments of one module*, not importable modules.  They share a
  flat namespace and contain no ``from ._x import y``.
* All imports live in ``10_setup.py``.
* A name is defined in exactly one part (``tests/test_snapshot_build.py`` checks
  the generated file for duplicate top-level definitions).
* Part order is definition order: anything evaluated at import time (a default
  argument, a module-level constant) must come from an earlier part.

Usage::

    python tools/build_snapshot.py            # (re)write git_remote_seafile/snapshot_tool.py
    python tools/build_snapshot.py --check    # exit 1 if the generated file is stale

The generated file is committed, so a clone or the sdist runs without a build
step, and the test suite fails when it drifts from the parts.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
PARTS_DIR = _TOOLS / "_snapshot"
#: The generated module is part of the package, so it ships in the wheel and
#: runs as `git-seafile-snapshot` / `git-remote-seafile snapshot`.  The file
#: tools/seafile_snapshot.py is a thin wrapper around it.
OUTPUT = _TOOLS.parent / "git_remote_seafile" / "snapshot_tool.py"


def part_paths() -> list[Path]:
    """The parts, in the order they are concatenated (their names sort)."""
    return sorted(p for p in PARTS_DIR.glob("*.py") if p.is_file())


def build() -> str:
    """The generated module text, with ``\\n`` line endings."""
    parts = part_paths()
    if not parts:
        raise SystemExit(f"no parts found in {PARTS_DIR}")
    chunks: list[str] = []
    for path in parts:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        if not text.endswith("\n"):
            text += "\n"
        chunks.append(text)
    return "".join(chunks)


def _normalised(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if git_remote_seafile/snapshot_tool.py differs from the parts",
    )
    args = parser.parse_args(argv)

    generated = build()
    if args.check:
        current = _normalised(OUTPUT) if OUTPUT.exists() else ""
        if current == generated:
            print(f"{OUTPUT.name} is up to date ({len(part_paths())} parts)")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(), generated.splitlines(),
            fromfile=f"{OUTPUT.name} (committed)", tofile=f"{OUTPUT.name} (from parts)",
            lineterm="", n=1,
        )
        for index, line in enumerate(diff):
            if index >= 40:
                print("... (diff truncated)")
                break
            print(line)
        print(
            f"\n{OUTPUT.name} is stale: run `python tools/build_snapshot.py` "
            f"and commit the result.",
            file=sys.stderr,
        )
        return 1

    OUTPUT.write_text(generated, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT} from {len(part_paths())} parts ({len(generated.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
