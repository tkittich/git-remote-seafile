

# -- Embedded repositories (see 70_stage.py) ---------------------------------------


def embedded_repositories(git_dir: Path, work_tree: Path) -> list[str]:
    """Paths the vault index holds as gitlinks (``160000``): a repository, not its files."""
    listing = git(git_dir, work_tree, ["ls-files", "-s", "-z"], cwd=work_tree).stdout
    found: list[str] = []
    for entry in listing.split("\0"):
        meta, _sep, path = entry.partition("\t")
        if path and meta.startswith("160000 "):
            found.append(path)
    return found


def _files_of_repository(root: Path, prefix: str) -> list[str]:
    """Index paths for everything under *root* except ``.git`` itself.

    ``os.walk`` rather than ``git ls-files``: git treats a directory holding a
    ``.git`` as one opaque thing and will not list what is inside it, which is
    the very reason the contents were missing.  A symlink is its own entry (git
    stores the link, not its target), and a junction is not followed, as in
    :func:`_walk_sizes`.  A repository nested inside this one is walked on the
    same terms, so depth does not matter.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        keep: list[str] = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if name == ".git":
                continue
            if os.path.islink(full):
                found.append(f"{prefix}/{os.path.relpath(full, root)}".replace(os.sep, "/"))
            elif not getattr(os.lstat(full), "st_file_attributes", 0) & 0x400:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if name == ".git":  # a submodule's or linked worktree's pointer file
                continue
            full = os.path.join(dirpath, name)
            found.append(f"{prefix}/{os.path.relpath(full, root)}".replace(os.sep, "/"))
    return found


def inline_embedded_repositories(
    git_dir: Path,
    work_tree: Path,
    excludes: list[str],
    skip_paths: "frozenset[str] | None" = None,
    extra_dirs: "list[str] | None" = None,
) -> list[tuple[str, int]]:
    """Replace each gitlink in the index by the files under it; ``[(path, files)]``.

    ``git add`` records a directory that has its own ``.git`` as a pointer to that
    repository's commit and copies nothing, so a restore produced an empty
    directory in something sold as a *full* backup.  The working tree is what a
    snapshot is for, so the files go in -- ignored ones too, like everywhere else
    in the tree, and subject to ``--exclude`` and to payload mode.  The nested
    ``.git`` is not captured: the snapshot has the files as they were, not the
    repository's history, and the report says so.

    The index is written with ``update-index`` rather than ``add`` because
    ``add`` goes through the same directory scan that refuses to look inside.
    """
    links = embedded_repositories(git_dir, work_tree)
    for extra in extra_dirs or ():
        if extra not in links:
            links.append(extra)  # a repository with no commit has no gitlink
    if not links:
        return []
    skipped = {unicodedata.normalize("NFC", p) for p in (skip_paths or ())}
    drop_from_index(git_dir, work_tree, links)
    report: list[tuple[str, int]] = []
    for link in links:
        root = work_tree / link
        files = _files_of_repository(root, link) if root.is_dir() else []
        files = [
            path for path in files
            if not any(exclude_matches(path, pat) for pat in excludes)
            and unicodedata.normalize("NFC", path) not in skipped
        ]
        for start in range(0, len(files), _INDEX_BATCH):
            batch = files[start : start + _INDEX_BATCH]
            git(
                git_dir,
                work_tree,
                ["update-index", "--add", "-z", "--stdin"],
                cwd=work_tree,
                stdin="\0".join(batch) + "\0",
            )
        report.append((link, len(files)))
    return report


def embedded_note(report: list[tuple[str, int]]) -> str:
    """The notice printed when embedded repositories were captured as plain files."""
    lines = [
        "\nNOTE: these paths are embedded git repositories.  Their files were captured "
        "as ordinary\nfiles; their .git directories (history, branches, stashes) were not:"
    ]
    lines += [f"  {path} ({count} file(s))" for path, count in report]
    lines.append("  Back the history up separately, or pass --no-embedded-repos to record "
                 "only each repository's commit id.")
    return "\n".join(lines)


def _junction_filter(work_tree: Path) -> Any:
    """A predicate: is this index path behind a Windows junction (a reparse-point directory)?

    Symlinks are not junctions (git stores a symlink as itself); only a
    directory reparse point that is not a symlink is followed by git for
    Windows and by nothing else here.
    """
    cache: dict[str, bool] = {}

    def is_junction(directory: str) -> bool:
        if directory not in cache:
            try:
                st = os.lstat(work_tree / directory)
                cache[directory] = bool(
                    getattr(st, "st_file_attributes", 0) & 0x400 and not stat.S_ISLNK(st.st_mode)
                )
            except OSError:
                cache[directory] = False
        return cache[directory]

    def behind(rel: str) -> bool:
        parent = rel.rpartition("/")[0]
        while parent:
            if is_junction(parent):
                return True
            parent = parent.rpartition("/")[0]
        return False

    return behind


def embedded_roots(paths: list[str], work_tree: Path) -> list[str]:
    """The directories (never the source root) that hold their own ``.git`` and contain *paths*."""
    exists: dict[str, bool] = {}
    roots: set[str] = set()
    for path in paths:
        prefix = ""
        for part in path.split("/")[:-1]:
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix not in exists:
                exists[prefix] = os.path.lexists(work_tree / prefix / ".git")
            if exists[prefix]:
                roots.add(prefix)
    return sorted(roots)


def handle_embedded_repositories(
    git_dir: Path,
    work_tree: Path,
    excludes: list[str],
    skip_paths: "frozenset[str] | None",
    *,
    capture: bool,
    extra_dirs: "list[str] | None" = None,
    report: "list[tuple[str, int]] | None" = None,
) -> None:
    """Leave the index holding every embedded repository the way the run asked.

    With ``capture`` (the default) the files go in, gitlinks and repositories
    with no commit included.  Without it each embedded repository is recorded
    as its commit id again, and any files an earlier run captured are removed,
    so the flag means the same on the second run as on the first.  ``report``
    receives ``(directory, files)`` for every embedded repository whose files
    are in the snapshot, on every run (the note that their history is not
    captured used to be printed on the first run only).
    """
    if capture:
        inline_embedded_repositories(git_dir, work_tree, excludes, skip_paths, extra_dirs)
    paths = index_paths(git_dir, work_tree)
    roots = embedded_roots(paths, work_tree)
    if not roots:
        return
    if capture:
        if report is not None:
            for root in roots:
                report.append((root, sum(1 for p in paths if p.startswith(root + "/"))))
        return
    # Opt-out: gitlink (when there is a commit to name), not files.
    for root in roots:
        under = [p for p in paths if p.startswith(root + "/")]
        if not under:
            continue
        drop_from_index(git_dir, work_tree, under)
        head = git(
            git_dir, work_tree, ["-C", str(work_tree / root), "rev-parse", "--verify", "HEAD"],
            cwd=work_tree, check=False,
        )
        sha = head.stdout.strip() if head.returncode == 0 else ""
        if sha:
            git(
                git_dir, work_tree,
                ["update-index", "--add", "--cacheinfo", f"160000,{sha},{root}"],
                cwd=work_tree,
            )
