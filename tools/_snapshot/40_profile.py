

def disable_object_compression(vault: Path, git_dir: Path) -> None:
    """Set ``core.compression=0`` so new objects skip real deflation.

    Level 0 keeps zlib's wrapper but stores the content in uncompressed blocks:
    nothing shrinks, yet git reads it back byte-identical.  Measured on a tree
    dominated by already-compressed archives, that traded zero bytes for 3x the
    staging speed (41 -> 126 MiB/s).  Existing objects keep the form they were
    written in -- a blob SHA covers content, not storage encoding -- so flipping
    this either way is safe at any point.
    """
    if _read_local_config_map(vault).get("core.compression") == ["0"]:
        return
    git(git_dir, vault, ["config", "core.compression", "0"], cwd=vault)


def _object_store_stats(git_dir: Path, work_tree: Path) -> dict[str, int]:
    """The numbers from ``git count-objects -v``, as ints."""
    proc = git(git_dir, None, ["count-objects", "-v"], cwd=work_tree)
    stats: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        try:
            stats[key.strip()] = int(value.strip())
        except ValueError:
            pass  # newer git versions add non-numeric lines; the ones we print are ints
    return stats


# The automatic incremental repack gates itself on the size of the loose tier:
# below it, a pass over the object directory is not worth it; above it, packing
# is cheap next to the push that follows.  Only *bytes* are gated.  An object
# *count* trigger already exists in git itself -- `git fetch`, which this tool
# runs every time a remote is configured, launches `gc --auto` once roughly
# 6,700 loose objects exist -- whereas nothing in git looks at size, and a vault
# of a few dozen huge files never trips a count.
_REPACK_MIN_LOOSE_BYTES = 256 * 1024 * 1024


def auto_incremental_repack(git_dir: Path, work_tree: Path) -> None:
    """Pack the loose tier after a snapshot commit; never fail the snapshot.

    ``repack -d`` moves reachable loose objects into packs (existing packs are
    untouched and serve as delta bases); unreachable ones stay loose so the
    following ``prune`` can delete them -- dead space is never sealed into a
    pack.  Git swaps in packfiles atomically, so even an interrupted run leaves
    the repository intact; that is what entitles this step to be best-effort.
    """
    stats = _object_store_stats(git_dir, work_tree)
    loose_bytes = stats.get("size", 0) * 1024
    count = stats.get("count", 0)
    if loose_bytes < _REPACK_MIN_LOOSE_BYTES:
        return
    print(f"consolidating {count} loose object(s), {human_bytes(loose_bytes)}, "
          f"before the push ...")
    try:
        git(git_dir, None, ["repack", "-d"], cwd=work_tree,
            status="packing the vault's loose objects", measure_dir=git_dir / "objects")
        git(git_dir, None, ["prune"], cwd=work_tree)
    except SnapshotError as exc:
        print(f"Note: incremental repack failed ({str(exc).splitlines()[0]}); "
              f"the snapshot itself succeeded", file=sys.stderr)


# -- Large-payload profile --------------------------------------------------------
#
# Two git costs scale with *raw* bytes and buy nothing on data that does not
# compress or deltify, and both showed up on a 20 GB+ tree that timed out:
#
#   * staging (`git add`) deflates every blob: 42 MB/s on incompressible data,
#     131 MB/s at core.compression=0;
#   * `pack-objects` -- the helper's push, and the vault's own background
#     `gc --auto` -- runs a delta search over every blob below
#     core.bigFileThreshold (default 512 MiB): 8-10 MB/s regardless of the
#     compression level, against 320 MB/s when the big blobs are streamed.
#
# The profile is decided once, from a small sample taken before the first
# staging, and then remembered in the vault's config (`snapshot.profile`).
# Nothing here changes content: object *storage encoding* and *packing
# strategy* only, both of which are safe to flip at any time.

