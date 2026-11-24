from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import common
from . import mtimes
from . import payload_cli
from . import subcommands


class Declared:
    """``add_argument`` for the parser of one command.

    Every option is defined for every command, so a value after an option the command does not read is
    still parsed as that option's value (and then refused by ``cli_checks.validate_args``, with one
    message).  What changes per command is what ``--help`` shows: an option the command does not read is
    hidden.  ``dests`` collects ``{dest: flag}``; with *suppress* an option left out of the command line is
    left out of the result too, which is how ``parse_args`` learns what was typed.
    """

    def __init__(
        self, target: Any, shown: frozenset[str] | None, dests: dict[str, str], *, suppress: bool = False,
        sections: Any = None, operand: bool = False,
    ) -> None:
        self._target, self._shown, self._dests, self._suppress = target, shown, dests, suppress
        self._sections, self._operand = sections, operand

    def add_argument(self, *names: str, **kwargs: Any) -> Any:
        flag = names[0] if names[0].startswith("-") else "SOURCE"
        if flag == "SOURCE" and self._operand:
            return None  # the command's own operand takes the place of the source
        dest = kwargs.get("dest") or names[0].lstrip("-").replace("-", "_")
        self._dests[dest] = flag
        if self._shown is not None and flag not in self._shown:
            kwargs["help"] = argparse.SUPPRESS
        if self._suppress:
            kwargs["default"] = argparse.SUPPRESS
        target = self._sections if (self._sections is not None and names[0].startswith("-")) else self._target
        return target.add_argument(*names, **kwargs)

    def add_argument_group(self, *args: Any, **kwargs: Any) -> Declared:
        return Declared(
            self._target.add_argument_group(*args, **kwargs), self._shown, self._dests, suppress=self._suppress
        )


