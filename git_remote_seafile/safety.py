"""safety.py - Pre-flight safety checks preventing repository corruption and sync loops."""

from __future__ import annotations

import difflib
import fnmatch
import os
from pathlib import Path

from .git_util import get_git_config_bool, run_git
from .seafile_paths import get_candidate_db_paths
from .sqlite_read import open_live_sqlite_ro


class SafetyError(Exception):
    """Raised when an operation would lead to repository corruption or desktop sync chaos."""
    pass


def is_safety_checks_enabled() -> bool:
    """Check if safety checks are active or bypassed."""
    # 1. Environment variable override
    env_val = os.environ.get("SEAFILE_SKIP_SAFETY_CHECKS", "").strip().lower()
    if env_val in ("1", "true", "yes", "on"):
        return False
    # 2. Git config override
    if get_git_config_bool("seafile.skipsafetychecks", False):
        return False
    return True


def get_local_work_tree() -> Path | None:
    """Return the absolute path of the current Git working tree, if any."""
    out, err, code = run_git(["rev-parse", "--show-toplevel"])
    if code == 0:
        raw = out.decode("utf-8", errors="replace").strip()
        if raw:
            p = Path(raw)
            return p.resolve()
    return None


def _safe_relative_to(target: Path, base: Path) -> Path | None:
    """Safely compute relative path, handling Windows case differences."""
    try:
        return target.resolve().relative_to(base.resolve())
    except ValueError:
        try:
            target_str = target.resolve().as_posix().lower()
            base_str = base.resolve().as_posix().lower().rstrip("/")
            if target_str == base_str:
                return Path(".")
            if target_str.startswith(base_str + "/"):
                rel_str = target_str[len(base_str) + 1 :]
                return Path(rel_str)
        except Exception:
            pass
        return None


def discover_local_synced_libraries() -> list[dict]:
    """Find all libraries actively synced by the local Seafile desktop client.
    
    Reads repo.db from local Seafile client configuration.
    Returns list of dicts: [{'repo_id': str, 'worktree': Path, 'name': str, 'server_url': str}]
    """
    candidates = get_candidate_db_paths("repo.db")

    results = []
    seen_ids = set()

    for rdb in candidates:
        props: dict[str, dict[str, str]] = {}
        with open_live_sqlite_ro(rdb) as con:
            if con is None:
                continue
            try:
                for rid, k, v in con.execute("SELECT repo_id, key, value FROM RepoProperty"):
                    props.setdefault(rid, {})[k] = v
            except Exception:
                continue

        for rid, kv in props.items():
            if rid in seen_ids:
                continue
            wt = kv.get("worktree")
            if wt:
                wt_path = Path(wt).resolve()
                if wt_path.is_dir():
                    seen_ids.add(rid)
                    results.append({
                        "repo_id": rid,
                        "worktree": wt_path,
                        "name": wt_path.name,
                        "server_url": kv.get("server-url", ""),
                        "username": kv.get("username", ""),
                    })

    return results


def read_seafile_ignore_rules(worktree: Path) -> list[str]:
    """Read patterns from <worktree>/seafile-ignore.txt."""
    ignore_file = worktree / "seafile-ignore.txt"
    if not ignore_file.is_file():
        return []
    try:
        lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
        rules = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                rules.append(stripped)
        return rules
    except Exception:
        return []


def is_path_ignored(rel_path: str, rules: list[str]) -> bool:
    """Check if rel_path (e.g. 'code/myproject' or 'seafile-git/myproject') matches ignore rules.
    
    Seafile ignore rules are anchored at the library root.
    A rule 'code/' matches 'code', 'code/', and 'code/anything'.
    A rule 'code' matches 'code', 'code/', and 'code/anything'.
    """
    clean_path = rel_path.strip("/").lower()
    path_parts = [p for p in clean_path.split("/") if p]

    for r in rules:
        clean_rule = r.strip("/").lower()
        if not clean_rule:
            continue

        # 1. Glob / wildcard match
        if fnmatch.fnmatch(clean_path, clean_rule) or fnmatch.fnmatch(clean_path, f"{clean_rule}/*"):
            return True

        # 2. Directory prefix match
        rule_parts = [p for p in clean_rule.split("/") if p]
        if path_parts[:len(rule_parts)] == rule_parts:
            return True

    return False


