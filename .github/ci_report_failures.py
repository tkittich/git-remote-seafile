#!/usr/bin/env python
"""Print a unittest run's output and surface failures as GitHub annotations.

Why this exists: workflow *logs* require admin rights to download, but
check-run *annotations* are publicly readable.  Emitting ``::error::`` for each
failing test means anyone can see which test failed -- and on which
platform/version -- without repository admin access.

Reads the captured unittest output, prints it verbatim (so the log is still
complete), then:

  * emits one ``::error::`` annotation per ``FAIL:``/``ERROR:`` line;
  * emits a second annotation with the last line of that failure's traceback,
    which is normally the assertion or exception message -- the thing that
    actually explains the failure;
  * exits 0 only if unittest reported a top-level ``OK`` line.

That last check is deliberate: a crash or import error produces no ``FAIL:``
lines at all, and must still be reported as a failure.
"""

from __future__ import annotations

import pathlib
import re
import sys

FAIL_LINE = re.compile(r"^(?:FAIL|ERROR): ")
SEPARATOR = re.compile(r"^(?:=+|-+)$")
_MAX_ANNOTATION = 400


def _iter_failures(lines: list[str]):
    """Yield ``(header, detail)`` for each failure block in unittest output.

    A block runs from its ``FAIL:``/``ERROR:`` header to the next block or the
    ``====`` separator, and its last non-empty line is the exception message.
    """
    i = 0
    while i < len(lines):
        if not FAIL_LINE.match(lines[i]):
            i += 1
            continue
        header = lines[i].strip()
        i += 1
        # Skip the dashes that sit directly under the header, then read the
        # traceback up to its closing separator.  Without that stop the block
        # would swallow "Ran N tests" and "FAILED (failures=1)" and the detail
        # would be the summary line instead of the exception message.
        if i < len(lines) and SEPARATOR.match(lines[i].strip()):
            i += 1
        detail_lines: list[str] = []
        while i < len(lines):
            stripped = lines[i].strip()
            if lines[i].startswith("====") or FAIL_LINE.match(lines[i]) or SEPARATOR.match(stripped):
                break
            if stripped:
                detail_lines.append(stripped)
            i += 1
        yield header, (detail_lines[-1] if detail_lines else "")


def _emit(line: str) -> None:
    print(f"::error::{line[:_MAX_ANNOTATION]}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: ci_report_failures.py <unittest-output-file>", file=sys.stderr)
        return 2

    path = pathlib.Path(argv[1])
    if not path.is_file():
        _emit(f"unittest produced no output file at {path}")
        return 1

    output = path.read_text(encoding="utf-8", errors="replace")
    print(output, flush=True)

    lines = output.splitlines()
    failures = list(_iter_failures(lines))
    for header, detail in failures:
        _emit(header)
        if detail and detail != header:
            _emit(f"    {detail}")

    if re.search(r"(?m)^OK\b", output):
        return 0

    # No OK line: either tests failed (annotated above) or the run crashed
    # before reaching the summary.  Either way this job must be red.
    if not failures:
        _emit("unittest did not report OK and showed no FAIL/ERROR lines "
              "-- the run crashed or could not collect tests")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
