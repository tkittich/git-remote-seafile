

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="git-seafile-snapshot",
        description=(
            "Snapshot a working tree (gitignored files included) into a separate "
            "vault repository and push it to Seafile. The source directory is "
            "never modified.\n\n"
            "Every option has a default, so a bare run works: the source is the "
            "current directory, the vault is '<source>-vault' beside it, and the "
            "remote, branch and exclusions are remembered in the vault after the "
            "first run. An explicit flag always wins, and is written back to the "
            "vault for next time."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "defaults, in the order they are tried:\n"
            "  1. the flag you passed\n"
            "  2. what the vault remembered from its last run\n"
            "  3. the source repository (a seafile:// remote names the vault's\n"
            "     destination: seafile://code/app -> seafile://code/app-vault)\n"
            "  4. the current directory (--source) / '<source>-vault' (--vault)\n"
            "     (inside a git repository, the repository root: a snapshot is of the\n"
            "     whole repository, and --source naming a subfolder is refused)\n"
            "\nrecommended exclusions (never applied silently -- this is what\n"
            "--default-excludes adds):\n  "
            + " ".join(f"--exclude '{p}'" for p in RECOMMENDED_EXCLUDES)
            + "\n  plus each Python virtualenv found (a directory holding pyvenv.cfg)"
            "\n\nA pattern with no slash matches that name at ANY depth, so\n"
            "--exclude node_modules also drops packages/*/node_modules.\n"
            "Quote wildcards so your own shell does not expand them first.\n"
            + PAYLOAD_EPILOG
            + "\nNote: neither the source nor the vault may live inside a\n"
            "Seafile-synced folder -- the sync client rewrites files underneath\n"
            "it and competes with git for every path. Checked at startup.\n"
            "The guard lets a synced path through when that library's auto sync\n"
            "is disabled or seafile-ignore.txt covers it; --allow-synced\n"
            "overrides the refusal entirely.\n"
            "\nlong steps (staging large trees, first pushes of large vaults,\n"
            "restores) print one status line to stderr every minute while they\n"
            "run; push and clone also stream git's own progress live.  A step\n"
            "with no measurable progress for 30 minutes is killed as stalled.\n"
            "The remote helper auto-compacts a busy vault after each push --\n"
            "that runs inside the push step and shows up under its status lines.\n"
        ),
    )
    parser.add_argument(
        "source_positional",
        nargs="?",
        type=Path,
        default=None,
        metavar="SOURCE",
        help="directory to snapshot (same as --source, so "
        "'seafile_snapshot.py C:/code/app' works)",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="directory to snapshot (default: the current directory)",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault repository location (default: '<source>-vault' beside the "
        "source)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help=f"branch in the vault holding snapshots "
        f"(default: remembered, else {DEFAULT_BRANCH})",
    )
    parser.add_argument(
        "--remote",
        default=None,
        help="Seafile remote URL to push to, e.g. seafile://code/myproject-vault "
        "(default: remembered, else derived from the source's own seafile:// "
        "remote; pass --no-push to snapshot locally instead)",
    )
    parser.add_argument(
        "--code-remote",
        default=None,
        help="also run a normal 'git push <name>' in the source repo first "
        "(default: remembered, else the code repo is not pushed)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help="path matching this glob is left out of the snapshot (repeatable; "
        "default: remembered, else nothing is excluded)",
    )
    parser.add_argument(
        "--default-excludes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="add the recommended exclusions on top of any --exclude "
        f"({', '.join(RECOMMENDED_EXCLUDES)}) and any Python virtualenv (a "
        "directory holding a pyvenv.cfg, whatever its name). A pattern is kept for the run "
        "when the source's own git repository tracks a file it matches. Not on by default: a snapshot is "
        "a backup, and silently dropping files is how a backup fails at the one "
        "moment it mattered",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="this run pushes nothing -- neither the vault nor the code repo "
        "-- and leaves the vault's remembered remote alone, so a one-off "
        "local run does not unhook a scheduled job",
    )
    parser.add_argument("--message", default=None, help="snapshot commit message")
    parser.add_argument(
        "--restore",
        action="store_true",
        help="restore a snapshot instead of taking one (needs --remote and "
        "--into); byte-exact, unlike a plain git clone",
    )
    parser.add_argument(
        "--into",
        type=Path,
        default=None,
        help="destination directory for --restore",
    )
    parser.add_argument(
        "--ref",
        default=None,
        help=f"snapshot to restore (default: the vault branch, "
        f"{DEFAULT_BRANCH}); any revision works, e.g. 'snapshot~3' to roll back",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the snapshot but do not move the branch or push "
        "(the vault is still initialised if missing, so its location and "
        "config can be inspected; the remote is still fetched, so the diff is "
        "shown against the real base). It is a full run up to the commit: the "
        "git-bound part of the tree is staged into the vault, so it takes the "
        "time and disk a real run would, and it uploads nothing",
    )
    parser.add_argument(
        "--reset-to-remote",
        action="store_true",
        help="if the local vault and the remote have diverged, discard the "
        "local-only snapshots and continue from the remote chain instead of "
        "refusing",
    )
    parser.add_argument(
        "--allow-shared-remote",
        action="store_true",
        help="permit --remote to be a URL the source repository already uses. "
        "The snapshot holds the files .gitignore keeps out of the code "
        "repository, so this publishes them; refused by default",
    )
    parser.add_argument(
        "--allow-synced",
        action="store_true",
        help="proceed even when the source or vault (or a restore destination) "
        "is inside a Seafile-synced library. The guard already lets this through "
        "when that library's auto sync is disabled or the path is covered by its "
        "seafile-ignore.txt; this overrides every remaining refusal and puts the "
        "risk of the client rewriting files under Git on you",
    )
    parser.add_argument(
        "--allow-low-disk",
        action="store_true",
        help="stage even when the estimate says the vault's volume cannot hold "
        "the tree. The estimate is an upper bound (tree size, scaled by the "
        "sampled compressibility, minus what the vault already stores); this "
        "is for data you know is more compressible than the sample suggests. "
        "With --restore: download a payload that does not appear to fit",
    )
    add_payload_groups(parser)
    add_mtimes_arguments(parser)
    add_status_arguments(parser)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="with --restore, also run 'git fsck' on the clone. Byte-exactness "
        "is checked on every restore regardless",
    )
    parser.add_argument(
        "--restore-drill",
        nargs="?",
        const="10",
        default=None,
        metavar="N",
        help="prove the newest snapshot of --remote restores: clone it into a "
        "temporary directory, check every file byte-for-byte and run git fsck, "
        "download N randomly chosen payload files (default 10) and check their "
        "SHA-256, then delete the directory. Needs the disk for the whole vault "
        "(set TMP or TMPDIR to use a bigger volume); --ref drills an older snapshot",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write one JSON document to stdout (status, exit_code, warnings, error, "
        "result) and move the human text to stderr; for scheduled runs",
    )
    parser.add_argument(
        "--exit-warnings",
        action="store_true",
        help="exit 3 instead of 0 when the run completed but printed a Warning:",
    )
    parser.add_argument(
        "--no-object-compression",
        action="store_true",
        help=        "disable deflation for newly written vault objects "
        "(repo-local core.compression=0, stored at full size). For trees "
        "dominated by already-"
        "compressed payloads -- zip/SAFE archives, images, video -- deflation "
        "saves nothing and costs 2-3x the staging time; this flag removes it. "
        "For compressible text-heavy trees it trades some vault size for speed",
    )
    parser.add_argument(
        "--maintain",
        action="store_true",
        help="local vault housekeeping instead of a snapshot: by default just "
        "reports on the object store; with --full-repack it consolidates the "
        "whole store into one packfile and prunes unreachable objects. Never "
        "touches the source tree or any remote",
    )
    parser.add_argument(
        "--full-repack",
        action="store_true",
        help="with --maintain, run a one-shot full repack + prune of the entire "
        "object store. Ordinary snapshot runs already pack the loose tier "
        "incrementally before each push (once it passes roughly 256 MiB); "
        "use this after large deletions to reclaim space",
    )
    parser.add_argument(
        "--no-embedded-repos",
        action="store_true",
        help="record a nested git repository (a directory with its own .git) only as "
        "its commit id, as 0.9.0 did, instead of capturing its files",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="drop the vault's index before staging, so every git-stored file is read and "
        "hashed again. The git-side counterpart of --payload-rehash: an edit that left size "
        "and timestamp untouched (a memory-mapped writer on Windows, a volume with no change "
        "time) is invisible to the stat cache, and this is the audit that re-reads the bytes",
    )
    parser.add_argument(
        "--no-repack",
        action="store_true",
        help="skip the automatic incremental repack that runs before a push",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # A path that is not UTF-8 reaches us as lone surrogates (see git()); the
    # default strict stdout would turn naming it into a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass
    return run_reported(parse_args(argv), run_command)


def run_command(args: argparse.Namespace) -> int:
    END_OF_RUN_NOTES.clear()
    # Set when the push landed but a check *behind* it failed: the run exits
    # non-zero without unwinding past the remember block.
    late_failure = False

    try:
        validate_args(args)
        payload_rc = payload_command(args)
        if payload_rc is not None:
            return payload_rc
        if args.restore_drill is not None:
            return restore_drill_command(args)
        if args.restore:
            if not args.remote:
                raise SnapshotError("--restore requires --remote <url>")
            if not args.into:
                raise SnapshotError("--restore requires --into <directory>")
            into = args.into.expanduser().resolve()
            # The snapshot guard protects where the tool *writes* a tree; a
            # restore writes one too, and a restored tree -- secrets included,
            # by design -- inside a synced library would be uploaded straight
            # back into its original library by the desktop client.
            check_paths_are_unsynced(
                ("restore destination", into), allow_synced=args.allow_synced
            )
            restore(
                args.remote,
                into,
                args.branch or DEFAULT_BRANCH,
                args.ref,
                verify=args.verify,
                branch_explicit=bool(args.branch),
                allow_low_disk=args.allow_low_disk,
            )
            RUN_FACTS.update(into=str(into), ref=args.ref or args.branch or DEFAULT_BRANCH)
            return 0

        source, vault, remote, code_remote, exclude, branch = (
            resolve_snapshot_settings(args)
        )

        # --status shares the resolution ladder so it reports what the next run
        # would use, and stops before anything that writes: the vault, its
        # identity settings and the remembered values are read, never made.
        if args.status:
            return status_command(
                args, source, vault, remote, code_remote, exclude, branch
            )

        # Housekeeping diverges here on purpose: it must not push the code
        # repo, initialise identity settings or remember anything -- only read
        # and tidy the vault's own object store.
        if args.maintain:
            return maintain_vault(vault, full_repack=args.full_repack)

        if args.dry_run and remote:
            print(f"dry run -- would push the vault to {remote}")

        args.payload_min_size, payload_default = resolve_payload_value(
            args.payload_min_size, vault, remote=remote, no_push=args.no_push,
            no_payload=args.no_payload,
        )
        if args.payload_report_days is not None and not payload_by_default_possible(
            remote, args.no_push
        ):
            raise SnapshotError(
                "--payload-report-days needs a run that pushes to a seafile:// remote "
                "(there is no payload store to report on here)"
            )
        payload_ratio = resolve_payload_ratio(args.payload_auto_ratio, vault)
        payload_survey = new_survey()
        payload_min, payload_candidates, payload_rule = plan_payload(
            args.payload_min_size, remote, source, exclude,
            ratio=payload_ratio, ratio_typed=args.payload_auto_ratio is not None,
            survey=payload_survey,
        )
        tuning_kind = tuning_mode(
            args.payload_auto_ratio, vault, dry_run=args.dry_run,
            auto_rule=payload_rule == "auto" and bool(payload_min), remote=remote,
            has_history=(vault / ".git").exists()
            and current_tip(vault / ".git", source, branch) is not None,
        )
        payload_tuning = (
            tune_payload_ratio(remote or "", source, payload_survey, forced=tuning_kind == "forced")
            if tuning_kind else None
        )
        if payload_tuning:
            print(payload_tuning["message"], flush=True)
            if payload_tuning.get("ratio") is not None and payload_tuning["ratio"] != payload_ratio:
                payload_ratio = payload_tuning["ratio"]
                band = {rel: seen for _size, seen, rel in payload_survey["mid"]}
                payload_survey = new_survey()
                payload_min, payload_candidates, payload_rule = plan_payload(
                    args.payload_min_size, remote, source, exclude,
                    ratio=payload_ratio, ratio_typed=args.payload_auto_ratio is not None,
                    survey=payload_survey, sampled=band,
                )
        payload_skip = frozenset(payload_candidates)
        git_dir = ensure_vault(vault, branch)
        # One read of the vault's config serves identity, byte-exactness and
        # the remembered settings below: none of them writes a key another one
        # compares against, so the map cannot go stale between the three.
        vault_config = _read_local_config_map(vault)
        if payload_tuning and payload_tuning.get("upload"):
            # Just measured, not yet remembered (that waits for a complete run).
            vault_config["snapshot.uplink"] = [str(payload_tuning["upload"])]
            vault_config["snapshot.uplink1"] = [str(payload_tuning["single"])]
        if payload_rule == "auto" and payload_min and (payload_candidates or payload_survey["mid"]):
            for line in placement_lines(
                payload_survey, payload_candidates, payload_ratio, source, vault_config
            ):
                print(line, flush=True)
        if not args.dry_run:
            # A preview writes nothing -- not even the byte-exactness config
            # and the inherited identity.  Those writes were safe-looking but
            # a "pure preview" that pins identity and attributes is mutating
            # state a later real run will commit under, without disclosure
            # Reads are harmless and still happen, so a dry run reports the
            # same settings a real run would apply.
            ensure_identity(vault, source, vault_config=vault_config)
            ensure_byte_exact(vault, git_dir, vault_config=vault_config)
            if args.no_object_compression:
                apply_large_payload_profile(vault, git_dir)
            else:
                choose_profile(vault, git_dir, source, exclude, vault_config, write=True,
                               skip=payload_skip or None)
        else:
            # The identity *check* still runs on a preview -- failing
            # early with the actionable message beats a commit-tree
            # traceback -- but read-only: a dry run writes nothing at all
            ensure_identity(vault, source, vault_config=vault_config,
                            write_identity=False)
            if not args.no_object_compression:
                choose_profile(vault, git_dir, source, exclude, vault_config, write=False,
                               skip=payload_skip or None)
        # Free what a killed earlier run left behind first, under the vault
        # lock: it can be tens of GiB, and the free-space check that follows is
        # only honest once it is gone.
        if not args.dry_run:
            with VaultLock(git_dir):
                sweep_temp_packs(git_dir)
                if args.rebuild_index:
                    # The git-side audit: without index entries, staging reads
                    # and re-hashes every file, so an edit that left the stat
                    # cache's fields untouched (a memory-mapped writer, a
                    # volume with no change time) is caught by the bytes.
                    (git_dir / "index").unlink(missing_ok=True)
                    print("rebuilding the vault index: every git-stored file is re-read")
        # Before a byte is staged: a tree the volume cannot hold must be refused
        # now, not discovered when the disk fills (a dry run stages too).
        staging_expect = check_free_space(
            git_dir, vault, source, exclude, vault_config,
            allow_low_disk=args.allow_low_disk, skip=payload_skip or None,
        )
        # The hostname is in the default message because a shared vault is a
        # single chain of snapshots from several machines, and the commit author
        # is the same person on all of them -- so without this, `git log` cannot
        # say which machine saw which tree.  The offset is there because those
        # machines need not share a timezone.
        message = args.message or (
            f"snapshot {datetime.now().astimezone().isoformat(timespec='seconds')} "
            f"on {socket.gethostname()}"
        )

        refuse_flagless_payload_vault(git_dir, source, branch, payload_min)
        remote_tip: str | None = None
        with VaultLock(git_dir):
            # The code push happens here -- identity is settled and no other
            # run holds the vault -- rather than first thing: every check that
            # can refuse this run has already had its say, so a refused run no
            # longer leaves the code pushed and the run reported failed.
            if code_remote:
                if args.dry_run:
                    print(
                        f"dry run -- would push the source repo to "
                        f"'{code_remote}'"
                    )
                else:
                    print(f"pushing source repo to '{code_remote}' ...")
                    proc = _run(
                        ["git", "-C", str(source), "push", code_remote],
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    sys.stdout.write(proc.stdout)
                    if proc.returncode != 0:
                        raise SnapshotError(
                            f"pushing the source repo failed (rc={proc.returncode}); "
                            f"snapshot aborted before the vault was changed"
                        )

            # Decide the base *after* consulting the remote, so a second machine
            # appends to the shared chain rather than building an unrelated one.
            joining = False
            if remote:
                remote_tip = fetch_remote_tip(git_dir, source, remote, branch)
                joining = remote_tip is not None and current_tip(git_dir, source, branch) is None
                base = reconcile_with_remote(
                    git_dir,
                    source,
                    branch,
                    remote_tip,
                    reset_to_remote=args.reset_to_remote,
                    move=not args.dry_run,
                )
            else:
                base = current_tip(git_dir, source, branch)

            # A dry run stops one step before the branch move, so it does not
            # need the commit either -- and building one would need an
            # identity the preview has deliberately not written (the dry run
            # writes nothing).  CI runners have
            # no global identity, which is why passing locally did not mean
            # passing there.  write-tree alone answers every question a
            # preview prints (file count, size, diffstat, secrets, gitlinks).
            manifest, payload_todo = run_payload_step(
                git_dir, source, base, remote, payload_min, payload_candidates,
                dry_run=args.dry_run, jobs=args.payload_jobs, rule=payload_rule,
                ratio=payload_ratio,
                ratio_remembered=vault_remembers_ratio(vault),
                ratio_typed=args.payload_auto_ratio is not None,
                rehash=args.payload_rehash, from_default=payload_default,
                skip_changing=args.payload_skip_changing,
            )
            embedded_report: list[tuple[str, int]] = []
            tree, commit, excluded = build_snapshot(
                git_dir, source, base, exclude, message,
                make_commit=not args.dry_run,
                skip_paths=payload_skip or None, manifest=manifest,
                capture_embedded=not args.no_embedded_repos,
                embedded_report=embedded_report,
                staging_bytes=staging_expect,
                record_mtimes=resolve_record_mtimes(
                    args, vault, remote=remote, no_push=args.no_push,
                    parent_has_record=parent_has_mtimes(git_dir, source, base),
                ),
            )
            count, size = describe_tree(git_dir, source, tree)
            print(f"source : {source}")
            print(f"vault  : {vault}")
            print(f"branch : {branch}")
            print(f"files  : {count} ({human_bytes(size)})")
            RUN_FACTS.update(
                source=str(source), vault=str(vault), branch=branch, files=count, bytes=size,
                dry_run=bool(args.dry_run), base=base, remote=remote,
                payload_objects_to_upload=payload_todo[0] if manifest is not None else 0,
            )
            print(f"base   : {base[:12] if base else '(first snapshot)'}")
            if joining and base and remote:
                warn_if_foreign_chain(git_dir, source, base, tree, remote, branch)
            if manifest is not None:
                print(payload_summary(
                    manifest, payload_todo, args.dry_run,
                    remote=remote, from_default=payload_default,
                ))
            if excluded:
                print(f"excluded: {len(excluded)} path(s) left out")

            suggest_excludes(git_dir, source, excluded)

            if embedded_report:
                print(embedded_note(embedded_report))

            gitlinks = find_gitlinks(git_dir, source, tree)
            if gitlinks:
                print(
                    "\nWarning: these paths are embedded git repositories.  Only "
                    "their commit id is\nrecorded, NOT their contents, so a "
                    "restore will produce a broken directory:"
                )
                for path in gitlinks:
                    print(f"  {path}")
                print(
                    "  Back them up separately, or make them submodules with a "
                    "remote that survives."
                )

            secrets = find_secrets(
                index_paths(git_dir, source)
                + [item["path"] for item in (manifest or {}).get("files", [])]
            )
            if secrets:
                print(
                    f"\nWarning: {len(secrets)} captured path(s) look like "
                    f"credentials.  Git history is\npermanent -- push this vault "
                    f"only to a library only you can read:"
                )
                for path in secrets[:20]:
                    print(f"  {path}")
                if len(secrets) > 20:
                    print(f"  ... and {len(secrets) - 20} more")

            # A real run's ``None`` commit *is* the "tree unchanged" signal:
            # build_snapshot returns one when there is nothing new to record.  A
            # dry run never makes a commit, so there ``None`` says nothing and
            # the tree is compared with the base's instead -- otherwise a first
            # snapshot of 8 GiB was reported as "no changes since the last
            # snapshot".
            if args.dry_run:
                if base:
                    base_tree = git(
                        git_dir, source, ["rev-parse", base + "^{tree}"], cwd=source, check=False
                    ).stdout.strip()
                    changed = tree != base_tree
                else:
                    changed = True
            else:
                changed = commit is not None
            if not changed:
                print("no changes since the last snapshot")
            elif base:
                stat = diff_stat(git_dir, source, base, tree)
                if stat:
                    print(stat)

            if args.dry_run:
                if not changed:
                    print("\ndry run -- nothing to commit, nothing pushed")
                elif commit is None:
                    print(
                        "\ndry run -- branch not moved, nothing pushed "
                        "(a real run would record this tree)"
                    )
                else:
                    print(
                        f"\ndry run -- branch not moved, nothing pushed "
                        f"(would be {commit[:12]})"
                    )
                return 0

            if commit is None:
                print("\nnothing to commit; branch left where it is")
            else:
                set_branch(git_dir, source, branch, commit, base)
                RUN_FACTS["commit"] = commit
                print(f"\nmoved refs/heads/{branch} -> {commit[:12]}")

            if not remote and not args.no_push:
                print(no_remote_warning(source), file=sys.stderr, flush=True)
            elif not remote:
                print("no --remote; snapshot kept locally (pass --remote, or set "
                      "one in the source repo, to push)")
            elif commit is None and base == remote_tip:
                print("the remote already has this snapshot; nothing to push")
                if base and not args.dry_run:
                    remember_pushed_tip(git_dir, source, branch, base)
            else:
                if not args.no_repack:
                    auto_incremental_repack(git_dir, source)
                # `commit or base`: when the tree is unchanged but this machine
                # holds snapshots the remote has never seen (taken offline),
                # there is no *new* commit to send -- the existing branch is the
                # thing to push, and `base` is where it points.
                verify_payload_before_push(
                    git_dir, source, remote, remote_tip, commit or base or "",
                    manifest, jobs=args.payload_jobs or 3,
                )
                push_snapshot(
                    git_dir,
                    source,
                    remote,
                    branch,
                    commit or base or "",
                    tree,
                    base,
                    message,
                )
                RUN_FACTS["pushed"] = True
                if manifest is not None:
                    try:
                        verify_payload_after_push(
                            source, remote, manifest, jobs=args.payload_jobs or 3,
                        )
                    except SnapshotError as exc:
                        # The push has landed; this check looks *behind* it, so
                        # failing the run here used to skip the remember block
                        # below -- the vault then forgot its payload setting
                        # and the next bare run was refused by R10's guard for
                        # a decision this run had already made.  The snapshot
                        # is on the server either way: say it as a warning,
                        # remember everything, and still exit non-zero.
                        print(f"Warning: {exc}", file=sys.stderr, flush=True)
                        END_OF_RUN_NOTES.append(
                            "the snapshot was pushed, but a payload object it names could not "
                            "be re-verified afterwards; run --payload-verify and take a new "
                            "snapshot if it names a missing object"
                        )
                        late_failure = True

        # Remember only a run that got all the way through, so the first
        # successful run leaves the next one with nothing to type -- and a typo'd
        # --remote that failed to push does not become the vault's remote for
        # every scheduled run after it.  A preview never gets here.
        remember_payload_value(vault, args.payload_min_size, dry_run=args.dry_run)
        remember_payload_ratio(vault, args.payload_auto_ratio, dry_run=args.dry_run)
        remember_tuning(vault, payload_tuning, dry_run=args.dry_run)
        remember_record_mtimes(vault, args, dry_run=args.dry_run)
        remember_settings(
            vault,
            source,
            remote=_UNSET if args.no_push else remote,
            code_remote=_UNSET if args.no_push else code_remote,
            exclude=exclude,
            branch=branch,
            vault_config=vault_config,
        )
        # Last, after everything the run must keep: the report is optional, and
        # an interrupt during it must not lose the settings this run earned.
        if manifest is not None and remote and not args.dry_run:
            maybe_report_payload_gc(git_dir, vault, remote, args.payload_report_days)
        for note in END_OF_RUN_NOTES:
            print(note, flush=True)
            print(note, file=sys.stderr, flush=True)
        return 1 if late_failure else 0

    except SnapshotError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
