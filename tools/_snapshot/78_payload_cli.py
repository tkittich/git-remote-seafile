

# -- Payload command line (see 75_payload.py) --------------------------------------

# -- command line ----------------------------------------------------------------


def add_payload_arguments(parser: argparse.ArgumentParser) -> None:
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
        "Needs a remote; a restore downloads and verifies them. 0 turns it off. A vault "
        "that has used it needs the flag on every run (PAYLOAD.md)",
    )


def add_payload_jobs_argument(parser: argparse.ArgumentParser) -> None:
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
    if args.payload_min_size is not None and args.no_push:
        problems.append(
            "--payload-min-size cannot be combined with --no-push: payload files exist "
            "only on the server, so a snapshot that is not pushed would reference files "
            "that were never uploaded"
        )
    if args.payload_jobs is not None:
        if args.payload_jobs < 1:
            problems.append("--payload-jobs must be at least 1")
        if not args.payload_min_size:
            problems.append("--payload-jobs only applies with --payload-min-size")
    if args.payload_min_size is not None and (args.restore or args.maintain):
        problems.append("--payload-min-size does not apply to --restore or --maintain")
    return problems


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
            "or pass --payload-min-size 0 to leave payload mode deliberately"
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
) -> tuple[dict[str, Any] | None, tuple[int, int]]:
    """Hash and upload the payload; ``(manifest, (objects, bytes) to upload)``."""
    previous = load_manifest(git_dir, source, base)
    if previous is not None and min_size is None:
        raise SnapshotError(
            "this vault's last snapshot used payload mode, so its large files are not "
            "in git; running without --payload-min-size would stage them into git "
            "instead. Repeat the flag (--payload-min-size "
            f"{'auto' if previous.get('rule') == 'auto' else previous.get('min_size', 'SIZE')}), "
            "or pass --payload-min-size 0 to leave payload mode deliberately"
        )
    if not min_size:
        return None, (0, 0)
    if not candidates and previous is None:
        # Nothing qualifies and the vault never used payload mode: no manifest,
        # so a tree that merely *could* grow payload files is not locked into
        # needing the flag.
        return None, (0, 0)
    manifest, cache = build_manifest(
        candidates, source, load_hash_cache(git_dir), min_size, rule=rule
    )
    todo = publish_payload(
        remote or "", source, manifest, dry_run=dry_run, jobs=jobs or 3
    )
    if not dry_run:
        save_hash_cache(git_dir, cache)
    return manifest, todo


def payload_summary(manifest: dict[str, Any], todo: tuple[int, int], dry_run: bool) -> str:
    total = sum(item["size"] for item in manifest["files"])
    verb = "to upload" if dry_run else "uploaded"
    line = (
        f"payload: {len(manifest['files'])} file(s) ({human_bytes(total)}) bypass git; "
        f"snapshot {manifest['snapshot']}; {verb} {todo[0]} object(s) ({human_bytes(todo[1])})"
    )
    warning = portable_name_warning([item["path"] for item in manifest["files"]])
    return line + (warning or "")
