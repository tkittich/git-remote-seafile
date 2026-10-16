

def default_vault_for(source: Path) -> Path:
    """Where the vault lives when ``--vault`` is not given.

    Beside the source, as a sibling: ``/code/myproject`` -> ``/code/myproject-vault``.
    Predictable, easy to find, and inspectable with plain ``git`` -- a vault the
    user cannot locate is a vault they will not trust.
    """
    return source.with_name(source.name + "-vault")


def matched_synced_libraries(path: Path) -> list[dict]:
    """The desktop client's synced libraries whose worktree contains ``path``.

    One dict per containing library, carrying the facts a safety decision needs:
    its name and worktree, whether auto sync is enabled for it (a disable is
    persisted in repo.db as RepoProperty 'auto-sync' = 'false', so this survives
    client restarts -- unlike the global pause, which lives only in the daemon's
    memory), and which seafile-ignore.txt rule covers ``path`` relative to that
    worktree, if any.

    An empty list means "not under a synced folder" -- including when the check
    itself could not run.  Read failures (a corrupt repo.db, an import problem)
    mean "cannot tell", and callers treat empty as safe, so the failure is
    announced on stderr first: silently answering "safe" for a state nobody
    verified is the one thing a fail-closed tool must not do -- the same
    reasoning as safety.check_preflight_safety's transient-failure warning.
    """
    target = path.resolve()
    try:
        import git_remote_seafile.safety as _safety  # sys.path set above
        libraries = _safety.discover_local_synced_libraries()
    except Exception as ex:
        print(
            f"Warning: could not check for Seafile-synced folders ({ex}); "
            "treating the destination as not synced, so the guard could not run. "
            "Check the path is not inside a synced library.",
            file=sys.stderr,
        )
        sys.stderr.flush()
        return []
    matched: list[dict] = []
    for lib in libraries:
        worktree = lib.get("worktree")
        if worktree is None:
            continue
        # Case-insensitive on Windows, identity on POSIX: a library synced to
        # C:\Users\me\Seafile and a vault path spelled c:\users\me\seafile\vault
        # are one directory, and a literal comparison would let the vault
        # escape the guard -- which for the vault is the only guard there is.
        rel = _safety.safe_relative_to(target, Path(worktree))
        if rel is None:
            continue
        # read_seafile_ignore_rules announces an unreadable file on stderr and
        # returns [] for it, so a missing *or* broken ignore file yields no rule:
        # when in doubt the path counts as one the client would touch, and the
        # refusal stands.
        rules = _safety.read_seafile_ignore_rules(Path(worktree))
        matched.append({
            "name": lib.get("name", worktree),
            "worktree": Path(worktree),
            "auto_sync": lib.get("auto_sync", True),
            "ignore_rule": _safety.covering_ignore_rule(rel.as_posix(), rules),
        })
    return matched


def check_paths_are_unsynced(*paths: tuple[str, Path], allow_synced: bool = False) -> None:
    """Refuse to snapshot a source, or write a vault, inside a synced library.

    This is the tool's own version of the helper's Trap 1/Trap 2 guard, and the
    reason it exists is the same: the Seafile client rewrites files underneath
    whatever lives in a synced folder, and it fights Git for control of every
    path Git touches.  The helper cannot see the vault (it is told about the
    *source* via ``GIT_WORK_TREE`` and nothing else), so for the vault this check
    is the only line of defence.

    A containing library is harmless -- and let through with a note on stderr
    saying exactly why -- when its auto sync is disabled (the daemon then
    un-watches that worktree) or the path is covered by that library's
    seafile-ignore.txt (the client never touches it).  Re-enabling auto sync or
    removing the rule makes this guard refuse again.  ``allow_synced`` skips the
    refusal outright; it is the escape hatch for everything else and puts the
    risk on the person passing it, like --allow-shared-remote does.
    """
    for label, path in paths:
        matched = matched_synced_libraries(path)
        if not matched:
            continue

        live = [m for m in matched
                if m["auto_sync"] and m["ignore_rule"] is None]
        names = ", ".join(str(m["name"]) for m in matched)

        if allow_synced:
            print(
                f"Warning: --allow-synced overrides the synced-folder guard for "
                f"{label} ({path}); library {names} can still rewrite files under it",
                file=sys.stderr,
            )
            sys.stderr.flush()
            continue

        if not live:
            for m in matched:
                reason = (f"covered by seafile-ignore.txt rule {m['ignore_rule']!r}"
                          if m["auto_sync"] else "its auto sync is disabled")
                print(
                    f"Note: {label} ({path}) is inside synced library "
                    f"{m['name']} ({m['worktree']}), but the client will not touch it: "
                    f"{reason}. Re-enabling auto sync, or removing that ignore rule, "
                    f"makes this guard refuse again.",
                    file=sys.stderr,
                )
            sys.stderr.flush()
            continue

        raise SnapshotError(
            f"{label} is inside a Seafile-synced library ({names}):\n  {path}\n"
            f"The sync client rewrites files underneath it and competes with "
            f"Git for every path, which is the exact problem this tool "
            f"exists to avoid.\n"
            f"Move it outside the synced folder -- e.g. under C:/code/ -- or "
            f"sync that library with 'auto sync' disabled; pass --allow-synced "
            f"to take responsibility for the risk instead."
        )
