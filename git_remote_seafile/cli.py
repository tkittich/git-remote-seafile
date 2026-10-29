"""cli.py - Entry point for git-remote-seafile."""

from __future__ import annotations

import io
import os
import signal
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote
from .helper import RemoteHelper
from .client import SeafileAuthError, SeafileClient
from .lock import RemoteLock, RepositoryLockedError
from .safety import (
    SafetyError,
    check_preflight_safety,
    discover_local_synced_libraries,
    get_local_work_tree,
    safe_relative_to,
)
from .diagnose import (
    CONFLICT_WALK_MAX_DIRS,
    MIB,
    PROBE_CEILING_BYTES,
    PROBE_FLOOR_BYTES,
    chunk_warning,
    find_conflict_copies,
    measure_upload_limit,
    recommended_pack_mib,
    upload_attempt,
)
from .units import format_bytes
from .url import mask_url


# (name, usage, summary, options) for every subcommand, in listing order.
# Both the top-level listing and `git-remote-seafile <cmd> --help` render from
# this one table: a flag added to a usage line cannot then go missing from the
# per-command help, or vice versa (D17).  The flags used to be documented
# nowhere -- `--min-packs` and `--force` appeared only in the strings that
# happened to print when the command was called wrongly.
_COMMANDS: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    ("doctor", "git-remote-seafile doctor [<seafile://url>] [--upload-limit [--max-mib N]]",
     "Check login, library, safety, write access and conflict copies in one go",
     (("--upload-limit",
       "Also measure the largest single upload the server accepts and print the "
       "seafile.gcmaxpackmib that fits (uploads up to 1023 MiB of zeros into a "
       "scratch folder, deleted afterwards)"),
      ("--max-mib N", "With --upload-limit: the largest body to try (default 1023, the most "
                      "one request can be before the client sends chunks)"))),
    ("config", "git-remote-seafile config",
     "Show every behavioural setting, its value and where the value comes from", ()),
    ("test", "git-remote-seafile test <seafile://url>",
     "Test connection and discover refs", ()),
    ("check-safety", "git-remote-seafile check-safety <seafile://url>",
     "Run pre-flight safety checks (Trap 1 & 2)", ()),
    ("check-auth", "git-remote-seafile check-auth",
     "Verify active Seafile login", ()),
    ("gc", "git-remote-seafile gc <seafile://url> [--min-packs N | --tail]",
     "Compact multiple remote packfiles",
     (("--min-packs N",
       "Only compact when the remote holds at least N packfiles (default: 2)"),
      ("--tail",
       "Run exactly what seafile.autogc runs: merge only the small packfiles, "
       "leave large ones in place, no pruning"))),
    ("repair", "git-remote-seafile repair <seafile://url> [--dry-run]",
     "Restore refs whose objects no remote packfile carries, from this clone",
     (("--dry-run", "Report the broken refs without uploading anything"),)),
    ("lfs-transfer", "git-remote-seafile lfs-transfer <seafile://url>",
     "Git LFS Custom Transfer Agent", ()),
    ("set-head", "git-remote-seafile set-head <seafile://url> <branch>",
     "Set default branch (HEAD) on remote", ()),
    ("lock-status", "git-remote-seafile lock-status <seafile://url>",
     "Inspect repository lock state", ()),
    ("unlock", "git-remote-seafile unlock <seafile://url> [--force]",
     "Clear repository lock",
     (("--force", "Clear the lock even when another machine holds it"),)),
    ("desktop-url", "git-remote-seafile desktop-url <path>",
     "Generate seafile:// URL from local path", ()),
    ("snapshot", "git-remote-seafile snapshot [SOURCE] [options]",
     "Snapshot a whole tree, ignored files included, into a vault on Seafile "
     "(also installed as git-seafile-snapshot; see its own --help)", ()),
    ("version", "git-remote-seafile version", "Display version", ()),
)

_COMMAND_TABLE: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {
    name: (usage, summary, options) for name, usage, summary, options in _COMMANDS
}

# Two spaces of gutter between the longest usage line and its summary.
_USAGE_WIDTH = max(len(usage) for _, usage, _, _ in _COMMANDS) + 2


def print_help() -> None:
    print("git-remote-seafile - Git remote helper for Seafile")
    print()
    print("Usage as Git Remote Helper:")
    print("  git clone seafile://<server>/<library>/<path>")
    print("  git remote add origin seafile://<library>/<path>")
    print("  git push origin main")
    print("  git fetch origin")
    print()
    print("Commands:")
    for _, usage, summary, _ in _COMMANDS:
        print(f"  {usage:<{_USAGE_WIDTH}}{summary}")
    print()
    print("Run 'git-remote-seafile <command> --help' for command-specific options.")