#: Below this much raw data neither cost is worth a decision.
_PROFILE_MIN_TREE_BYTES = 2 * 1024 ** 3
#: A file this big is "payload"; smaller ones (source, config) stay as they are.
_PROFILE_BIG_FILE_BYTES = 1024 ** 2
#: How many of the largest files are sampled, and how much of each.
_PROFILE_SAMPLE_FILES = 32
_PROFILE_SAMPLE_CHUNK = 256 * 1024
#: Sampled data that keeps at least this fraction of its size under a fast
#: deflate is treated as not worth compressing.  This is a break-even, not a
#: purity test: deflate costs ~1/45 s per MiB on one core against ~1/186 raw
#: (an extra ~0.017 s/MiB), and the bytes it saves are worth (1 - ratio)/32
#: s/MiB at the ~32 MiB/s upload measured on the real server.  The two meet
#: near a ratio of 0.45, so anything above 0.5 is faster raw end to end --
#: measured on the flood tree, whose sample kept 85 %: deflating staged it at
#: ~45 MiB/s (about 50 minutes for 131 GiB) where raw takes about 11.  The old
#: 0.9 cut-off sent exactly such mixed data down the slow path.
_PROFILE_INCOMPRESSIBLE_RATIO = 0.5
#: What to set for a raw payload.  The threshold is the one measured in
#: git_remote_seafile.git_util.PACK_BIG_FILE_THRESHOLD (kept as a literal so
#: this file does not need the package just to name a string).
_PROFILE_BIG_FILE_THRESHOLD = "50m"


def apply_large_payload_profile(vault: Path, git_dir: Path) -> None:
    """Store new objects uncompressed and stream big blobs past delta search.

    Idempotent.  Both settings are repo-local, so they travel with the vault and
    are read by every git process that touches it -- the helper's push-time
    ``pack-objects`` and the vault's own background ``gc --auto`` included, which
    is why the profile is applied here and not only on the helper's command
    line.
    """
    disable_object_compression(vault, git_dir)
    if _read_local_config_map(vault).get("core.bigfilethreshold") != [_PROFILE_BIG_FILE_THRESHOLD]:
        git(git_dir, vault, ["config", "core.bigFileThreshold", _PROFILE_BIG_FILE_THRESHOLD], cwd=vault)
    git(git_dir, vault, ["config", "snapshot.profile", "raw"], cwd=vault)


def _walk_sizes(
    source: Path, excludes: list[str], skip: "frozenset[str] | None" = None
) -> list[tuple[int, str]]:
    """``(size, relative posix path)`` for every regular file under *source*.

    Stat only -- no content is read.  ``.git`` directories are skipped (a source
    that is itself a repository), and excluded paths are skipped, so the
    decision describes what will actually be staged.
    """
    found: list[tuple[int, str]] = []
    root = str(source)

    def _keep(d: str) -> bool:
        if d == ".git":
            return False
        try:
            return not (getattr(os.lstat(os.path.join(dirpath, d)), "st_file_attributes", 0) & 0x400)
        except OSError:
            # Vanished between the listing and the stat: nothing to walk.
            return False

    for dirpath, dirnames, filenames in os.walk(root):
        # A junction or mount point is a directory to os.walk but not a symlink,
        # and git does not follow it: neither may a walk that decides what is
        # uploaded, or one junction to a drive root would hash the drive.
        dirnames[:] = [d for d in dirnames if _keep(d)]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if excludes and any(exclude_matches(rel, pat) for pat in excludes):
                continue
            if skip and rel in skip:
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                found.append((st.st_size, rel))
    return found


