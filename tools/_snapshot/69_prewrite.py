

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

#: A step up in writers is kept only if throughput grew by this factor.
_RAMP_GAIN = 1.15


def prewrite_workers() -> "tuple[int, bool]":
    """``(ceiling, forced)``: how many ``git hash-object`` processes may write at once.

    Unset, the ceiling is the CPU count (at most 16) and :class:`_Ramp` finds
    the right number below it.  ``SEAFILE_SNAPSHOT_STAGE_WORKERS=N`` is a
    typed setting, so it wins over the measurement: exactly N, no ramp; ``1``
    turns the step off.  A bad value falls back to the default with a warning,
    because a setting from the environment must never fail a run that works.
    """
    default = max(1, min(_PREWRITE_MAX_WORKERS, os.cpu_count() or 1))
    raw = os.environ.get("SEAFILE_SNAPSHOT_STAGE_WORKERS", "").strip()
    if not raw:
        return default, False
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        print(
            f"Warning: SEAFILE_SNAPSHOT_STAGE_WORKERS={raw!r} is not a whole number "
            f"of at least 1; using {default}.",
            file=sys.stderr,
        )
        return default, False
    return value, True


class _Ramp:
    """How many writers to run: grow while it pays, stop when it does not.

    More writers helped a lot on an NVMe drive (41.6 s to 17 s for 20,000
    files) and nothing was measured on a spinning disk, where parallel writers
    and readers can only make the head seek -- and the files are then read a
    second time by ``git add``.  So the count is found on the disk in front of
    us.  It starts at one writer, takes the files-per-second of a window of
    ``2 * level + 1`` batches, and doubles only while the rate grew by
    ``_RAMP_GAIN``.  The first level that does not pay falls back to the best
    one so far; if that is one writer, parallelism buys nothing here and the
    step stops (``gave_up``), leaving the rest to ``git add`` -- the price of
    finding out was a few hundred files read twice.

    Only batches *started* inside a window count towards it, so work left over
    from the previous level cannot flatter the new one.  All times are passed
    in, so a test can feed it any disk it can describe.
    """

    def __init__(self, ceiling: int, forced: bool, now: float) -> None:
        self.ceiling = max(1, ceiling)
        self.level = self.ceiling if forced else 1
        self.settled = forced
        self.gave_up = False
        self.best_level = self.level
        self.best_rate = 0.0
        self.rates: dict[int, float] = {}
        self._reset(now)

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
        rate = self._files / max(ended - self._first_end, 1e-9)
        self.rates[self.level] = rate
        if rate >= self.best_rate * _RAMP_GAIN:
            self.best_rate, self.best_level = rate, self.level
            if self.level >= self.ceiling:
                self.settled = True
            else:
                self.level = min(self.level * 2, self.ceiling)
                self._reset(ended)
        else:
            self.level = self.best_level
            self.settled = True
            self.gave_up = self.best_level == 1


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
    found: dict[str, None] = {}
    for flag in ("-o", "-m"):
        listing = git(git_dir, work_tree, ["ls-files", "-z", flag], cwd=work_tree).stdout
        for entry in listing.split("\0"):
            if entry and not entry.endswith("/"):
                found[entry] = None
    out: list[tuple[str, int]] = []
    for path in found:
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
) -> int:
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
    Returns the number of files handed to workers (0 when the step was skipped).
    """
    ceiling, forced = prewrite_workers()
    if ceiling < 2:
        return 0
    files = _prewrite_candidates(git_dir, work_tree, only)
    if len(files) < _PREWRITE_MIN_FILES:
        return 0
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
        one, two = ramp.rates.get(1, 0.0), ramp.rates.get(2, 0.0)
        print(
            f"staging: parallel writing did not help on this disk ({one:.0f} files/s with one "
            f"process, {two:.0f} with two); the remaining {len(files) - written} file(s) are "
            "left to git add",
            file=sys.stderr,
        )
    elif not forced:
        tried = ", ".join(f"{n}: {r:.0f}" for n, r in ramp.rates.items())
        print(
            f"staging: {'settled on' if ramp.settled else 'was still trying, at'} "
            f"{ramp.level} parallel git process(es) (files/s by process count: {tried})",
            file=sys.stderr,
        )
    return written
