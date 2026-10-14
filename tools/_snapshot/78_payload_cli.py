

# -- Payload command line (see 75_payload.py) --------------------------------------

# -- command line ----------------------------------------------------------------


PAYLOAD_EPILOG = (
    "\npayload mode (large files): on by default ('auto') when the vault has a\n"
    "seafile:// remote. Files of 64 MiB or more, and 4 MiB or more that do not\n"
    "compress, go to Seafile by hash instead of through git; a tree with no such\n"
    "file is snapshotted exactly as without it. --no-payload turns it off, and a\n"
    "complete real run remembers the setting. --restore, --payload-verify,\n"
    "--payload-gc and --payload-view act on a remote's payload; see PAYLOAD.md.\n"
)


PAYLOAD_DEFAULT_NOTICE = (
    "payload: on by default (auto: files of 64 MiB or more, or 4 MiB or more that do not "
    "compress, bypass git); --no-payload turns it off and is remembered"
)


def add_payload_groups(parser: argparse.ArgumentParser) -> None:
    """Every payload option, in two help groups: taking a snapshot, and tools."""
    mode_group = parser.add_argument_group(
        "payload mode: large files stored by hash instead of through git",
        "On by default (auto) when there is a seafile:// remote and the run pushes; "
        "--no-payload turns it off. A complete real run remembers the setting in the "
        "vault. See PAYLOAD.md.",
    )
    add_payload_arguments(mode_group)
    add_payload_jobs_argument(mode_group)
    tools_group = parser.add_argument_group(
        "payload tools: act on a remote's payload (no snapshot is taken)",
        "Each needs --remote. Reports and verification change nothing; "
        "--payload-delete and --payload-view write only what they say.",
    )
    add_payload_gc_arguments(tools_group)
    add_payload_view_arguments(tools_group)


def add_payload_arguments(parser: Any) -> None:
    parser.add_argument(
        "--payload-min-size",
        metavar="SIZE|auto",
        nargs="?",
        const="auto",
        default=None,
        help="payload mode: files bypass git; they are hashed (SHA-256) and uploaded to "
        "Seafile by hash under <path>/payload/objects, and the snapshot records a manifest "
        "instead. With no value, or 'auto': files of 64 MiB or more, plus files of 4 MiB or "
        "more that do not compress. With SIZE (e.g. 64M, 1G): files of at least SIZE. "
        "Needs a seafile:// remote; a restore downloads and verifies them. Default: what "
        "the vault remembered, else auto; 0 (or --no-payload) turns it off",
    )
    parser.add_argument(
        "--no-payload",
        action="store_true",
        help="payload mode: off for this run, and remembered (the same as "
        "--payload-min-size 0, without its argument parsing); a vault whose last "
        "snapshot used payload mode keeps its large files out of git until it is "
        "turned back on, so this stages them into git instead",
    )


def add_payload_jobs_argument(parser: Any) -> None:
    parser.add_argument(
        "--payload-rehash",
        action="store_true",
        help="payload mode: ignore the hash cache for this run and hash every payload file "
        "again (default: use the cache), e.g. for a tree whose timestamps were restored by a "
        "copy tool, or as a periodic audit",
    )
    parser.add_argument(
        "--payload-jobs",
        type=int,
        default=None,
        metavar="N",
        help="payload mode: upload N files at once (default 3; measured on one server: "
        "1 upload ~32 MiB/s, 3 at once ~53 MiB/s, so more rarely helps)",
    )


def payload_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.no_payload and args.payload_min_size is not None:
        problems.append("--no-payload and --payload-min-size contradict each other")
    # Only a flag typed on this command line is checked here: a default or a
    # remembered setting must never turn a run that worked into an error.
    if args.payload_min_size is not None and args.no_push:
        problems.append(
            "--payload-min-size cannot be combined with --no-push: payload files exist "
            "only on the server, so a snapshot that is not pushed would reference files "
            "that were never uploaded"
        )
    if args.payload_jobs is not None:
        if args.payload_jobs < 1:
            problems.append("--payload-jobs must be at least 1")
        if args.no_payload or (
            args.payload_min_size is not None and not _explicit_size(args.payload_min_size)
        ):
            problems.append("--payload-jobs only applies when payload mode is on")
    if args.restore or args.maintain:
        for flag, given in (("--payload-min-size", args.payload_min_size is not None),
                            ("--no-payload", args.no_payload)):
            if given:
                problems.append(f"{flag} does not apply to --restore or --maintain")
    if args.payload_rehash and (args.restore or args.maintain or args.payload_gc):
        problems.append("--payload-rehash only applies to a snapshot run")
    return problems


