#!/usr/bin/env python3
"""Run the snapshot tool from a source checkout: ``python tools/seafile_snapshot.py``.

The tool itself is ``git_remote_seafile/snapshot_tool.py``, generated from the
parts in ``tools/_snapshot/`` and shipped in the wheel.  An installed package
gives the same program as ``git-seafile-snapshot`` (so ``git seafile-snapshot``
works) and ``git-remote-seafile snapshot``.  This file only makes sure that, run
from a checkout, the *checkout's* copy is the one used, and keeps existing
commands and scheduled jobs working.

Imported (``import seafile_snapshot``) it stands in for the real module, so a
caller sees exactly the module's names.
"""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import git_remote_seafile.snapshot_tool as _tool  # noqa: E402

if __name__ == "__main__":
    sys.exit(_tool.main())
else:
    sys.modules[__name__] = _tool
