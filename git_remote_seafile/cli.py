"""cli.py - Entry point for git-remote-seafile."""

from __future__ import annotations

import sys
from pathlib import Path
from .helper import RemoteHelper
from .client import SeafileClient


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
    print("  git-remote-seafile check-auth                    Verify active Seafile login")
    print("  git-remote-seafile gc <seafile://url>            Compact multiple remote packfiles")
    print("  git-remote-seafile lfs-transfer <seafile://url>  Git LFS Custom Transfer Agent")
    print("  git-remote-seafile set-head <seafile://url> <branch> Set default branch (HEAD) on remote")
    print("  git-remote-seafile desktop-url <path>            Generate seafile:// URL from local path")
    print("  git-remote-seafile version                       Display version")


def main() -> int:
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
            resp = client.session.get(info_url)
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
            return 0
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
                    min_packs = int(args[idx + 1])
                except ValueError:
                    pass
        try:
            helper = RemoteHelper("gc", url)
            from .gc import compact_repository
            res = compact_repository(helper.client, helper.repo_id, helper.repo_path, min_packs=min_packs)
            if res.get("status") == "ok":
                print(f"Compacted {res['old_packs']} packfiles into {res['new_packs']} (saved {res['saved_kb']} KB).")
            else:
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
            agent = LFSTransferAgent(helper.client, helper.repo_id, helper.repo_path)
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

    if args[0] == "desktop-url":
        if len(args) < 2:
            print("Usage: git-remote-seafile desktop-url <path>")
            return 1
        target_path = Path(args[1]).resolve()
        try:
            client = SeafileClient()
            # Try to resolve against accounts.db libraries
            import sqlite3
            candidates = []
            for ini_path in [Path.home() / "ccnet" / "seafile.ini", Path.home() / ".ccnet" / "seafile.ini"]:
                if ini_path.is_file():
                    try:
                        data_dir = Path(ini_path.read_text(encoding="utf-8").strip())
                        candidates.append(data_dir / "repo.db")
                    except Exception:
                        pass
            candidates.extend([
                Path.home() / "ccnet" / "repo.db",
                Path.home() / ".ccnet" / "repo.db",
                Path.home() / "Seafile" / "seafile-data" / "repo.db",
                Path.home() / ".seafile-data" / "repo.db",
                Path.home() / "Seafile" / ".seafile-data" / "repo.db",
                Path.home() / "Library" / "Application Support" / "Seafile" / "repo.db",
            ])
            for rdb in candidates:
                if rdb.is_file():
                    con = sqlite3.connect(f"file:{rdb.as_posix()}?mode=ro", uri=True)
                    props = {}
                    for rid, k, v in con.execute("SELECT repo_id, key, value FROM RepoProperty"):
                        props.setdefault(rid, {})[k] = v
                    con.close()
                    for rid, kv in props.items():
                        wt = kv.get("worktree")
                        if wt and target_path.is_relative_to(Path(wt).resolve()):
                            rel = target_path.relative_to(Path(wt).resolve())
                            lib_name = Path(wt).name
                            server_host = client.server_url.replace("https://", "").replace("http://", "")
                            print(f"seafile://{server_host}/{lib_name}/{rel.as_posix()}")
                            return 0
            print(f"Could not match '{target_path}' to any local Seafile library worktree.")
            return 1
        except Exception as ex:
            print(f"Error resolving path: {ex}")
            return 1

    # Standard Git remote helper invocation: git-remote-seafile <remote-name> <url>
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
