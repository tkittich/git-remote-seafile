"""cli.py - Entry point for git-remote-seafile."""

from __future__ import annotations

import sys
from pathlib import Path
from .helper import RemoteHelper
from .client import SeafileAuthError, SeafileClient


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
    print("  git-remote-seafile test <seafile://url>           Test connection and discover refs")
    print("  git-remote-seafile check-safety <seafile://url>  Run pre-flight safety checks (Trap 1 & 2)")
    print("  git-remote-seafile check-auth                    Verify active Seafile login")
    print("  git-remote-seafile gc <seafile://url>            Compact multiple remote packfiles")
    print("  git-remote-seafile lfs-transfer <seafile://url>  Git LFS Custom Transfer Agent")
    print("  git-remote-seafile set-head <seafile://url> <branch> Set default branch (HEAD) on remote")
    print("  git-remote-seafile lock-status <seafile://url>   Inspect repository lock state")
    print("  git-remote-seafile unlock <seafile://url> [--force] Clear repository lock")
    print("  git-remote-seafile desktop-url <path>            Generate seafile:// URL from local path")
    print("  git-remote-seafile version                       Display version")


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

    if args[0] in ("-v", "--version", "version"):
        from . import __version__
        print(f"git-remote-seafile v{__version__}")
        return 0

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
            from .safety import check_preflight_safety, SafetyError, discover_local_synced_libraries, get_local_work_tree
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
            helper.cmd_list()
            print("Connection successful!")

            from .safety import check_preflight_safety, SafetyError
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
        if len(args) < 2:
            print("Usage: git-remote-seafile gc <seafile://url> [--min-packs N]")
            return 1
        url = args[1]
        min_packs = 2
        if "--min-packs" in args:
            idx = args.index("--min-packs")
            if idx + 1 < len(args):
                try:
                    val = int(args[idx + 1])
                    if val < 1:
                        sys.stderr.write(
                            f"Warning: --min-packs must be a positive integer, got '{args[idx + 1]}'. Using default ({min_packs}).\n"
                        )
                    else:
                        min_packs = val
                except ValueError:
                    sys.stderr.write(
                        f"Warning: invalid --min-packs value '{args[idx + 1]}'. Using default ({min_packs}).\n"
                    )
            else:
                sys.stderr.write(f"Warning: --min-packs requires an integer value. Using default ({min_packs}).\n")
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
            if res.get("status") == "ok":
                print(
                    f"Compacted {res['old_packs']} packfiles into {res['new_packs']} "
                    f"({describe_size_delta(res['saved_kb'])})."
                )
                return 0
            if res.get("status") == "error":
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
                sys.stderr.write(f"Error: branch '{head_target}' does not exist on remote {url}\n")
                return 1
            # set-head is an explicit administrative maintenance command. It bypasses
            # RemoteLock to allow recovering remotes where an interrupted operation left a
            # broken HEAD or stale lock, while strictly validating that the target branch exists.
            head_content = f"ref: {head_target}\n".encode("utf-8")
            helper.client.upload_file(
                helper.repo_id,
                helper.repo_path,
                "HEAD",
                head_content,
                replace=True,
            )
            print(f"Updated remote HEAD on {url} to {head_target}")
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
            from .lock import RemoteLock
            lock = RemoteLock(helper.client, helper.repo_id, helper.repo_path)
            status = lock.get_status()
            if not status.get("locked"):
                print(f"Repository at {url} is UNLOCKED.")
                return 0
            print(f"Repository at {url} is LOCKED:")
            print(f"  Owner    : {status.get('owner')}")
            print(f"  Machine  : {status.get('machine')}")
            if status.get("pid"):
                print(f"  PID      : {status.get('pid')}")
            if status.get("nonce"):
                print(f"  Nonce    : {status.get('nonce')}")
            print(f"  Protocol : {status.get('protocol')}")
            print(f"  Expires  : in {status.get('expires_in')}s")
            return 0
        except Exception as ex:
            print(f"Failed to check lock status: {ex}")
            return 1

    if args[0] == "unlock":
        if len(args) < 2:
            print("Usage: git-remote-seafile unlock <seafile://url> [--force]")
            return 1
        url = args[1]
        force = "--force" in args
        try:
            helper = RemoteHelper("unlock", url)
            from .lock import RemoteLock, RepositoryLockedError
            lock = RemoteLock(helper.client, helper.repo_id, helper.repo_path)
            lock.unlock(force=force)
            if force:
                print(f"Forcibly unlocked repository at {url}.")
            else:
                print(f"Unlocked repository at {url}.")
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
            from .safety import discover_local_synced_libraries
            synced_libs = discover_local_synced_libraries()
            for lib in synced_libs:
                wt = lib["worktree"]
                try:
                    rel = target_path.relative_to(wt)
                except ValueError:
                    continue
                lib_name = lib["name"]
                server_url = lib.get("server_url") or client.server_url
                # Scheme comparison is case-insensitive per RFC 3986: a mixed-case
                # SEAFILE_SERVER (e.g. "HTTP://host") must classify as http, not https.
                head = server_url[:8].lower()
                if head.startswith("http://"):
                    server_host = server_url[7:].rstrip("/")
                    print(f"seafile://http://{server_host}/{lib_name}/{rel.as_posix()}")
                elif head.startswith("https://"):
                    server_host = server_url[8:].rstrip("/")
                    print(f"seafile://{server_host}/{lib_name}/{rel.as_posix()}")
                else:
                    # No scheme at all (bare host[:port]): keep the legacy bare form.
                    print(f"seafile://{server_url.rstrip('/')}/{lib_name}/{rel.as_posix()}")
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