def print_command_help(command: str) -> None:
    """Help for a single subcommand.

    `git-remote-seafile gc --help` used to fall through to the command itself
    and fail with "Compaction failed: ...", because only a bare `--help` with
    no subcommand was recognised.
    """
    usage, summary, options = _COMMAND_TABLE[command]
    print(f"Usage: {usage}")
    print()
    print(summary)
    if options:
        width = max(len(flag) for flag, _ in options) + 2
        print()
        print("Options:")
        for flag, description in options:
            print(f"  {flag:<{width}}{description}")


def _relative_within(target: Path, root: Path) -> Path | None:
    """*target* relative to *root*, or None when target is not inside root.

    Kept as a named alias because the call sites read better with it; the
    comparison itself lives in ``safety.safe_relative_to`` so that this guard,
    the doctor's ``where`` and the snapshot tool's synced-folder check all
    decide containment the same way.  Two implementations of one guard is how
    one of them ends up stale.
    """
    return safe_relative_to(target, root)


def _url_path(rel: Path) -> str:
    """The path segment of a generated URL, or "" when the library root is meant.

    Two jobs.  The root: ``Path(".")`` renders as ``.``, which the URL grammar
    refuses as a path component, so the root must produce *no* segment and let
    the parser fall back to its ``git-repo`` default.  And encoding: a worktree
    path can contain a space, ``#``, ``?`` or a literal ``%``, any of which
    changes the meaning of the URL it is pasted into -- ``#`` would truncate it
    to a fragment.  Each component is encoded separately and the separators are
    re-joined, so encoding never invents one.
    """
    if rel == Path("."):
        return ""
    return "/" + "/".join(quote(part, safe="") for part in rel.parts)


def _positional_args(args: list[str], *, value_options: tuple[str, ...] = ()) -> list[str]:
    """Arguments that are not options, nor the values those options consume.

    Pass the arguments *after* the subcommand name: `args[0]` is the
    subcommand, so feeding the whole list in returns the subcommand as the
    first operand.

    `unlock --force seafile://host/lib` read "--force" as the URL (D14): the
    flag was looked for anywhere in argv, but the URL was taken blindly from
    `args[1]`.  The same shape broke `gc --min-packs 3 <url>`.
    """
    positional: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg in value_options:
            skip_value = True
            continue
        if arg.startswith("-") and arg != "-":
            continue
        positional.append(arg)
    return positional


def _parse_min_packs(args: list[str], default: int = 2) -> int:
    """Read `--min-packs N` / `--min-packs=N`, warning and falling back on junk."""
    raw: str | None = None
    for index, arg in enumerate(args):
        if arg == "--min-packs":
            if index + 1 >= len(args):
                sys.stderr.write(
                    f"Warning: --min-packs requires an integer value. Using default ({default}).\n"
                )
                return default
            raw = args[index + 1]
            break
        if arg.startswith("--min-packs="):
            raw = arg.split("=", 1)[1]
            break

    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.stderr.write(
            f"Warning: invalid --min-packs value '{raw}'. Using default ({default}).\n"
        )
        return default
    if value < 1:
        sys.stderr.write(
            f"Warning: --min-packs must be a positive integer, got '{raw}'. Using default ({default}).\n"
        )
        return default
    return value


def _account_email(resp: Any) -> str | None:
    """The account's email from an ``/api2/account/info/`` answer; ``None`` when the body is not an object.

    A 200 carrying a JSON array, a string or no JSON at all is what a captive
    portal or a wrong URL looks like, and used to escape as an AttributeError
    or ValueError traceback from two read-only diagnostics.
    """
    try:
        info = resp.json()
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    return str(info.get("email") or "(no email reported)")


