"""cli.py - Entry point for git-remote-seafile."""

from __future__ import annotations

import io
import sys
from pathlib import Path
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
from .url import mask_url


# (name, usage, summary, options) for every subcommand, in listing order.
# Both the top-level listing and `git-remote-seafile <cmd> --help` render from
# this one table: a flag added to a usage line cannot then go missing from the
# per-command help, or vice versa (D17).  The flags used to be documented
# nowhere -- `--min-packs` and `--force` appeared only in the strings that
# happened to print when the command was called wrongly.
_COMMANDS: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    ("doctor", "git-remote-seafile doctor [<seafile://url>]",
     "Check login, library, safety and write access in one go", ()),
    ("test", "git-remote-seafile test <seafile://url>",
     "Test connection and discover refs", ()),
    ("check-safety", "git-remote-seafile check-safety <seafile://url>",
     "Run pre-flight safety checks (Trap 1 & 2)", ()),
    ("check-auth", "git-remote-seafile check-auth",
     "Verify active Seafile login", ()),
    ("gc", "git-remote-seafile gc <seafile://url> [--min-packs N]",
     "Compact multiple remote packfiles",
     (("--min-packs N",
       "Only compact when the remote holds at least N packfiles (default: 2)"),)),
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


def _doctor(url: str | None) -> int:
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
    ok(f"logged in to {client.server_url} as {resp.json().get('email')}")
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
    print(f"Next: git remote add origin {url} && git push -u origin <branch>")
    return 0


