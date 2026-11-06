

#: A step up in parallelism is kept only if throughput grew by this factor over
#: the best level so far.
_RAMP_GAIN = 1.15

#: How many levels in a row may fail to beat the best before the search stops.
#: One was not enough on the real server: 2 -> 4 writers gained 3-15% (inside
#: the run-to-run noise) and 8 then gained 30% more, so a search that gave up
#: at the first flat step would have stopped at 2 with 50% of the link unused.
_RAMP_PATIENCE = 2

#: Parallel uploads: the old fixed default is now the first guess, so a short
#: upload is no slower than it was, and the ceiling is what 12 writers showed
#: still helping a little (63-69 MiB/s against 61-63 at 8).
_UPLOAD_START_JOBS = 3
_UPLOAD_MAX_JOBS = 16

#: A window ignores this long after a level change (slots filling, TCP slow
#: start), then reads throughput over this long.  Bytes over time, not files
#: completed: the queue is largest-first, and three completions of a 4 GiB file
#: would take an hour to arrive.
_CLIMB_SETTLE_SECONDS = 4.0
_CLIMB_WINDOW_SECONDS = 10.0

#: What a server says when it is being asked for too much at once.
_THROTTLE_STATUS = re.compile(r"HTTP (429|502|503|504)\b")

#: After a push-back the queue pauses this long, and further push-backs inside
#: this long are the same episode (several requests were in flight together).
_THROTTLE_PAUSE_SECONDS = 3.0
_THROTTLE_EPISODE_SECONDS = 10.0