def _doctor(url: str | None, *, upload_limit: bool = False, max_mib: int | None = None) -> int:
    """Run every first-use check in order and say what to do next.

    Stops at the first failing step: each step needs the one before it.  With no
    URL only the login is checked.  The write probe creates and removes one
    small file inside the repository path (creating the path, which the first
    push would do anyway), so a green run means a push can write there.
    """
    def ok(msg: str) -> None:
        print(f"[ ok ] {msg}")

    def fail(msg: str) -> int:
        print(f"[FAIL] {msg}")
        return 1

    try:
        client = SeafileClient()
        resp = client.session.get(f"{client.server_url}/api2/account/info/", timeout=client.timeout)
    except Exception as ex:
        return fail(f"no usable login: {ex}")
    if resp.status_code != 200:
        return fail(f"{client.server_url} rejected the token (HTTP {resp.status_code}); log in again")
    email = _account_email(resp)
    if email is None:
        return fail(
            f"{client.server_url} answered the account request with HTTP 200 but not a "
            "Seafile account object; check the server URL (a proxy or login page?)"
        )
    ok(f"logged in to {client.server_url} as {email}")
    if not url:
        print("Next: git-remote-seafile doctor <seafile://library/path> to check a destination.")
        return 0

    try:
        helper = RemoteHelper("doctor", url)
    except Exception as ex:
        return fail(f"cannot resolve {mask_url(url)}: {ex}")
    ok(
        f"{mask_url(url)} is read as: server {helper.client.server_url}, "
        f"library '{helper.library_name}', path '{helper.repo_path}'"
    )
    if helper.path_defaulted:
        print(f"       (no path was given; '{helper.repo_path}' is the default)")

    try:
        wt = get_local_work_tree()
        check_preflight_safety(
            helper.client, helper.library_name, helper.repo_path,
            local_worktree=wt, push_mode=True, synced_libs=discover_local_synced_libraries(),
        )
    except SafetyError as safe_err:
        return fail("safety check blocked this destination:" + chr(10) + f"{safe_err}")
    except Exception as ex:
        return fail(f"safety check could not run: {ex}")
    ok("safety checks passed")

    import uuid

    PROBE_BYTES = b"git-seafile doctor"
    probe = f".git-seafile-doctor-{uuid.uuid4().hex[:8]}"
    try:
        helper.client.upload_file(helper.repo_id, helper.repo_path, probe, PROBE_BYTES, replace=True)
        landed = helper.client.get_file_bytes(helper.repo_id, f"{helper.repo_path}/{probe}")
        removed = helper.client.delete_entry(helper.repo_id, f"{helper.repo_path}/{probe}")
    except Exception as ex:
        return fail(f"cannot write to {helper.repo_path}: {ex}")
    if landed != PROBE_BYTES or not removed:
        return fail(f"write probe did not round-trip cleanly in {helper.repo_path}")
    ok(f"can write and delete in {helper.repo_path}")

    try:
        copies, capped = find_conflict_copies(helper.client, helper.repo_id, helper.repo_path)
    except Exception as ex:
        return fail(f"cannot list {helper.repo_path} to look for conflict copies: {ex}")
    if copies:
        blocking = [c for c in copies if "/refs/" in c]
        shown = chr(10).join(f"       {c}" for c in copies[:20])
        more = f"{chr(10)}       ... and {len(copies) - 20} more" if len(copies) > 20 else ""
        print(
            f"[FAIL] {len(copies)} Seafile conflict copy(ies) under {helper.repo_path}"
            + (f", {len(blocking)} of them under refs/ (gc refuses to run until they are resolved)"
               if blocking else "")
            + chr(10) + shown + more
        )
        print("Hint: keep the file that holds the right content, delete the others (RUNBOOK.md, "
              "\"Conflict Artifacts\"), then run doctor again")
        return 1
    if capped:
        # Not [ ok ]: the walk did not finish, so "none found" is not "none".
        print(f"Warning: stopped after listing {CONFLICT_WALK_MAX_DIRS} directories under "
              f"{helper.repo_path} and found no conflict copy; the rest was not looked at "
              "(gc walks refs/ without that limit and refuses on a copy there)")
    else:
        ok(f"no Seafile conflict copies under {helper.repo_path}")

    if upload_limit:
        code = _doctor_upload_limit(helper, max_mib)
        if code:
            return code
    print(f"Next: git remote add origin {url} && git push -u origin <branch>")
    return 0


