#!/usr/bin/env python
"""Print a unittest run's output and surface failures as GitHub annotations.

Why this exists: workflow *logs* require admin rights to download, but
check-run *annotations* are publicly readable.  Emitting ``::error::`` for each
failing test means anyone can see which test failed -- and on which
platform/version -- without repository admin access.

Reads the captured unittest output, prints it verbatim (so the log is still
complete), then:

  * emits one ``::error::`` annotation per ``FAIL:``/``ERROR:`` line;
  * exits 0 only if unittest reported a top-level ``OK`` line.

That last check is deliberate: a crash or import error produces no ``FAIL:``
lines at all, and must still be reported as a failure.
"""

from __future__ import annotations

import pathlib
import re
import sys

FAIL_LINE = re.compile(r"^(?:FAIL|ERROR): ")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: ci_report_failures.py <unittest-output-file>", file=sys.stderr)
        return 2

    path = pathlib.Path(argv[1])
    if not path.is_file():
        print(f"::error::unittest produced no output file at {path}")
        return 1

    output = path.read_text(encoding="utf-8", errors="replace")
    print(output, flush=True)

    for line in output.splitlines():
        if FAIL_LINE.match(line):
            # Annotations are single-line; strip the trailing detail that
            # unittest appends after the test id on the same line.
            print(f"::error::{line.strip()}")

    if re.search(r"(?m)^OK\b", output):
        return 0

    # No OK line: either tests failed (annotated above) or the run crashed
    # before reaching the summary.  Either way this job must be red.
    if not any(FAIL_LINE.match(line) for line in output.splitlines()):
        print("::error::unittest did not report OK and showed no FAIL/ERROR lines "
              "-- the run crashed or could not collect tests")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
