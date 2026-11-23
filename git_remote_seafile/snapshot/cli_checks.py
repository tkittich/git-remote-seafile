from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from . import common
from . import vault_store
from . import stage
from . import mtimes
from . import payload_gc_run
from . import payload_cli
from . import payload_view_run
from . import remote_ops
from . import subcommands
from . import restore_run
from . import first_run
from . import guards


def literal_exclude(path: str) -> str:
    """A directory found by looking at the source, as an exclude pattern that matches only itself.

    Excludes are fnmatch patterns; ``a[1]/build`` as one would also remove a tracked ``a1/build/tool.py``
    (review c305b0a, fable-S4-2).  ``[`` ``*`` ``?`` become one-character classes.
    """
    return "".join(f"[{ch}]" if ch in "*?[" else ch for ch in path)


def options_for_other_commands(args: argparse.Namespace) -> list[str]:
    """Options typed on the command line that this command does not read.

    One table decides (:data:`subcommands.ALLOWED`); before 0.11 each mode kept its own list of what it
    refused, and the review rounds kept finding the flag a list had forgotten (ROADMAP AA73/AA74).
    """
    from . import cli_args
    table = cli_args.option_table()
    every = frozenset(table.values())
    mine = subcommands.allowed(args.mode, every)
    problems = []
    for dest in sorted(args.given):
        flag = table[dest]
        if flag in mine:
            continue
        owners = [subcommands.name(m) for m in subcommands.BY_MODE if flag in subcommands.allowed(m, every)]
        belongs = f" (it is an option of: {', '.join(owners)})" if owners else ""
        label = "SOURCE (positional)" if flag == "SOURCE" else flag
        problems.append(f"{label} does not apply to {subcommands.name(args.mode)}{belongs}")
    return problems


def local_snapshot_problems(args: argparse.Namespace) -> list[str]:
    """What only tunes an upload, given to a snapshot kept local (``--no-push``): dead weight."""
    if args.mode != "snapshot" or not args.no_push:
        return []
    given: tuple[tuple[str, Any], ...] = (
        ("--payload-jobs", args.payload_jobs is not None),
        ("--payload-auto-ratio", args.payload_auto_ratio is not None),
        ("--payload-skip-changing", args.payload_skip_changing), ("--payload-rehash", args.payload_rehash),
    )
    return [f"{flag} does not apply to --no-push" for flag, value in given if value]


def validate_args(args: argparse.Namespace) -> None:
    """Reject combinations that would otherwise be silently ignored.

    Each of these is a flag that does nothing where it is passed, which is worse
    than a wrong answer: the user believes they asked for something and gets no
    signal either way.  ``--ref`` on a snapshot is the sharpest -- a run
    meant to restore an *old* snapshot quietly takes a *new* one instead.
    """
    problems: list[str] = (
        ([f"{' and '.join(args.mode_flags)} are different modes: run one command at a time"]
         if len(args.mode_flags) > 1 else [])
        + options_for_other_commands(args)
        + payload_gc_run.payload_gc_conflicts(args) + payload_cli.payload_conflicts(args)
        + payload_view_run.payload_view_conflicts(args) + mtimes.mtimes_conflicts(args)
        + local_snapshot_problems(args)
    )
    # Missing operands are a wrong command line too: exit 2, not the 1 of a failed run (fable-E3).
    if args.restore:
        if not args.remote:
            problems.append("restore requires --remote <url>")
        if not args.into:
            problems.append("restore requires --into <directory>")
    if args.restore_drill is not None:
        if not args.remote:
            problems.append("drill requires --remote <url>")
        try:
            restore_run.drill_sample(args.restore_drill)
        except common.SnapshotError as exc:
            problems.append(str(exc))

    if args.no_embedded_repos and not args.allow_gitlinks:
        # A gitlink is a commit id: a restore gives an empty directory, so it is chosen in so many words.
        problems.append(
            "--no-embedded-repos records an embedded repository as a gitlink (its commit id, not its "
            "files), so a restore gives an empty directory; add --allow-gitlinks to accept that, or "
            "drop --no-embedded-repos to capture the files"
        )
    if args.allow_gitlinks and not args.no_embedded_repos:
        problems.append("--allow-gitlinks only applies together with --no-embedded-repos")

    if args.mode == "snapshot" and args.no_push:
        if args.remote:
            problems.append("--no-push and --remote contradict each other")
        if args.no_object_compression:
            # The raw profile is the vault's: written, every later scheduled run stored
            # its objects uncompressed (review 06cc623, glm G4.1, gemini G4.1a), and a
            # --no-push run remembers nothing (SNAPSHOT section 4).
            problems.append(
                "--no-object-compression sets the vault's profile for every later run, which "
                "a --no-push run does not change; run it with a push, or drop --no-push"
            )
        if args.code_remote:
            # Only the typed flag: a remembered code remote under --no-push is a
            # default, and is skipped without complaint.
            problems.append(
                "--code-remote pushes the source repository, which --no-push "
                "forbids; drop one of them"
            )
    if (
        args.source is not None
        and args.source_positional is not None
        and vault_store.expand_path(args.source)
        != vault_store.expand_path(Path(args.source_positional))
    ):
        problems.append(
            f"two different sources were given: '{args.source_positional}' "
            f"(positional) and '{args.source}' (--source)"
        )

    if problems:
        raise common.SnapshotError(
            "the arguments do not make sense together:\n  "
            + "\n  ".join(problems)
        )


