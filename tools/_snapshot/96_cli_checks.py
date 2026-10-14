def validate_args(args: argparse.Namespace) -> None:
    """Reject combinations that would otherwise be silently ignored.

    Each of these is a flag that does nothing where it is passed, which is worse
    than a wrong answer: the user believes they asked for something and gets no
    signal either way.  ``--ref`` without ``--restore`` is the sharpest -- a run
    meant to restore an *old* snapshot quietly takes a *new* one instead.
    """
    problems: list[str] = (
        payload_gc_conflicts(args) + payload_conflicts(args) + payload_view_conflicts(args)
        + mtimes_conflicts(args)
    )

    if args.maintain and args.restore:
        problems.append("--maintain and --restore are different modes")
    if args.maintain:
        # Housekeeping works on the vault's object store alone, so every flag
        # that exists for taking or restoring a snapshot is dead weight here.
        for flag, value in (
            ("--dry-run", args.dry_run),
            ("--message", args.message),
            ("--code-remote", args.code_remote),
            ("--exclude", args.exclude),
            ("--default-excludes", args.default_excludes),
            ("--reset-to-remote", args.reset_to_remote),
            ("--allow-shared-remote", args.allow_shared_remote),
            ("--no-push", args.no_push),
            ("--no-embedded-repos", args.no_embedded_repos),
            ("--verify", args.verify),
            ("--into", args.into),
            ("--ref", args.ref),
            # Housekeeping performs the repack itself; skipping it is a
            # snapshot-run concern.
            ("--no-repack", args.no_repack),
        ):
            if value:
                problems.append(f"{flag} does not apply to --maintain")
    elif not args.restore:
        for flag, value in (("--verify", args.verify), ("--into", args.into),
                            ("--ref", args.ref)):
            if value:
                problems.append(f"{flag} only applies to --restore")
    else:
        for flag, value in (
            ("--vault", args.vault),
            ("--dry-run", args.dry_run),
            ("--message", args.message),
            ("--code-remote", args.code_remote),
            ("--exclude", args.exclude),
            ("--default-excludes", args.default_excludes),
            ("--reset-to-remote", args.reset_to_remote),
            ("--allow-shared-remote", args.allow_shared_remote),
            ("--no-push", args.no_push),
            ("--no-embedded-repos", args.no_embedded_repos),
            # A restore fetches and checks out; it packs nothing, so skipping
            # the push-time repack would be silently ignored.
            ("--no-repack", args.no_repack),
            # A restore fetches and checks out; it writes no new objects, so a
            # compression preference here would be silently ignored.
            ("--no-object-compression", args.no_object_compression),
            # The source is meaningless on a restore -- the tree comes from the
            # remote -- and would otherwise be silently ignored.
            ("--source", args.source),
            ("SOURCE (positional)", args.source_positional),
        ):
            if value:
                problems.append(f"{flag} does not apply to --restore")

    # Outside --maintain there is no repack to run, so the switch that selects
    # one cannot mean anything (it would otherwise be silently ignored).
    if not args.maintain and args.full_repack:
        problems.append("--full-repack only applies to --maintain")

    if args.no_embedded_repos and (
        args.payload_gc or args.payload_verify is not None or args.payload_view is not None
    ):
        problems.append("--no-embedded-repos only applies to a snapshot run")

    if args.no_push and args.remote:
        problems.append("--no-push and --remote contradict each other")
    if args.no_push and args.code_remote and not (args.maintain or args.restore):
        # Only the typed flag: a remembered code remote under --no-push is a
        # default, and is skipped without complaint.
        problems.append(
            "--code-remote pushes the source repository, which --no-push "
            "forbids; drop one of them"
        )
    if (
        args.source is not None
        and args.source_positional is not None
        and args.source.expanduser().resolve()
        != Path(args.source_positional).expanduser().resolve()
    ):
        problems.append(
            f"two different sources were given: '{args.source_positional}' "
            f"(positional) and '{args.source}' (--source)"
        )

    if problems:
        raise SnapshotError(
            "the arguments do not make sense together:\n  "
            + "\n  ".join(problems)
        )