def main() -> int:
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

    args = sys.argv[1:]
    if not args:
        print_help()
        return 0

    if args[0] in ("-h", "--help", "help"):
        print_help()
        return 0

    if args[0] in _COMMAND_TABLE and any(arg in ("-h", "--help") for arg in args[1:]):
        print_command_help(args[0])
        return 0

    if args[0] in ("-v", "--version", "version"):
        from . import __version__
        print(f"git-remote-seafile v{__version__}")
        return 0

    if args[0] == "doctor":
        return _doctor(args[1] if len(args) > 1 else None)

    if args[0] == "check-auth":
        try:
            client = SeafileClient()
            info_url = f"{client.server_url}/api2/account/info/"
            resp = client.session.get(info_url, timeout=client.timeout)
            if resp.status_code == 200:
                email = resp.json().get("email")
                print(f"Authenticated successfully with {client.server_url} as {email}")
                return 0
            else:
                print(f"Authentication failed: HTTP {resp.status_code} {resp.text}")
                return 1
        except Exception as ex:
            print(f"Error: {ex}")
            return 1

    if args[0] == "check-safety":
        if len(args) < 2:
            print("Usage: git-remote-seafile check-safety <seafile://url>")
            return 1
        url = args[1]
        try:
            helper = RemoteHelper("check-safety", url)
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
            print(f"Safety check error: {ex}")
            return 1

    if args[0] == "test":
        if len(args) < 2:
            print("Usage: git-remote-seafile test <seafile://url>")
            return 1
        url = args[1]
        try:
            helper = RemoteHelper("test", url)
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
            print(f"Test failed: {ex}")
            return 1

    if args[0] == "gc":
        # args[1:]: args[0] is the subcommand itself, not a positional operand.
        positional = _positional_args(args[1:], value_options=("--min-packs",))
        if not positional:
            print("Usage: git-remote-seafile gc <seafile://url> [--min-packs N]")
            return 1
        url = positional[0]
        min_packs = _parse_min_packs(args)
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
            status = res.get("status")
            if status == "ok":
                print(
                    f"Compacted {res['old_packs']} packfiles into {res['new_packs']} "
                    f"({describe_size_delta(res['saved_kb'])})."
                )
                return 0
            if status == "incomplete":
                # The consolidation succeeded and was verified; the *cleanup*
                # stopped short because ownership of the lock was lost.  Nothing
                # was lost, so this is not an error -- but it is not "ok" either,
                # and a scheduled job that reads only the exit code has to be
                # able to tell a finished compaction from one that left orphans.
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
                # An aborted compaction is a real failure (download error,
                # size mismatch, invalid pack name); scripts must see it.
                print(res.get("message", "Compaction failed."))
                return 1
            print(res.get("message", "Compaction skipped."))
            return 0
        except Exception as ex:
            print(f"Compaction failed: {ex}")
            return 1

    if args[0] == "lfs-transfer":
        if len(args) < 2:
            print("Usage: git-remote-seafile lfs-transfer <seafile://url>")
            return 1
        url = args[1]
        try:
            helper = RemoteHelper("lfs", url)
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
            sys.stderr.write(f"LFS transfer error: {ex}\n")
            return 1

    if args[0] == "set-head":
        if len(args) < 3:
            print("Usage: git-remote-seafile set-head <seafile://url> <branch>")
            return 1
        url = args[1]
        branch = args[2]
        try:
            helper = RemoteHelper("set-head", url)
            head_target = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
            from .refs import iter_refs
            existing_branches = {
                ref_name
                for ref_name, _ in iter_refs(helper.client, helper.repo_id, helper.repo_path, "refs/heads")
            }
            if head_target not in existing_branches:
                sys.stderr.write(f"Error: branch '{head_target}' does not exist on remote {mask_url(url)}\n")
                return 1
            # set-head is an explicit administrative maintenance command. It bypasses
            # RemoteLock to allow recovering remotes where an interrupted operation left a
            # broken HEAD or stale lock, while strictly validating that the target branch exists.
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
            print(f"Failed to set HEAD: {ex}")
            return 1

    if args[0] == "lock-status":
        if len(args) < 2:
            print("Usage: git-remote-seafile lock-status <seafile://url>")
            return 1
        url = args[1]
        try:
            helper = RemoteHelper("lock-status", url)
            lock = RemoteLock(helper.client, helper.repo_id, helper.repo_path)
            status = lock.get_status()
            if status.get("locked") is None:
                # A listing failure is not "unlocked": reporting it as such
                # sent operators to `unlock --force` on a false confidence.
                # Fail loudly and distinctly instead.
                sys.stderr.write(
                    f"Could not inspect the lock queue for {mask_url(url)} "
                    f"({status.get('error', 'unknown error')}).\n"
                )
                return 1
            if not status.get("locked"):
                print(f"Repository at {mask_url(url)} is UNLOCKED.")
                return 0
            print(f"Repository at {mask_url(url)} is LOCKED:")
            if status.get("stale"):
                print("  State    : STALE -- the ticket's lease has expired, so a "
                      "plain push would reclaim it")
            print(f"  Owner    : {status.get('owner')}")
            print(f"  Machine  : {status.get('machine')}")
            if status.get("pid"):
                print(f"  PID      : {status.get('pid')}")
            if status.get("nonce"):
                print(f"  Nonce    : {status.get('nonce')}")
            print(f"  Protocol : {status.get('protocol')}")
            if not status.get("holding"):
                # The grant marker is what stops a newcomer from preempting an
                # active holder, so its absence is worth naming: either the
                # winning ticket is from a client older than v0.8.4, or the
                # queue is abandoned and needs --force.
                print("  Granted  : not marked -- a pre-v0.8.4 ticket, or an "
                      "abandoned queue that --force would clear")
            print(f"  Expires  : in {status.get('expires_in')}s")
            return 0
        except Exception as ex:
            print(f"Failed to check lock status: {ex}")
            return 1

    if args[0] == "unlock":
        positional = _positional_args(args[1:])
        if not positional:
            print("Usage: git-remote-seafile unlock <seafile://url> [--force]")
            return 1
        url = positional[0]
        force = "--force" in args
        try:
            helper = RemoteHelper("unlock", url)
            lock = RemoteLock(helper.client, helper.repo_id, helper.repo_path)
            was_locked = lock.unlock(force=force)
            if not was_locked:
                print(f"Repository at {mask_url(url)} is not locked; nothing to release.")
                return 0
            if force:
                print(f"Forcibly unlocked repository at {mask_url(url)}.")
            else:
                print(f"Unlocked repository at {mask_url(url)}.")
            return 0
        except RepositoryLockedError as rle:
            sys.stderr.write(f"Error: {rle}\n")
            return 1
        except Exception as ex:
            sys.stderr.write(f"Failed to unlock repository: {ex}\n")
            return 1

    if args[0] == "desktop-url":
        if len(args) < 2:
            print("Usage: git-remote-seafile desktop-url <path>")
            return 1
        target_path = Path(args[1]).resolve()
        try:
            client = SeafileClient(require_credentials=False)
        except SeafileAuthError as auth_err:
            # Only the server URL is needed here: this command builds a URL and
            # makes no authenticated request.  Requiring a token made it fail
            # for users who have the desktop client installed but no reachable
            # credentials, which is exactly the audience for this command.
            print(f"Cannot determine the Seafile server: {auth_err}")
            return 1

        try:
            synced_libs = discover_local_synced_libraries()
            for lib in synced_libs:
                rel = _relative_within(target_path, lib["worktree"])
                if rel is None:
                    continue
                # The *repo id*, not the worktree folder name: a synced folder
                # is named by the user and need not match the library's name on
                # the server (and two libraries can share one).  get_repo_id
                # resolves an id before it tries any name, so the URL works
                # whatever the folder is called.
                server_url = (lib.get("server_url") or client.server_url or "").rstrip("/")
                if not server_url:
                    print(
                        f"Matched '{target_path}' to library '{lib['repo_id']}', but no "
                        f"server URL is known for it. Set SEAFILE_SERVER and retry."
                    )
                    return 1
                # Always state the scheme.  A bare first segment is read as a
                # *library* unless it happens to look like a host, so the
                # scheme-less spelling the command used to emit was the one form
                # the URL grammar could misread.
                scheme, sep, rest = server_url.partition("://")
                if not sep:
                    scheme, rest = "https", server_url
                print(f"seafile://{scheme.lower()}://{rest}/{quote(lib['repo_id'], safe='')}{_url_path(rel)}")
                return 0
            print(f"Could not match '{target_path}' to any local Seafile library worktree.")
            return 1
        except Exception as ex:
            print(f"Error resolving path: {ex}")
            return 1

    # Standard Git remote helper invocation: git-remote-seafile <remote-name> <url>
    # or git-remote-seafile <url> when Git is handed a URL directly.  Both forms
    # carry the seafile:// URL, so an invocation with no such URL is not a helper
    # call at all -- it is a mistyped subcommand.  Without this check a typo like
    # `git-remote-seafile chck-auth` was passed to the helper as the *remote URL*
    # and surfaced as a baffling "library not found" or auth error.
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


if __name__ == "__main__":
    sys.exit(main())
