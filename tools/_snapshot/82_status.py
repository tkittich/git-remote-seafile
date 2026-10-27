

# -- --status: is my backup current? (read-only) ---------------------------------
#
# Answers "is my backup current?" without staging anything (ROADMAP §1.0).  The
# last snapshot's time and size come from the vault; what changed in the source
# since it is a size-and-mtime comparison against the two records the snapshot
# itself wrote into the tip -- the payload manifest for the large files, and
# ``.seafile-payload/mtimes.json`` for the git-stored ones -- so the baseline
# travels with the snapshot and is not one machine's cache.  The comparison is
# explicitly a heuristic: it is what a stat can see, and the snapshot run
# re-hashes precisely because a change that keeps size and mtime can hide.


def add_status_arguments(parser: Any) -> None:
    parser.add_argument(
        "--status",
        action="store_true",
        help="report the last snapshot's time and size, what changed in the "
        "source since it (a size-and-mtime comparison against the records the "
        "snapshot wrote -- no staging, no hashing, nothing uploaded), the "
        "settings the next run would use, and the vault lock",
    )


def status_conflicts(args: argparse.Namespace) -> list[str]:
    if not args.status:
        return []
    problems: list[str] = []
    if args.restore or args.restore_drill is not None or args.maintain:
        problems.append(
            "--status reports and changes nothing, so it does not combine with "
            "--restore, --restore-drill or --maintain"
        )
    if args.payload_gc or args.payload_verify is not None or args.payload_view is not None:
        problems.append(
            "--status reports and changes nothing, so it does not combine with "
            "the payload commands"
        )
    if args.record_mtimes or args.no_record_mtimes:
        problems.append(
            "--record-mtimes and --no-record-mtimes only apply to a snapshot run"
        )
    if args.no_push or args.dry_run:
        problems.append(
            "--status describes the next run as it would be made with the vault's "
            "remembered settings, so it does not combine with --no-push or --dry-run"
        )
    return problems


def _status_examples(paths: list[str], limit: int = 10) -> list[str]:
    """Up to *limit* paths, then one line saying how many more."""
    shown = [p for p in paths[:limit]]
    more = len(paths) - len(shown)
    if more > 0:
        shown.append(f"... and {more} more")
    return shown


def _status_walk_nested(work_tree: Path, nested: str, excludes: list[str]) -> list[str]:
    """The files a snapshot captures under one embedded repository.

    ``stageable_paths`` reports a nested repository as ``nested/`` and the
    snapshot replaces the gitlink with the files under it (``71_embedded``), so
    a change scan that skipped the subtree would call every file inside it
    invisible.  The walk mirrors the capture's skips: the nested ``.git`` and
    the exclude patterns; a junction or symlink is not descended (the capture
    refuses or reports those, and a status that hang-walked a loop would be
    worse than a slightly short answer).
    """
    found: list[str] = []
    root = work_tree / nested
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, work_tree).replace(os.sep, "/")
        keep = []
        for name in dirnames:
            child = name if rel == "." else f"{rel}/{name}"
            if child == f"{nested}/.git" or name == ".git":
                continue
            if any(exclude_matches(child, pat) for pat in excludes):
                continue
            try:
                if getattr(os.lstat(os.path.join(dirpath, name)), "st_file_attributes", 0) & 0x400:
                    continue
            except (OSError, AttributeError):
                pass
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            child = name if rel == "." else f"{rel}/{name}"
            if any(exclude_matches(child, pat) for pat in excludes):
                continue
            found.append(child)
    return found


def _status_junction_filter(source: Path) -> Any:
    """The run's own junction predicate on Windows (git follows a junction, a run leaves it out)."""
    return _junction_filter(source) if os.name == "nt" else None


