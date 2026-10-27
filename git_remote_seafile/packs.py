"""packs.py - Packfile transfer, verification and staging for git-remote-seafile."""

from __future__ import annotations

from pathlib import Path
import inspect
import json
import re
import sys
import tempfile
from typing import Any

from .client import SeafileClient, SeafileAPIError
from .git_util import install_packfile, remove_tree

#: Lowercase only.  Git writes pack names as lowercase hex, and a name that
#: differs from a local pack only by case is one file on Windows and macOS: a
#: remote ``pack-<UPPERCASE>.pack`` passed the "do I already have it" check
#: (case-sensitive) and then replaced the local pack in place.  No name this
#: tool or git produces contains an uppercase letter, so none is accepted.
PACK_NAME_RE = re.compile(r"^pack-[0-9a-z._-]+\.pack$")
MAX_IN_MEMORY_PACK_BYTES = 16 * 1024 * 1024


class ListedPackVanishedError(SeafileAPIError):
    """A packfile the listing showed was already gone when it was downloaded.

    The 404-after-listing signature of a concurrent compaction: gc merges the
    pack and removes it between the caller's ``list_dir`` and the GET.  The
    fetch path re-lists once and retries from the fresh listing
    (``helper.cmd_fetch``); this reaching the top level means the second
    listing could not serve the pack either, and the fetch must fail loudly.
    A subclass of :class:`SeafileAPIError` so every existing handler keeps
    working unchanged.
    """


def is_valid_pack_name(pack_name: str) -> bool:
    """Validate remote packfile name against pattern and directory traversal.

    ``fullmatch``, not ``match``: ``$`` also matches just before a trailing
    newline, so ``PACK_NAME_RE.match("pack-a.pack\\n")`` is truthy and a remote
    listing could smuggle a line break into a name this function is the only
    thing standing in front of.  The same reason ``HEX_SHA_RE`` is used with
    ``fullmatch``.
    """
    if not PACK_NAME_RE.fullmatch(pack_name):
        return False
    if ".." in pack_name or "/" in pack_name or "\\" in pack_name:
        return False
    return True


def check_remote_has_packs(client: SeafileClient, repo_id: str, remote_pack_dir: str) -> bool:
    """True if the remote already holds at least one packfile.

    Used to tell a genuinely empty repository apart from one whose ref
    listing failed.  A missing pack directory is the genuine-empty answer;
    any *other* listing failure propagates, so the caller's empty-repository
    guard cannot be silenced by the same transient error that emptied the ref
    listing.
    """
    entries = client.list_dir(repo_id, remote_pack_dir)  # 404 -> []; failures raise
    if not isinstance(entries, list):
        return False
    return any(e.get("name", "").endswith(".pack") for e in entries if isinstance(e, dict))


#: Bumped if the on-disk shape changes; an unrecognised version is ignored
#: rather than guessed at, which costs one re-download.
_PACK_STATE_VERSION = 1