def _explicit_size(value: str) -> bool:
    """True unless the value is a size of zero, which means "off"."""
    text = value.strip().lower()
    if text == "auto":
        return True
    try:
        return parse_size(value) > 0
    except SnapshotError:
        return True  # a malformed size is reported by plan_payload with its own message


def payload_by_default_possible(remote: str | None, no_push: bool) -> bool:
    """Whether payload mode may come on by default: the run pushes to a seafile:// remote."""
    if not remote or no_push:
        return False
    # parse_seafile_url is lenient (it reads a plain path as library/path), so the
    # scheme is checked first: a local path or an ssh URL is never a Seafile remote.
    if not remote.lower().startswith("seafile://"):
        return False
    try:
        payload_location(remote)
    except SnapshotError:
        return False
    return True


def resolve_payload_value(
    value: str | None, vault: Path, *, remote: str | None = None, no_push: bool = False,
    no_payload: bool = False,
) -> tuple[str | None, bool]:
    """``(value, from_default)``: the flag as given, else what this vault remembered
    from its last real run, else ``auto`` when the run pushes to a seafile:// remote.

    So a scheduled bare ``seafile_snapshot.py`` keeps using payload mode (or keeps
    deliberately not using it, if ``0`` was remembered) the way the remote and the
    excludes are kept.  The default quietly stays off where payload mode cannot work
    (no remote, a remote that is not seafile://, ``--no-push``): a default must not
    make a run fail that worked before.
    """
    if no_payload:
        return "0", False
    if value is not None:
        return value, False
    possible = payload_by_default_possible(remote, no_push)
    if (vault / ".git").exists():
        remembered = _config(vault, "snapshot.payload")
        # A remembered value is not a request for *this* run: a vault that
        # remembered payload mode still takes a local --no-push snapshot, or one
        # without a seafile:// remote, without an error (explicit flags are
        # checked in payload_conflicts and plan_payload).
        if remembered and possible:
            return remembered, False
        if remembered:
            return None, False
    if possible:
        return "auto", True
    return None, False


