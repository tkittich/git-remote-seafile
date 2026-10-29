"""library_setup.py - the one-time library chores ``doctor`` can do for you.

A push needs a library that exists and, to keep the server's disk from filling
with every pack a ``gc`` removes, a short history.  Both used to be web-UI steps
the README asked the user to remember; ``doctor`` now checks the second and can
do either on request (``--create-library``, ``--set-history``).
"""

from __future__ import annotations

from typing import Any

#: What a new library is given, and what the README recommends: long enough to
#: undo a mistaken gc, short enough that removed packs do not accumulate.
RECOMMENDED_HISTORY_DAYS = 30

#: Libraries are created by name; a UUID is an existing library's address.
_HEX = set("0123456789abcdef-")


def looks_like_library_id(name: str) -> bool:
    return len(name) == 36 and set(name.lower()) <= _HEX and name.count("-") == 4


def create_library(client: Any, name: str) -> tuple[str, bool]:
    """Create *name* with the recommended history; return ``(repo_id, history_was_set)``."""
    rid = client.create_library(name)
    return rid, client.set_history_limit(rid, RECOMMENDED_HISTORY_DAYS)


def history_finding(days: int | None) -> tuple[str, str]:
    """``(level, text)`` for a library's history setting; level is ``ok``, ``warn`` or ``skip``."""
    if not isinstance(days, int) or isinstance(days, bool):
        return "skip", ""
    if days < 0:
        return "warn", (
            "the library keeps its full history, so every pack that gc or the tail merge "
            "removes stays on the server's disk; "
            f"run doctor with --set-history {RECOMMENDED_HISTORY_DAYS} (or Library settings -> "
            "History Setting in the web UI)"
        )
    if days == 0:
        return "warn", (
            "the library keeps no history, so a mistaken gc or delete cannot be undone from the "
            f"server; run doctor with --set-history {RECOMMENDED_HISTORY_DAYS} (or Library "
            "settings -> History Setting in the web UI)"
        )
    return "ok", f"the library keeps {days} days of history"
