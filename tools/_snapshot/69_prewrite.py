

#: Fewer files than this and the serial ``git add`` is quick enough that the
#: extra processes would only add start-up cost.
_PREWRITE_MIN_FILES = 2000

#: One ``hash-object`` process gets at most this many files or bytes.  Batches
#: are handed out as workers free up, so one slow batch does not hold the rest,
#: they are small enough for :class:`_Ramp` to take a reading every few seconds
#: on a slow disk, and a batch stays well inside ``GIT_TIMEOUT``.
_PREWRITE_BATCH_FILES = 500
_PREWRITE_BATCH_BYTES = 128 * 1024 * 1024

#: The most writers the ramp may try.  On a 24-core NVMe machine 16 and 24 took
#: the same time; the disk, not the CPU, is the limit by then.
_PREWRITE_MAX_WORKERS = 16


def prewrite_workers() -> "tuple[int, bool]":
    """``(ceiling, forced)``: how many ``git hash-object`` processes may write at once.

    Unset, the ceiling is the CPU count (at most 16) and :class:`_Ramp` finds
    the right number below it.  ``SEAFILE_SNAPSHOT_STAGE_WORKERS=N`` is a
    typed setting, so it wins over the measurement: exactly N, no ramp; ``1``
    turns the step off.  A bad value falls back to the default with a warning,
    because a setting from the environment must never fail a run that works.
    """
    default = max(1, min(_PREWRITE_MAX_WORKERS, os.cpu_count() or 1))
    return env_workers("SEAFILE_SNAPSHOT_STAGE_WORKERS",
                       os.environ.get("SEAFILE_SNAPSHOT_STAGE_WORKERS", ""), default)


class _Ramp:
    """How many writers to run: grow while it pays, stop when it does not.

    More writers helped a lot on an NVMe drive (41.6 s to 17 s for 20,000
    files) and nothing was measured on a spinning disk, where parallel writers
    and readers can only make the head seek -- and the files are then read a
    second time by ``git add``.  So the count is found on the disk in front of
    us, by :class:`_Climber`: one writer first, then doubling while the rate
    grows.  If no level beats one writer, parallelism buys nothing here and the
    step stops (``gave_up``), leaving the rest to ``git add`` -- the price of
    finding out was about ten thousand files read twice.

    This driver takes a reading from a window of ``2 * level + 1`` finished
    batches (files per second between the first and last completion).  Only
    batches *started* inside a window count towards it, so work left over from
    the previous level cannot flatter the new one.  All times are passed in, so
    a test can feed it any disk it can describe.
    """

    def __init__(self, ceiling: int, forced: bool, now: float) -> None:
        self._climber = _Climber(ceiling, 1, forced)
        self._reset(now)

    level = property(lambda self: self._climber.level)
    ceiling = property(lambda self: self._climber.ceiling)
    settled = property(lambda self: self._climber.settled)
    gave_up = property(lambda self: self._climber.gave_up)
    best_rate = property(lambda self: self._climber.best_rate)
    rates = property(lambda self: self._climber.rates)

    def _reset(self, now: float) -> None:
        self._window_start = now
        self._files = 0
        self._batches = 0
        self._first_end = now

    def finished(self, started: float, files: int, ended: float) -> None:
        if self.settled or started < self._window_start:
            return
        # The rate is read between the first and the last completion of the
        # window, not from its start: the first batches of a level are still
        # filling the new writers, and timing from the start made 4 writers
        # look no better than 2 on a drive where 8 were 2.3x faster than 1.
        if self._batches == 0:
            self._first_end = ended
        else:
            self._files += files
        self._batches += 1
        if self._batches < 2 * self.level + 1:
            return
        self._climber.result(self._files / max(ended - self._first_end, 1e-9))
        self._reset(ended)


def stall_limit_after_prewrite(seconds: float) -> float:
    """The no-progress limit for the ``git add`` that follows a pre-write.

    The watchdog counts bytes newly present under ``objects``.  After a
    pre-write that final ``add`` writes nothing -- every object is already
    there (measured: the object store is byte-identical before and after) -- so
    on a disk where reading the tree back takes longer than the limit, a
    healthy run would be killed as stalled.  Reading and hashing what was just
    written cannot take much longer than writing it did, so the limit is the
    larger of the usual one and three times the write pass.
    """
    return max(_stall_limit_from_env(), 3.0 * seconds)


