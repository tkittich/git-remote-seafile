

#: How many paths to hand to a single ``update-index`` invocation.  A tree can
#: hold far more matches than one command line can carry, and Windows' limit is
#: the smallest of the platforms this runs on.
_INDEX_BATCH = 500


def exclude_matches(path: str, pattern: str) -> bool:
    """Whether ``path`` (a slash-separated index path) matches ``pattern``.

    Deliberately *not* ``git rm``'s pathspec semantics, which are the wrong
    shape here and fail silently.  A literal pathspec is anchored at the tree
    root, so ``--exclude node_modules`` removes the top-level ``node_modules/``
    and leaves ``packages/api/node_modules/`` in the snapshot -- measured on a
    tree holding ``node_modules/x.js``, ``pkg/node_modules/y.js`` and
    ``a/b/node_modules/z.js``, where it removed one of the three.

    The rule used instead is the one people expect:

    * the pattern as a path *prefix* -- ``--exclude packages/web/build``
      excludes everything under it, and matches case-insensitively so one
      command line excludes the same set on every platform;
    * ``fnmatch`` against the whole path, which is what makes a wildcard work
      (``*.pyc`` matches ``a/b/c.pyc``: fnmatch's ``*`` crosses ``/``); or
    * any single path *component* equal to the pattern (case-insensitively),
      which is what makes a bare name mean "a directory by that name, at any
      depth".
    """
    # The pattern travels the same road as the index: forward slashes, no
    # trailing slash, and -- so one command line means one snapshot -- a case
    # comparison that does not depend on the OS (T-11/SNAP-06; index paths are
    # case-sensitive data, and fnmatch's normcase would make Windows behave
    # differently from every other platform).
    pattern = pattern.strip().replace("\\", "/").rstrip("/")
    if not pattern:
        return False
    # A pattern naming a directory prefix (--exclude packages/web/build)
    # excludes everything inside it -- the more-precise spelling must exclude
    # at least as much as the bare name it refines (SNAP-03).
    if path == pattern or path.startswith(pattern + "/"):
        return True
    if fnmatch.fnmatch(path, pattern):
        return True
    # Wildcard patterns keep their fnmatch shape, so match the prefix form of
    # those too; the component check is exact (and case-sensitive both ways)
    # so a bare name still means "a directory by that name, at any depth".
    if any(ch in pattern for ch in "*?[") and fnmatch.fnmatch(path, pattern + "/*"):
        return True
    parts = [component.lower() for component in path.split("/")]
    pattern_parts = [component.lower() for component in pattern.split("/")]
    # Multi-segment patterns match the path's leading components (the
    # prefix rule, case-insensitively); single-segment ones match any
    # component.
    if len(pattern_parts) > 1:
        return parts[: len(pattern_parts)] == pattern_parts
    return pattern_parts[0] in parts


def drop_from_index(git_dir: Path, work_tree: Path, paths: list[str]) -> None:
    """Remove *paths* from the vault index, in batches.

    ``update-index --force-remove`` with NUL-separated paths on stdin, rather
    than one ``git rm`` per path: the matching is ours (see
    :func:`exclude_matches`), and batching keeps the argument list short -- a
    tree with a large ``node_modules`` holds far more paths than one command
    line can carry, and Windows' limit is the smallest of the platforms this
    runs on.
    """
    for start in range(0, len(paths), _INDEX_BATCH):
        batch = paths[start : start + _INDEX_BATCH]
        git(
            git_dir,
            work_tree,
            ["update-index", "--force-remove", "-z", "--stdin"],
            cwd=work_tree,
            stdin="\0".join(batch) + "\0",
        )


def apply_excludes(git_dir: Path, work_tree: Path, excludes: list[str]) -> list[str]:
    """Drop index paths matching *excludes*; return what was dropped.

    This is the post-staging **safety net**, not the mechanism: staging already
    skips excluded paths (see :func:`stageable_paths`), so a non-empty return
    means a path reached the index that the pre-filter did not account for --
    worth knowing about, and worth not letting into a snapshot.
    """
    if not excludes:
        return []
    listing = git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout
    dropped = [
        path
        for path in listing.split("\0")
        if path and any(exclude_matches(path, pat) for pat in excludes)
    ]
    if dropped:
        drop_from_index(git_dir, work_tree, dropped)
    return dropped