def _pack_state_path(git_dir: Path, repo_id: str) -> Path:
    """Where the record of installed remote packs lives for one library.

    ``.git/seafile/<repo id>/packs.json``.  Keyed on the *repo id* rather than
    the remote's name: the same library is routinely called ``origin`` in one
    clone and ``seafile`` in the next, and a remote can be renamed at any time,
    while the id names the same set of packs everywhere.  The id is sanitised
    defensively -- it is a UUID in practice, and a stray character must not
    escape the directory.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo_id) or "unknown"
    return git_dir / "seafile" / safe / "packs.json"


def _read_pack_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _PACK_STATE_VERSION:
        return {}
    return data


def load_installed_packs(git_dir: Path | None, repo_id: str, repo_path: str) -> dict[str, int]:
    """Remote packs this clone has already fetched, as ``{pack name: size}``.

    Why this exists: the skip that keeps a fetch from re-downloading everything
    used to be keyed on the *local* pack filenames under ``objects/pack``, and a
    local ``git gc`` changes those.  Repacking leaves the objects in place but
    renames them, so the next fetch matched nothing and downloaded every remote
    pack again -- the whole point of the guard, undone by an unrelated command.
    The fact worth remembering is not "a local file of this name exists" but
    "this clone has already fetched this remote pack", and a local repack cannot
    invalidate it: the objects a downloaded pack provides are reachable from the
    refs, so repacking preserves them.

    Returns ``{}`` when there is no state file, when it is unreadable or
    truncated, or when the recorded entry is for a different ``repo_path`` --
    the same library holds a different set of packs per destination.  Every one
    of those answers costs a re-download and never a missing object.
    """
    if git_dir is None:
        return {}
    data = _read_pack_state(_pack_state_path(git_dir, repo_id))
    entry = data.get("remotes", {}).get(repo_path) if isinstance(data.get("remotes"), dict) else None
    if not isinstance(entry, dict):
        return {}
    return {
        name: size
        for name, size in entry.items()
        if isinstance(name, str) and isinstance(size, int)
    }


def record_installed_packs(
    git_dir: Path | None,
    repo_id: str,
    repo_path: str,
    packs: dict[str, int | None],
) -> None:
    """Record *packs* as fetched from *repo_path*, merging with what is there.

    Best-effort on purpose.  A state file that cannot be written costs one
    re-download on the next fetch -- exactly the behaviour before this file
    existed -- so a failure is reported and swallowed rather than failing a
    fetch that has already succeeded.  The write goes through a temporary file
    and ``os.replace``: a half-written JSON file would read as "nothing
    installed", and a truncated one could read as valid JSON that is missing
    entries, which is the one way this file could cause work to be skipped.
    """
    if git_dir is None or not packs:
        return
    path = _pack_state_path(git_dir, repo_id)
    try:
        data = _read_pack_state(path) or {"version": _PACK_STATE_VERSION}
        remotes = data.get("remotes")
        if not isinstance(remotes, dict):
            remotes = data["remotes"] = {}
        entry = remotes.get(repo_path)
        if not isinstance(entry, dict):
            entry = remotes[repo_path] = {}
        for name, size in packs.items():
            entry[name] = size if isinstance(size, int) else 0
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name("packs.json.tmp")
        scratch.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        scratch.replace(path)
    except OSError as ex:
        sys.stderr.write(
            f"Warning: could not record the fetched packfiles in {path} ({ex}); "
            f"a later fetch may download them again.\n"
        )
        sys.stderr.flush()


def fetch_pack_artifact(
    client: SeafileClient,
    repo_id: str,
    remote_path: str,
    dest: Path,
    expected_size: int | None = None,
    *,
    progress_callback: Any = None,
) -> str | None:
    """Download one remote pack artifact (a .pack or .idx) to *dest*.

    Streams via download_file_to when the client supports it, else falls back
    to an in-memory GET capped at MAX_IN_MEMORY_PACK_BYTES -- a larger artifact
    raises instead, since that path means streaming already failed and
    buffering is the only option left.

    Returns None when *dest* holds a non-empty file that matches
    *expected_size* whenever the listing provided one; otherwise returns the
    failure reason -- 'missing' (nothing downloaded), 'empty' (zero bytes), or
    'truncated' (size mismatch) -- and the caller decides whether that is
    fatal (a pack) or regenerable (an index).

    Residual: when the listing omitted the size entirely (``expected_size is
    None``), the in-memory fallback cannot be size-checked up front, and a
    huge artifact would be buffered before it could be refused.  Every
    current caller passes the size from the listing; keep that contract for
    any future caller.
    """
    downloaded = False
    if hasattr(client, "download_file_to"):
        try:
            downloaded = bool(
                client.download_file_to(
                    repo_id, remote_path, dest,
                    progress_callback=progress_callback,
                    # A body larger than the listing's size is a lying or
                    # hostile server: the stream is cut off at the expected
                    # size instead of being written whole and refused after
                    # (REVIEW.sonnet-5.5 C1).
                    max_bytes=expected_size,
                )
            ) and dest.is_file()
        except SeafileAPIError:
            # The cap raises from inside the stream.  With a size on record
            # there is no honest fallback: the unbounded path below would
            # fetch exactly what the cap exists to refuse.
            if expected_size is not None:
                raise
        except Exception as exc:
            sys.stderr.write(f"Warning: streaming download of {dest.name} failed: {exc}\n")
            sys.stderr.flush()
            downloaded = False
    if not downloaded:
        if expected_size is not None and expected_size > MAX_IN_MEMORY_PACK_BYTES:
            raise SeafileAPIError(
                f"{dest.name} streaming download failed and its size ({expected_size} bytes) "
                f"exceeds in-memory buffer limit ({MAX_IN_MEMORY_PACK_BYTES} bytes)."
            )
        data = client.get_file_bytes(repo_id, remote_path)
        if data:
            dest.write_bytes(data)
            downloaded = True
    if not downloaded:
        return "missing"
    actual = dest.stat().st_size
    if actual == 0:
        return "empty"
    if expected_size is not None and actual != expected_size:
        return "truncated"
    return None


def fetch_and_install_pack(
    client: SeafileClient,
    repo_id: str,
    remote_pack_dir: str,
    pack_name: str,
    git_dir: Path | None = None,
    expected_size: int | None = None,
    expected_idx_size: int | None = None,
    installer: Any = install_packfile,
) -> None:
    """Download a remote packfile and its index, verify integrity, and install atomically."""
    idx_name = pack_name.removesuffix(".pack") + ".idx"
    sys.stderr.write(f"Downloading {pack_name} from Seafile...\n")
    sys.stderr.flush()

    staging_dir = Path(tempfile.mkdtemp(prefix="grs-fetch-", dir=str(git_dir) if git_dir else None))
    try:
        staged_pack = staging_dir / pack_name
        staged_idx = staging_dir / idx_name

        reason = fetch_pack_artifact(
            client, repo_id, f"{remote_pack_dir}/{pack_name}", staged_pack, expected_size=expected_size
        )
        if reason == "truncated":
            raise SeafileAPIError(
                f"Packfile {pack_name} download was truncated: expected {expected_size} bytes, "
                f"got {staged_pack.stat().st_size} bytes."
            )
        if reason == "missing":
            # The 404-after-listing signature: the pack was merged and removed
            # by a concurrent gc between the caller's listing and the GET.  A
            # distinct type so the fetch can re-list and retry once while every
            # other handler keeps treating it as the SeafileAPIError it is.
            raise ListedPackVanishedError(
                f"Packfile {pack_name} is listed at {remote_pack_dir} but was gone when "
                "downloaded; a concurrent compaction may have merged and removed it, and "
                "the local repository would be missing objects."
            )
        if reason is not None:
            raise SeafileAPIError(
                f"Packfile {pack_name} is listed at {remote_pack_dir} but could not be "
                "downloaded -- the local repository would be missing objects. Retry the "
                "fetch, or run 'git-remote-seafile test <url>' to diagnose."
            )

        # A missing or truncated index is regenerable locally; only a size
        # mismatch is worth telling the user about.
        idx_reason = fetch_pack_artifact(
            client, repo_id, f"{remote_pack_dir}/{idx_name}", staged_idx, expected_size=expected_idx_size
        )
        idx_downloaded = idx_reason is None
        if idx_reason == "truncated":
            sys.stderr.write(f"Warning: downloaded {idx_name} size mismatch; will regenerate locally.\n")
            sys.stderr.flush()
            idx_downloaded = False

        target = installer or install_packfile
        # Custom installer callbacks may predate the ``move`` parameter; probe
        # the signature instead of retrying on TypeError -- a bare except would
        # mask a genuine failure occurring *after* the staged files were moved.
        try:
            accepts_move = "move" in inspect.signature(target).parameters
        except (TypeError, ValueError):
            accepts_move = False
        if accepts_move:
            target(pack_name, staged_pack, staged_idx if idx_downloaded else None, move=True)
        else:
            target(pack_name, staged_pack, staged_idx if idx_downloaded else None)
    finally:
        remove_tree(staging_dir)