def _listed_changes(git_dir: Path, work_tree: Path) -> "dict[str, None]":
    """Untracked (ignored ones included) and modified paths, as ``ls-files`` spells them.

    A nested repository comes back with its trailing slash.  Deleted files are
    listed as modified; whoever uses the result drops them against a list of what
    exists.
    """
    found: dict[str, None] = {}
    for flag in ("-o", "-m"):
        listing = git(git_dir, work_tree, ["ls-files", "-z", flag], cwd=work_tree).stdout
        for entry in listing.split("\0"):
            if entry:
                found[entry] = None
    return found


def paths_to_stage(git_dir: Path, work_tree: Path, kept: list[str]) -> list[str]:
    """The members of *kept* that ``git add`` has anything to do for, in *kept*'s order.

    Handing ``git add`` a path list makes it match every path of the list against
    every path of the index, so the cost grows with the square of the tree:
    measured with every path of an unchanged tree listed, 20,000 files took 3.9 s,
    40,000 took 18 s and 80,000 took 76 s, against 0.1 s for ``git add .`` -- and
    222,000 files, 2,600 of them new, took 22 minutes.  An unchanged tracked file
    needs nothing from ``add`` (its stat data in the index already says so), so
    only the untracked and modified ones are listed.

    ``update-index --refresh`` goes first: a file that was only touched (new
    modification time, same bytes) is brought up to date in the index, as ``add``
    would have, instead of being read again by every later run.
    """
    git(git_dir, work_tree, ["update-index", "-q", "--refresh"], cwd=work_tree, check=False)
    changed = {entry.rstrip("/") for entry in _listed_changes(git_dir, work_tree)}
    return [path for path in kept if path in changed]


#: From this many paths the list goes to ``update-index`` instead of ``add``.  Both
#: hash the same bytes, apply the same filters and record the same stat data (they
#: share ``index_path``), but ``add`` matches every listed path against the index and
#: ``update-index --stdin`` does not.  With the objects already written, a first run
#: on scratch trees took 14.1 s with ``add`` and 10.1 s with ``update-index`` for
#: 20,000 files, and 121.5 s against 43.0 s for 80,000 (identical trees); the
#: first grows faster than the file count, the second with it.
_INDEX_ROUTE_MIN_PATHS = 10_000