def stageable_paths(git_dir: Path, work_tree: Path) -> list[str]:
    """Every path ``git add -A -f .`` would consider, as the index spells it.

    ``ls-files -c -o`` *without* ``--exclude-standard``: ``-c`` is what is
    already staged and ``-o`` everything untracked **including ignored files**,
    which is exactly the set ``-f`` forces in -- adding ``--exclude-standard``
    would silently drop the ignored files that ``-f`` exists to capture.

    Two adjustments make the list safe to hand back as literal pathspecs (the
    git environment sets ``GIT_LITERAL_PATHSPECS=1``, so these names are never
    read as globs).  A nested repository is reported as ``nested/`` while the
    index records it as ``nested``, so the trailing slash goes.  And
    ``--deleted`` entries are dropped: they are in the index but gone from disk,
    and a pathspec naming a missing file makes ``git add`` fail outright ("did
    not match any files") rather than staging the deletion.
    """
    listing = git(git_dir, work_tree, ["ls-files", "-z", "-c", "-o"], cwd=work_tree).stdout
    deleted = {
        path.rstrip("/")
        for path in git(
            git_dir, work_tree, ["ls-files", "-z", "--deleted"], cwd=work_tree
        ).stdout.split("\0")
        if path
    }
    return [
        path
        for path in (entry.rstrip("/") for entry in listing.split("\0") if entry)
        if path not in deleted
    ]