def resolve_snapshot_settings(
    args: argparse.Namespace,
) -> tuple[Path, Path, str | None, str | None, list[str], str]:
    """Turn a parsed command line into the settings a run actually uses.

    The chain, most specific first:

    1. **the flag you passed** -- always wins;
    2. **what the vault remembered** from its last run;
    3. **the source repository** -- a single ``seafile://`` remote names the
       vault's destination;
    4. **the current directory** for ``--source``, ``'<source>-vault'`` for
       ``--vault``.

    Returns ``(source, vault, remote, code_remote, exclude, branch)``.
    """
    explicit = args.source or args.source_positional
    source = (explicit or Path.cwd()).expanduser().resolve()
    vault = (args.vault or default_vault_for(source)).expanduser().resolve()
    source, vault = resolve_paths(source, vault)

    # A vault may not exist yet, so the bottom of the chain is remembered-first:
    # there is nothing to remember until a first run has happened.
    remembered = remembered_settings(vault, source)

    branch = args.branch or str(remembered.get("branch") or DEFAULT_BRANCH)

    remote: str | None
    code_remote: str | None
    if args.no_push:
        # Suppressed for THIS run only.  remember_settings is told (via _UNSET)
        # to leave the vault's memory alone, so a one-off local run does not
        # unhook a scheduled job from its remote; the code repo is not pushed
        # either -- the flag's name promises "do not push at all".
        remote = None
        code_remote = None
    else:
        remote = args.remote or remembered.get("remote")
        if not remote:
            remote = derive_vault_remote(source)
            if remote and not args.dry_run:
                # Said once: after the first complete run the vault remembers it.
                print(
                    f"note: the vault's destination was not given, so it is {remote}, "
                    "derived from the source's own seafile:// remote -- the same library as "
                    "your code.  Everyone who can read that library can read this snapshot, "
                    ".env files and keys included.  Pass --remote to send it somewhere only "
                    "you can read.",
                    flush=True,
                )
        code_remote = args.code_remote or remembered.get("code_remote")

    exclude = list(args.exclude) if args.exclude is not None else list(
        remembered.get("exclude") or []
    )
    if args.default_excludes:
        for pattern in RECOMMENDED_EXCLUDES:
            if pattern not in exclude:
                exclude.append(pattern)

    # Everything that can be checked before a single byte is written: a typo in
    # a remote name or a vault inside a synced folder is far cheaper to catch
    # now than after the first push has published a whole tree.
    if remote:
        check_remote_is_not_a_code_remote(
            source, remote, allowed=args.allow_shared_remote
        )
    if code_remote:
        check_code_remote_is_git_remote(source, code_remote)
    check_paths_are_unsynced(
        ("source", source), ("vault", vault), allow_synced=args.allow_synced
    )

    return source, vault, remote, code_remote, exclude, branch


def check_code_remote_is_git_remote(source: Path, name: str) -> None:
    """Fail before the snapshot if ``--code-remote <name>`` is not a remote.

    ``git push origin`` against a name that does not exist fails *after* the
    snapshot is built, which is a confusing half-done state: the vault has a new
    commit for a run the user will read as failed.  A one-line check up front
    turns that into the error it is.
    """
    if not source.is_dir() or not (source / ".git").exists():
        raise SnapshotError(
            f"--code-remote '{name}' was given, but {source} is not a git "
            f"repository, so there is no '{name}' to push to"
        )
    names = {remote_name for remote_name, _url in source_remote_urls(source)}
    if name not in names:
        listing = ", ".join(sorted(names)) or "none"
        raise SnapshotError(
            f"--code-remote '{name}' is not a remote of {source} "
            f"(it has: {listing})"
        )
