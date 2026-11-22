

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


def _files_of_repository(
    root: Path, prefix: str, unreadable: "list[str] | None" = None, excludes: "list[str] | None" = None
) -> list[str]:
    """Index paths for everything under *root* except ``.git`` itself.

    ``os.walk`` rather than ``git ls-files``: git treats a directory holding a
    ``.git`` as one opaque thing and will not list what is inside it, which is
    the very reason the contents were missing.  A symlink is its own entry (git
    stores the link, not its target), and a junction is not followed, as in
    :func:`_walk_sizes`.  A repository nested inside this one is walked on the
    same terms, so depth does not matter.

    ``unreadable`` collects directories the walk cannot open.  git cannot warn
    about a directory it refuses to enter, so its stderr guard (70_stage)
    covers the rest of the tree but is structurally blind here; without this,
    an unreadable directory inside an embedded repository left a hole in a
    "full" backup with exit 0 (REVIEW.iq3_s E1, reproduced on Linux).
    """
    found: list[str] = []

    def _deny(err: OSError) -> None:
        if unreadable is not None:
            # The directory that could not be opened, so --exclude on it (as the error says) can match it.
            named = os.fspath(err.filename) if getattr(err, "filename", None) else ""
            try:
                rel = os.path.relpath(named, root).replace(os.sep, "/") if named else ""
            except ValueError:
                rel = ""
            unreadable.append(
                f"{prefix}/{rel}".rstrip("/") if rel and not rel.startswith("..") else prefix.rstrip("/")
            )

    for dirpath, dirnames, filenames in walk_long(root, onerror=_deny):
        keep: list[str] = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if name == ".git":
                continue
            if excludes and any(
                exclude_matches(f"{prefix}/{os.path.relpath(full, root)}".replace(os.sep, "/"), pat)
                for pat in excludes
            ):
                continue  # left out on purpose: do not open it, and do not fail on it (gemini-S1-2)
            try:
                is_link = os.path.islink(extended_path(Path(full)))
                is_reparse = bool(
                    getattr(os.lstat(extended_path(Path(full))), "st_file_attributes", 0) & 0x400
                )
            except OSError:
                # Not "vanished": a path the stat cannot reach (past 260 characters without
                # long-path support) read so dropped the folder and everything under it with
                # exit 0 (fable-G0.2).  A real vanish is also reported; running again settles it.
                if unreadable is not None:
                    unreadable.append(f"{prefix}/{os.path.relpath(full, root)}".replace(os.sep, "/"))
                continue
            if is_link:
                found.append(f"{prefix}/{os.path.relpath(full, root)}".replace(os.sep, "/"))
            elif not is_reparse:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if name == ".git":  # a submodule's or linked worktree's pointer file
                continue
            full = os.path.join(dirpath, name)
            if _is_special_file(full):  # git skips a FIFO or socket; update-index refuses one (fable-S1-7)
                continue
            found.append(f"{prefix}/{os.path.relpath(full, root)}".replace(os.sep, "/"))
    return found


def _is_special_file(path: str) -> bool:
    """A FIFO, socket or device node: not something git records, so not something to hand ``update-index``."""
    try:
        mode = os.lstat(extended_path(Path(path))).st_mode
    except OSError:
        return False  # unreadable here is reported where it is read; do not hide it as "special"
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


def _one_directory(work_tree: Path, a: str, b: str) -> bool:
    """Whether two spellings name the same directory (a folding disk: ``G.BIN`` and ``g.bin``).

    A file replaced by a repository is listed under the index's old spelling and found under the disk's
    new one; capturing both put every file in twice, as ``G.BIN/x`` and ``g.bin/x``, and the next run's
    ``git add`` refused the alias.  Same folded key and ``samestat``, so two real directories on a case-sensitive
    disk are never merged.
    """
    if _skip_key(a) != _skip_key(b):
        return False
    try:
        return os.path.samestat(os.lstat(extended_path(work_tree / a)), os.lstat(extended_path(work_tree / b)))
    except OSError:
        return False


