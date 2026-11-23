#!/usr/bin/env python3
"""Run the snapshot tool from a source checkout: ``python tools/seafile_snapshot.py``.

The tool itself is the package ``git_remote_seafile.snapshot``, shipped in the wheel.  An installed
package gives the same program as ``git-seafile-snapshot`` (so ``git seafile-snapshot`` works) and
``git-remote-seafile snapshot``.  This file only makes sure that, run from a checkout, the *checkout's*
copy is the one used, and keeps existing commands and scheduled jobs working.

Imported (``import seafile_snapshot``) it stands in for the package, so a caller sees exactly the
package's names.
"""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import git_remote_seafile.snapshot as _tool  # noqa: E402
from git_remote_seafile.snapshot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
else:
    sys.modules[__name__] = _tool