def check_preflight_safety(
    client,
    library_name: str,
    repo_path: str,
    local_worktree: Path | None = None,
    push_mode: bool = True,
    synced_libs: list[dict] | None = None,
) -> list[str]:
    """Perform pre-flight sanity and safety checks.
    
    Returns a list of warning strings (if any).
    Raises SafetyError on dangerous conditions.
    """
    if not is_safety_checks_enabled():
        return []

    if not library_name:
        return []

    warnings: list[str] = []

    # 1. Check Library Root Pollution
    clean_repo_path = repo_path.strip("/")
    if not clean_repo_path:
        raise SafetyError(
            f"Cannot use the library root '/' as a Git remote destination.\n"
            f"You must specify a repository subfolder name (e.g. 'seafile://{library_name}/myproject' "
            f"or 'seafile://{library_name}/seafile-git/myproject')."
        )

    # 2. Check Library Existence / Typo
    target_repo_id = None
    if client:
        try:
            target_repo_id = client.get_repo_id(library_name)
        except Exception:
            # Try to list accessible libraries to give a helpful suggestion
            try:
                resp = client.session.get(
                    f"{client.server_url}/api2/repos/",
                    timeout=getattr(client, "timeout", 30),
                )
                if resp.status_code == 200:
                    available_libs = [r.get("name") for r in resp.json() if r.get("name")]
                    suggestions = difflib.get_close_matches(library_name, available_libs, n=3, cutoff=0.5)
                    sugg_str = ""
                    if suggestions:
                        sugg_str = f"\nDid you mean: {', '.join(suggestions)}?"
                    avail_str = f"\nAvailable libraries: {', '.join(sorted(available_libs))}" if available_libs else ""
                    raise SafetyError(
                        f"Library '{library_name}' not found on {client.server_url}.{sugg_str}{avail_str}"
                    )
            except SafetyError:
                raise
            except Exception:
                pass

    # 3. Discover local synced libraries
    if synced_libs is None:
        synced_libs = discover_local_synced_libraries()

    if not synced_libs:
        return warnings  # Client not running or no synced libraries

    if local_worktree is None:
        local_worktree = get_local_work_tree()
    if local_worktree is None:
        # `git clone` runs the helper in the directory the user is standing in,
        # which is the *parent* of the directory being created -- and that
        # parent is not a repository yet, so git cannot name a work tree.
        # Checking it is the whole point: `git clone seafile://Documents/code/x`
        # run from inside the synced Documents/ library is exactly the Trap 1
        # collision the README warns about, and until now clone was unguarded.
        # The process's own cwd is the best available stand-in.
        try:
            local_worktree = Path.cwd()
        except OSError:
            local_worktree = None

    # Find if the target library is actively synced locally.
    # Prefer matching by unique repo_id since the local worktree folder name may
    # differ from the server library name.
    target_synced_lib = None
    if target_repo_id:
        for lib in synced_libs:
            if lib.get("repo_id") == target_repo_id:
                target_synced_lib = lib
                break
    else:
        # Only fall back to name matching when target_repo_id is unknown
        for lib in synced_libs:
            if lib.get("name", "").lower() == library_name.lower():
                target_synced_lib = lib
                break

    # 4. Check Trap 1: Working Tree & Remote Path Collision
    if local_worktree and target_synced_lib:
        worktree_path = target_synced_lib["worktree"].resolve()
        rel_local = _safe_relative_to(local_worktree, worktree_path)
        if rel_local is not None:
            rel_posix = rel_local.as_posix().lower()
            clean_repo = clean_repo_path.lower()
            is_collision = (
                rel_posix in ("", ".")
                or clean_repo == rel_posix
                or clean_repo.startswith(rel_posix + "/")
                or rel_posix.startswith(clean_repo + "/")
            )
            if is_collision:
                raise SafetyError(
                    f"DANGEROUS PATH COLLISION DETECTED (Trap 1)!\n"
                    f"  Local working tree: {local_worktree}\n"
                    f"  Remote destination: seafile://{library_name}/{clean_repo_path}\n\n"
                    f"Your local Git working tree is inside the synced '{library_name}' library, "
                    f"and your remote URL points to the EXACT SAME path.\n"
                    f"Pushing bare Git packfiles and refs here will overwrite and corrupt your active files!\n\n"
                    f"Recommended Solutions:\n"
                    f"  1. (Recommended) Push to a dedicated, unsynced library (e.g. 'code'):\n"
                    f"     git remote set-url origin seafile://code/{Path(clean_repo_path).name}\n"
                    f"  2. Push to an ignored subfolder within '{library_name}':\n"
                    f"     git remote set-url origin seafile://{library_name}/seafile-git/{Path(clean_repo_path).name}\n"
                    f"     (and add 'seafile-git/' to {worktree_path / 'seafile-ignore.txt'})"
                )

    # 5. Check Trap 2: Download Reflection Loop in Synced Library
    if push_mode and target_synced_lib:
        worktree_path = target_synced_lib["worktree"].resolve()
        ignore_rules = read_seafile_ignore_rules(worktree_path)
        top_folder = clean_repo_path.split("/")[0]

        if not is_path_ignored(clean_repo_path, ignore_rules):
            raise SafetyError(
                f"UNIGNORED REMOTE PATH IN SYNCED LIBRARY (Trap 2 - Download Reflection)!\n"
                f"  Remote destination: seafile://{library_name}/{clean_repo_path}\n"
                f"  Local synced folder: {worktree_path}\n\n"
                f"The target library '{library_name}' is actively synced by Seafile desktop client, "
                f"but '{top_folder}/' is NOT listed in:\n"
                f"  {worktree_path / 'seafile-ignore.txt'}\n\n"
                f"If you push here, the desktop client will detect the uploaded packfiles on the server "
                f"and immediately download them back down to your local drive.\n\n"
                f"Recommended Solutions:\n"
                f"  1. (Recommended) Push to a dedicated, unsynced library (e.g. 'code'):\n"
                f"     git remote set-url origin seafile://code/{clean_repo_path}\n"
                f"  2. Add '{top_folder}/' to {worktree_path / 'seafile-ignore.txt'} at the library root before pushing."
            )

    # 6. Check Active Working Tree in Synced Library Warning
    if local_worktree:
        for lib in synced_libs:
            wt_path = lib["worktree"].resolve()
            rel_local = _safe_relative_to(local_worktree, wt_path)
            if rel_local is not None:
                ignore_rules = read_seafile_ignore_rules(wt_path)
                rel_posix = rel_local.as_posix()
                if not is_path_ignored(rel_posix, ignore_rules):
                    top_part = rel_posix.split("/")[0]
                    warnings.append(
                        f"Notice: Your active Git working tree ({local_worktree}) is inside synced library '{lib['name']}'\n"
                        f"and is not listed in {wt_path / 'seafile-ignore.txt'}.\n"
                        f"The Seafile desktop sync daemon may churn on .git/ files and lock your repository.\n"
                        f"Consider moving your code outside synced folders or adding '{top_part}/' to seafile-ignore.txt."
                    )
                break

    return warnings