def add_options(out: Declared) -> None:
    """Every option of the tool, defined once; a command reads the ones :data:`subcommands.ALLOWED` lists."""
    out.add_argument(
        "source_positional",
        nargs="?",
        type=Path,
        default=None,
        metavar="SOURCE",
        help="directory to snapshot (same as --source, so "
        "'git seafile-snapshot C:/code/app' works)",
    )
    out.add_argument(
        "--source",
        type=Path,
        default=None,
        help="directory to snapshot (default: the current directory)",
    )
    out.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault repository location (default: '<source>-vault' beside the "
        "source)",
    )
    out.add_argument(
        "--branch",
        default=None,
        help=f"branch in the vault holding snapshots "
        f"(default: remembered, else {common.DEFAULT_BRANCH})",
    )
    out.add_argument(
        "--remote",
        default=None,
        help="Seafile remote URL, e.g. seafile://code/myproject-vault "
        "(snapshot: where to push; default: remembered, else derived from the source's own seafile:// "
        "remote; pass --no-push to snapshot locally instead)",
    )
    out.add_argument(
        "--code-remote",
        default=None,
        help="also run a normal 'git push <name>' in the source repo first "
        "(default: remembered, else the code repo is not pushed)",
    )
    out.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help="path matching this glob is left out of the snapshot (repeatable; "
        "default: remembered, else nothing is excluded)",
    )
    out.add_argument(
        "--default-excludes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="add the recommended exclusions on top of any --exclude "
        f"({', '.join(common.RECOMMENDED_EXCLUDES)}) and any Python virtualenv (a "
        "directory holding a pyvenv.cfg, whatever its name). build, dist and venv are left out "
        "only where git ignores everything in them (outside a repository: a project file beside them), "
        "and the run names what was left out; remembered as a choice. Not on by default: a snapshot is "
        "a backup, and silently dropping files is how a backup fails at the one "
        "moment it mattered",
    )
    out.add_argument(
        "--no-push",
        action="store_true",
        help="this run pushes nothing -- neither the vault nor the code repo "
        "-- and leaves the vault's remembered remote alone, so a one-off "
        "local run does not unhook a scheduled job",
    )
    out.add_argument("--message", default=None, help="snapshot commit message")
    out.add_argument(
        "--into",
        type=Path,
        default=None,
        help="destination directory of the restore",
    )
    out.add_argument(
        "--ref",
        default=None,
        help=f"snapshot to restore (default: the vault branch, "
        f"{common.DEFAULT_BRANCH}); any revision works, e.g. 'snapshot~3' to roll back",
    )
    out.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="PATH",
        help="restore only this file or folder of the snapshot (repeatable; a path relative to the snapshot's "
        "root, written with /, matched literally -- no wildcards). The git history still arrives whole; only "
        "the files named are written and only their payload objects downloaded",
    )
    out.add_argument(
        "--dry-run",
        action="store_true",
        help="build the snapshot but do not move the branch or push "
        "(the vault is still initialised if missing, so its location and "
        "config can be inspected; the remote is still fetched, so the diff is "
        "shown against the real base). It is a full run up to the commit: the "
        "git-bound part of the tree is staged into the vault, so it takes the "
        "time and disk a real run would, and it uploads nothing",
    )
    out.add_argument(
        "--reset-to-remote",
        action="store_true",
        help="if the local vault and the remote have diverged, discard the "
        "local-only snapshots and continue from the remote chain instead of "
        "refusing",
    )
    out.add_argument(
        "--allow-shared-remote",
        action="store_true",
        help="permit --remote to be a URL the source repository already uses. "
        "The snapshot holds the files .gitignore keeps out of the code "
        "repository, so this publishes them; refused by default",
    )
    out.add_argument(
        "--allow-synced",
        action="store_true",
        help="proceed even when the source or vault (or a restore destination) "
        "is inside a Seafile-synced library. The guard already lets this through "
        "when that library's auto sync is disabled or the path is covered by its "
        "seafile-ignore.txt; this overrides every remaining refusal and puts the "
        "risk of the client rewriting files under Git on you",
    )
    out.add_argument(
        "--allow-low-disk",
        action="store_true",
        help="stage even when the estimate says the vault's volume cannot hold "
        "the tree. The estimate is an upper bound (tree size, scaled by the "
        "sampled compressibility, minus what the vault already stores); this "
        "is for data you know is more compressible than the sample suggests. "
        "For a restore: download a payload that does not appear to fit",
    )
    payload_cli.add_payload_groups(out)
    mtimes.add_mtimes_arguments(out)
    out.add_argument(
        "--verify",
        action="store_true",
        help="also run 'git fsck' on the restored clone. Byte-exactness "
        "is checked on every restore regardless",
    )
    out.add_argument(
        "--json",
        action="store_true",
        help="write one JSON document to stdout (status, exit_code, warnings, error, "
        "result) and move the human text to stderr; for scheduled runs",
    )
    out.add_argument(
        "--exit-warnings",
        action="store_true",
        help="exit 3 instead of 0 when the run completed but printed a Warning:",
    )
    out.add_argument(
        "--no-object-compression",
        action="store_true",
        help=        "disable deflation for newly written vault objects "
        "(repo-local core.compression=0, stored at full size). For trees "
        "dominated by already-"
        "compressed payloads -- zip/SAFE archives, images, video -- deflation "
        "saves nothing and costs 2-3x the staging time; this flag removes it. "
        "For compressible text-heavy trees it trades some vault size for speed",
    )
    out.add_argument(
        "--full-repack",
        action="store_true",
        help="run a one-shot full repack + prune of the entire "
        "object store. Ordinary snapshot runs already pack the loose tier "
        "incrementally before each push (once it passes roughly 256 MiB); "
        "use this after large deletions to reclaim space",
    )
    out.add_argument(
        "--no-embedded-repos",
        action="store_true",
        help="record a nested git repository (a directory with its own .git) only as "
        "its commit id, as 0.9.0 did, instead of capturing its files",
    )
    out.add_argument(
        "--refuse-secrets",
        action="store_true",
        help="stop a snapshot run, before the branch moves and before anything is pushed, when a captured "
        "path looks like a credential (.env, private keys, ...), instead of only warning about it; leave the "
        "path out with --exclude to go on",
    )
    out.add_argument(
        "--allow-gitlinks",
        action="store_true",
        help="with --no-embedded-repos: accept that an embedded repository is recorded as a gitlink "
        "(its commit id, not its files), so a restore gives an empty directory",
    )
    out.add_argument(
        "--rebuild-index",
        action="store_true",
        help="drop the vault's index before staging, so every git-stored file is read and "
        "hashed again. The git-side counterpart of --payload-rehash: an edit that left size "
        "and timestamp untouched (a memory-mapped writer on Windows, a volume with no change "
        "time) is invisible to the stat cache, and this is the audit that re-reads the bytes",
    )
    out.add_argument(
        "--no-repack",
        action="store_true",
        help="skip the automatic incremental repack that runs before a push",
    )


#: What each command takes as its operand, besides SOURCE: ``(dest, metavar, default, help)``.
OPERAND = {
    "drill": ("restore_drill", "N", "10",
              "how many randomly chosen payload files to download and check (default 10, or 'all'); "
              "needs the disk for the whole vault (set TMP or TMPDIR to use a bigger volume); "
              "--ref drills an older snapshot"),
    "verify": ("payload_verify", "N|all", "20",
               "how many randomly chosen payload objects to download and check (default 20, or 'all'); "
               "the server cannot report a content hash, so this is the only way to find an object that "
               "was damaged after it was stored"),
    "view": ("payload_view", "REF", "snapshot",
             "the snapshot to build the view of: a branch name, or one like snapshot~2 (default: the "
             "'snapshot' branch), beside the vault as <path>-view/<name>. No data is transferred"),
}