def _doctor_upload_limit(helper: Any, max_mib: int | None) -> int:
    """Measure the largest single upload and say what ``seafile.gcmaxpackmib`` fits."""
    import uuid
    from .config import RemoteConfig

    ceiling = (max_mib * MIB) if max_mib else PROBE_CEILING_BYTES
    scratch = f"{helper.repo_path.rstrip('/')}/.git-seafile-doctor-{uuid.uuid4().hex[:8]}"
    print(f"measuring the largest single upload (up to {format_bytes(ceiling)} of zeros into "
          f"{scratch}, deleted afterwards) ...", flush=True)
    try:
        result = measure_upload_limit(
            upload_attempt(helper.client, helper.repo_id, scratch), ceiling=ceiling
        )
    finally:
        # delete_entry answers False for a refusal and raises only on a transport
        # failure, so both are a folder left behind.
        try:
            removed, why = helper.client.delete_entry(helper.repo_id, scratch), ""
        except Exception as ex:
            removed, why = False, f": {ex}"
        if not removed:
            print(f"Warning: could not remove {scratch}{why}")
    for size, outcome in result.trials:
        print(f"       {format_bytes(size):>10}  " + ("accepted" if outcome is None else f"refused: {outcome}"))
    configured = RemoteConfig.load().gc_max_pack_mib
    if result.accepted == 0:
        print(f"[FAIL] even {format_bytes(PROBE_FLOOR_BYTES)} was refused ({result.reason}); that is "
              "not a size limit, so there is no cap to recommend. Fix what the line above says first")
        return 1
    if result.unlimited_below_ceiling:
        print(f"[ ok ] a single upload of {format_bytes(result.accepted)} was accepted (the most "
              f"tried); seafile.gcmaxpackmib is {configured}")
        return 0
    fits = recommended_pack_mib(result.accepted)
    print(f"[{'FAIL' if configured > fits else ' ok '}] the server accepts a single upload of "
          f"about {format_bytes(result.accepted)} and refuses {format_bytes(result.rejected or 0)}")
    if chunk_warning(result.accepted):
        print("Warning: a file of 1 GiB or more is sent in 128 MiB chunks, and this server refuses "
              "a body that size; such a file cannot be pushed until the limit is raised")
    if configured > fits:
        print(f"       seafile.gcmaxpackmib is {configured}, so a push of more than "
              f"{format_bytes(result.accepted)} fails after the lock is taken")
        print(f"Hint: git config seafile.gcmaxpackmib {fits}   (or raise the server's limit: "
              "client_max_body_size in nginx)")
        return 1
    print(f"       seafile.gcmaxpackmib is {configured}, which fits")
    return 0


# --------------------------------------------------------------------------
# Subcommands
#
# One convention for all of them (E2):
#
#   stdout  what the command is *for*: its result or report, including the
#           verdict of a report command ("[FAIL]", "[BLOCKED ...]", LOCKED).
#   stderr  everything that is not the result: usage errors, failures that kept
#           the command from producing a result, warnings.
#   exit    0 done / nothing to do;  1 the command ran and the answer is "no"
#           or it failed;  2 the command line itself was wrong.
#
# Each command is a function taking the arguments *after* its name.
# --------------------------------------------------------------------------


def _usage(command: str) -> int:
    sys.stderr.write(f"Usage: {_COMMAND_TABLE[command][0]}\n")
    return 2


def _error(message: str) -> int:
    sys.stderr.write(f"{message}\n")
    return 1


def _cmd_check_auth(rest: list[str]) -> int:
    try:
        client = SeafileClient()
        info_url = f"{client.server_url}/api2/account/info/"
        resp = client.session.get(info_url, timeout=client.timeout)
    except Exception as ex:
        return _error(f"Error: {ex}")
    if resp.status_code == 200:
        email = _account_email(resp)
        if email is None:
            return _error(
                f"Error: {client.server_url} answered the account request with HTTP 200 but "
                "not a Seafile account object; check the server URL (a proxy or login page?)"
            )
        print(f"Authenticated successfully with {client.server_url} as {email}")
        return 0
    return _error(f"Authentication failed: HTTP {resp.status_code} {resp.text}")


def _cmd_doctor(rest: list[str]) -> int:
    positional = _positional_args(rest, value_options=("--max-mib",))
    max_mib: int | None = None
    for index, arg in enumerate(rest):
        raw = (rest[index + 1] if index + 1 < len(rest) else "") if arg == "--max-mib"             else arg.split("=", 1)[1] if arg.startswith("--max-mib=") else None
        if raw is None:
            continue
        try:
            max_mib = int(raw)
        except ValueError:
            max_mib = 0
        if not 1 <= max_mib <= PROBE_CEILING_BYTES // MIB:
            sys.stderr.write(
                f"Error: --max-mib needs a whole number from 1 to {PROBE_CEILING_BYTES // MIB} "
                f"(a body of 1 GiB or more is sent in chunks), got '{raw}'\n"
            )
            return 2
    if max_mib is not None and "--upload-limit" not in rest:
        sys.stderr.write("Error: --max-mib only applies with --upload-limit\n")
        return 2
    return _doctor(positional[0] if positional else None,
                   upload_limit="--upload-limit" in rest, max_mib=max_mib)


