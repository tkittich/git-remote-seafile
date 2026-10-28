"""Server-side checks behind ``git-remote-seafile doctor <url>``.

Two questions the helper cannot answer from inside a push, both about the
*deployment* rather than the repository:

* Are there Seafile conflict copies anywhere under the repository path?  One
  under ``refs/`` stops ``gc`` for good (ROADMAP S14).
* How large a single upload does the server, or the proxy in front of it,
  accept?  The default pack cap (``seafile.gcmaxpackmib`` = 1 GiB) is one
  deployment's number; a smaller body limit turns every large push into a
  failure after the lock is taken (ROADMAP S15).

The checks take a client, so they run against the in-process stub in tests and
against a real server (and a real nginx limit) in the container workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .client import CHUNKED_UPLOAD_MIN_BYTES, UPLOAD_CHUNK_BYTES
from .refs import is_conflict_copy

MIB = 1024 * 1024

#: The smallest body tried.  A server that refuses even this has a problem that
#: is not a size limit, and the probe says so instead of recommending a cap.
PROBE_FLOOR_BYTES = MIB
#: The largest single request tried by default: one MiB under the size at which
#: the client switches to ``Content-Range`` chunks, because above it the body of
#: each request is a chunk and this probe would be measuring the wrong thing.
PROBE_CEILING_BYTES = CHUNKED_UPLOAD_MIN_BYTES - MIB
#: The search stops when the accepted and rejected sizes are this close, or
#: within 1/8 of the accepted size if that is more: 100 MiB is found to ~12 MiB
#: and 5 MiB to 1 MiB, and a body that is refused costs little to send.
PROBE_RESOLUTION_BYTES = MIB
PROBE_RESOLUTION_FRACTION = 8
#: A pack may overrun ``--max-pack-size`` by one object and carries framing, so
#: the recommendation leaves this much of the measured limit unused.
PACK_HEADROOM = 0.8
#: Directories listed by the conflict walk before it stops and says so.
CONFLICT_WALK_MAX_DIRS = 5000


def find_conflict_copies(
    client: Any, repo_id: str, repo_path: str, *, max_dirs: int = CONFLICT_WALK_MAX_DIRS
) -> tuple[list[str], bool]:
    """Conflict copies under *repo_path*, and whether the walk stopped at the cap.

    Walks the whole repository path, not just ``refs/``: a copy of a pack or of
    the lock ticket is also a sign that two writers raced, and the report names
    where each one is so the caller can say which of them blocks ``gc``.
    """
    found: list[str] = []
    stack = [repo_path.rstrip("/") or "/"]
    listed = 0
    while stack:
        if listed >= max_dirs:
            return sorted(found), True
        directory = stack.pop()
        listed += 1
        for entry in client.list_dir(repo_id, directory):
            name = entry.get("name", "")
            full = f"{directory.rstrip('/')}/{name}"
            if entry.get("type") == "dir":
                stack.append(full)
            elif is_conflict_copy(name):
                found.append(full)
    return sorted(found), False


class ZeroStream:
    """A file-like object of *size* zero bytes that is never held in memory.

    ``upload_file`` streams anything with ``read``, ``seek`` and ``tell`` and
    sends a ``Content-Length``, which is how a pack is uploaded; a probe built
    on the same call measures the same path.
    """

    def __init__(self, size: int):
        self.size = size
        self.pos = 0

    def read(self, n: int = -1) -> bytes:
        remaining = self.size - self.pos
        n = remaining if n is None or n < 0 else min(n, remaining)
        self.pos += n
        return bytes(n)

    def seek(self, offset: int, whence: int = 0) -> int:
        base = (0, self.pos, self.size)[whence]
        self.pos = max(0, min(self.size, base + offset))
        return self.pos

    def tell(self) -> int:
        return self.pos


@dataclass
class UploadLimit:
    #: The largest body that was accepted (0: even the floor was refused).
    accepted: int
    #: The smallest body that was refused, or None when the ceiling was accepted.
    rejected: int | None
    #: What the server said about the refusal that bounds the answer.
    reason: str = ""
    #: ``(size, None if accepted else reason)`` in the order tried.
    trials: list[tuple[int, str | None]] = field(default_factory=list)

    @property
    def unlimited_below_ceiling(self) -> bool:
        return self.rejected is None


def measure_upload_limit(
    attempt: Callable[[int], str | None],
    *,
    floor: int | None = None,
    ceiling: int | None = None,
    resolution: int | None = None,
) -> UploadLimit:
    """Find the largest body *attempt* accepts, to within *resolution* or 1/8 of it.

    *attempt(size)* returns ``None`` when a body of that many bytes was stored
    whole, and a reason otherwise.  The ceiling is tried first: a server with no
    limit below it (the usual case) costs one upload, not a ramp.  The search
    assumes a limit is a threshold, which a body limit is; a flaky link is not
    one, so every trial is returned for the caller to show.
    """
    floor = PROBE_FLOOR_BYTES if floor is None else floor
    ceiling = PROBE_CEILING_BYTES if ceiling is None else ceiling
    resolution = PROBE_RESOLUTION_BYTES if resolution is None else resolution
    trials: list[tuple[int, str | None]] = []

    def tried(size: int) -> bool:
        outcome = attempt(size)
        trials.append((size, outcome))
        return outcome is None

    if tried(ceiling):
        return UploadLimit(accepted=ceiling, rejected=None, trials=trials)
    if not tried(floor):
        return UploadLimit(accepted=0, rejected=floor, reason=trials[-1][1] or "", trials=trials)
    low, high = floor, ceiling
    while high - low > max(resolution, low // PROBE_RESOLUTION_FRACTION):
        mid = (low + high) // 2 // MIB * MIB
        if mid <= low or mid >= high:
            break
        if tried(mid):
            low = mid
        else:
            high = mid
    reason = next((why or "" for size, why in reversed(trials) if size == high), "")
    return UploadLimit(accepted=low, rejected=high, reason=reason, trials=trials)


def recommended_pack_mib(accepted: int) -> int:
    """The ``seafile.gcmaxpackmib`` that fits under a measured body limit."""
    return max(1, int(accepted * PACK_HEADROOM) // MIB)


def upload_attempt(client: Any, repo_id: str, directory: str) -> Callable[[int], str | None]:
    """An *attempt* for ``measure_upload_limit`` that uploads into *directory*.

    One try per size (a retry would turn a limit into a slow success), the
    stored size read back (a proxy that accepts and truncates is a limit too),
    and the file removed again so a probe leaves no more behind than the
    largest body that succeeded.
    """
    def attempt(size: int) -> str | None:
        name = f"probe-{size}.bin"
        try:
            client.upload_file(
                repo_id, directory, name, ZeroStream(size), replace=True, attempts=1,
            )
        except Exception as exc:
            # "Failed to upload <name> to <dir>: HTTP 413 (POST <origin>) ..." -- the status is the news.
            text = str(exc).strip()
            if "HTTP " in text:
                # nginx's 413 body is a page of HTML; the status and the origin say it.
                text = text[text.index("HTTP "):].split("<", 1)[0].strip()
            elif "Connection" in text:
                # A proxy that refuses a body often closes the socket while it is
                # still being sent, so the client sees a reset and never the 413.
                text = "the connection was closed during the upload"
            return text[:200] or type(exc).__name__
        try:
            stored = next(
                (e.get("size") for e in client.list_dir(repo_id, directory) if e.get("name") == name),
                None,
            )
            client.delete_entry(repo_id, f"{directory.rstrip('/')}/{name}")
        except Exception as exc:
            return f"stored, but the check failed: {str(exc).strip()[:160]}"
        if stored != size:
            return f"{stored} bytes were stored of {size} sent"
        return None

    return attempt


def chunk_warning(accepted: int) -> bool:
    """True when the limit is below one chunk of a chunked upload.

    A single object of 1 GiB or more is uploaded as ``Content-Range`` chunks of
    ``UPLOAD_CHUNK_BYTES``; there is no setting for that size, so a limit below
    it means such an object cannot be pushed at all.
    """
    return accepted < UPLOAD_CHUNK_BYTES