_DESCRIPTION = (
    "Snapshot a working tree (gitignored files included) into a separate "
    "vault repository and push it to Seafile. The source directory is "
    "never modified.\n\n"
    "Every option has a default, so a bare run works: the source is the "
    "current directory, the vault is '<source>-vault' beside it, and the "
    "remote, branch and exclusions are remembered in the vault after the "
    "first run. An explicit flag always wins, and is written back to the "
    "vault for next time."
)

_EPILOG = (
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
    + " ".join(f"--exclude '{p}'" for p in common.RECOMMENDED_EXCLUDES)
    + "\n  plus each Python virtualenv found (a directory holding pyvenv.cfg)"
    "\n\nA pattern with no slash matches that name at ANY depth, so\n"
    "--exclude node_modules also drops packages/*/node_modules.\n"
    "Quote wildcards so your own shell does not expand them first.\n"
    + payload_cli.PAYLOAD_EPILOG
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
)


def command_list() -> str:
    width = max(len(subcommands.name(c.mode)) for c in subcommands.COMMANDS)
    lines = ["commands (the first word; a bare run is 'snapshot'):"]
    for command in subcommands.COMMANDS:
        first = command.summary.split(";")[0].split(":")[0]
        lines.append(f"  {subcommands.name(command.mode):<{width}}  {first}")
    lines.append("run 'git seafile-snapshot <command> --help' for the options of one command.")
    return "\n".join(lines)


def build_parser(
    mode: str, *, suppress: bool = False, dests: dict[str, str] | None = None
) -> argparse.ArgumentParser:
    """The parser of one command: all options defined, the ones it does not read hidden from ``--help``."""
    from . import cli_checks
    command = subcommands.BY_MODE[mode]
    snapshot = mode == "snapshot"
    parser = argparse.ArgumentParser(
        prog="git-seafile-snapshot" + ("" if snapshot else " " + subcommands.name(mode)),
        description=(_DESCRIPTION + "\n\n" + command_list()) if snapshot else command.summary + ".",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG if snapshot else None,
    )
    table: dict[str, str] = {} if dests is None else dests
    # The option table is the snapshot's, which defines every option; only the help is per command.
    every = frozenset(option_table().values()) if dests is None else frozenset()
    shown = None if dests is not None else subcommands.allowed(mode, every)
    out = Declared(
        parser, shown, table, suppress=suppress, sections=cli_checks.HelpSections(parser), operand=mode in OPERAND
    )
    add_options(out)
    for group in parser._action_groups:
        # A group none of whose options this command reads would still print its description.
        if group._group_actions and all(a.help == argparse.SUPPRESS for a in group._group_actions):
            group.description = None
    if mode in OPERAND:
        dest, metavar, default, text = OPERAND[mode]
        parser.add_argument(dest, nargs="?", default=argparse.SUPPRESS if suppress else default, metavar=metavar, help=text)
    return parser


_TABLE: dict[str, str] = {}


def option_table() -> dict[str, str]:
    """``{dest: flag}`` for every option the tool defines (``SOURCE`` for the positional)."""
    if not _TABLE:
        build_parser("snapshot", dests=_TABLE)
    return _TABLE


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """A command line as the flat namespace the run code reads.

    ``args.mode`` is the command, ``args.restore`` / ``status`` / ``maintain`` / ``payload_gc`` are true for
    their commands, and ``restore_drill`` / ``payload_verify`` / ``payload_view`` hold the operand of theirs
    (None otherwise).  ``args.given`` names the options typed on the command line, which is what
    ``cli_checks.validate_args`` checks against :data:`subcommands.ALLOWED`.
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        mode, rest, legacy = subcommands.split_command(raw)
    except subcommands.UsageError as exc:
        build_parser("snapshot").error(str(exc))
    subcommands.say_legacy(legacy)
    args = build_parser(mode).parse_args(rest)
    typed = vars(build_parser(mode, suppress=True).parse_args(rest))
    table = option_table()
    args.given = frozenset(dest for dest in typed if dest in table)
    args.mode = mode
    args.mode_flags = legacy
    if mode in OPERAND:
        args.source_positional = None  # the command's operand stands in its place
    args.restore = mode == "restore"
    args.status = mode == "status"
    args.maintain = mode == "maintain"
    args.payload_gc = mode == "gc"
    for flag_mode, dest in (("drill", "restore_drill"), ("verify", "payload_verify"), ("view", "payload_view")):
        if mode != flag_mode:
            setattr(args, dest, None)
    return args
