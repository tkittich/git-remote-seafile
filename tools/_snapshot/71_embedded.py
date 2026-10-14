

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
