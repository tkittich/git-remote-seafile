"""A local log of what this machine put, so a lost reference can be found again by its file name (round 5, E2).

The store keeps no names (D12 is backlog: two writers can set one name differently), so a person who closes the terminal
holds a 71-character hash and no memory of which file it was.  This log is one writer per machine, one JSON line per
file, appended; it never goes to the server, and it is not the cache (clearing the cache keeps it).  It holds local
paths: ``--no-log`` (or ``SFBLOB_LOG=off``) writes nothing.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from pathlib import Path

from . import cache as cache_mod


def log_path(cache: "cache_mod.Cache") -> "Path | None":
    """``$SFBLOB_LOG`` (``off`` disables), else beside the cache folder, not inside it."""
    env = os.environ.get("SFBLOB_LOG")
    if env:
        return None if env.lower() == "off" else Path(env)
    return cache.root.parent / "sfblob-put-log.jsonl"


def append(cache: "cache_mod.Cache", store_url: str, rows: "list[tuple[str, int, str]]") -> None:
    """Record ``(reference, size, path)`` rows; best effort, a log that cannot be written never fails a put."""
    path = log_path(cache)
    if path is None or not rows:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    text = "".join(json.dumps({"time": now, "store": store_url, "ref": ref, "size": size, "path": where},
                              sort_keys=True) + "\n" for ref, size, where in rows)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def search(cache: "cache_mod.Cache", pattern: "str | None") -> "list[dict[str, object]]":
    """Logged puts whose path matches *pattern* (shell-style, or a plain substring), newest last."""
    path = log_path(cache)
    if path is None or not path.is_file():
        return []
    needle = (pattern or "").lower()
    wild = any(c in needle for c in "*?[")
    found: "list[dict[str, object]]" = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        where = row["path"].lower()
        if not needle or (fnmatch.fnmatchcase(where, needle) if wild else needle in where):
            found.append(row)
    return found