def inline_embedded_repositories(
    git_dir: Path,
    work_tree: Path,
    excludes: list[str],
    skip_paths: "frozenset[str] | None" = None,
    extra_dirs: "list[str] | None" = None,
    unreadable: "list[str] | None" = None,
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
        if extra not in links and not any(_one_directory(work_tree, extra, link) for link in links):
            links.append(extra)  # a repository with no commit has no gitlink
    if not links:
        return []
    skipped = payload_skip_matcher(work_tree, skip_paths)
    drop_from_index(git_dir, work_tree, links)
    report: list[tuple[str, int]] = []
    for link in links:
        root = work_tree / link
        # extended_path: a plain is_dir past 260 characters answers False on a Windows without
        # LongPathsEnabled and the repository vanished from the snapshot, exit 0 (fable-G0.1).
        files = _files_of_repository(root, link, unreadable, excludes) if extended_path(root).is_dir() else []
        files = [
            path for path in files
            if not any(exclude_matches(path, pat) for pat in excludes)
            and not skipped(path)
        ]
        for start in range(0, len(files), _INDEX_BATCH):
            batch = files[start : start + _INDEX_BATCH]
            git(
                git_dir,
                work_tree,
                ["update-index", "--add", "-z", "--stdin"],
                cwd=work_tree,
                stdin="\0".join(batch) + "\0",
                # It hashes and writes the objects, so growth is measurable: the watchdog, not the plain
                # wall clock, bounds a batch of large files (review c305b0a fable-S1-6).
                status="staging an embedded repository",
                measure_dir=git_dir / "objects",
            )
        report.append((link, len(files)))
    return report


def embedded_note(report: list[tuple[str, int]]) -> str:
    """The notice printed when embedded repositories were captured as plain files."""
    lines = [
        "\nNote: these paths are embedded git repositories.  Their files were captured "
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
                st = os.lstat(extended_path(work_tree / directory))
                cache[directory] = bool(
                    getattr(st, "st_file_attributes", 0) & 0x400 and not stat.S_ISLNK(st.st_mode)
                )
            except OSError:
                cache[directory] = False
        return cache[directory]

    def behind(rel: str, self_too: bool = False) -> bool:
        # ``self_too`` is for a directory that is itself the thing being
        # considered (a nested repository): a junction *to* a repository is
        # followed just as one above it is.
        parent = rel if self_too else rel.rpartition("/")[0]
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
                exists[prefix] = os.path.lexists(extended_path(work_tree / prefix / ".git"))
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
    unreadable: "list[str] | None" = None,
) -> None:
    """Leave the index holding every embedded repository the way the run asked.

    With ``capture`` (the default) the files go in, gitlinks and repositories
    with no commit included.  Without it each embedded repository is recorded
    as its commit id again, and any files an earlier run captured are removed,
    so the flag means the same on the second run as on the first.  ``report``
    receives ``(directory, files)`` for every embedded repository whose files
    are in the snapshot, on every run (the note that their history is not
    captured used to be printed on the first run only).  ``unreadable`` is the
    list build_snapshot's refusal reads: a directory inside an embedded
    repository the walk cannot open lands there, because git's own warning
    cannot see inside one.
    """
    if capture:
        inline_embedded_repositories(
            git_dir, work_tree, excludes, skip_paths, extra_dirs, unreadable
        )
    paths = index_paths(git_dir, work_tree)
    # Outermost repositories only: a repository inside another is part of that
    # one's files, and its own gitlink would make "a file and a directory".
    outer: list[str] = []
    for root in embedded_roots(paths, work_tree):
        if not any(root.startswith(kept + "/") for kept in outer):
            outer.append(root)
    if not outer:
        return
    under: dict[str, list[str]] = {root: [] for root in outer}
    outer_set = set(outer)
    for path in paths:  # one pass: the nearest enclosing root claims the path
        ancestor = path.rpartition("/")[0]
        while ancestor:
            if ancestor in outer_set:
                under[ancestor].append(path)
                break
            ancestor = ancestor.rpartition("/")[0]
    if capture:
        if report is not None:
            for root in outer:
                report.append((root, len(under[root])))
        return
    # Opt-out: the commit id, not the files -- of the *nested* repository.  The
    # vault's own git directory must not be consulted for it: ``git()`` always
    # passes --git-dir, so a ``-C`` through it names the vault's HEAD.
    env = dict(os.environ)
    for var in _REPO_ENV_VARS:
        env.pop(var, None)
    for root in outer:
        if not under[root]:
            continue
        # The directory is asked, not its ancestors: a ``.git`` that is not a repository (an
        # empty folder left by an interrupted clone) made ``rev-parse`` walk up and answer with
        # the *source's* HEAD, which was then recorded as the directory's commit (fable-S1-2).
        env["GIT_CEILING_DIRECTORIES"] = str((work_tree / root).parent)
        head = _run(
            ["git", "-C", str(work_tree / root), "rev-parse", "--verify", "-q", "HEAD"],
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        )
        sha = head.stdout.strip() if head.returncode == 0 else ""
        if not sha:
            raise SnapshotError(
                f"{root} is a git repository with no commit, which cannot be recorded as a "
                "commit id (--no-embedded-repos). Leave that flag out to capture its files, "
                "or --exclude the directory."
            )
        drop_from_index(git_dir, work_tree, under[root])
        git(
            git_dir, work_tree,
            ["update-index", "--add", "--cacheinfo", f"160000,{sha},{root}"],
            cwd=work_tree,
        )


def _skip_key(path: str) -> str:
    """The spelling two names of one file share: NFC, case folded (every letter, not only ASCII).

    NTFS and APFS fold more than ASCII, so ``Über.dat`` renamed to ``über.dat`` is one file there
    (review 134c8af, fable-G1.1).  A key that is too wide only asks ``samestat`` more often: it
    alone decides whether two names are one file.
    """
    return unicodedata.normalize("NFC", path).casefold()


def embedded_tracked_paths(source: Path) -> list[str]:
    """Paths tracked by the repositories embedded directly in the source's own repository.

    The outer repository lists a nested one as a single untracked directory, so a recommended exclude such
    as ``node_modules`` was held against its own tracked list only and dropped what the nested repository
    had committed (the sibling of review c305b0a fable-S4-1).  Only a nested repository that git reports as
    a directory of its own is read, one level deep.
    """
    proc = _run(
        ["git", "-C", str(source), "ls-files", "-z", "-o", "--exclude-standard", "--directory"],
        text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return []
    found: list[str] = []
    for entry in proc.stdout.split("\0"):
        rel = entry.rstrip("/")
        if not rel or not entry.endswith("/") or not extended_path(source / rel / ".git").exists():
            continue
        inner = _run(
            ["git", "-C", str(source / rel), "ls-files", "-z"],
            text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if inner.returncode == 0:
            found.extend(f"{rel}/{path}" for path in inner.stdout.split("\0") if path)
    return found


def embedded_tracked_paths_walk(source: Path) -> list[str]:
    """Paths tracked by the repositories found under a source that is not itself a repository.

    The same question as ``embedded_tracked_paths``, asked of a plain folder: there is no outer repository
    to list the nested ones, so the folder is walked (not into ``.git``, ``node_modules`` or a repository
    already found: that one lists its own).  Without it the fixed excludes were held against nothing and
    dropped what a nested repository had committed (AA73, the non-repository variant of fable-S4-1).
    """
    root = str(source)
    found: list[str] = []
    for dirpath, dirnames, _files in walk_long(root):
        if dirpath != root and extended_path(Path(dirpath) / ".git").exists():
            dirnames[:] = []
            rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            inner = _run(
                ["git", "-C", dirpath, "ls-files", "-z"],
                text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            if inner.returncode == 0:
                found.extend(f"{rel}/{path}" for path in inner.stdout.split("\0") if path)
            continue
        dirnames[:] = [name for name in dirnames if name not in (".git", "node_modules")]
    return found


def old_spellings(work_tree: Path, paths: "list[str]") -> list[str]:
    """Of *paths*, the ones that are the old spelling of a file the disk lists under another.

    NTFS and APFS fold every letter, git folds A-Z only (and APFS folds more than NTFS: ``straße.txt`` answers
    ``lstat`` as ``STRASSE.TXT``), so a file renamed by case leaves its old index entry answering ``lstat``.  Names of
    one folded spelling are twins; the live one is the spelling its folder lists (NFC, as macOS lists NFD); the others
    go when ``samestat`` says they are the same file.  Not "staged now": a rename makes both spellings look new to
    git, so both were staged and neither was old (fuzz seed 2094 on macOS).  Only groups of two or more are looked
    at, so a tree without twins costs nothing.
    """
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(_skip_key(path), []).append(path)
    twins = [members for members in groups.values() if len(members) > 1]
    if not twins:
        return []
    listings: dict[str, set[str]] = {}

    def spelled_as_on_disk(path: str) -> bool:
        folder = ""
        for part in path.split("/"):
            if folder not in listings:
                try:
                    listings[folder] = {unicodedata.normalize("NFC", n) for n in os.listdir(extended_path(work_tree / folder))}
                except OSError:
                    return True    # cannot tell: keep
            if unicodedata.normalize("NFC", part) not in listings[folder]:
                return False
            folder = f"{folder}/{part}" if folder else part
        return True

    old: list[str] = []
    for members in twins:
        live = [m for m in members if spelled_as_on_disk(m)]
        for path in (m for m in members if m not in live) if live else ():
            try:
                here = os.lstat(extended_path(work_tree / path))
                if any(os.path.samestat(here, os.lstat(extended_path(work_tree / new))) for new in live):
                    old.append(path)
            except OSError:
                continue
    return old


def case_twins(work_tree: Path, kept: list[str]) -> list[str]:
    """Index entries that are the old spelling of a file kept under a new one (the filtered route).

    A file renamed by the case of a non-ASCII letter (``Über.txt`` -> ``über.txt``) is one file to NTFS and
    APFS and two to git: the old entry still answers ``lstat``, so it stayed in every snapshot beside the new
    name (review c305b0a fable-S1-10).  ``old_spellings`` says which is old.
    """
    return old_spellings(work_tree, kept)


def drop_twin_spellings(git_dir: Path, work_tree: Path) -> None:
    """After a plain ``git add -A`` on a volume that folds case: drop an index entry that is the old spelling of a file.

    ``case_twins`` does this for the filtered route; the unfiltered route (every run on macOS and Linux with no
    exclude) never read the index, so ``Über.txt`` renamed to ``über.txt`` stayed in every snapshot beside the new
    name on APFS (review c305b0a fable-S1-10, reproduced on macOS CI).  Only index entries sharing a folded name are
    looked at, so a tree without twins costs one ``ls-files``.
    """
    if git(git_dir, work_tree, ["config", "--bool", "core.ignorecase"], cwd=work_tree,
           check=False).stdout.strip() != "true":
        return
    entries = [p for p in git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout.split("\0") if p]
    old = old_spellings(work_tree, entries)
    if old:
        drop_from_index(git_dir, work_tree, old)


def payload_skip_matcher(work_tree: Path, skip_paths: Any) -> Any:
    """``skipped(path)``: is *path* one of the payload files kept out of the index?

    macOS hands the walk decomposed (NFD) names while git reports them precomposed,
    so the two are compared in one form -- but only when they are one file.  On NTFS
    and ext4 ``cafe\u0301.bin`` and ``caf\u00e9.bin`` are two files, and comparing
    their NFC forms alone dropped the small one from git while only the large one
    was in the manifest: lost, exit 0 (review 06cc623, gemini E1).

    The same for case: under ``core.ignorecase`` git reports a tracked file by its *index*
    spelling and the payload walk by the disk's, so ``model.dat`` renamed to ``Model.dat``
    and grown past the size went into git and the manifest both (review 9fe723f, fable-G1.1).
    The key folds ASCII case as git does; ``samestat`` still decides whether they are one file,
    so two files that differ only by case on a case-sensitive disk stay two.
    """
    exact = set(skip_paths or ())
    by_form: dict[str, list[str]] = {}
    for path in exact:
        by_form.setdefault(_skip_key(path), []).append(path)

    def skipped(path: str) -> bool:
        if path in exact:
            return True
        others = by_form.get(_skip_key(path))
        if not others:
            return False
        try:
            # The long-path form: a plain lstat past 260 characters fails on a Windows
            # without LongPathsEnabled and the file stayed in git too (AA17, Z13's sibling).
            here = os.lstat(extended_path(work_tree / path))
            return any(os.path.samestat(here, os.lstat(extended_path(work_tree / other)))
                       for other in others)
        except OSError:
            return False   # kept: at worst a file in git and the manifest, never in neither

    return skipped