def _cmd_check_safety(rest: list[str]) -> int:
    positional = _positional_args(rest)
    if not positional:
        return _usage("check-safety")
    try:
        helper = RemoteHelper("check-safety", positional[0])
        wt = get_local_work_tree()
        synced = discover_local_synced_libraries()
        print(f"Target Server     : {helper.client.server_url}")
        print(f"Target Library    : {helper.library_name}")
        print(f"Remote Path       : {helper.repo_path}")
        print(f"Local Working Tree: {wt if wt else '(none detected)'}")
        print(f"Synced Libraries  : {len(synced)} active on this machine")
        for s in synced:
            print(f"  - {s['name']} -> {s['worktree']}")

        print("\nEvaluating safety guardrails (Trap 1 & Trap 2)...")
        warnings = check_preflight_safety(
            helper.client,
            helper.library_name,
            helper.repo_path,
            local_worktree=wt,
            push_mode=True,
            synced_libs=synced,
        )
        for w in warnings:
            print(f"\n[Warning]\n{w}")

        print("\n[PASSED] Pre-flight safety checks completed successfully with no blocking issues.")
        return 0
    except SafetyError as safe_err:
        print(f"\n[BLOCKED - SAFETY ERROR]\n{safe_err}")
        return 1
    except Exception as ex:
        return _error(f"Safety check error: {ex}")


def _cmd_test(rest: list[str]) -> int:
    positional = _positional_args(rest)
    if not positional:
        return _usage("test")
    try:
        helper = RemoteHelper("test", positional[0])
        print(f"Server      : {helper.client.server_url}")
        print(f"Library     : {helper.library_name} (ID: {helper.repo_id})")
        print(f"Remote Path : {helper.repo_path}")
        print("Fetching remote ref list...")
        # cmd_list writes the wire protocol (and enforces the empty-repo
        # guard); capture it and render the listing for humans instead.
        import contextlib

        protocol = io.StringIO()
        with contextlib.redirect_stdout(protocol):
            helper.cmd_list()
        for line in protocol.getvalue().splitlines():
            if not line.strip() or line.startswith(":"):
                continue
            if line.startswith("@"):
                print(f"  HEAD -> {line[1:].split(' ', 1)[0]}")
            else:
                sha, _, name = line.partition(" ")
                print(f"  {name} {sha}")
        print("Connection successful!")

        warnings = check_preflight_safety(helper.client, helper.library_name, helper.repo_path, push_mode=False)
        for w in warnings:
            print(f"[Warning] {w}")
        return 0
    except SafetyError as safe_err:
        print(f"[Safety Warning] {safe_err}")
        return 1
    except Exception as ex:
        return _error(f"Test failed: {ex}")


