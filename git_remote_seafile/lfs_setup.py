"""lfs_setup.py - ``git-remote-seafile lfs-setup``: point Git LFS at Seafile in this repository.

Git LFS needs four settings and the library name appears in one of them, so a
repository whose remote was renamed or moved kept transferring to the old
library until a large file was pushed.  This derives the destination from the
repository's own ``seafile://`` remote and writes the settings, the same way
every time.
"""

from __future__ import annotations

import re
import shutil
import sys

from .git_util import run_git
from .url import mask_url

_URL_KEY = re.compile(r"^remote\.(.+)\.(url|pushurl)$")
USAGE = "Usage: git-remote-seafile lfs-setup [<seafile://url>] [--all-remotes]\n"
AGENT = "lfs.customtransfer.seafile"


def _git(*args: str) -> tuple[str, int]:
    out, _err, code = run_git(list(args))
    return out.decode("utf-8", "replace").strip(), code


def repository_remotes() -> list[tuple[str, str]]:
    """``(remote name, url)`` for every fetch and push URL in this repository's own config."""
    out, code = _git("config", "--local", "--get-regexp", r"^remote\..*\.(url|pushurl)$")
    if code != 0:
        return []
    found = []
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        match = _URL_KEY.match(key)
        if match and value:
            found.append((match.group(1), value.strip()))
    return found


def _set(key: str, value: str) -> bool:
    """Set *key* locally; True when it changed."""
    current, code = _git("config", "--local", "--get", key)
    if code == 0 and current == value:
        return False
    _out, code = _git("config", "--local", key, value)
    if code != 0:
        raise RuntimeError(f"git config {key} failed")
    return True


def stale_lfs_destination() -> tuple[str, list[str]] | None:
    """``(configured, remotes)`` when this repository's LFS agent names a place none of its Seafile remotes does.

    The library is spelled twice -- in the remote and in ``lfs.customtransfer.seafile.args`` -- so a
    remote that was renamed or moved leaves large files going to the old library, and the first sign
    is a failed push of a large file.  ``None`` outside a repository, with no agent configured, with
    no ``seafile://`` remote to compare against, or when they agree.
    """
    _dir, code = _git("rev-parse", "--git-dir")
    if code != 0:
        return None
    args, code = _git("config", "--local", "--get", f"{AGENT}.args")
    if code != 0 or not args.startswith("lfs-transfer "):
        return None
    configured = args[len("lfs-transfer "):].strip()
    remotes = sorted({url for _n, url in repository_remotes() if url.lower().startswith("seafile://")})
    if not remotes or configured.rstrip("/") in {r.rstrip("/") for r in remotes}:
        return None
    return configured, remotes


def run_lfs_setup(rest: list[str]) -> int:
    positional = [a for a in rest if not a.startswith("-")]
    unknown = [a for a in rest if a.startswith("-") and a != "--all-remotes"]
    if unknown or len(positional) > 1:
        sys.stderr.write(USAGE)
        return 2
    _git_dir, code = _git("rev-parse", "--git-dir")
    if code != 0:
        sys.stderr.write("Error: lfs-setup configures the repository you are in, and this is not a git repository.\n")
        return 1

    remotes = repository_remotes()
    seafile_urls = sorted({url for _n, url in remotes if url.lower().startswith("seafile://")})
    if positional:
        url = positional[0]
        if not url.lower().startswith("seafile://"):
            sys.stderr.write(f"Error: '{url}' is not a seafile:// URL.\n")
            return 2
    elif len(seafile_urls) == 1:
        url = seafile_urls[0]
    elif not seafile_urls:
        sys.stderr.write(
            "Error: this repository has no seafile:// remote to take the library from; "
            "pass the URL: git-remote-seafile lfs-setup seafile://<library>/<path>\n"
        )
        return 1
    else:
        sys.stderr.write(
            "Error: this repository has several seafile:// remotes (" + ", ".join(map(mask_url, seafile_urls))
            + "); say which one holds the large files: git-remote-seafile lfs-setup <url>\n"
        )
        return 1

    previous, _ = _git("config", "--local", "--get", f"{AGENT}.args")
    try:
        changed = [
            _set(f"{AGENT}.path", "git-remote-seafile"),
            _set(f"{AGENT}.args", f"lfs-transfer {url}"),
            _set(f"{AGENT}.direction", "both"),
        ]
        others = sorted({f"{n} ({mask_url(u)})" for n, u in remotes if not u.lower().startswith("seafile://")})
        select = not others or "--all-remotes" in rest
        if select:
            changed.append(_set("lfs.standalonetransferagent", "seafile"))
    except RuntimeError as ex:
        sys.stderr.write(f"Error: {ex}\n")
        return 1

    old = previous.removeprefix("lfs-transfer ").strip()
    if previous and old != url:
        print(f"Note: the LFS destination was {mask_url(old)}; it is now {mask_url(url)}.")
    print(
        f"Git LFS in this repository now stores large files in {mask_url(url)}."
        if any(changed) else
        f"Git LFS in this repository already stores large files in {mask_url(url)}; nothing to change."
    )
    if not select:
        already, _ = _git("config", "--local", "--get", "lfs.standalonetransferagent")
        if already == "seafile":
            print(
                "Warning: lfs.standalonetransferagent is already 'seafile' here, and this repository also has "
                + ", ".join(others)
                + ", so their large files go to Seafile too (a clone from there could not resolve them); "
                "scope the agent to a server instead (User Guide, section 11.1)."
            )
        else:
            print(
                "Warning: lfs.standalonetransferagent was not set, because this repository also has "
                + ", ".join(others)
                + ", and setting it would send their large files to Seafile too (a clone from there "
                "could not resolve them). If Seafile is the only LFS destination, run "
                "'git-remote-seafile lfs-setup --all-remotes'; otherwise scope the agent to a server "
                "(User Guide, section 11.1)."
            )
    if shutil.which("git-remote-seafile") is None:
        print(
            "Warning: git-remote-seafile is not on your PATH, so Git LFS cannot start the transfer "
            "agent; install it with 'uv tool install' or 'pipx install' (README, Quick Start)."
        )
    _ver, lfs_code = _git("lfs", "version")
    if lfs_code != 0:
        print("Warning: Git LFS itself is not installed; get it from https://git-lfs.com, then 'git lfs install'.")
    print("Next: git lfs track \"*.bin\"   (use your own patterns), then git add .gitattributes")
    return 0