def exclude_relative_to(pattern: str, source: Path) -> str:
    """*pattern* as a path under *source* when it is an absolute path of something there, else as typed."""
    if not os.path.isabs(pattern):
        return pattern
    try:
        return Path(pattern).resolve().relative_to(source.resolve()).as_posix()
    except (OSError, ValueError):
        return pattern


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
    from . import run_settings
    explicit = args.source or args.source_positional
    source = vault_store.expand_path(explicit or Path.cwd())
    vault = vault_store.expand_path(args.vault or guards.default_vault_for(source))
    source, vault = vault_store.resolve_paths(source, vault)

    # A snapshot backs up a repository, as `git push` does: from a subfolder the
    # vault's chain would otherwise be a different tree under the same remote
    # (a 6807-file "deletion" diff on top of the whole repository's vault).
    # A vault that already remembers this very subfolder keeps working -- that
    # is a default, and a default must not turn a run that worked into an error.
    root = run_settings.repo_toplevel(source)
    if root is not None and root != source and not run_settings._vault_remembers(vault, source):
        if explicit:
            raise common.SnapshotError(
                f"{source} is a subfolder of the repository at {root}.\n"
                f"A snapshot is of the whole repository: give the repository root "
                f"(--source {root}) or run it without --source from inside the "
                f"repository."
            )
        print(f"Note: {source} is inside the repository at {root}; snapshotting "
              f"the whole repository.", flush=True)
        source = root
        vault = vault_store.expand_path(args.vault or guards.default_vault_for(source))
        source, vault = vault_store.resolve_paths(source, vault)

    # A vault may not exist yet, so the bottom of the chain is remembered-first:
    # there is nothing to remember until a first run has happened.
    remembered = run_settings.remembered_settings(vault, source)

    branch = args.branch or str(remembered.get("branch") or common.DEFAULT_BRANCH)

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
            remote = run_settings.derive_vault_remote(source)
            if remote and not args.dry_run:
                # Said once: after the first complete run the vault remembers it.
                print(
                    f"Note: the vault's destination was not given, so it is {remote}, "
                    "derived from the source's own seafile:// remote -- the same library as "
                    "your code.  Everyone who can read that library can read this snapshot, "
                    ".env files and keys included.  Pass --remote to send it somewhere only "
                    "you can read.",
                    flush=True,
                )
        code_remote = args.code_remote or remembered.get("code_remote")

    first_run.offer_default_excludes(args, source, vault, remembered)
    exclude = list(args.exclude) if args.exclude is not None else list(
        remembered.get("exclude") or []
    )
    # An absolute path of a folder in the source means that folder (it matched nothing, and the
    # "excluded" file was committed with exit 0: review c305b0a, fable-S1-3).
    exclude = [exclude_relative_to(pattern, source) for pattern in exclude]
    # Typed on this command line, else what the vault remembered (a default is not a
    # request, but a choice the vault already holds is repeated).  What it expands to
    # is worked out again on every run and is not itself remembered, so a directory
    # that later gains source is not left out for ever.
    use_default = (args.default_excludes if args.default_excludes is not None
                   else bool(remembered.get("default_excludes")))
    derived: list[str] = []
    if use_default:
        tracked: list[str] | None = None
        for pattern in common.RECOMMENDED_EXCLUDES:
            if pattern in exclude or pattern in first_run._AMBIGUOUS_NAMES:
                continue
            # The list is a guess about reproducible output; the source's own
            # repository knows better.  "build" must not drop a tracked
            # tools/build/make.py -- a restore would "reconstitute the working
            # directory" without source the user committed.
            if tracked is None:
                tracked = stage.source_tracked_paths(source)
            held = next((path for path in tracked if stage.exclude_matches(path, pattern)), None)
            if held is not None:
                print(
                    f"Note: --default-excludes keeps '{pattern}' for this run: the source's own "
                    f"git repository tracks {held!r}, and a backup that drops committed source "
                    f"is not a backup. Pass --exclude '{pattern}' to leave it out anyway.",
                    flush=True,
                )
                continue
            derived.append(pattern)
        # build, dist and venv are source in some projects: decided per directory.
        for rel in first_run.ambiguous_directories(source):
            if any(stage.exclude_matches(rel, pat) for pat in [*exclude, *derived]):
                continue
            if first_run.is_reproducible_directory(source, rel):
                derived.append(literal_exclude(rel))
            else:
                print(
                    f"Note: --default-excludes keeps '{rel}' for this run: git does not ignore "
                    "everything in it (or, outside a git repository, no project file sits beside "
                    f"it), so it may be source. Pass --exclude '{rel}' to leave it out anyway.",
                    flush=True,
                )
        # Named by what makes it one, not by what it is called (.venv-audit, env).
        for venv in stage.find_virtualenvs(source, [*exclude, *derived]):
            pattern = literal_exclude(venv)
            if pattern not in exclude and pattern not in derived:
                derived.append(pattern)
        exclude.extend(d for d in derived if d not in exclude)
    args.derived_excludes = derived
    args.default_excludes_on = use_default

    # Everything that can be checked before a single byte is written: a typo in
    # a remote name or a vault inside a synced folder is far cheaper to catch
    # now than after the first push has published a whole tree.
    # --maintain and --status push nothing, so a remembered remote that went away (or one the vault shares
    # with the source, which --allow-shared-remote cannot be passed to them to allow) must not stop them.
    if remote and not (args.maintain or args.status):
        remote_ops.check_remote_is_not_a_code_remote(
            source, remote, allowed=args.allow_shared_remote
        )
    if code_remote and not (args.maintain or args.status):
        check_code_remote_is_git_remote(source, code_remote)
    guards.check_paths_are_unsynced(
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
        raise common.SnapshotError(
            f"--code-remote '{name}' was given, but {source} is not a git "
            f"repository, so there is no '{name}' to push to"
        )
    names = {remote_name for remote_name, _url in remote_ops.source_remote_urls(source)}
    if name not in names:
        listing = ", ".join(sorted(names)) or "none"
        raise common.SnapshotError(
            f"--code-remote '{name}' is not a remote of {source} "
            f"(it has: {listing})"
        )


#: The help sections of ``--help``, in the order they print, and which options
#: open each.  Fifty flags in one alphabet-free list made the four a first run
#: needs as hard to find as the one that rewrites a vault; an option named in
#: no section lands in the last one, so a new flag is placed by default rather
#: than lost.
HELP_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("everyday options", (
        "--source", "--vault", "--remote", "--branch", "--exclude",
        "--default-excludes", "--no-push", "--dry-run", "--message",
    )),
    ("restore and verify", (
        "--into", "--ref", "--verify",
    )),
    ("for scheduled runs", ("--json", "--exit-warnings")),
    ("local vault housekeeping", ("--full-repack",)),
    ("advanced options", ()),
)


class HelpSections:
    """``add_argument`` for a parser whose options print in :data:`HELP_SECTIONS`."""

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self._parser = parser
        self._groups = {title: parser.add_argument_group(title) for title, _ in HELP_SECTIONS}
        self._home = {
            flag: title for title, flags in HELP_SECTIONS for flag in flags
        }

    def add_argument(self, *names: str, **kwargs: Any) -> Any:
        if not names[0].startswith("-"):
            return self._parser.add_argument(*names, **kwargs)
        title = self._home.get(names[0], HELP_SECTIONS[-1][0])
        return self._groups[title].add_argument(*names, **kwargs)


def help_word_to_flag(argv: list[str]) -> list[str]:
    """``help`` alone means ``--help`` -- unless a directory called ``help`` is being snapshotted.

    ``git seafile-snapshot --help`` never reaches the tool: git reads ``--help``
    after an external command's name as a request for that command's man page,
    and fails ("no manual entry") for a command that ships none.  ``git
    seafile-snapshot help`` is passed through untouched.
    """
    if argv == ["help"] and not Path("help").exists():
        return ["--help"]
    return argv