class _Climber:
    """Decides how much parallelism a machine, a disk or a link can use.

    Fed one reading per level (``result``), it doubles the level while the
    reading beats the best so far by ``_RAMP_GAIN``, stops after
    ``_RAMP_PATIENCE`` levels in a row that did not, and settles on the best
    level seen -- never on the last one tried.  Nothing here reads a clock or a
    disk: the driver supplies the readings, so a test can describe any machine.
    """

    def __init__(self, ceiling: int, start: int = 1, forced: bool = False) -> None:
        self.ceiling = max(1, ceiling)
        self.level = min(max(1, start), self.ceiling)
        if forced:
            self.level = self.ceiling
        self.settled = forced
        self.forced = forced
        self.gave_up = False
        self.best_level = self.level
        self.best_rate = 0.0
        self.rates: dict[int, float] = {}
        self._misses = 0
        self._throttled_at = -1e9

    def result(self, rate: float) -> None:
        """The throughput measured at the current level."""
        self.rates[self.level] = rate
        if rate >= self.best_rate * _RAMP_GAIN:
            self.best_level, self.best_rate = self.level, rate
            self._misses = 0
        else:
            self._misses += 1
        if self._misses >= _RAMP_PATIENCE or self.level >= self.ceiling:
            self.level = self.best_level
            self.settled = True
            self.gave_up = self.best_level <= 1
        else:
            self.level = min(self.level * 2, self.ceiling)

    def throttled(self, now: float) -> bool:
        """The server pushed back at this level: halve it, stop searching.

        Several requests in flight answer together, so a second signal within a
        few seconds is the same episode and changes nothing.  Returns whether
        the level changed.  A typed level (``forced``) is never changed: the item
        is still tried again after the pause, at the level the person chose.
        """
        if self.forced:
            return False
        if now - self._throttled_at < _THROTTLE_EPISODE_SECONDS or self.level <= 1:
            return False
        self._throttled_at = now
        self.ceiling = max(1, self.level // 2)
        self.level = self.best_level = self.ceiling
        self.settled = True
        return True


class _RateWindow:
    """Turns ``(time, bytes so far)`` samples into one throughput reading per level."""

    def __init__(self, settle: "float | None" = None, window: "float | None" = None) -> None:
        # Resolved when used, not at import: a test (or a caller) can still change the module's.
        self._settle_for, self._window_for = settle, window
        self.reset(0.0)

    def reset(self, now: float) -> None:
        self._since = now
        self._armed_at: "float | None" = None
        self._armed_total = 0

    def sample(self, now: float, total: int, saturated: bool) -> "float | None":
        """Bytes per second once a full window has run, else ``None``.

        ``saturated`` is whether every slot is busy and more work is queued; a
        reading taken while slots sit empty (the tail of the queue) says
        nothing about the level, so such a window starts over.
        """
        if not saturated:
            self.reset(now)
            return None
        if self._armed_at is None:
            settle = _CLIMB_SETTLE_SECONDS if self._settle_for is None else self._settle_for
            if now - self._since >= settle:
                self._armed_at, self._armed_total = now, total
            return None
        window = _CLIMB_WINDOW_SECONDS if self._window_for is None else self._window_for
        if now - self._armed_at >= window:
            return (total - self._armed_total) / (now - self._armed_at)
        return None


class _ByteMeter:
    """A running total of bytes handed to the network, shared by every worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total = 0

    def add(self, nbytes: int) -> None:
        with self._lock:
            self.total += nbytes


def throttle_status(text: str) -> "str | None":
    """``"502"`` (or 429, 503, 504) when an error message says the server pushed back."""
    found = _THROTTLE_STATUS.search(text)
    return found.group(1) if found else None


def env_workers(name: str, raw: str, default: int) -> "tuple[int, bool]":
    """``(count, typed)`` from *raw*, the value of environment variable *name*, else the default.

    The caller reads the variable itself, with the name spelled out where it is
    read, so the docs guard that cross-checks documented against consulted
    variables can see it.

    A typed number is a setting, so it beats any measurement (exactly that many,
    no search; ``1`` is the plain serial path).  A bad value falls back to the
    default with a warning, because a setting from the environment must never
    fail a run that works.
    """
    raw = raw.strip()
    if not raw:
        return default, False
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        print(
            f"Warning: {name}={raw!r} is not a whole number of at least 1; using {default}.",
            file=sys.stderr,
        )
        return default, False
    return value, True


def climb_line(climber: _Climber, noun: str) -> str:
    """What the search found, in one line (the run's record of what it chose)."""
    by_count = ", ".join(f"{n}: {r / 1048576:.0f}" for n, r in climber.rates.items())
    head = (f"settled on {climber.level}" if climber.settled
            else f"finished while still measuring, at {climber.level}")
    return f"payload: {head} parallel {noun}(s) (MiB/s by count: {by_count})"


def run_climbing(
    items: "list[str]",
    work: "Callable[[str], tuple[str, str] | None]",
    *,
    ceiling: int,
    meter: _ByteMeter,
    exhausted: "Callable[[str, str], None]",
    start: int = _UPLOAD_START_JOBS,
    noun: str = "upload",
    clock: "Callable[[], float]" = time.monotonic,
    poll: float = 1.0,
    forced: bool = False,
    say: "Callable[[str], None] | None" = None,
    abort: "Callable[[], None] | None" = None,
    settle: "float | None" = None,
    window_seconds: "float | None" = None,
) -> _Climber:
    """Run ``work(item)`` over *items*, with a parallelism that is searched for.

    ``work`` returns ``None`` when it is done with the item (success or a
    failure it recorded itself) or ``("throttled", message)`` when the server
    pushed back.  A throttled item goes to the front of the queue again, at most
    twice, and the level is halved for the rest of the run (when it was above
    one); once the tries are spent ``exhausted(item, message)`` is called.
    One line (to *say*, default ``print``) tells what was found, so a slow run
    can be read afterwards.  ``forced`` runs exactly *ceiling* workers with no
    search.  An exception from ``work`` stops the run: *abort* is called first, so
    workers still running can stop at their next block instead of finishing a
    file that may take minutes.
    """
    if say is None:
        def say(line: str) -> None:
            print(line, flush=True)
    climber = _Climber(ceiling, start, forced)
    window = _RateWindow(settle, window_seconds)
    window.reset(clock())
    queue = list(reversed(items))      # pop() from the end = the front of the order
    running: "dict[Any, str]" = {}
    tries: dict[str, int] = {}
    resume_at = 0.0
    said = False
    pool = ThreadPoolExecutor(max_workers=climber.ceiling, thread_name_prefix=f"grs-{noun}")
    try:
        while queue or running:
            now = clock()
            while queue and len(running) < climber.level and now >= resume_at:
                item = queue.pop()
                running[pool.submit(work, item)] = item
            # Judged here, with the slots just filled: after reaping, a finished
            # upload leaves its slot empty for an instant, and judging then made
            # every completion look like an idle tail and reset the window (the
            # first real run never took a reading).
            saturated = bool(queue) and len(running) >= climber.level
            if running:
                done, _ = wait(set(running), timeout=poll, return_when="FIRST_COMPLETED")
            else:
                time.sleep(min(poll, max(0.01, resume_at - now)))
                done = set()
            now = clock()
            for future in done:
                item = running.pop(future)
                outcome = future.result()
                if outcome is None:
                    continue
                if tries.get(item, 0) < 2:
                    tries[item] = tries.get(item, 0) + 1
                    queue.append(item)
                    if climber.throttled(now):
                        say(
                            f"Note: the server pushed back ({outcome[1][:80]}); "
                            f"limiting this run to {climber.level} parallel {noun}s"
                        )
                        window.reset(now)
                    resume_at = now + _THROTTLE_PAUSE_SECONDS
                else:
                    exhausted(item, outcome[1])
            if not climber.settled:
                rate = window.sample(now, meter.total, saturated=saturated)
                if rate is not None:
                    climber.result(rate)
                    window.reset(now)
                    if climber.settled and not said:
                        said = True
                        say(climb_line(climber, noun))
    except BaseException:
        if abort is not None:
            abort()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if climber.rates and not said:
        say(climb_line(climber, noun))
    return climber