def _gc_tail(url: str) -> int:
    """``gc --tail``: the automatic tier, on demand.

    Lets a user run, test or schedule exactly what ``seafile.autogc`` does.
    It honours the same movable-pack threshold, so "skipped" is a normal answer,
    but not the download budget: that exists to keep an *automatic* merge from
    holding the push lock for minutes, and this one was asked for.
    """
    try:
        from .config import RemoteConfig
        from .gc import describe_size_delta, tail_merge
        helper = RemoteHelper("gc", url)
        res = tail_merge(
            helper.client, helper.repo_id, helper.repo_path, config=RemoteConfig.load(),
            respect_budget=False,
        )
    except Exception as ex:
        return _error(f"Tail merge failed: {ex}")
    status = res.get("status")
    if status in ("ok", "incomplete"):
        delta = describe_size_delta((res["tail_bytes"] - res["new_pack_bytes"]) // 1024)
        print(
            f"Merged {res['merged_packs']} small packfiles ({delta}); "
            f"{res['frozen_packs']} large packfile(s) left in place."
        )
        if status == "incomplete":
            stopped = str(res.get("cleanup_stopped", "")).strip()
            if stopped:
                print(stopped)
            return 1
        return 0
    if status == "error":
        return _error(str(res.get("message", "Tail merge failed.")))
    print(res.get("message", "Tail merge skipped."))
    return 0


def _cmd_gc(rest: list[str]) -> int:
    positional = _positional_args(rest, value_options=("--min-packs",))
    if not positional:
        return _usage("gc")
    url = positional[0]
    if "--tail" in rest:
        return _gc_tail(url)
    min_packs = _parse_min_packs(rest)
    try:
        from .config import RemoteConfig
        helper = RemoteHelper("gc", url)
        from .gc import compact_repository, describe_size_delta
        res = compact_repository(
            helper.client,
            helper.repo_id,
            helper.repo_path,
            min_packs=min_packs,
            config=RemoteConfig.load(),
        )
    except Exception as ex:
        return _error(f"Compaction failed: {ex}")
    status = res.get("status")
    if status == "ok":
        print(
            f"Compacted {res['old_packs']} packfiles into {res['new_packs']} "
            f"({describe_size_delta(res['saved_kb'])})."
        )
        return 0
    if status == "incomplete":
        # The consolidation succeeded and was verified; the *cleanup* stopped
        # short because ownership of the lock was lost.  Nothing was lost, so
        # this is not an error -- but it is not "ok" either, and a scheduled job
        # that reads only the exit code has to tell a finished compaction from
        # one that left orphans.
        print(
            f"Compacted {res['old_packs']} packfiles into {res['new_packs']} "
            f"({describe_size_delta(res['saved_kb'])}) but did not finish "
            f"removing the superseded ones."
        )
        stopped = str(res.get("cleanup_stopped", "")).strip()
        if stopped:
            print(stopped)
        return 1
    if status == "error":
        # An aborted compaction is a real failure (download error, size
        # mismatch, invalid pack name); scripts must see it.
        return _error(str(res.get("message", "Compaction failed.")))
    print(res.get("message", "Compaction skipped."))
    return 0


def _cmd_repair(rest: list[str]) -> int:
    positional = _positional_args(rest)
    if not positional:
        return _usage("repair")
    url = positional[0]
    dry_run = "--dry-run" in rest
    try:
        from .config import RemoteConfig
        from .repair import repair_remote
        helper = RemoteHelper("repair", url)
        res = repair_remote(
            helper.client,
            helper.repo_id,
            helper.repo_path,
            dry_run=dry_run,
            config=RemoteConfig.load(),
        )
    except Exception as ex:
        return _error(f"Repair failed: {ex}")
    status = res.get("status")
    if status == "clean":
        print("Every ref names objects the remote's packfiles carry; nothing to repair.")
        return 0
    if status == "broken":
        print(f"{len(res['broken_refs'])} ref(s) name objects no remote packfile carries:")
        for name, sha in res["broken_refs"]:
            print(f"  {name} ({sha[:12]})")
        print("Re-run without --dry-run to upload them from this clone.")
        return 1
    if status == "ok":
        print(
            f"Repaired {len(res['repaired'])} ref(s): uploaded "
            f"{len(res['packs_uploaded'])} packfile(s) to the remote."
        )
        return 0
    if status in ("partial", "error") and "unrepaired" in res:
        for name in res["repaired"]:
            print(f"  repaired: {name}")
        for name, why in res["unrepaired"]:
            print(f"  not repaired: {name} -- {why}")
        return 1
    return _error(str(res.get("message", "Repair failed.")))


def _cmd_lfs_transfer(rest: list[str]) -> int:
    positional = _positional_args(rest)
    if not positional:
        return _usage("lfs-transfer")
    try:
        helper = RemoteHelper("lfs", positional[0])
        from .lfs import LFSTransferAgent
        git_dir = None
        try:
            from .git_util import get_git_dir
            git_dir = get_git_dir()
        except Exception:
            pass
        agent = LFSTransferAgent(helper.client, helper.repo_id, helper.repo_path, git_dir=git_dir)
        agent.run()
        return 0
    except Exception as ex:
        return _error(f"LFS transfer error: {ex}")


def _cmd_set_head(rest: list[str]) -> int:
    positional = _positional_args(rest)
    if len(positional) < 2:
        return _usage("set-head")
    url, branch = positional[0], positional[1]
    try:
        helper = RemoteHelper("set-head", url)
        head_target = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
        from .refs import iter_refs
        existing_branches = {
            ref_name
            for ref_name, _ in iter_refs(helper.client, helper.repo_id, helper.repo_path, "refs/heads")
        }
        if head_target not in existing_branches:
            return _error(f"Error: branch '{head_target}' does not exist on remote {mask_url(url)}")
        # set-head is an explicit administrative maintenance command. It bypasses
        # RemoteLock to allow recovering remotes where an interrupted operation
        # left a broken HEAD or stale lock, while strictly validating that the
        # target branch exists.
        old_head = helper.client.get_file_text(helper.repo_id, helper.repo_path + "/HEAD")
        head_content = f"ref: {head_target}\n".encode("utf-8")
        helper.client.upload_file(
            helper.repo_id,
            helper.repo_path,
            "HEAD",
            head_content,
            replace=True,
        )
        print(f"Updated remote HEAD on {mask_url(url)} to {head_target} (was {old_head or 'unset'})")
        return 0
    except Exception as ex:
        return _error(f"Failed to set HEAD: {ex}")


def _cmd_lock_status(rest: list[str]) -> int:
    positional = _positional_args(rest)
    if not positional:
        return _usage("lock-status")
    url = positional[0]
    try:
        helper = RemoteHelper("lock-status", url)
        lock = RemoteLock(helper.client, helper.repo_id, helper.repo_path)
        status = lock.get_status()
    except Exception as ex:
        return _error(f"Failed to check lock status: {ex}")
    if status.get("locked") is None:
        # A listing failure is not "unlocked": reporting it as such sent
        # operators to `unlock --force` on a false confidence.
        return _error(
            f"Could not inspect the lock queue for {mask_url(url)} "
            f"({status.get('error', 'unknown error')})."
        )
    if not status.get("locked"):
        print(f"Repository at {mask_url(url)} is UNLOCKED.")
        return 0
    print(f"Repository at {mask_url(url)} is LOCKED:")
    if status.get("stale"):
        print("  State    : STALE -- the ticket's lease has expired, so a plain push would reclaim it")
    print(f"  Owner    : {status.get('owner')}")
    print(f"  Machine  : {status.get('machine')}")
    if status.get("pid"):
        print(f"  PID      : {status.get('pid')}")
    if status.get("nonce"):
        print(f"  Nonce    : {status.get('nonce')}")
    print(f"  Protocol : {status.get('protocol')}")
    if not status.get("holding"):
        # The grant marker is what stops a newcomer from preempting an active
        # holder, so its absence is worth naming: either the winning ticket is
        # from a client older than v0.8.4, or the queue is abandoned and needs
        # --force.
        print("  Granted  : not marked -- a pre-v0.8.4 ticket, or an "
              "abandoned queue that --force would clear")
    print(f"  Expires  : in {status.get('expires_in')}s")
    return 0


def _cmd_unlock(rest: list[str]) -> int:
    positional = _positional_args(rest)
    if not positional:
        return _usage("unlock")
    url = positional[0]
    force = "--force" in rest
    try:
        helper = RemoteHelper("unlock", url)
        lock = RemoteLock(helper.client, helper.repo_id, helper.repo_path)
        was_locked = lock.unlock(force=force)
    except RepositoryLockedError as rle:
        return _error(f"Error: {rle}")
    except Exception as ex:
        return _error(f"Failed to unlock repository: {ex}")
    if not was_locked:
        print(f"Repository at {mask_url(url)} is not locked; nothing to release.")
    elif force:
        print(f"Forcibly unlocked repository at {mask_url(url)}.")
    else:
        print(f"Unlocked repository at {mask_url(url)}.")
    return 0


def _cmd_desktop_url(rest: list[str]) -> int:
    positional = _positional_args(rest)
    if not positional:
        return _usage("desktop-url")
    target_path = Path(positional[0]).resolve()
    try:
        client = SeafileClient(require_credentials=False)
    except SeafileAuthError as auth_err:
        # Only the server URL is needed here: this command builds a URL and
        # makes no authenticated request.  Requiring a token made it fail for
        # users who have the desktop client installed but no reachable
        # credentials, which is exactly the audience for this command.
        return _error(f"Cannot determine the Seafile server: {auth_err}")

    try:
        synced_libs = discover_local_synced_libraries()
        for lib in synced_libs:
            rel = _relative_within(target_path, lib["worktree"])
            if rel is None:
                continue
            # The *repo id*, not the worktree folder name: a synced folder is
            # named by the user and need not match the library's name on the
            # server (and two libraries can share one).  get_repo_id resolves an
            # id before it tries any name, so the URL works whatever the folder
            # is called.
            server_url = (lib.get("server_url") or client.server_url or "").rstrip("/")
            if not server_url:
                return _error(
                    f"Matched '{target_path}' to library '{lib['repo_id']}', but no "
                    f"server URL is known for it. Set SEAFILE_SERVER and retry."
                )
            # Always state the scheme.  A bare first segment is read as a
            # *library* unless it happens to look like a host, so the
            # scheme-less spelling the command used to emit was the one form the
            # URL grammar could misread.
            scheme, sep, rest_url = server_url.partition("://")
            if not sep:
                scheme, rest_url = "https", server_url
            host, slash, context = rest_url.partition("/")
            if slash and context:
                # A server under a path has no spelling in a seafile:// URL
                # (the first segment after the host would be read as the
                # library), so the short form is printed and the server named
                # where it can be set.
                print(
                    f"Note: this server is served under /{context}; a seafile:// URL cannot "
                    f"carry that, so set SEAFILE_SERVER={scheme.lower()}://{rest_url} "
                    "(or let the desktop client's account supply it)",
                    file=sys.stderr,
                )
                print(f"seafile://{quote(lib['repo_id'], safe='')}{_url_path(rel)}")
                return 0
            print(f"seafile://{scheme.lower()}://{rest_url}/{quote(lib['repo_id'], safe='')}{_url_path(rel)}")
            return 0
        return _error(f"Could not match '{target_path}' to any local Seafile library worktree.")
    except Exception as ex:
        return _error(f"Error resolving path: {ex}")


def _cmd_snapshot(rest: list[str]) -> int:
    # Imported here: the snapshot tool is large and a `git push` never needs it.
    from .snapshot_tool import main as snapshot_main
    try:
        return int(snapshot_main(rest))
    except SystemExit as exc:  # argparse: --help, or a usage error
        return int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 2)


