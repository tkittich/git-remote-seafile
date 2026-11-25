"""A progress line on standard error for a long transfer (round 5: all four reviewers asked for it).

The tool exists for objects of tens of gigabytes; a command that prints nothing for an hour looks hung and gets
killed, and a killed upload starts again.  The line is drawn only when standard error is a terminal (a pipe, a log
and an agent's capture stay clean) and never with ``--quiet``; standard output is never touched.  The words are the
snapshot tool's own (``payload_upload._progress_line``), so every long phase of every command reads the same.
"""

from __future__ import annotations

import sys
import time
from typing import IO


class Progress:
    """Called as ``progress(done, total)`` by the transfer; redraws at most a few times a second."""

    def __init__(self, verb: str, stream: "IO[str] | None" = None, *, enabled: "bool | None" = None,
                 interval: float = 0.25, clock=time.monotonic) -> None:
        self.verb = verb                    # "uploaded", "downloaded"
        self.stream = stream if stream is not None else sys.stderr
        isatty = getattr(self.stream, "isatty", lambda: False)
        self.enabled = bool(isatty()) if enabled is None else enabled
        self.interval = interval
        self._clock = clock
        self._start = clock()
        self._last = -interval
        self._drawn = False

    def __call__(self, done: int, total: int) -> None:
        if not self.enabled:
            return
        now = self._clock()
        if done < total and now - self._last < self.interval:
            return
        self._last = now
        self._drawn = True
        self.stream.write("\r" + self.line(done, total, now - self._start))
        self.stream.flush()

    def line(self, done: int, total: int, elapsed: float) -> str:
        from ..snapshot.payload_upload import _progress_line
        return _progress_line(done, total, elapsed, self.verb) + "   "

    def finish(self) -> None:
        """Wipe the line, so the answer that follows starts clean."""
        if self.enabled and self._drawn:
            self.stream.write("\r" + " " * 79 + "\r")
            self.stream.flush()
        self._drawn = False
