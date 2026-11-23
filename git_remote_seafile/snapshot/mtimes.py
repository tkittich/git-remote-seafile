from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from . import gitrun
from . import vault_store


# -- Modification times of the files git stores (see 70_stage.py) ------------------
#
# Git records content and a mode, not a timestamp, so a restore used to give
# every git-tracked file the time of the checkout.  Payload files already carry
# their mtime in the manifest.  This records the rest in one more file inside
# the reserved directory, ``.seafile-payload/mtimes.json``, and a restore puts
# the times back.

_MTIMES_NAME = "mtimes.json"
_MTIMES_FORMAT = 1


def mtimes_path() -> str:
    """Where the record lives in a snapshot tree."""
    from . import payload_upload
    return f"{payload_upload.PAYLOAD_RESERVED_DIR}/{_MTIMES_NAME}"


def add_mtimes_arguments(parser: Any) -> None:
    parser.add_argument(
        "--record-mtimes",
        action="store_true",
        help="record the modification time of every git-stored file in "
        ".seafile-payload/mtimes.json so a restore can put them back (default for a run "
        "that pushes to a seafile:// remote; remembered by the vault)",
    )
    parser.add_argument(
        "--no-record-mtimes",
        action="store_true",
        help="do not record modification times (the vault remembers this; a restore then "
        "gives git-stored files the time of the checkout, as before 0.9.2)",
    )


def mtimes_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.record_mtimes and args.no_record_mtimes:
        problems.append("--record-mtimes and --no-record-mtimes contradict each other")
    if (args.record_mtimes or args.no_record_mtimes) and (
        args.restore or args.maintain or args.payload_gc or args.payload_verify is not None
        or args.payload_view is not None
    ):
        problems.append(
            "--record-mtimes and --no-record-mtimes only apply to a snapshot run"
        )
    return problems


def parent_has_mtimes(git_dir: Path, work_tree: Path, parent: str | None) -> bool:
    """Whether snapshot ``parent`` carries a times record."""
    if not parent:
        return False
    proc = gitrun.git(
        git_dir, work_tree, ["cat-file", "-e", f"{parent}:{mtimes_path()}"],
        cwd=work_tree, check=False,
    )
    return proc.returncode == 0


def resolve_record_mtimes(
    args: argparse.Namespace, vault: Path, *, remote: str | None, no_push: bool,
    parent_has_record: bool = False,
) -> bool:
    """The flag as typed, else what the vault remembered, else what the last snapshot did,
    else on for a run that pushes to a Seafile remote.

    A default is not a request, and neither is a run's route: a ``--no-push``
    run on a vault whose last snapshot has a record keeps recording, rather
    than dropping the record (and committing the difference) until the next
    pushing run rebuilds it.  With no seafile:// remote and no such history it
    stays off, so nothing that worked before changes.
    """
    from . import payload_cli
    if args.no_record_mtimes:
        return False
    if args.record_mtimes:
        return True
    if (vault / ".git").exists():
        remembered = vault_store._config(vault, "snapshot.mtimes")
        if remembered in ("0", "1"):
            return remembered == "1"
    return parent_has_record or payload_cli.payload_by_default_possible(remote, no_push)