def remember_payload_value(vault: Path, value: str | None, *, dry_run: bool) -> None:
    """Write the setting after a *complete* real run, never from a preview."""
    if value is None or dry_run:
        return
    _run(
        ["git", "-C", str(vault), "config", "snapshot.payload", value],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def plan_payload(
    value: str | None, remote: str | None, source: Path, excludes: list[str]
) -> tuple[int | None, dict[str, Candidate], str]:
    """``(min_size, candidates, rule)``; ``(None, {}, "size")`` when payload mode is off."""
    auto = value is not None and value.strip().lower() == "auto"
    if value is not None and not auto and value.strip().isdigit() and Path(value).is_dir():
        raise SnapshotError(
            f"--payload-min-size took {value!r} as its size, but that is also a directory: "
            "put SOURCE before the flag, or write --payload-min-size=auto"
        )
    min_size = _AUTO_UPPER if auto else (parse_size(value) if value is not None else None)
    rule = "auto" if auto else "size"
    if not min_size:
        return min_size, {}, rule
    if not remote:
        raise SnapshotError(
            "payload mode needs a remote to upload to: pass --remote "
            "seafile://<library>/<path> (or run from a source with a seafile:// remote)"
        )
    return min_size, find_payload_files(source, excludes, min_size, auto), rule


def refuse_flagless_payload_vault(
    git_dir: Path, source: Path, branch: str, min_size: int | None
) -> None:
    """Refuse a run without the flag when the local branch's last snapshot used
    payload mode -- before anything outward happens (a ``--code-remote`` push)."""
    if min_size is not None:
        return
    tip = current_tip(git_dir, source, branch)
    previous = load_manifest(git_dir, source, tip)
    if previous is not None:
        raise SnapshotError(
            "this vault's last snapshot used payload mode, so its large files are not in "
            "git; running without --payload-min-size would stage them into git instead. "
            "Repeat the flag (--payload-min-size "
            f"{'auto' if previous.get('rule') == 'auto' else previous.get('min_size', 'SIZE')}), "
            "or pass --no-payload (--payload-min-size 0) to leave payload mode deliberately. A --no-push snapshot cannot use payload mode, since its files exist only on the server: for a local-only snapshot use --no-payload, or drop --no-push"
        )


def conversion_warning(candidates: dict[str, Candidate], dry_run: bool) -> str:
    """Said *before* any hashing or upload, when the default moves an existing vault's
    large files out of git: a scheduled run cannot be asked, but it can be read."""
    total = sum(size for size, _mtime, _mode, _ino in candidates.values())
    verb = "would move" if dry_run else "is moving"
    return (
        f"NOTICE: payload mode is on by default and this vault has earlier snapshots that "
        f"did not use it. This run {verb} {len(candidates)} file(s) "
        f"({human_bytes(total)}) out of git and uploads them as payload objects (one full "
        f"pass).\n"
        f"  - Copies already in the vault's history stay there, so the server holds them "
        f"twice; the vault does not shrink.\n"
        f"  - Snapshots taken before this one restore as before; this one and later ones "
        f"need the payload (0.8.8 cannot restore them completely).\n"
        f"  - To keep this vault as it was: stop now and run with --no-payload (remembered)."
    )


def run_payload_step(
    git_dir: Path,
    source: Path,
    base: str | None,
    remote: str | None,
    min_size: int | None,
    candidates: dict[str, Candidate],
    *,
    dry_run: bool,
    jobs: int | None = None,
    rule: str = "size",
    rehash: bool = False,
    from_default: bool = False,
) -> tuple[dict[str, Any] | None, tuple[int, int]]:
    """Hash and upload the payload; ``(manifest, (objects, bytes) to upload)``."""
    warn_if_newer_tool(git_dir, source, base)
    previous = load_manifest(git_dir, source, base)
    if previous is not None and min_size is None:
        raise SnapshotError(
            "this vault's last snapshot used payload mode, so its large files are not "
            "in git; running without --payload-min-size would stage them into git "
            "instead. Repeat the flag (--payload-min-size "
            f"{'auto' if previous.get('rule') == 'auto' else previous.get('min_size', 'SIZE')}), "
            "or pass --no-payload (--payload-min-size 0) to leave payload mode deliberately. A --no-push snapshot cannot use payload mode, since its files exist only on the server: for a local-only snapshot use --no-payload, or drop --no-push"
        )
    if not min_size:
        return None, (0, 0)
    if from_default and previous is not None and previous.get("rule") != "auto":
        # Two machines on different thresholds would move files between git and
        # the payload on every alternate run: the default must not choose one.
        raise SnapshotError(
            "this remote's snapshots use payload rule --payload-min-size "
            f"{previous.get('min_size', 'SIZE')}, but this vault has no setting yet and the "
            "default is auto. Repeat the flag the remote uses, or pass --payload-min-size "
            "auto to switch deliberately"
        )
    if not candidates and previous is None:
        # Nothing qualifies and the vault never used payload mode: no manifest,
        # so a tree that merely *could* grow payload files is not locked into
        # needing the flag.
        return None, (0, 0)
    if from_default and previous is None and base is not None:
        print(conversion_warning(candidates, dry_run), flush=True)
    manifest, cache = build_manifest(
        candidates, source, {} if rehash else load_hash_cache(git_dir), min_size, rule=rule
    )
    todo = publish_payload(
        remote or "", source, manifest, dry_run=dry_run, jobs=jobs or 3
    )
    if not dry_run:
        save_hash_cache(git_dir, cache)
    return manifest, todo


def payload_summary(
    manifest: dict[str, Any], todo: tuple[int, int], dry_run: bool,
    *, remote: str | None = None, from_default: bool = False,
) -> str:
    total = sum(item["size"] for item in manifest["files"])
    verb = "to upload" if dry_run else "uploaded"
    line = (
        f"payload: {len(manifest['files'])} file(s) ({human_bytes(total)}) bypass git; "
        f"snapshot {manifest['snapshot']}; {verb} {todo[0]} object(s) ({human_bytes(todo[1])})"
    )
    warning = portable_name_warning([item["path"] for item in manifest["files"]])
    text = line + (warning or "")
    if from_default:
        text = PAYLOAD_DEFAULT_NOTICE + "\n" + text
    if remote and todo[0] and not dry_run:
        # Nothing here ever deletes an object, so say how big the store has become.
        store = payload_store_line(remote)
        if store:
            text += "\n" + store
    return text