def sample_payload(
    source: Path, excludes: list[str], skip: "frozenset[str] | None" = None
) -> dict[str, float]:
    """Measure what staging this tree will cost, from a small sample.

    Returns ``total`` (bytes of all files), ``payload`` (bytes in files of at
    least ``_PROFILE_BIG_FILE_BYTES``), ``sampled`` (bytes actually read) and
    ``ratio`` (compressed / raw over the sample; 0.0 when nothing was sampled).
    At most ``_PROFILE_SAMPLE_FILES`` of the largest files are read, three
    chunks of ``_PROFILE_SAMPLE_CHUNK`` each (start, middle, end), so the cost
    is bounded by ~24 MiB of reads however large the tree is.
    """
    files = _walk_sizes(source, excludes, skip)
    total = sum(size for size, _ in files)
    big = sorted(((s_, r) for s_, r in files if s_ >= _PROFILE_BIG_FILE_BYTES), reverse=True)
    payload = sum(size for size, _ in big)
    raw = comp = 0
    for size, rel in big[:_PROFILE_SAMPLE_FILES]:
        try:
            with open(source / rel, "rb") as fh:
                for offset in sorted({0, max(0, size // 2 - _PROFILE_SAMPLE_CHUNK // 2),
                                       max(0, size - _PROFILE_SAMPLE_CHUNK)}):
                    fh.seek(offset)
                    chunk = fh.read(_PROFILE_SAMPLE_CHUNK)
                    if chunk:
                        raw += len(chunk)
                        comp += len(zlib.compress(chunk, 1))
        except OSError:
            continue
    return {
        "total": float(total),
        "payload": float(payload),
        "sampled": float(raw),
        "ratio": (comp / raw) if raw else 0.0,
    }


def decide_profile(measure: dict[str, float]) -> str:
    """``"raw"`` or ``"default"`` from :func:`sample_payload`'s numbers.

    A raw-data profile needs all three: a tree big enough for the costs to
    matter, a payload that dominates it (so small source files are not being
    deprived of delta compression for the sake of a few large ones), and a
    sample that really does not compress.
    """
    if measure["total"] < _PROFILE_MIN_TREE_BYTES:
        return "default"
    if measure["payload"] < 0.5 * measure["total"]:
        return "default"
    if measure["sampled"] <= 0 or measure["ratio"] < _PROFILE_INCOMPRESSIBLE_RATIO:
        return "default"
    return "raw"


def _use_profile_for_this_process_only() -> None:
    """Give this process's git children the raw-payload settings, writing nothing.

    A preview stages the whole tree -- that is how it can report the diffstat,
    the secrets and the gitlinks -- and until this was added it did so with
    git's defaults while the real run would use the raw profile.  Measured on
    the flood tree: deflating incompressible zips ran at ~46 MiB/s on one core
    (the machine looked idle: 1 of 24 threads busy) against ~186 MiB/s with
    ``core.compression=0``, so the preview took four times as long as the run
    it was previewing.  The settings go in through git's own environment
    channel, so no config file is touched and the vault stays exactly as a dry
    run promises.
    """
    base = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    settings = (("core.compression", "0"), ("core.bigFileThreshold", _PROFILE_BIG_FILE_THRESHOLD))
    for offset, (key, value) in enumerate(settings):
        os.environ[f"GIT_CONFIG_KEY_{base + offset}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{base + offset}"] = value
    os.environ["GIT_CONFIG_COUNT"] = str(base + len(settings))


def choose_profile(
    vault: Path, git_dir: Path, source: Path, excludes: list[str],
    vault_config: dict[str, list[str]], *, write: bool, recheck: bool = False,
    skip: "frozenset[str] | None" = None,
) -> None:
    """Decide the vault's storage profile on the first run that needs it.

    Skipped when the profile is already ``raw``, or when the user pinned
    ``core.compression`` themselves -- a vault someone tuned by hand is not
    second-guessed.  A ``default`` verdict stands until the tree has doubled
    since it was made (:func:`profile_recheck_due`), or *recheck* is set: a vault
    that began small and became a 20 GB payload would otherwise stay on the slow
    settings forever.  That test runs here, *before* staging, from the same stat
    walk the sample uses.  It once ran after staging, so the run that grew the
    tree still deflated all of it (8 minutes of a 30 GB run, then the verdict
    arrived for the next one).  Under ``--dry-run`` (``write=False``) the sample
    is still taken and reported, but nothing is written.
    """
    profile = (vault_config.get("snapshot.profile") or [""])[0]
    if profile == "raw" or vault_config.get("core.compression"):
        return
    if profile and not recheck:
        total = sum(size for size, _ in _walk_sizes(source, excludes, skip))
        if not profile_recheck_due(vault_config, total):
            return
    measure = sample_payload(source, excludes, skip)
    verdict = decide_profile(measure)
    if verdict == "raw":
        what = (
            f"large raw payload detected ({human_bytes(measure['payload'])} in big files of "
            f"{human_bytes(measure['total'])}; the sample keeps {measure['ratio']:.0%} of its "
            f"size under deflate)"
        )
        if write:
            apply_large_payload_profile(vault, git_dir)
            print(f"{what}: storing objects uncompressed and skipping delta search for big "
                  f"blobs (core.compression=0, core.bigFileThreshold={_PROFILE_BIG_FILE_THRESHOLD}). "
                  f"Undo with `git -C {vault} config --unset core.compression` and "
                  f"`--unset core.bigFileThreshold`.")
        else:
            _use_profile_for_this_process_only()
            print(f"dry run -- {what}: a real run would store objects uncompressed and "
                  f"skip delta search for big blobs (applied to this preview only, "
                  f"so it stages at the speed a real run would).")
    elif write:
        git(git_dir, vault, ["config", "snapshot.profile", "default"], cwd=vault)
        git(git_dir, vault, ["config", "snapshot.profilebytes", str(int(measure["total"]))], cwd=vault)


#: Head-room kept free after staging: the index, the repack's temporary
#: packfile and whatever else the volume is doing.
_FREE_SPACE_MARGIN_BYTES = 1024 ** 3


def estimate_staging_bytes(
    git_dir: Path, vault: Path, source: Path, excludes: list[str],
    vault_config: dict[str, list[str]], skip: "frozenset[str] | None" = None,
) -> tuple[int, int]:
    """``(tree_bytes, new_bytes)``: the tree's size and what staging will add.

    The estimate is deliberately an upper bound on the *new* data: the tree's
    size, scaled by the sampled compressibility unless objects are stored
    uncompressed, minus what the vault's object store already holds.  A second
    run of an unchanged tree therefore estimates ~0; a first run of a 130 GiB
    tree estimates ~130 GiB.  It can over-estimate (renamed files hash to
    objects the vault already has) but not by enough to matter against a
    refusal that is one flag to override.
    """
    measure = sample_payload(source, excludes, skip)
    total = measure["total"]
    uncompressed = (
        (vault_config.get("snapshot.profile") or [""])[0] == "raw"
        or (vault_config.get("core.compression") or [""])[0] == "0"
        or decide_profile(measure) == "raw"
    )
    ratio = measure["ratio"]
    factor = 1.0 if (uncompressed or not 0.0 < ratio < 1.0) else ratio
    stats = _object_store_stats(git_dir, vault)
    held = (stats.get("size", 0) + stats.get("size-pack", 0)) * 1024
    return int(total), max(0, int(total * factor) - held)


def check_free_space(
    git_dir: Path, vault: Path, source: Path, excludes: list[str],
    vault_config: dict[str, list[str]], *, allow_low_disk: bool = False,
    skip: "frozenset[str] | None" = None,
) -> int:
    """Refuse to start staging a tree the vault's volume cannot hold; else return
    the bytes staging is estimated to add (an upper bound, for the heartbeat).

    ``git add`` writes every new object before anything can be undone, and a
    full volume does not fail cleanly: the vault is left with a partial object
    store and everything else on that disk -- including the Seafile client's
    own data -- starts failing too.  Staging a 130 GiB tree onto a volume with
    60 GiB free is exactly that, and a dry run stages as much as a real one.
    """
    tree, need = estimate_staging_bytes(
        git_dir, vault, source, excludes, vault_config, skip
    )
    free = shutil.disk_usage(vault).free
    if need + _FREE_SPACE_MARGIN_BYTES <= free or allow_low_disk:
        return need
    raise SnapshotError(
        f"refusing to stage: the tree is {human_bytes(tree)} and staging it needs about "
        f"{human_bytes(need)} of new object storage in {vault}, but only {human_bytes(free)} is free "
        f"on that volume (and {human_bytes(_FREE_SPACE_MARGIN_BYTES)} is kept in reserve). "
        "Put the vault on a larger volume with --vault, narrow the tree with --exclude, "
        "or pass --allow-low-disk if the data is far more compressible than the sample "
        "suggests."
    )


def profile_recheck_due(vault_config: dict[str, list[str]], tree_bytes: int) -> bool:
    """Whether a ``default`` vault has grown enough to be worth sampling again.

    True only for a vault decided ``default`` whose tree is now past the size
    where the profile matters and at least twice what it was when last sampled,
    so a large-but-compressible tree is sampled once per doubling, not per run.
    """
    if (vault_config.get("snapshot.profile") or [""])[0] != "default":
        return False
    if tree_bytes < _PROFILE_MIN_TREE_BYTES:
        return False
    try:
        last = int((vault_config.get("snapshot.profilebytes") or ["0"])[0])
    except ValueError:
        last = 0
    return tree_bytes >= 2 * last


#: A temporary pack younger than this may belong to a git process that is still
#: writing it; older ones were abandoned.
_TEMP_PACK_MIN_AGE_SECONDS = 3600


def sweep_temp_packs(git_dir: Path, *, min_age: float = _TEMP_PACK_MIN_AGE_SECONDS) -> int:
    """Delete ``tmp_pack_*`` files an interrupted run left in the object store.

    ``git pack-objects`` and ``repack`` write ``objects/pack/tmp_pack_<random>``
    and rename it when complete; a run that is killed mid-write leaves the file
    behind, and git never removes it (``count-objects`` reports it as "garbage").
    On the flood vault one such file was 22.5 GiB -- nearly half the free space
    on its volume -- and nothing in the tool mentioned it.  Called with the
    vault lock held, so no other snapshot run can be writing one; the age
    threshold covers a git process the user started by hand.  Returns bytes
    freed.
    """
    freed = 0
    cutoff = time.time() - min_age
    pack_dir = git_dir / "objects" / "pack"
    try:
        entries = list(pack_dir.glob("tmp_pack_*"))
    except OSError:
        return 0
    failed: list[str] = []
    for path in entries:
        try:
            st = path.stat()
            if st.st_mtime > cutoff:
                continue
            size = st.st_size
            try:
                path.unlink()
            except PermissionError:
                # Windows refuses to delete a read-only file, and git leaves its
                # temporary packs read-only.  Clear the attribute and retry; if
                # that fails too, something still has the file open.
                os.chmod(path, stat.S_IWRITE)
                path.unlink()
            freed += size
        except OSError as exc:
            failed.append(f"{path.name} ({exc.strerror or exc})")
    if freed:
        print(f"removed leftover temporary pack file(s) from an interrupted run "
              f"({human_bytes(freed)} freed)")
    if failed:
        print("Warning: could not remove leftover temporary pack file(s): " + "; ".join(failed))
    return freed


def maintain_vault(vault: Path, *, full_repack: bool = False) -> int:
    """Housekeep the vault's object store locally.

    Everything here works on ``.git/objects`` alone -- the worktree and the
    source tree are never read for content or written at all, which is what
    makes this safe beside a live snapshot job (the two only contend for git's
    lock).  A full repack reads every object once and writes the new packfile
    next to the old one until it is swapped in, so the free-space check below
    uses the whole store size as the headroom it needs.

    Without *full_repack* this is pure reporting: what is loose vs packed, how
    much that is, and whether a full repack would fit on this volume.  Snapshot
    runs maintain the same store incrementally (loose tier only) before each
    push; this command's ``--full-repack`` is the one-shot reset of it.
    """
    git_dir = vault / ".git"
    if not git_dir.is_dir():
        raise SnapshotError(f"no vault repository at {vault} -- nothing to maintain")

    stats = _object_store_stats(git_dir, vault)
    loose_kb = stats.get("size", 0)
    packed_kb = stats.get("size-pack", 0)
    store_bytes = (loose_kb + packed_kb) * 1024
    free = shutil.disk_usage(vault).free

    print(f"vault object store: {stats.get('count', 0)} loose object(s), "
          f"{human_bytes(loose_kb * 1024)} loose, {human_bytes(packed_kb * 1024)} "
          f"in {stats.get('packs', 0)} packfile(s)")
    if stats.get("garbage", 0):
        print(f"Note: {stats['garbage']} leftover temporary file(s) from an interrupted run "
              f"hold {human_bytes(stats.get('size-garbage', 0) * 1024)} in objects/pack; "
              "a snapshot run (or --maintain --full-repack) removes them")

    profile = _read_config_key(git_dir, "snapshot.profile")
    if profile:
        print(f"storage profile: {profile}"
              + (" (core.compression=0, core.bigFileThreshold="
                 f"{_PROFILE_BIG_FILE_THRESHOLD})" if profile == "raw" else ""))

    if not full_repack:
        need = human_bytes(store_bytes)
        have = human_bytes(free)
        verdict = "fits" if free >= store_bytes else "does NOT fit -- free space first"
        print(f"a full repack would need roughly {need} of headroom; this volume has {have} ({verdict}).")
        print("re-run with --maintain --full-repack to consolidate the whole store.")
        return 0

    if free < store_bytes:
        raise SnapshotError(
            f"refusing to repack: the object store is {human_bytes(store_bytes)} but only "
            f"{human_bytes(free)} is free on this volume; a repack keeps the old packfiles "
            "until the new ones are complete, so it needs roughly that much headroom"
        )

    with VaultLock(git_dir):
        sweep_temp_packs(git_dir)

    print("repacking (this reads every object once; large vaults take a while) ...")
    git(
        git_dir, None, ["repack", "-a", "-d"], cwd=vault,
        status="repacking the vault's object store", measure_dir=git_dir / "objects",
    )
    git(git_dir, None, ["prune"], cwd=vault)

    after = _object_store_stats(git_dir, vault)
    print(f"done: {after.get('count', 0)} loose object(s), "
          f"{human_bytes(after.get('size-pack', 0) * 1024)} in "
          f"{after.get('packs', 0)} packfile(s).")
    return 0