def _cmd_config(rest: list[str]) -> int:
    if rest:
        return _usage("config")
    from .settings_report import effective_settings, format_report
    print(format_report(effective_settings()))
    return 0


def _cmd_version(rest: list[str]) -> int:
    from . import __version__
    print(f"git-remote-seafile v{__version__}")
    return 0


_HANDLERS = {
    "doctor": _cmd_doctor,
    "config": _cmd_config,
    "test": _cmd_test,
    "check-safety": _cmd_check_safety,
    "check-auth": _cmd_check_auth,
    "gc": _cmd_gc,
    "repair": _cmd_repair,
    "lfs-transfer": _cmd_lfs_transfer,
    "set-head": _cmd_set_head,
    "lock-status": _cmd_lock_status,
    "unlock": _cmd_unlock,
    "desktop-url": _cmd_desktop_url,
    "snapshot": _cmd_snapshot,
    "version": _cmd_version,
}


def _run_helper(args: list[str]) -> int:
    """The remote-helper invocation: ``<remote-name> <url>`` or just ``<url>``.

    Both forms carry the seafile:// URL, so an invocation with no such URL is
    not a helper call at all -- it is a mistyped subcommand.  Without this check
    a typo like `chck-auth` was passed to the helper as the *remote URL* and
    surfaced as a baffling "library not found" or auth error.
    """
    if not any(arg.lower().startswith("seafile://") for arg in args):
        sys.stderr.write(f"git-remote-seafile: unknown command '{args[0]}'\n\n")
        print_help()
        return 2
    remote_name = args[0]
    url = args[1] if len(args) > 1 else args[0]
    try:
        helper = RemoteHelper(remote_name, url)
        helper.run()
        return 0
    except Exception as ex:
        sys.stderr.write(f"git-remote-seafile fatal error: {ex}\n")
        return 1


