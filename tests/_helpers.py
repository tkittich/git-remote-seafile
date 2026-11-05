"""Shared test helpers.

Not collected by the runners (they discover ``test*.py``), imported by the
modules that would otherwise carry a second copy.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
from unittest.mock import MagicMock


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


def remove_tree(path: "str | os.PathLike[str]") -> None:
    """Delete a directory tree a test left behind, even a read-only or locked one.

    ``shutil.rmtree(path, ignore_errors=True)`` is how this suite used to clean
    up, and on Windows it silently leaves a repository behind: git's object files
    are read-only, so deleting them fails and the failure is swallowed.  The
    module that probes for symlink support made a repository on every import, so
    one full run left about 400 ``grs-*`` directories in the temporary directory
    (14,495 had piled up).  This clears the read-only bit and retries.  A tree
    that still cannot be removed is reported on stderr, not ignored.
    """
    target = os.fspath(path)

    def clear_read_only(function, failed, _error):
        try:
            os.chmod(failed, 0o700)
            function(failed)
        except OSError:
            pass  # the check below reports what is still there

    def attempt() -> None:
        if not os.path.lexists(target):
            return
        if os.path.islink(target) or os.path.isfile(target):
            os.unlink(target)
        elif sys.version_info >= (3, 12):
            shutil.rmtree(target, onexc=clear_read_only)
        else:
            shutil.rmtree(target, onerror=clear_read_only)

    try:
        attempt()
    except OSError:
        pass
    if os.path.lexists(target):
        sys.stderr.write(f"remove_tree: could not remove {target}\n")


def make_helper(**attrs):
    """A ``RemoteHelper`` with every attribute set and no network or credentials.

    Tests used to build one with ``RemoteHelper.__new__(RemoteHelper)`` and set
    whichever attributes the test happened to need -- which meant the production
    class carried defaults (a class-level ``dry_run``, a ``hasattr`` before
    ``_refs_cache``) whose only job was to keep those half-built objects from
    raising.  This builds the complete object once; override anything with
    keyword arguments.
    """
    from git_remote_seafile.helper import RemoteHelper

    helper = RemoteHelper.__new__(RemoteHelper)
    defaults = {
        "remote_name": "origin",
        "raw_url": "seafile://lib/repo",
        "server_url": None,
        # Empty on purpose: check_preflight_safety() returns immediately for a
        # helper with no library name, so a test that does not exercise the
        # guardrails never reads this machine's real Seafile client databases.
        "library_name": "",
        "repo_path": "/git-repo",
        "repo_id": "repo1",
        "client": MagicMock(),
        "dry_run": False,
        "_refs_cache": {},
    }
    defaults.update(attrs)
    for name, value in defaults.items():
        setattr(helper, name, value)
    return helper

