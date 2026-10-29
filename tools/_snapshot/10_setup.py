
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, TypedDict

DEFAULT_BRANCH = "snapshot"

#: How old an unparseable lock file must be before it is reclaimed: a live run
#: writes its identity immediately after creating the file, so a fresh one may
#: simply be mid-write.
_UNPARSEABLE_LOCK_GRACE_SECONDS = 30.0

RECOMMENDED_EXCLUDES = (
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "*.pyc",
    "dist",
    "build",
)

# Settings a run remembers in the vault, so a bare re-run repeats the last one.
# ``source`` is recorded too, and a mismatch is refused (``remembered_settings``)
# -- the vault names, and belongs to, exactly one source directory.
_REMEMBERED_KEYS = ("source", "remote", "coderemote", "excludes", "branch")


class SnapshotError(RuntimeError):
    """A failure the operator can act on."""