def remember_record_mtimes(vault: Path, args: argparse.Namespace, *, dry_run: bool) -> None:
    """Remember an explicit choice after a complete real run (never a default)."""
    if dry_run or not (args.record_mtimes or args.no_record_mtimes):
        return
    gitrun._run(
        ["git", "-C", str(vault), "config", "snapshot.mtimes", "0" if args.no_record_mtimes else "1"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _blobs_of_tree(git_dir: Path, work_tree: Path, rev: str) -> dict[str, str]:
    """``{path: blob id}`` for every file in snapshot ``rev``."""
    listing = gitrun.git(git_dir, work_tree, ["ls-tree", "-r", "-z", rev], cwd=work_tree).stdout
    found: dict[str, str] = {}
    for entry in listing.split("\0"):
        meta, _sep, path = entry.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[1] == "blob":
            found[path] = parts[2]
    return found


def _recorded_mtimes(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, int]:
    """The times recorded in snapshot ``rev``; empty when it has none or they are unusable."""
    from . import payload_upload
    if not rev:
        return {}
    proc = gitrun.git(
        git_dir, work_tree, ["show", f"{rev}:{mtimes_path()}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
        files = data["files"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}
    if not isinstance(files, dict) or data.get("format") != _MTIMES_FORMAT:
        return {}  # a record of another format is not read here either (apply_recorded_mtimes refuses it)
    return {
        path: value for path, value in files.items()
        if isinstance(path, str) and isinstance(value, int) and not isinstance(value, bool)
        and 0 <= value < payload_upload._MAX_MTIME_NS
    }


def has_usable_mtimes(git_dir: Path, work_tree: Path, rev: str | None) -> bool:
    """Whether snapshot ``rev`` carries a times record this tool reads, even one with no files in it.

    ``_recorded_mtimes`` answers ``{}`` both for "no record" and for a record of an empty tree; a tree that
    holds only payload files has the second, and ``--status`` then called every new file uncomparable
    (review c305b0a, gemini-S3-3).
    """
    if not rev:
        return False
    proc = gitrun.git(git_dir, work_tree, ["show", f"{rev}:{mtimes_path()}"], cwd=work_tree, check=False)
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout)
    except (ValueError, RecursionError):
        return False
    return isinstance(data, dict) and data.get("format") == _MTIMES_FORMAT and isinstance(data.get("files"), dict)


def _recorded_sizes(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, int]:
    """The sizes recorded in snapshot ``rev`` beside the times; empty for a snapshot that has none.

    An additive key of the same document (``"sizes"``), so a tool that does not
    know it reads ``"files"`` as before.  Only regular files carry one: a
    symlink's own size is its target's length and says nothing about content.
    """
    if not rev:
        return {}
    proc = gitrun.git(
        git_dir, work_tree, ["show", f"{rev}:{mtimes_path()}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
        sizes = data["sizes"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}
    if not isinstance(sizes, dict) or data.get("format") != _MTIMES_FORMAT:
        return {}
    return {
        path: value for path, value in sizes.items()
        if isinstance(path, str) and isinstance(value, int) and not isinstance(value, bool)
        and value >= 0
    }


def record_mtimes_in_index(git_dir: Path, work_tree: Path, parent: str | None) -> int:
    """Write ``.seafile-payload/mtimes.json`` into the vault index; return how many times.

    The times are the real ones.  Only when nothing in the tree changed since
    the parent snapshot is the parent's record reused unchanged: a touch is not
    a change (a build that rewrites a generated file with the same bytes must
    not make a new snapshot every night).  Carrying times forward per file was
    tried and is wrong: a source reverted to an older version kept its old
    time, which could sort it *before* an object built from the version it
    replaced, so ``make`` kept the stale object.
    """
    from . import payload_upload, payload_restore
    listing = gitrun.git(git_dir, work_tree, ["ls-files", "-s", "-z"], cwd=work_tree).stdout
    current: dict[str, str] = {}
    for entry in listing.split("\0"):
        meta, _sep, path = entry.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[0] != "160000":
            current[path] = parts[1]
    wanted = {
        path: blob for path, blob in current.items()
        if path != payload_upload.PAYLOAD_RESERVED_DIR and not path.startswith(payload_upload.PAYLOAD_RESERVED_DIR + "/")
    }
    if parent and wanted == {
        path: blob for path, blob in _blobs_of_tree(git_dir, work_tree, parent).items()
        if path != payload_upload.PAYLOAD_RESERVED_DIR and not path.startswith(payload_upload.PAYLOAD_RESERVED_DIR + "/")
    }:
        previous = gitrun.git(
            git_dir, work_tree, ["rev-parse", "--verify", "-q", f"{parent}:{mtimes_path()}"],
            cwd=work_tree, check=False,
        )
        if previous.returncode == 0 and previous.stdout.strip():
            gitrun.git(
                git_dir, work_tree,
                ["update-index", "--add", "--cacheinfo",
                 f"100644,{previous.stdout.strip()},{mtimes_path()}"],
                cwd=work_tree,
            )
            return len(_recorded_mtimes(git_dir, work_tree, parent))

    times: dict[str, int] = {}
    sizes: dict[str, int] = {}
    for path in wanted:
        try:
            info = os.lstat(payload_restore.extended_path(work_tree / path))
        except OSError:
            continue
        stamp = info.st_mtime_ns
        if 0 <= stamp < payload_upload._MAX_MTIME_NS:
            times[path] = stamp
            if stat.S_ISREG(info.st_mode):
                sizes[path] = info.st_size
    if not times:
        return 0
    record: dict[str, Any] = {"format": _MTIMES_FORMAT, "files": times}
    if sizes:
        # Beside the times, not instead of them: `--status` compares size too, so a
        # rewrite that restores the mtime no longer reads as "unchanged".
        record["sizes"] = sizes
    document = json.dumps(record, sort_keys=True, separators=(",", ":"))
    blob_id = gitrun.git(
        git_dir, work_tree, ["hash-object", "-w", "--no-filters", "--stdin"],
        cwd=work_tree, stdin=document,
    ).stdout.strip()
    gitrun.git(
        git_dir, work_tree,
        ["update-index", "--add", "--cacheinfo", f"100644,{blob_id},{mtimes_path()}"],
        cwd=work_tree,
    )
    return len(times)


def _without_extended_prefix(text: str) -> str:
    """*text* without the ``\\\\?\\`` (or ``\\\\?\\UNC\\``) prefix, so it compares with a plain root."""
    bs = chr(92)
    if text.startswith(bs * 2 + "?" + bs + "UNC" + bs):
        return bs * 2 + text[8:]
    if text.startswith(bs * 2 + "?" + bs):
        return text[4:]
    return text


def apply_recorded_mtimes(into: Path) -> tuple[int, int]:
    """Set the recorded times on a restored tree; ``(applied, skipped)``.

    Best effort and bounded to *into*: the record came from a remote, so each
    path goes through the same checks as a manifest path, and a time that does
    not fit is skipped rather than written.
    """
    from . import payload_manifest, payload_upload, payload_restore
    record = into / payload_upload.PAYLOAD_RESERVED_DIR / _MTIMES_NAME
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        files = data["files"]
        if not isinstance(files, dict):
            return 0, 0
        # A record of another format is not read as this one: its stamps may mean something else
        # (the manifest is held to the same rule; review dfd2269, gemini-4-hydrogen-2-E2).  The
        # times are best effort, so they are counted as skipped, not applied.
        if data.get("format") != _MTIMES_FORMAT:
            return 0, len(files)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, AttributeError):
        return 0, 0
    root = into.resolve()
    applied = skipped = 0
    for rel, stamp in files.items():
        if (
            not isinstance(rel, str) or isinstance(stamp, bool) or not isinstance(stamp, int)
            or not 0 <= stamp < payload_upload._MAX_MTIME_NS or payload_manifest.path_security_problem(rel) is not None
            or (os.name == "nt" and payload_manifest.windows_name_problem(rel) is not None)
            # A record entry is remote data like a manifest entry, so on Windows it gets the same
            # name check: `.git::$INDEX_ALLOCATION/config` passed the security check and moved
            # .git/config's time (REVIEW.sonnet-5.5 D10, reproduced).  Only there: on Linux and macOS
            # a `:` or a trailing dot is a legal name and lost its time for it (gemini-S2-6).
        ):
            skipped += 1
            continue
        plain = into / rel
        target = payload_restore.extended_path(plain)  # past 260 characters on Windows too
        try:
            # Resolved in the extended-length form: a plain ``resolve`` of a folder past 260 characters raised on
            # a Windows without LongPathsEnabled and the time was skipped (review c305b0a gemini-S2-6 b).
            parent = os.path.normcase(_without_extended_prefix(str(payload_restore.extended_path(plain.parent).resolve())))
            if os.path.commonpath([os.path.normcase(str(root)), parent]) != os.path.normcase(str(root)):
                skipped += 1
                continue
            # A committed link (``tool -> .git``) must not carry an entry into the repository's own files or
            # the reserved directory; validate_manifest refuses the same shape (review c305b0a, fable-S2-5).
            if any(parent == guarded or parent.startswith(guarded + os.sep)
                   for guarded in (os.path.normcase(str(root / ".git")),
                                   os.path.normcase(str(root / payload_upload.PAYLOAD_RESERVED_DIR)))):
                skipped += 1
                continue
            if target.is_symlink():
                # A platform that cannot set a link's own time (Windows) is not a
                # failure to report: the link is restored, only its time is not.
                if os.utime in os.supports_follow_symlinks:
                    os.utime(target, ns=(stamp, stamp), follow_symlinks=False)
                    applied += 1
            elif target.is_file():
                os.utime(target, ns=(stamp, stamp))
                applied += 1
            else:
                skipped += 1
        except (OSError, ValueError, OverflowError):
            skipped += 1
    return applied, skipped
