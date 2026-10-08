"""__main__.py - Entry point when invoked via python -m git_remote_seafile."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
