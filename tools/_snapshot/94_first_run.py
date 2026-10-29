
#: Directory names a first interactive run offers to leave out: the ones
#: ``--default-excludes`` would add that are directories (``*.pyc`` is a file glob).
_OFFERED_DIRECTORY_NAMES = frozenset(
    p for p in RECOMMENDED_EXCLUDES if not any(ch in p for ch in "*?[")
)

#: Measuring the candidates must not make the prompt the slow part of a first run.
_MEASURE_SECONDS = 5.0


def _directory_bytes(path: str, deadline: float) -> int | None:
    """Bytes under *path*, or None when the deadline passed before the walk finished."""
    total = 0
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return None
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def reproducible_candidates(source: Path) -> list[tuple[str, int | None]]:
    """``(path, bytes)`` of each directory a first run would offer to leave out.

    Directories named like the recommended excludes and every Python virtualenv,
    except any holding a file the source's own repository tracks (``build`` also
    names ``tools/build/make.py``, and committed source is never offered).  The
    walk does not descend into a candidate or into ``.git``.  Bytes is None when
    the measurement ran out of time.
    """
    tracked = source_tracked_paths(source)
    deadline = time.monotonic() + _MEASURE_SECONDS
    found: list[tuple[str, int | None]] = []
    root = str(source)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel != "." and ("pyvenv.cfg" in filenames or os.path.basename(dirpath) in _OFFERED_DIRECTORY_NAMES):
            if not any(path.startswith(rel + "/") for path in tracked):
                found.append((rel, _directory_bytes(dirpath, deadline)))
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d != ".git"]
    return sorted(found)


def offer_default_excludes(
    args: argparse.Namespace,
    source: Path,
    vault: Path,
    remembered: Mapping[str, Any],
    *,
    interactive: bool | None = None,
    ask: Callable[[str], str] = input,
) -> None:
    """On a person's first run, ask whether to leave out reproducible directories.

    ``--default-excludes`` is off by default on purpose -- a backup that quietly
    omits a file is the failure that cannot be forgiven -- but "captures a 2 GiB
    ``node_modules`` and says so afterwards" is a poor first impression.  Asked
    out loud, the omission is not quiet.  Only a first run (no vault yet), at a
    terminal, with no exclusion typed or remembered, and not a scheduled job
    (``--json`` / ``--exit-warnings``): a default must never change a run that
    worked.  A yes sets ``--default-excludes``, whose own rules (tracked files
    kept, virtualenvs found by ``pyvenv.cfg``, remembered by the vault) then apply.
    """
    if args.default_excludes is not None or args.exclude is not None or remembered.get("exclude"):
        return
    if (vault / ".git").exists() or args.maintain or args.status or args.json or args.exit_warnings:
        return
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        return
    candidates = reproducible_candidates(source)
    if not candidates:
        return
    print("These directories look reproducible and are usually not worth backing up:")
    for rel, size in candidates[:12]:
        print(f"  {rel}  ({human_bytes(size) if size is not None else 'size not measured'})")
    if len(candidates) > 12:
        print(f"  ... and {len(candidates) - 12} more")
    try:
        answer = ask("Leave them out of this and later snapshots? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = "n"
    if answer in ("", "y", "yes"):
        args.default_excludes = True
        print("Leaving them out; the vault remembers this. Name --exclude yourself to change the list.")
    else:
        print("Capturing everything. Pass --default-excludes on a later run to leave them out.")


_SECRETS_KEY = "snapshot.secretsseen"


def warn_about_secrets(git_dir: Path, work_tree: Path, secrets: list[str], *, remember: bool) -> None:
    """Warn that captured paths look like credentials -- when that set is news.

    The warning is a decision point: the first push is the last moment to pick
    a library only you can read.  Said on every run it stopped being news, and
    it made ``--exit-warnings`` exit 3 for ever on any tree with a ``.env``, so
    a scheduled job could no longer tell "something is wrong" from "as before".
    The vault remembers a digest of the paths it last warned about and warns
    again when the set changes; a dry run never writes the digest.
    """
    digest = hashlib.sha256("\n".join(sorted(secrets)).encode("utf-8", "surrogateescape")).hexdigest()
    if digest == _read_config_key(git_dir, _SECRETS_KEY):
        return
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
    if remember:
        git(git_dir, work_tree, ["config", _SECRETS_KEY, digest], cwd=work_tree, check=False)