def build_snapshot(
    git_dir: Path,
    work_tree: Path,
    parent: str | None,
    excludes: list[str],
    message: str,
    make_commit: bool = True,
    skip_paths: "frozenset[str] | None" = None,
    manifest: "dict[str, Any] | None" = None,
    capture_embedded: bool = True,
    embedded_report: "list[tuple[str, int]] | None" = None,
) -> tuple[str, str | None, list[str]]:
    """Stage the tree into the vault index and write a commit object.

    Excluded paths are filtered out **before** staging.  ``git add -A -f .``
    reads, hashes and writes a loose object for every file in the tree and only
    then lets :func:`apply_excludes` drop the matching entries, so excluded bulk
    -- a 2 GB ``node_modules``, say -- is read, compressed and left in the
    object store on every run; and because the excluded paths never reach the
    index, the stat cache cannot help, so each run re-reads them (B2).  The file
    list is enumerated instead and the survivors handed to git, so an excluded
    path is never opened.  With no exclusions there is nothing to filter and the
    plain ``add`` is used unchanged.

    Returns ``(tree, commit, excluded)``, where ``commit`` is ``None`` when the
    tree is identical to ``parent`` -- an unchanged run should not pile up empty
    commits -- or when ``make_commit`` is false: a preview stops at the tree,
    because a commit would need an identity the preview has deliberately not
    written, and every report the preview prints is derived from the tree
    alone.  ``parent`` is passed in rather than read from the branch, because
    the caller may have just re-based on the remote tip.  The branch is *not*
    moved here: that is the caller's job, so a dry run can stop one step short.

    An embedded repository is captured as its files (:func:`inline_embedded_repositories`)
    unless ``capture_embedded`` is false; each one is appended to ``embedded_report``.
    """
    reserved = (work_tree / PAYLOAD_RESERVED_DIR).exists()
    if excludes or skip_paths or reserved:
        candidates = stageable_paths(git_dir, work_tree)
        excluded = [
            path
            for path in candidates
            if any(exclude_matches(path, pat) for pat in excludes)
        ]
        excluded_set = set(excluded)
        # Payload files are not excluded in the user's sense -- they are
        # recorded in the manifest and live on the server -- so they are
        # kept out of the index without being reported as excluded.
        # macOS hands the walk decomposed (NFD) names while git reports them
        # precomposed, so the two sides are compared in one form.
        skipped = {unicodedata.normalize("NFC", p) for p in (skip_paths or ())}
        kept = [
            path for path in candidates
            if path not in excluded_set
            and unicodedata.normalize("NFC", path) not in skipped
            and path != PAYLOAD_RESERVED_DIR
            and not path.startswith(PAYLOAD_RESERVED_DIR + "/")
        ]
        kept_set = set(kept)
        if kept:
            git(
                git_dir,
                work_tree,
                ["add", "-A", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
                cwd=work_tree,
                stdin="\0".join(kept) + "\0",
                status="staging the tree",
                measure_dir=git_dir / "objects",
            )
        # `git add` stages no deletion through a pathspec, and this index
        # persists between runs, so anything it still holds that is not in
        # `kept` -- a file deleted from the source, or one an exclude now
        # covers -- has to be dropped explicitly.
        indexed = git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout
        stale = [
            path for path in indexed.split("\0") if path and path not in kept_set
        ]
        if stale:
            drop_from_index(git_dir, work_tree, stale)
        # The pre-filter above is the mechanism; this is the net under it, so a
        # path that reached the index anyway is reported rather than shipped.
        missed = apply_excludes(git_dir, work_tree, excludes)
        if missed:
            excluded = sorted(set(excluded) | set(missed))
    else:
        git(
            git_dir,
            work_tree,
            ["add", "-A", "-f", "."],
            cwd=work_tree,
            status="staging the tree",
            measure_dir=git_dir / "objects",
        )
        excluded = []

    if capture_embedded:
        captured = inline_embedded_repositories(git_dir, work_tree, excludes, skip_paths)
        if embedded_report is not None:
            embedded_report.extend(captured)

    if manifest is not None:
        add_manifest_to_index(git_dir, work_tree, manifest)
    tree = git(git_dir, work_tree, ["write-tree"], cwd=work_tree).stdout.strip()

    if not make_commit:
        return tree, None, excluded

    if parent:
        parent_tree = git(
            git_dir, work_tree, ["rev-parse", f"{parent}^{{tree}}"], cwd=work_tree
        ).stdout.strip()
        if parent_tree == tree:
            return tree, None, excluded

    return tree, commit_tree(git_dir, work_tree, tree, parent, with_tool_trailer(message)), excluded


def describe_tree(git_dir: Path, work_tree: Path, tree: str) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for a tree."""
    listing = git(
        git_dir, work_tree, ["ls-tree", "-r", "-l", "--full-tree", tree], cwd=work_tree
    ).stdout
    count = 0
    total = 0
    for line in listing.splitlines():
        if not line.strip():
            continue
        count += 1
        meta = line.split("\t", 1)[0].split()
        if len(meta) >= 4 and meta[3].isdigit():
            total += int(meta[3])
    return count, total


def find_gitlinks(git_dir: Path, work_tree: Path, tree: str) -> list[str]:
    """Paths recorded as gitlinks -- embedded repositories whose contents are absent.

    ``git add`` records a directory that contains its own ``.git`` as a single
    reference to that repository's commit, and the commit is *not* copied.  A
    restore therefore produces a broken directory rather than the files, which
    is silent data loss in something sold as a full backup.
    """
    listing = git(git_dir, work_tree, ["ls-tree", "-r", tree], cwd=work_tree).stdout
    return [
        line.split("\t", 1)[1]
        for line in listing.splitlines()
        if line.startswith("160000 commit ")
    ]


#: Names that mean "this file is a credential".  Matched against the basename,
#: except for the ones containing a slash, which are matched against the path.
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "secrets.*",
    "*.secret",
    ".aws/credentials",
    ".ssh/*",
)


def find_secrets(paths: list[str]) -> list[str]:
    """Return the captured paths that look like credentials.

    The tool backs up ``.env`` and private keys quite happily -- that is what a
    *full* backup means.  But Git history is permanent, so the moment before the
    first push is the last moment the user can still choose a different remote.
    Saying so costs one line; not saying so is how secrets end up in a library
    someone else can read.

    A pattern with a slash (``.ssh/*``) names a location, not a filename, so it
    is matched against the whole path -- but anchored at any depth, not just
    the tree root: ``deploy/.ssh/known_hosts`` must be caught as readily as a
    top-level ``.ssh`` directory.
    """
    found: list[str] = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        for pattern in SECRET_PATTERNS:
            if "/" in pattern:
                if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(
                    path, f"*/{pattern}"
                ):
                    found.append(path)
                    break
            elif fnmatch.fnmatch(name, pattern):
                found.append(path)
                break
    return found


def index_paths(git_dir: Path, work_tree: Path) -> list[str]:
    """Every path staged in the vault's index."""
    listing = git(git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree).stdout
    return [path for path in listing.split("\0") if path]


def suggest_excludes(
    git_dir: Path, work_tree: Path, excluded: list[str]
) -> list[str]:
    """Name reproducible junk that was captured, and how to leave it out.

    Deliberately a *suggestion* and not a default.  A snapshot is a backup; the
    one failure that cannot be forgiven is a backup that quietly omitted the file
    that mattered.  ``node_modules`` is reproducible and ``.env`` is not, and the
    tool cannot tell them apart -- so it captures everything, and points at what
    is merely bulky.

    Returns the patterns found (for testing); prints the advice.
    """
    present = {pattern for pattern in RECOMMENDED_EXCLUDES} - set(excluded)
    if not present:
        return []
    listing = git(
        git_dir, work_tree, ["ls-files", "-z"], cwd=work_tree
    ).stdout
    paths = [p for p in listing.split("\0") if p]
    found: list[str] = []
    for pattern in sorted(present):
        hit = any(exclude_matches(path, pattern) for path in paths)
        if hit:
            found.append(pattern)
    if not found:
        return []
    flags = " ".join(f"--exclude {p!r}" for p in found)
    print(
        f"\nWARNING: the snapshot includes reproducible build output that is "
        f"usually not worth\nbacking up.  Leave it out next time with "
        f"--default-excludes, or explicitly:\n  {flags}"
    )
    return found