def _status_inventory(
    git_dir: Path, source: Path, excludes: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """The files a snapshot would capture now: ``(paths, nested_repos, unreadable_dirs)``.

    ``stageable_paths`` is the run's own list (``git ls-files -c -o`` from the
    vault's index point of view, ignored files included), and the excludes are
    applied with the run's own matcher, so a status cannot disagree with a run
    about what belongs.  Embedded repositories are walked separately, as the
    capture does.  A directory git could not open is returned, not dropped: its
    files are missing from the list, and without the name a status would call
    them deleted while the run it forecasts refuses to start.
    """
    nested: list[str] = []
    unreadable_dirs: list[str] = []
    listed = stageable_paths(git_dir, source, nested_out=nested, warnings_out=unreadable_dirs)
    # The three filters a run applies before staging (70_stage): a status that
    # skipped them would call "new" what no run will ever capture.
    behind = _status_junction_filter(source)
    paths = [
        path for path in listed
        if not any(exclude_matches(path, pat) for pat in excludes)
        and path != PAYLOAD_RESERVED_DIR
        and not path.startswith(PAYLOAD_RESERVED_DIR + "/")
        and not is_clock_probe_debris(path)
        and not (behind is not None and behind(path))
    ]
    nested = [d for d in nested if not (behind is not None and behind(d, True))]
    for repo in nested:
        paths.extend(_status_walk_nested(source, repo, excludes))
    return sorted(set(paths)), nested, unreadable_dirs


def _status_tip_facts(git_dir: Path, work_tree: Path, tip: str) -> tuple[str, str, int, int]:
    """``(ISO time, subject, git-stored file count, git-stored bytes)`` of *tip*."""
    meta = git(
        git_dir, work_tree, ["show", "-s", "--format=%cI%x00%s", tip], cwd=work_tree
    ).stdout
    when, _sep, subject = meta.partition("\x00")
    stored = git(
        git_dir, work_tree, ["ls-tree", "-r", "-l", "-z", tip], cwd=work_tree
    ).stdout
    count = 0
    total = 0
    for entry in stored.split("\0"):
        meta_part, _sep, _path = entry.partition("\t")
        parts = meta_part.split()
        if len(parts) == 4 and parts[1] == "blob" and parts[3].isdigit():
            count += 1
            total += int(parts[3])
    return when.strip(), subject.strip(), count, total


def _status_pushed(
    git_dir: Path, source: Path, branch: str, tip: str, remote: str | None
) -> tuple[bool | None, int | None]:
    """Print the ``pushed`` line; ``(pushed?, snapshots taken since)``, ``None`` when unknown.

    What this vault recorded at its last push, not a look at the server: this
    report reads only local state.  A vault with no remote has nothing to say.
    """
    if not remote:
        return None, None
    last = recorded_pushed_tip(git_dir, source, branch)
    if last is None:
        print(
            "pushed        : not recorded -- this vault has not pushed since it began "
            "keeping that record (the next push does)"
        )
        return None, None
    if last == tip:
        print("pushed        : yes -- the newest snapshot is the one this vault last pushed")
        return True, 0
    counted = git(
        git_dir, source, ["rev-list", "--count", f"{last}..{tip}"], cwd=source, check=False
    )
    ahead = int(counted.stdout.strip()) if counted.returncode == 0 and counted.stdout.strip().isdigit() else None
    if ahead:
        print(
            f"pushed        : NO -- {ahead} snapshot(s) were taken after the last push "
            "(a --no-push run, or a push that failed); the next run pushes them"
        )
        return False, ahead
    print("pushed        : NO -- the newest snapshot is not the one this vault last pushed")
    return False, None


def _status_unknown(tip: str | None, **extra: Any) -> dict[str, Any]:
    """The ``--json`` result when nothing can be compared: every key present, the counts null.

    SNAPSHOT.md promises the keys of a mode are only ever added to; a run with no
    vault used to return ``{"snapshots": 0}`` alone, so ``jq -e '.result.status.changed
    == 0'`` read ``null`` and a scheduler could take "no vault" for "all clear".
    Null never equals 0.
    """
    return {
        "tip": tip,
        "comparable": False,
        "changed": None,
        "new": None,
        "deleted": None,
        "unreadable": None,
        "uncompared": None,
        "pushed": None,
        "unpushed": None,
        **extra,
    }


def status_command(
    args: argparse.Namespace,
    source: Path,
    vault: Path,
    remote: str | None,
    code_remote: str | None,
    exclude: list[str],
    branch: str,
) -> int:
    """Print the status report; never stage, hash or upload anything."""
    payload_value, payload_from_default = resolve_payload_value(
        args.payload_min_size, vault, remote=remote, no_push=False,
        no_payload=args.no_payload,
    )
    if payload_value is None:
        payload_mode = "off"
    elif payload_value == "auto":
        payload_mode = f"auto (ratio {resolve_payload_ratio(args.payload_auto_ratio, vault):.2f})"
    else:
        payload_mode = f"files of {payload_value} or more"
    if payload_from_default and payload_mode != "off":
        payload_mode += " -- the default for a run that pushes to a seafile:// remote"
    mtimes_on = resolve_record_mtimes(args, vault, remote=remote, no_push=False)

    print(f"source        : {source}")
    print(f"vault         : {vault} (branch {branch})")
    print(f"remote        : {remote or 'none (a run would be local-only)'}")
    if code_remote:
        print(f"code remote   : {code_remote}")
    print(f"payload mode  : {payload_mode}")
    print(f"excludes      : {', '.join(exclude) if exclude else '(none)'}")
    print(f"mtimes record : {'on' if mtimes_on else 'off'}")

    git_dir = vault / ".git"
    if not git_dir.is_dir():
        print(
            f"no vault at {vault} yet -- nothing has been snapshotted; "
            "a first run creates it"
        )
        RUN_FACTS.update(status=_status_unknown(None, snapshots=0))
        return 0
    holder = VaultLock(git_dir).holder()
    if holder is None:
        print("lock          : free")
    else:
        host, pid, _start = holder
        alive = " (alive)" if host == socket.gethostname() and pid_is_alive(pid) else ""
        print(f"lock          : held by {host}, pid {pid}{alive}")

    tip = current_tip(git_dir, source, branch)
    if tip is None:
        print(f"last snapshot : none -- branch {branch} has no commits yet")
        RUN_FACTS.update(status=_status_unknown(None, snapshots=0))
        return 0

    when, subject, stored_count, stored_bytes = _status_tip_facts(git_dir, source, tip)
    print(f"last snapshot : {when}  \"{subject}\"")
    print(f"  git-stored  : {stored_count:,} file(s), {human_bytes(stored_bytes)}")

    pushed, unpushed = _status_pushed(git_dir, source, branch, tip, remote)
    manifest = load_manifest(git_dir, source, tip)
    payload_by_path: dict[str, dict[str, Any]] = {}
    if manifest is not None:
        for item in manifest.get("files") or []:
            path = item.get("path")
            if isinstance(path, str):
                payload_by_path[path] = item
        payload_bytes = sum(
            int(e.get("size") or 0) for e in payload_by_path.values()
        )
        print(
            f"  payload     : {len(payload_by_path):,} file(s), {human_bytes(payload_bytes)}"
        )
    recorded = _recorded_mtimes(git_dir, source, tip)
    recorded_sizes = _recorded_sizes(git_dir, source, tip)

    if manifest is None and not recorded:
        print(
            "  the last snapshot carries no change records (a snapshot taken by an "
            "older tool, or with --no-record-mtimes and no payload), so what changed "
            "since it cannot be compared; the next snapshot re-establishes them"
        )
        RUN_FACTS.update(status=_status_unknown(tip))
        return 0

    inventory, _nested, unreadable_dirs = _status_inventory(git_dir, source, exclude)
    inventory_set = set(inventory)
    changed: list[str] = []
    new: list[str] = []
    unreadable: list[str] = []
    uncompared = 0
    for path in inventory:
        try:
            st = os.lstat(extended_path(source / path))
        except OSError:
            unreadable.append(path)
            continue
        entry = payload_by_path.get(path)
        if entry is not None:
            if (
                st.st_size != int(entry.get("size") or -1)
                or st.st_mtime_ns != int(entry.get("mtime_ns") or -1)
            ):
                changed.append(path)
        elif path in recorded:
            if st.st_mtime_ns != recorded[path] or (
                path in recorded_sizes and stat.S_ISREG(st.st_mode)
                and st.st_size != recorded_sizes[path]
            ):
                changed.append(path)
        elif recorded:
            new.append(path)
        else:
            # A payload snapshot taken without the mtimes record: no baseline
            # exists for the git-stored files, and "everything is new" would be
            # the report's only possible answer for every one of them.
            uncompared += 1
    known = set(payload_by_path) | set(recorded)
    # What is under an unreadable directory is missing from the listing, not
    # deleted: it stays out of that count and the directory is named instead.
    prefixes = tuple(d.rstrip("/") + "/" for d in unreadable_dirs)
    deleted = sorted(
        p for p in known - inventory_set if not (prefixes and p.startswith(prefixes))
    )
    unreadable.extend(d.rstrip("/") + "/" for d in unreadable_dirs)

    print(
        "Note: the counts below are a size-and-mtime comparison against the records "
        "the last snapshot wrote; they err on both sides -- a change that keeps its "
        "recorded stats hides from this report (size and mtime, for a payload file and for "
        "a git-stored one whose snapshot recorded sizes; mtime alone for an older "
        "snapshot's), and a file "
        "whose time changed without its content shows as changed though the next run "
        "would find nothing new. The snapshot itself re-hashes what it stages. This "
        "report reads only local state and does not contact the remote: the pushed line is "
        "what this vault recorded at its last push, not a look at the server."
    )
    if uncompared:
        print(
            f"  {uncompared:,} git-stored file(s) could not be compared: the last "
            "snapshot has a payload manifest but no mtimes record (--no-record-mtimes); "
            "the next snapshot re-establishes it"
        )
    print(f"  changed since: {len(changed)} file(s), {len(new)} new, {len(deleted)} deleted")
    for line in _status_examples(sorted(changed)):
        print(f"  changed: {line}")
    for line in _status_examples(sorted(new)):
        print(f"  new: {line}")
    for line in _status_examples(deleted):
        print(f"  deleted: {line}")
    for line in _status_examples(sorted(unreadable)):
        print(f"  unreadable: {line}")
    if unreadable_dirs:
        count = len(unreadable_dirs)
        print(
            f"Warning: {count} director{'y' if count == 1 else 'ies'} could not be read, "
            "so what is under it is in none of the counts above; a snapshot run "
            "stops until it can be read or is left out with --exclude"
        )
    print(
        "quota        : not reported -- --status reads only local state; "
        "the library's page on the server names it"
    )
    RUN_FACTS.update(status={
        "tip": tip,
        "comparable": True,
        "last_snapshot": when,
        "changed": len(changed),
        "new": len(new),
        "deleted": len(deleted),
        "unreadable": len(unreadable),
        "uncompared": uncompared,
        "pushed": pushed,
        "unpushed": unpushed,
    })
    return 0
