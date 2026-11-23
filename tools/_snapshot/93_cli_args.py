

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
            "\ngit reads `git seafile-snapshot --help` itself and looks for a man page: ask for\n"
            "this text with `git seafile-snapshot help` or `-h`.\n"
        ),
    )
    sections = HelpSections(parser)
    parser.add_argument(
        "source_positional",
        nargs="?",
        type=Path,
        default=None,
        metavar="SOURCE",
        help="directory to snapshot (same as --source, so "
        "'seafile_snapshot.py C:/code/app' works)",
    )
    sections.add_argument(
        "--source",
        type=Path,
        default=None,
        help="directory to snapshot (default: the current directory)",
    )
    sections.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault repository location (default: '<source>-vault' beside the "
        "source)",
    )
    sections.add_argument(
        "--branch",
        default=None,
        help=f"branch in the vault holding snapshots "
        f"(default: remembered, else {DEFAULT_BRANCH})",
    )
    sections.add_argument(
        "--remote",
        default=None,
        help="Seafile remote URL to push to, e.g. seafile://code/myproject-vault "
        "(default: remembered, else derived from the source's own seafile:// "
        "remote; pass --no-push to snapshot locally instead)",
    )
    sections.add_argument(
        "--code-remote",
        default=None,
        help="also run a normal 'git push <name>' in the source repo first "
        "(default: remembered, else the code repo is not pushed)",
    )
    sections.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help="path matching this glob is left out of the snapshot (repeatable; "
        "default: remembered, else nothing is excluded)",
    )
    sections.add_argument(
        "--default-excludes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="add the recommended exclusions on top of any --exclude "
        f"({', '.join(RECOMMENDED_EXCLUDES)}) and any Python virtualenv (a "
        "directory holding a pyvenv.cfg, whatever its name). build, dist and venv are left out "
        "only where git ignores everything in them (outside a repository: a project file beside them), "
        "and the run names what was left out; remembered as a choice. Not on by default: a snapshot is "
        "a backup, and silently dropping files is how a backup fails at the one "
        "moment it mattered",
    )
    sections.add_argument(
        "--no-push",
        action="store_true",
        help="this run pushes nothing -- neither the vault nor the code repo "
        "-- and leaves the vault's remembered remote alone, so a one-off "
        "local run does not unhook a scheduled job",
    )
    sections.add_argument("--message", default=None, help="snapshot commit message")
    sections.add_argument(
        "--restore",
        action="store_true",
        help="restore a snapshot instead of taking one (needs --remote and "
        "--into); byte-exact, unlike a plain git clone",
    )
    sections.add_argument(
        "--into",
        type=Path,
        default=None,
        help="destination directory for --restore",
    )
    sections.add_argument(
        "--ref",
        default=None,
        help=f"snapshot to restore (default: the vault branch, "
        f"{DEFAULT_BRANCH}); any revision works, e.g. 'snapshot~3' to roll back",
    )
    sections.add_argument(
        "--dry-run",
        action="store_true",
        help="build the snapshot but do not move the branch or push "
        "(the vault is still initialised if missing, so its location and "
        "config can be inspected; the remote is still fetched, so the diff is "
        "shown against the real base). It is a full run up to the commit: the "
        "git-bound part of the tree is staged into the vault, so it takes the "
        "time and disk a real run would, and it uploads nothing",
    )
    sections.add_argument(
        "--reset-to-remote",
        action="store_true",
        help="if the local vault and the remote have diverged, discard the "
        "local-only snapshots and continue from the remote chain instead of "
        "refusing",
    )
    sections.add_argument(
        "--allow-shared-remote",
        action="store_true",
        help="permit --remote to be a URL the source repository already uses. "
        "The snapshot holds the files .gitignore keeps out of the code "
        "repository, so this publishes them; refused by default",
    )
    sections.add_argument(
        "--allow-synced",
        action="store_true",
        help="proceed even when the source or vault (or a restore destination) "
        "is inside a Seafile-synced library. The guard already lets this through "
        "when that library's auto sync is disabled or the path is covered by its "
        "seafile-ignore.txt; this overrides every remaining refusal and puts the "
        "risk of the client rewriting files under Git on you",
    )
    sections.add_argument(
        "--allow-low-disk",
        action="store_true",
        help="stage even when the estimate says the vault's volume cannot hold "
        "the tree. The estimate is an upper bound (tree size, scaled by the "
        "sampled compressibility, minus what the vault already stores); this "
        "is for data you know is more compressible than the sample suggests. "
        "With --restore: download a payload that does not appear to fit",
    )
    add_payload_groups(parser)
    add_mtimes_arguments(sections)
    add_status_arguments(sections)
    sections.add_argument(
        "--verify",
        action="store_true",
        help="with --restore, also run 'git fsck' on the clone. Byte-exactness "
        "is checked on every restore regardless",
    )
    sections.add_argument(
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
    sections.add_argument(
        "--json",
        action="store_true",
        help="write one JSON document to stdout (status, exit_code, warnings, error, "
        "result) and move the human text to stderr; for scheduled runs",
    )
    sections.add_argument(
        "--exit-warnings",
        action="store_true",
        help="exit 3 instead of 0 when the run completed but printed a Warning:",
    )
    sections.add_argument(
        "--no-object-compression",
        action="store_true",
        help=        "disable deflation for newly written vault objects "
        "(repo-local core.compression=0, stored at full size). For trees "
        "dominated by already-"
        "compressed payloads -- zip/SAFE archives, images, video -- deflation "
        "saves nothing and costs 2-3x the staging time; this flag removes it. "
        "For compressible text-heavy trees it trades some vault size for speed",
    )
    sections.add_argument(
        "--maintain",
        action="store_true",
        help="local vault housekeeping instead of a snapshot: by default just "
        "reports on the object store; with --full-repack it consolidates the "
        "whole store into one packfile and prunes unreachable objects. Never "
        "touches the source tree or any remote",
    )
    sections.add_argument(
        "--full-repack",
        action="store_true",
        help="with --maintain, run a one-shot full repack + prune of the entire "
        "object store. Ordinary snapshot runs already pack the loose tier "
        "incrementally before each push (once it passes roughly 256 MiB); "
        "use this after large deletions to reclaim space",
    )
    sections.add_argument(
        "--no-embedded-repos",
        action="store_true",
        help="record a nested git repository (a directory with its own .git) only as "
        "its commit id, as 0.9.0 did, instead of capturing its files",
    )
    sections.add_argument(
        "--allow-gitlinks",
        action="store_true",
        help="with --no-embedded-repos: accept that an embedded repository is recorded as a gitlink "
        "(its commit id, not its files), so a restore gives an empty directory",
    )
    sections.add_argument(
        "--rebuild-index",
        action="store_true",
        help="drop the vault's index before staging, so every git-stored file is read and "
        "hashed again. The git-side counterpart of --payload-rehash: an edit that left size "
        "and timestamp untouched (a memory-mapped writer on Windows, a volume with no change "
        "time) is invisible to the stat cache, and this is the audit that re-reads the bytes",
    )
    sections.add_argument(
        "--no-repack",
        action="store_true",
        help="skip the automatic incremental repack that runs before a push",
    )
    return parser.parse_args(argv)