def staging_commands(
    paths: list[str], work_tree: Path
) -> "tuple[list[tuple[list[str], str]], list[str]]":
    """``([(git arguments, stdin), ...], drop_first)``: how to record *paths* in the index.

    A short list is one ``add`` with every path, as it always was.  From
    ``_INDEX_ROUTE_MIN_PATHS`` paths on, the files go to ``update-index`` and
    **a path that is a directory on disk still goes to ``add``**: a tracked file
    that was replaced by a directory, which is rare, so that list is short.
    ``update-index`` refuses a directory, and skipping it was worse than refusing:
    ``add`` records a directory that holds its own ``.git`` as a gitlink, which
    the embedded-repository step then replaces with the repository's files, and an
    empty directory (or one holding only excluded files) is how ``add`` learns
    that the old file entry is gone.  Leaving the directory out lost the files of
    a nested repository that took a file's place, and kept the old file for an
    empty directory (both found by running the two routes through the same history
    in ``ExoticReplacementTests``).  The directories run first, so the entries they
    displace are gone before the files are added.

    ``--replace`` is what ``add`` passes too: a file that took the place of a
    directory (or the reverse) drops the entries it displaces.

    A path that no longer exists is left out and returned in *drop_first*, for the
    caller to remove from the index before staging.  The list is made before the
    pre-write, which can run for many minutes, and a scratch file deleted in that
    time failed the whole run (measured: ``add`` stops with ``pathspec 'x' did not
    match any files``, ``update-index`` with ``does not exist and --remove not
    passed``, rc 128 either way).  A file deleted from the tree is a file deleted
    from the snapshot.  ``update-index`` is also given ``--remove``, so one that
    vanishes in the few milliseconds after this check is dropped the same way.
    No path left means no command: ``add`` stops on an empty list.
    """
    use_index = len(paths) >= _INDEX_ROUTE_MIN_PATHS
    files: list[str] = []
    directories: list[str] = []
    drop_first: list[str] = []
    for path in paths:
        try:
            mode = os.lstat(work_tree / path).st_mode
        except (FileNotFoundError, NotADirectoryError):
            drop_first.append(path)
            continue
        except OSError:
            files.append(path)  # unreadable is git's to report, not ours to hide
            continue
        (directories if use_index and stat.S_ISDIR(mode) else files).append(path)
    add = ["add", "-A", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"]
    commands: list[tuple[list[str], str]] = []
    if not use_index:
        if files:
            commands.append((add, "\0".join(files) + "\0"))
        return commands, drop_first
    if directories:
        commands.append((add, "\0".join(directories) + "\0"))
    if files:
        commands.append((["update-index", "--add", "--remove", "--replace", "-z", "--stdin"],
                         "\0".join(files) + "\0"))
    return commands, drop_first


#: ``git add`` fails when a file shrinks or is replaced between its stat and its
#: read.  A file that was being written a moment ago is usually still when the
#: run is a minute old, so the same add is tried again before the run is given up.
_CHANGED_WHILE_READ = ("short read while indexing", "unstable object source data")
_READ_RETRIES = 2
_READ_RETRY_PAUSE_SECONDS = 2.0


def add_retrying(run: "Callable[[], Any]") -> Any:
    """``run()``, tried again (twice, after a pause) when git saw a file change mid-read.

    Only that failure is retried.  The first attempt left no index behind, and
    the objects it wrote are reused, so a retry costs little; a file that is
    still changing on the last try fails the run with git's own message.
    """
    for attempt in range(_READ_RETRIES + 1):
        try:
            return run()
        except SnapshotError as exc:
            text = str(exc)
            if attempt == _READ_RETRIES or not any(m in text for m in _CHANGED_WHILE_READ):
                raise
            named = re.search(r"short read while indexing (.+)", text)
            print(
                f"staging: {named.group(1).strip() if named else 'a file'} changed while git was "
                f"reading it; trying again in {_READ_RETRY_PAUSE_SECONDS:g} s "
                f"({attempt + 1} of {_READ_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(_READ_RETRY_PAUSE_SECONDS)


def _prewrite_candidates(
    git_dir: Path, work_tree: Path, only: "set[str] | None"
) -> list[tuple[str, int]]:
    """``(path, size)`` of the files ``git add`` would have to read and write.

    Untracked files (ignored ones included -- ``add -f`` takes them) and files
    git sees as modified.  An unchanged tracked file is left out: the stat cache
    already knows it, and hashing it here would re-read the whole tree on every
    incremental run.  Only regular files go in: ``hash-object`` follows a
    symlink and would store the target's bytes where ``add`` stores the link's
    text, and a FIFO would hang it.  A name ``--stdin-paths`` cannot carry (a
    newline, or a leading quote it would unquote) is left to ``add``.
    """
    out: list[tuple[str, int]] = []
    for path in _listed_changes(git_dir, work_tree):
        if path.endswith("/"):
            continue
        if only is not None and path not in only:
            continue
        if "\n" in path or "\r" in path or path.startswith('"'):
            continue
        try:
            info = os.lstat(work_tree / path)
        except OSError:
            continue  # vanished since the listing; add will say so if it matters
        if stat.S_ISREG(info.st_mode):
            out.append((path, info.st_size))
    return out


def _prewrite_batches(files: list[tuple[str, int]]) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for path, nbytes in files:
        if current and (
            len(current) >= _PREWRITE_BATCH_FILES or size + nbytes > _PREWRITE_BATCH_BYTES
        ):
            batches.append(current)
            current, size = [], 0
        current.append(path)
        size += nbytes
    if current:
        batches.append(current)
    return batches


def prewrite_objects(
    git_dir: Path,
    work_tree: Path,
    only: "set[str] | None" = None,
    expect_bytes: int = 0,
) -> "tuple[int, float]":
    """Write the loose objects ``git add`` is about to write, several at a time.

    ``git add`` hashes, deflates and writes one file after another.  Measured on
    20,000 files (254 MiB) on NVMe: 41.6 s alone; 21.5 s with 8 processes
    writing the objects first and ``add`` after; 17 s with 16.  The writing is
    most of the cost and parallelises *where the disk allows it* (see
    :class:`_Ramp`); the index update stays in ``add``, which still reads each
    file (and finds its object already there), because only ``add`` records the
    stat data that makes the next run cheap.  The tree it produces is
    byte-identical (compared by ``write-tree``).

    This is an optimisation and nothing depends on it: a batch that fails (a
    file changed or vanished under ``hash-object``) is dropped without a word,
    the unwritten files are written by the ``add`` that follows, and that
    ``add`` owns every error message.  ``only`` limits the step to the paths the
    caller has already decided to stage, so an excluded file is never opened.
    Returns ``(files handed to workers, seconds spent)``; ``(0, 0.0)`` when the step
    was skipped.
    """
    ceiling, forced = prewrite_workers()
    if ceiling < 2:
        return 0, 0.0
    files = _prewrite_candidates(git_dir, work_tree, only)
    if len(files) < _PREWRITE_MIN_FILES:
        return 0, 0.0
    batches = _prewrite_batches(files)
    total = sum(n for _, n in files)
    ceiling = min(ceiling, len(batches))
    print(
        f"staging: writing {len(files)} file(s) ({human_bytes(total)}) with "
        + (f"{ceiling} parallel git processes" if forced
           else f"up to {ceiling} parallel git processes, found by measuring"),
        file=sys.stderr,
    )
    stop = threading.Event()

    def write(batch: list[str]) -> None:
        if stop.is_set():
            return
        try:
            git(
                git_dir, work_tree, ["hash-object", "-w", "--stdin-paths"],
                cwd=work_tree, check=False, stdin="\n".join(batch) + "\n",
            )
        except SnapshotError:
            pass  # a timed-out batch is the serial add's to redo

    objects = git_dir / "objects"
    baseline = _dir_size(objects)
    start = last_line = time.monotonic()
    ramp = _Ramp(ceiling, forced, start)
    queue = list(reversed(batches))
    running: "dict[Any, tuple[float, int]]" = {}
    written = 0
    pool = ThreadPoolExecutor(max_workers=ceiling, thread_name_prefix="grs-prewrite")
    try:
        while running or (queue and not ramp.gave_up):
            while queue and not ramp.gave_up and len(running) < ramp.level:
                batch = queue.pop()
                running[pool.submit(write, batch)] = (time.monotonic(), len(batch))
            done, _ = wait(set(running), timeout=1.0, return_when="FIRST_COMPLETED")
            now = time.monotonic()
            for future in done:
                began, count = running.pop(future)
                written += count
                ramp.finished(began, count, now)
            if now - last_line >= STATUS_INTERVAL:
                last_line = now
                growth = max(0, _dir_size(objects) - baseline)
                sys.stderr.write(
                    _status_line("staging the tree", now - start, growth, objects, expect_bytes)
                    + "\n"
                )
                sys.stderr.flush()
    except BaseException:
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    if ramp.gave_up:
        tried = ", ".join(f"{n}: {r:.0f}" for n, r in ramp.rates.items())
        print(
            f"staging: parallel writing did not help on this disk (files/s by process count: "
            f"{tried}); the remaining {len(files) - written} file(s) are left to git add",
            file=sys.stderr,
        )
    elif not forced:
        tried = ", ".join(f"{n}: {r:.0f}" for n, r in ramp.rates.items())
        print(
            f"staging: {'settled on' if ramp.settled else 'was still trying, at'} "
            f"{ramp.level} parallel git process(es) (files/s by process count: {tried})",
            file=sys.stderr,
        )
    return written, time.monotonic() - start
