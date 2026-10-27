

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
    return f"{PAYLOAD_RESERVED_DIR}/{_MTIMES_NAME}"


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
    proc = git(
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
    if args.no_record_mtimes:
        return False
    if args.record_mtimes:
        return True
    if (vault / ".git").exists():
        remembered = _config(vault, "snapshot.mtimes")
        if remembered in ("0", "1"):
            return remembered == "1"
    return parent_has_record or payload_by_default_possible(remote, no_push)


def remember_record_mtimes(vault: Path, args: argparse.Namespace, *, dry_run: bool) -> None:
    """Remember an explicit choice after a complete real run (never a default)."""
    if dry_run or not (args.record_mtimes or args.no_record_mtimes):
        return
    _run(
        ["git", "-C", str(vault), "config", "snapshot.mtimes", "0" if args.no_record_mtimes else "1"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _blobs_of_tree(git_dir: Path, work_tree: Path, rev: str) -> dict[str, str]:
    """``{path: blob id}`` for every file in snapshot ``rev``."""
    listing = git(git_dir, work_tree, ["ls-tree", "-r", "-z", rev], cwd=work_tree).stdout
    found: dict[str, str] = {}
    for entry in listing.split("\0"):
        meta, _sep, path = entry.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[1] == "blob":
            found[path] = parts[2]
    return found


def _recorded_mtimes(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, int]:
    """The times recorded in snapshot ``rev``; empty when it has none or they are unusable."""
    if not rev:
        return {}
    proc = git(
        git_dir, work_tree, ["show", f"{rev}:{mtimes_path()}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
        files = data["files"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}
    if not isinstance(files, dict):
        return {}
    return {
        path: value for path, value in files.items()
        if isinstance(path, str) and isinstance(value, int) and not isinstance(value, bool)
        and 0 <= value < _MAX_MTIME_NS
    }


def _recorded_sizes(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, int]:
    """The sizes recorded in snapshot ``rev`` beside the times; empty for a snapshot that has none.

    An additive key of the same document (``"sizes"``), so a tool that does not
    know it reads ``"files"`` as before.  Only regular files carry one: a
    symlink's own size is its target's length and says nothing about content.
    """
    if not rev:
        return {}
    proc = git(
        git_dir, work_tree, ["show", f"{rev}:{mtimes_path()}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return {}
    try:
        sizes = json.loads(proc.stdout)["sizes"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}
    if not isinstance(sizes, dict):
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
    listing = git(git_dir, work_tree, ["ls-files", "-s", "-z"], cwd=work_tree).stdout
    current: dict[str, str] = {}
    for entry in listing.split("\0"):
        meta, _sep, path = entry.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[0] != "160000":
            current[path] = parts[1]
    wanted = {
        path: blob for path, blob in current.items()
        if path != PAYLOAD_RESERVED_DIR and not path.startswith(PAYLOAD_RESERVED_DIR + "/")
    }
    if parent and wanted == {
        path: blob for path, blob in _blobs_of_tree(git_dir, work_tree, parent).items()
        if path != PAYLOAD_RESERVED_DIR and not path.startswith(PAYLOAD_RESERVED_DIR + "/")
    }:
        previous = git(
            git_dir, work_tree, ["rev-parse", "--verify", "-q", f"{parent}:{mtimes_path()}"],
            cwd=work_tree, check=False,
        )
        if previous.returncode == 0 and previous.stdout.strip():
            git(
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
            info = os.lstat(extended_path(work_tree / path))
        except OSError:
            continue
        stamp = info.st_mtime_ns
        if 0 <= stamp < _MAX_MTIME_NS:
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
    blob_id = git(
        git_dir, work_tree, ["hash-object", "-w", "--no-filters", "--stdin"],
        cwd=work_tree, stdin=document,
    ).stdout.strip()
    git(
        git_dir, work_tree,
        ["update-index", "--add", "--cacheinfo", f"100644,{blob_id},{mtimes_path()}"],
        cwd=work_tree,
    )
    return len(times)


def apply_recorded_mtimes(into: Path) -> tuple[int, int]:
    """Set the recorded times on a restored tree; ``(applied, skipped)``.

    Best effort and bounded to *into*: the record came from a remote, so each
    path goes through the same checks as a manifest path, and a time that does
    not fit is skipped rather than written.
    """
    record = into / PAYLOAD_RESERVED_DIR / _MTIMES_NAME
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        files = data["files"]
        if not isinstance(files, dict):
            return 0, 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return 0, 0
    root = into.resolve()
    applied = skipped = 0
    for rel, stamp in files.items():
        if (
            not isinstance(rel, str) or isinstance(stamp, bool) or not isinstance(stamp, int)
            or not 0 <= stamp < _MAX_MTIME_NS or path_security_problem(rel) is not None
            or windows_name_problem(rel) is not None
            # A record entry is remote data like a manifest entry, so it gets
            # the same Windows-name check: `.git::$INDEX_ALLOCATION/config`
            # passed the security check and moved .git/config's time
            # (REVIEW.sonnet-5.5 D10, reproduced).
        ):
            skipped += 1
            continue
        plain = into / rel
        target = extended_path(plain)  # past 260 characters on Windows too
        try:
            if os.path.commonpath([str(root), str(plain.parent.resolve())]) != str(root):
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
