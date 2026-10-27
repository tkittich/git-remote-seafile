

#: The fixed headroom a restore or drill asks of the destination volume
#: before cloning: the vault's git size cannot be known before the transfer,
#: and a checkout roughly doubles the cloned bytes, so this is a floor, not
#: an estimate.
_CLONE_FLOOR_BYTES = 1024 ** 3


def _unrepresentable_paths(repo: Path, rev: str) -> list[str]:
    """Paths in *rev* whose names Windows cannot hold; always empty elsewhere."""
    if os.name != "nt":
        return []
    listing = _run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--name-only", "--full-tree", rev],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if listing.returncode != 0:
        return []  # the checkout below reports whatever is wrong with the revision
    names = (p.decode("utf-8", errors="surrogateescape") for p in listing.stdout.split(b"\0") if p)
    return [name for name in names if windows_name_problem(name) is not None]


def _checkout_around(repo: Path, rev: str, skipped: list[str], *, detach: bool) -> None:
    """Check *rev* out without the paths in *skipped*.

    ``git checkout`` refuses a whole tree for one name Windows cannot hold
    ("error: invalid path 'a:b.txt'") and writes nothing, and relaxing
    ``core.protectNTFS`` to get past it would also lift git's defence against
    hostile NTFS names (8.3 aliases of ``.git``).  So the index is built from
    the revision's own entries minus those paths and written with
    ``checkout-index``; HEAD is pointed at the revision by hand.
    """
    listing = _run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", rev],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if listing.returncode != 0:
        raise SnapshotError(
            f"could not list '{rev}' in {repo} (rc={listing.returncode})\n"
            f"{listing.stderr.decode('utf-8', errors='replace').strip()}"
        )
    drop = set(skipped)
    entries = bytearray()
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        if path.decode("utf-8", errors="surrogateescape") in drop:
            continue
        fields = meta.split()
        entries += fields[0] + b" " + fields[2] + b"\t" + path + b"\0"
    steps = [
        (["read-tree", "--empty"], None),
        (["update-index", "-z", "--add", "--index-info"], bytes(entries)),
        (["update-ref", "--no-deref", "HEAD", rev] if detach
         else ["symbolic-ref", "HEAD", f"refs/heads/{rev}"], None),
        (["-c", "core.longpaths=true", "checkout-index", "-a", "-f"], None),
    ]
    for args, stdin in steps:
        slow = "checkout-index" in args
        proc = _run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            status=f"restoring files into {repo}" if slow else None,
            measure_dir=repo if slow else None,
        )
        if proc.returncode != 0:
            raise SnapshotError(
                f"checkout of '{rev}' failed (rc={proc.returncode})\n"
                f"{proc.stdout.decode('utf-8', errors='replace').strip()}"
            )


def _checkout(repo: Path, rev: str, *, detach: bool = False) -> list[str]:
    """Check *rev* out; return the paths that could not be written (names this OS cannot hold)."""
    if rev.startswith("-"):
        # A remote ref is a file the library writer controls, and a name like
        # "--detach" is a legal one there; handed to git unguarded it parses
        # as the option, and the restore dies with "a branch yet to be born"
        # (REVIEW.sonnet-5.5 E3, reproduced on Windows and Linux).
        raise SnapshotError(
            f"cannot check out {rev!r}: the name reads as a git option. "
            "Restore a differently named branch, or pass a commit id"
        )
    skipped = _unrepresentable_paths(repo, rev)
    if skipped:
        _checkout_around(repo, rev, skipped, detach=detach)
        return skipped
    args = ["git", "-C", str(repo), "-c", "core.longpaths=true", "checkout", "-f"]
    if detach:
        args.append("--detach")
    args.append(rev)
    proc = _run(
        args,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        status=f"restoring files into {repo}",
        measure_dir=repo,
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"checkout of '{rev}' failed (rc={proc.returncode})\n{proc.stdout.strip()}"
        )
    return []


