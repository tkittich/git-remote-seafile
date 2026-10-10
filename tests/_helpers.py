"""Shared test helpers.

Not collected by the runners (they discover ``test*.py``), imported by the
modules that would otherwise carry a second copy.
"""

from __future__ import annotations

import os
import pathlib
import shutil


def find_bash() -> str | None:
    """Locate a POSIX bash, preferring the one that ships with git.

    On Windows ``shutil.which("bash")`` can resolve to ``System32\bash.exe``,
    which is not a shell at all -- it is a launcher for WSL, where the paths and
    the toolchain differ from the Git Bash this repository's scripts are written
    for.  Git Bash is the right interpreter there, so look next to git first and
    reject the WSL launcher outright.  ``GRS_BASH`` overrides the search.
    """
    override = os.environ.get("GRS_BASH")
    if override:
        return override if pathlib.Path(override).is_file() else None

    git = shutil.which("git")
    if git:
        git_dir = pathlib.Path(git).resolve().parent
        for candidate in (git_dir / "bash.exe", git_dir.parent / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)

    found = shutil.which("bash")
    if found and "system32" not in found.replace("\\", "/").lower():
        return found
    return None