def _is_git_invoking_the_helper(args: list[str]) -> bool:
    """True when ``<subcommand-name> <seafile url>`` is git talking, not a person.

    Both shapes are ``name url``, so argv cannot tell them apart.  Git, and only
    git, runs a helper with ``GIT_DIR`` set, no terminal on stdin, and the
    protocol's first word waiting on it, so all three must hold.  Peeking does
    not consume the byte stream: the helper still reads ``capabilities`` itself.
    """
    if len(args) != 2 or not args[1].lower().startswith("seafile://"):
        return False
    if not os.environ.get("GIT_DIR"):
        return False
    try:
        stdin = sys.stdin
        if stdin is None or stdin.isatty():
            return False
        raw: Any = stdin.buffer
        return bool(raw.peek(12)[:12].startswith(b"capabilities"))
    except Exception:
        return False


def _unwind_on_termination() -> None:
    """Turn SIGTERM (and Ctrl-Break on Windows) into an exit that unwinds.

    A bare SIGTERM ends the process without running ``finally`` blocks, so the
    lock ticket of a push cut off by a CI timeout or a ``kill`` stayed on the
    server for a full lease -- for every other machine; the dead-PID shortcut
    only helps the same one.  Raising ``SystemExit`` instead lets
    ``RemoteLock.__exit__`` delete the ticket.  Not installable off the main
    thread, and a console without the signal simply keeps its default.
    """
    def handler(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass


def main() -> int:
    _unwind_on_termination()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(newline="\n", encoding="utf-8")
        except Exception:
            pass
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        # A library name or path with non-ASCII characters on a legacy Windows
        # code page would otherwise raise UnicodeEncodeError from a diagnostic
        # write -- in the middle of a push.  ``replace`` keeps the message
        # readable and, above all, keeps it from becoming the failure.
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
        return 0
    if args[0] in ("-v", "--version"):
        return _cmd_version([])

    command = args[0]
    if command in _HANDLERS and _is_git_invoking_the_helper(args):
        # A remote *named* like a subcommand (`gc`, `test`, `unlock`) is run by
        # git as `git-remote-seafile <that name> <url>`; without this,
        # `git fetch gc` ran a full compaction.
        return _run_helper(args)
    if command in _HANDLERS:
        # `snapshot` has a full help of its own; every other command is rendered
        # from the table above.
        if command != "snapshot" and any(arg in ("-h", "--help") for arg in args[1:]):
            print_command_help(command)
            return 0
        return _HANDLERS[command](args[1:])

    return _run_helper(args)


if __name__ == "__main__":
    sys.exit(main())