def verify_restore(repo: Path, rev: str, skip: "set[str] | None" = None) -> list[str]:
    """Paths whose bytes on disk disagree with the snapshot's blobs.

    Byte-level on purpose, because the two filter-aware checks can both pass a
    *mangled* restore -- measured on a plain ``git clone`` of the vault:

    * ``git status`` compares *through* the attribute and ``core.autocrlf``
      settings, so a checkout that rewrote every LF as CRLF still reports a
      clean tree;
    * plain ``git hash-object`` applies the clean filter, so it re-normalises
      the very difference being looked for, and matches too.

    ``--no-filters`` hashes the bytes as they lie on disk, which is the only one
    of the three that cannot be fooled.  Our own :func:`restore` happens to pin
    ``core.autocrlf=false`` and ``* -text -filter -ident``, so on *that* tree the
    other two agree as well -- but this check must hold for any checkout,
    including one made by hand, so it does not lean on them.

    Every path is hashed in a single ``hash-object --stdin-paths`` process:
    one subprocess per file made a large restore spend its time in process
    start-up (``ls-tree -z`` emits raw, unquoted paths, which is what the
    NUL-separated stdin expects).  Both sides run in *bytes* end to end, so a
    filename that is not valid UTF-8 is hashed as the filename it actually is
    rather than a ``replace``-decoded approximation of it.
    """
    listing = _run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", rev],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if listing.returncode != 0:
        raise SnapshotError(
            f"could not list '{rev}' in {repo} (rc={listing.returncode})\n"
            f"{listing.stderr.decode('utf-8', errors='replace').strip()}"
        )
    paths: list[bytes] = []
    expected: list[bytes] = []
    symlinks: list[str] = []
    #: Blob SHA for *every* entry, symlinks included.  ``expected`` is parallel
    #: to ``paths``, which deliberately excludes symlinks, so a symlink must not
    #: be looked up by its position in that list: doing so compared the link
    #: against an unrelated file's blob, which reported every correctly-restored
    #: link as a mismatch -- and raised IndexError outright when the tree held
    #: nothing but symlinks.  The SHA is held decoded because it is handed to
    #: :func:`_run` as an element of a ``list[str]`` argument vector.
    blob_sha: dict[str, str] = {}
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) < 3 or fields[1] != b"blob":
            continue
        if skip and path.decode("utf-8", errors="surrogateescape") in skip:
            continue  # a name this OS cannot hold: not restored, and said so
        # A symlink is a blob (mode 120000) holding the link-target
        # string, and the plain hash path cannot verify one: on Windows
        # (core.symlinks=false) git checks it out as a TEXT FILE whose
        # bytes equal the blob, so hash-object would "verify" a file
        # where the source had a link; on POSIX hash-object FOLLOWS the
        # link and hashes the target, so a correct restore would report
        # a spurious mismatch.  Symlinks are verified separately (by
        # reading the link) and never sent through hash-object.
        if fields[0] == b"120000":
            link = path.decode("utf-8", errors="surrogateescape")
            symlinks.append(link)
            blob_sha[link] = fields[2].decode("ascii")
            continue
        paths.append(path)
        expected.append(fields[2])
    if not paths and not symlinks:
        return []
    # --stdin-paths is newline-separated, and git reads a line that starts with
    # a double quote as a C-quoted string -- so a name containing a newline, or
    # starting with `"`, cannot ride in the batch.  Those few are hashed one at
    # a time, as argv, where no quoting rule applies.
    odd = [
        (path, sha) for path, sha in zip(paths, expected, strict=True)
        if b"\n" in path or path.startswith(b'"')
    ]
    if odd:
        keep = [
            (path, sha) for path, sha in zip(paths, expected, strict=True)
            if not (b"\n" in path or path.startswith(b'"'))
        ]
        paths = [path for path, _ in keep]
        expected = [sha for _, sha in keep]
    odd_mismatched: list[str] = []
    for path, sha in odd:
        # `--` and a path relative to -C: a name starting with `-` is a path.
        one = _run(
            ["git", "-C", str(repo), "hash-object", "--no-filters", "--",
             os.fsdecode(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if one.returncode != 0 or one.stdout.strip() != sha:
            odd_mismatched.append(path.decode("utf-8", errors="surrogateescape"))

    # Reported after the batch's own mismatches, with the links.
    mismatched_symlinks: list[str] = list(odd_mismatched)
    for link in symlinks:
        link_path = repo / link
        try:
            actual = os.readlink(str(link_path)).encode("utf-8", errors="surrogateescape")
        except (OSError, ValueError):
            # The entry did not come back as a link at all (the Windows
            # text-file case): that is a difference worth naming.
            mismatched_symlinks.append(link)
            continue
        blob = _run(
            ["git", "-C", str(repo), "cat-file", "blob", blob_sha[link]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if blob.returncode != 0 or blob.stdout != actual:
            mismatched_symlinks.append(link)
    if not paths:
        return mismatched_symlinks
    proc = _run(
        ["git", "-C", str(repo), "hash-object", "--no-filters", "--stdin-paths"],
        input=b"\n".join(paths) + b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        status="verifying restored files (re-hashing every path)",
        # heartbeat-only: this step reads every file and writes nothing, so a
        # stall limit would have no signal to watch.
        stall_limit=None,
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"could not hash the restored files in {repo} (rc={proc.returncode})\n"
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    hashes = proc.stdout.split()
    if len(hashes) != len(expected):
        # git outputs one hash per input path, in order; a short output means
        # something ate a path, and guessing which would be worse than failing.
        # The path it could not open is named on stderr -- append it, or the
        # count mismatch is all the user gets.
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(
            f"hash-object returned {len(hashes)} hashes for {len(paths)} "
            f"paths in {repo}"
            + (f"\n{detail}" if detail else "")
        )
    return [
        path.decode("utf-8", errors="surrogateescape")
        for path, expected_sha, actual_sha in zip(paths, expected, hashes, strict=True)
        if actual_sha != expected_sha
    ] + mismatched_symlinks


def _resolve_assumed_branch(clone: Path, assumed: str) -> str:
    """The branch to restore when the user did not name one.

    The assumed name wins when the remote has it.  Otherwise a remote with
    exactly one branch is unambiguous; with several, naming them beats guessing.
    """
    proc = _run(
        ["git", "-C", str(clone), "for-each-ref", "--format=%(refname)", "refs/remotes/origin"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    prefix = "refs/remotes/origin/"
    names = [
        line[len(prefix):]
        for line in proc.stdout.splitlines()
        if line.startswith(prefix) and line != prefix + "HEAD"
    ]
    if assumed in names or not names:
        return assumed
    if len(names) == 1:
        print(f"Note: the remote has no branch '{assumed}'; restoring its only branch '{names[0]}'")
        return names[0]
    raise SnapshotError(
        f"the remote has no branch '{assumed}' and holds several: {', '.join(sorted(names))}. "
        f"Choose one with --branch."
    )


def restore(
    remote: str,
    into: Path,
    branch: str,
    ref: str | None,
    verify: bool = False,
    branch_explicit: bool = True,
    allow_low_disk: bool = False,
    payload_sample: int | None = None,
) -> None:
    """Materialise a snapshot into a new directory; say what a failure leaves behind.

    A run that fails after the clone leaves a partial tree, and the retry then
    stops with "destination is not empty" -- naming nothing about why it is
    not.  The failure says where the partial restore is and that it has to go
    before trying again.  It is not deleted for the user: a restore that failed
    its byte check is the evidence worth looking at.
    """
    was_empty = not into.exists() or not any(into.iterdir())
    try:
        _restore(remote, into, branch, ref, verify, branch_explicit, allow_low_disk,
                 payload_sample)
    except SnapshotError as exc:
        if was_empty and into.exists() and any(into.iterdir()):
            raise SnapshotError(
                f"{exc}\nThe partial restore was left in {into} for inspection; "
                "delete it before running the restore again."
            ) from exc
        raise


def _restore(
    remote: str,
    into: Path,
    branch: str,
    ref: str | None,
    verify: bool = False,
    branch_explicit: bool = True,
    allow_low_disk: bool = False,
    payload_sample: int | None = None,
) -> None:
    """Materialise a snapshot into a new directory, byte-for-byte.

    ``branch_explicit`` is False when the caller only assumed the default
    branch name.  A restore starts from nothing -- there is no vault here to
    remember a branch from -- so the default may simply be wrong for this
    remote; then the one branch the remote has is used, and several are listed.

    A plain ``git clone`` of the vault is *not* enough on Windows: the clone
    inherits ``core.autocrlf=true`` from Git for Windows' system config, and
    ``info/attributes`` -- which is what makes the vault byte-exact -- is not
    transferred by a clone.  The source's own ``.gitattributes`` *is*
    transferred, so it would re-trigger conversion on checkout.

    So: clone without checking anything out, apply the same byte-exact settings
    to the clone, and only then check out.
    """
    if into.exists() and any(into.iterdir()):
        raise SnapshotError(f"restore destination is not empty: {into}")

    # The payload's need is measured before its download (D13); the git half
    # cannot be — the vault's packed size is unknowable before the transfer —
    # so the destination volume gets a fixed headroom floor instead (the
    # review round's deferred item: the drill and the restore filled temp
    # volumes and kept the partial tree).  A full checkout roughly doubles
    # the cloned bytes, hence the generous floor.
    if not allow_low_disk:
        try:
            free = shutil.disk_usage(into).free
        except OSError:
            free = None
        if free is not None and free < _CLONE_FLOOR_BYTES:
            raise SnapshotError(
                f"the volume holding {into} has {human_bytes(free)} of free space; a restore "
                f"needs at least {human_bytes(_CLONE_FLOOR_BYTES)} of headroom before "
                "the clone, and the vault's git size cannot be known before the "
                "transfer (the payload's need is measured separately, below). "
                "--allow-low-disk tries anyway"
            )

    into.parent.mkdir(parents=True, exist_ok=True)
    print(f"cloning {remote} -> {into} (no checkout) ...")
    # stderr is inherited, not captured: a clone of a large vault transfers for
    # hours, and git's "Receiving objects" progress goes to the terminal only
    # when it is not being piped.  The watchdog adds one line per minute on top.
    proc = _run(
        ["git", "-c", "core.longpaths=true", "clone", "--no-checkout", remote, str(into)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        status=f"cloning {remote}",
        measure_dir=into / ".git",
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"clone failed (rc={proc.returncode})\n{proc.stdout.strip()}"
        )

    ensure_byte_exact(into, into / ".git")

    if not branch_explicit:
        branch = _resolve_assumed_branch(into, branch)

    # A --no-checkout clone has no local branch, so a revision like
    # "snapshot~3" cannot resolve until the branch exists locally.  When the
    # requested revision *is* the branch, checking it out materialises both at
    # once; otherwise the branch ref is created without touching the worktree
    # (``git branch``), and only the requested revision is checked out --
    # checking the branch out first would write the whole tree twice.
    target = branch
    verified_rev = branch
    if ref and ref != branch:
        # Whether the clone already made the branch depends on what the remote
        # advertises as HEAD: a remote whose HEAD is this branch (the helper's
        # case) gets a local branch from `clone --no-checkout`; a bare remote
        # with another HEAD does not.  Creating it unconditionally failed with
        # "a branch named ... already exists" on the first kind.
        exists = _run(
            ["git", "-C", str(into), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).returncode == 0
        if not exists:
            made = _run(
                ["git", "-C", str(into), "branch", branch, f"origin/{branch}"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if made.returncode != 0:
                raise SnapshotError(
                    f"could not materialise '{branch}' in the clone "
                    f"(rc={made.returncode})\n{made.stderr.strip()}"
                )
        proc = _run(
            ["git", "-C", str(into), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise SnapshotError(
                f"cannot resolve '{ref}' in the vault\n{proc.stderr.strip()}"
            )
        sha = proc.stdout.strip()
        unholdable = _checkout(into, sha, detach=True)
        target = f"{ref} ({sha[:12]})"
        verified_rev = sha
    else:
        unholdable = _checkout(into, branch)
    if unholdable:
        shown = "\n".join(f"  {path}" for path in unholdable[:10])
        more = f"\n  ... and {len(unholdable) - 10} more" if len(unholdable) > 10 else ""
        print(
            f"\nWarning: {len(unholdable)} file(s) in this snapshot have names Windows cannot hold "
            f"and were not restored:\n{shown}{more}\nRestore on Linux or macOS to get them; "
            "everything else was restored and is verified below."
        )

    # The snapshot-time warning about embedded repositories is years and
    # machines away by the time someone restores; the restored revision is
    # where the broken directory actually materialises, so repeat it here
    # rather than let the certification below claim byte-exactness for a tree
    # with holes in it.
    restored_gitlinks = find_gitlinks(into / ".git", into, verified_rev)
    if restored_gitlinks:
        print(
            "\nWarning: these paths in the restored tree are embedded git "
            "repositories.  Only\ntheir commit id was recorded at snapshot "
            "time, NOT their contents -- these\ndirectories are empty on "
            "purpose but incomplete as a backup:"
        )
        for path in restored_gitlinks:
            print(f"  {path}")
        print(
            "  Back them up separately, or make them submodules with a "
            "remote that survives."
        )

    # Git stores no timestamps; the record the snapshot wrote (if it wrote one)
    # puts them back.  After the checkout and before the byte check, which
    # reads content only and so is not affected.
    applied, skipped = apply_recorded_mtimes(into)
    if applied or skipped:
        print(f"restored the modification time of {applied} file(s)"
              + (f" ({skipped} could not be set)" if skipped else ""))

    # A restore that differs from the snapshot is not a restore, and the
    # autocrlf class of bug is invisible to `git status` -- so check the bytes.
    mismatched = verify_restore(into, verified_rev, set(unholdable))
    if mismatched:
        shown = "\n".join(f"  {path}" for path in mismatched[:20])
        extra = (
            ""
            if len(mismatched) <= 20
            else f"\n  ... and {len(mismatched) - 20} more"
        )
        raise SnapshotError(
            f"{len(mismatched)} restored file(s) do not match the snapshot "
            f"byte-for-byte:\n{shown}{extra}\n"
            f"The blobs in the vault are intact -- this is a defect in the "
            f"checkout, so please report it rather than trusting the restore."
        )

    if verify:
        proc = _run(
            ["git", "-C", str(into), "fsck", "--no-progress", "--no-dangling"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if proc.returncode != 0:
            raise SnapshotError(
                f"git fsck failed in {into} (rc={proc.returncode})\n"
                f"{proc.stdout.strip()}"
            )
        print("git fsck: object store intact")

    restore_payload(remote, into, allow_low_disk=allow_low_disk, sample=payload_sample)

    files = sum(1 for p in into.rglob("*") if p.is_file() and ".git" not in p.parts)
    print(f"restored {files} files from {target} into {into}")
    print(
        f"verified byte-exact against {verified_rev[:12]}"
        + (f" ({len(unholdable)} unrepresentable name(s) excluded)" if unholdable else "")
    )


def _remove_tree(path: Path) -> None:
    """``rmtree`` that also removes git's read-only object files on Windows."""
    def writable(func: Any, name: str, _exc: Any) -> None:
        os.chmod(name, stat.S_IWRITE)
        func(name)
    shutil.rmtree(extended_path(path), onerror=writable)


def restore_drill(
    remote: str, branch: str, ref: str | None, sample: int, *, branch_explicit: bool,
    allow_low_disk: bool = False, allow_synced: bool = False,
) -> None:
    """Restore into a temporary directory, check it, and delete it.

    The point is the answer to "would a restore work today", asked without the
    operator choosing a destination or cleaning up.  Everything a restore checks
    is checked (every file byte-for-byte, ``git fsck``); only the payload is
    sampled, since it is the part that costs hours.  A failure keeps the
    directory (:func:`restore` names it): a restore that failed is the evidence.
    """
    scratch = Path(tempfile.mkdtemp(prefix="grs-drill-"))
    into = scratch / "restore"
    try:
        check_paths_are_unsynced(("drill directory", scratch), allow_synced=allow_synced)
    except SnapshotError:
        _remove_tree(scratch)
        raise
    started = time.monotonic()
    print(f"restore drill: restoring into {into} (removed afterwards)", flush=True)
    restore(remote, into, branch, ref, verify=True, branch_explicit=branch_explicit,
            allow_low_disk=allow_low_disk, payload_sample=sample)
    seconds = time.monotonic() - started
    _remove_tree(scratch)
    print(f"restore drill passed in {_fmt_elapsed(seconds)}: the snapshot restores byte-exact, "
          "git fsck is clean and every sampled payload file matches its SHA-256", flush=True)
    RUN_FACTS.update(drill_seconds=round(seconds, 1), sample=sample)


def drill_sample(text: str) -> int:
    """The payload files a drill fetches: a positive number, or ``all``."""
    if text == "all":
        return sys.maxsize
    if not text.isdigit() or int(text) < 1:
        raise SnapshotError(f"--restore-drill takes a positive number or 'all', not '{text}'")
    return int(text)


def restore_drill_command(args: argparse.Namespace) -> int:
    if not args.remote:
        raise SnapshotError("--restore-drill requires --remote <url>")
    restore_drill(
        args.remote, args.branch or DEFAULT_BRANCH, args.ref, drill_sample(args.restore_drill),
        branch_explicit=bool(args.branch), allow_low_disk=args.allow_low_disk,
        allow_synced=args.allow_synced,
    )
    return 0
