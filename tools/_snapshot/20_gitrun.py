

#: How long one git subprocess may run before it is killed.  A snapshot holds
#: its vault lock for the whole run, so a git child that never returns blocks
#: every *other* local snapshot as well -- a credential-helper prompt on a
#: scheduled machine, or a server that accepts the connection and then stalls.
#: Long enough for a first push of a large tree, bounded so a hang is an error
#: rather than a wait.  Not a config key, for the same reason the library's own
#: ``GIT_TIMEOUT`` is not one.
GIT_TIMEOUT = 600

#: Long-running steps (staging a large tree, the first push of a large vault,
#: cloning it for a restore) would otherwise be silent for tens of minutes and
#: read as a hang: git prints progress only to a terminal, and this tool runs
#: under schedulers.  While a child marked with ``status=`` is still running,
#: one line per this many seconds goes to stderr instead -- enough to show the
#: step is alive and how far it has got without chattering on short runs.
STATUS_INTERVAL = 60

#: A status-marked step is killed when it shows no measurable progress for this
#: long: bytes newly present under its ``measure_dir``.  That replaces a flat
#: wall-clock timeout, which cannot tell "hashing a 130 GB tree" (fine) from "dead
#: disk" (not): the first keeps making objects appear for an hour, the second
#: makes none.  The limit is a *sliding* one -- every observed growth restarts it.
#:
#: Steps with no measurable progress -- above all the push, whose upload phase
#: is silent while it runs -- are passed ``stall_limit=None`` and are never
#: killed: they get the heartbeat only, and a dead link fails on its own through
#: the helper's per-request HTTP timeout.
#:
#: Overridable with ``SEAFILE_SNAPSHOT_STALL_LIMIT`` (seconds) for a machine whose
#: disks are slow enough that half an hour without a new object is normal.
STALL_LIMIT = 1800


def _stall_limit_from_env() -> float:
    raw = os.environ.get("SEAFILE_SNAPSHOT_STALL_LIMIT", "").strip()
    if not raw:
        return STALL_LIMIT
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        print(
            f"Warning: SEAFILE_SNAPSHOT_STALL_LIMIT={raw!r} is not a positive number "
            f"of seconds; using {STALL_LIMIT}.",
            file=sys.stderr,
        )
        return STALL_LIMIT
    return value


class _UnsetStall:
    """Sentinel meaning "use the stall limit" (distinct from ``None``, which is "never kill")."""

    __slots__ = ()


_UNSET_STALL = _UnsetStall()


def _dir_size(path: Path) -> int:
    """Total bytes of regular files under *path*; unreadable entries count as zero."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _fmt_bytes(num: float) -> str:
    if num >= 1073741824:
        return f"{num / 1073741824:.1f} GiB"
    if num >= 1048576:
        return f"{num / 1048576:.0f} MiB"
    return f"{int(num)} B"


def _fmt_elapsed(seconds: float) -> str:
    if seconds >= 3600:
        hours, rem = divmod(int(seconds), 3600)
        return f"{hours}h{rem // 60:02d}m"
    if seconds >= 60:
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"


def _kill_tree(proc: "subprocess.Popen[Any]") -> None:
    """Kill *proc* and, where the platform lets us, what it started.

    ``git push`` spawns the remote helper, which holds the pipes open; killing
    only the parent would leave ``communicate`` waiting on a child nobody can
    see.  Windows has ``taskkill /T``; elsewhere the parent is killed and the
    pipe drain below is bounded.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run(
    cmd: list[str],
    *,
    status: str | None = None,
    measure_dir: Path | None = None,
    stall_limit: "float | None | _UnsetStall" = _UNSET_STALL,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` for git, failing as a ``SnapshotError`` when it hangs.

    Without ``status`` this is a plain run bounded by ``GIT_TIMEOUT``.

    With ``status`` the child runs under a *no-progress* watchdog instead of a
    wall-clock one, driven from this thread by retrying
    ``communicate(timeout=STATUS_INTERVAL)`` -- which keeps stdin, stdout and
    stderr intact across the timeouts, so no helper threads are needed.  Each
    time the interval passes without the child exiting:

    * one line goes to stderr ("still staging the tree ... 4m12s (9 GiB written
      to objects)"), because nothing else speaks during such a step;
    * bytes newly present under ``measure_dir`` count as progress and restart the
      stall clock;
    * if nothing has grown for ``STALL_LIMIT`` seconds the child (and, on
      Windows, its process tree) is killed and a ``SnapshotError`` raised.

    ``stall_limit=None`` is heartbeat-only mode, for steps with no measurable
    progress (a push's silent upload, a verification that re-reads every file
    and writes nothing): the lines keep coming and nothing is ever killed.
    """
    if status is None:
        kwargs.setdefault("timeout", GIT_TIMEOUT)
        try:
            return subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired:
            raise SnapshotError(
                f"git {' '.join(cmd[1:])} did not finish within {GIT_TIMEOUT:g}s and was killed"
            ) from None

    # isinstance, not ``is``: mypy narrows the union on the former, and the
    # sentinel's type is as good a discriminator as its identity.
    limit = _stall_limit_from_env() if isinstance(stall_limit, _UnsetStall) else stall_limit

    stdin_data = kwargs.pop("input", None)
    if stdin_data is not None and kwargs.get("stdin") != subprocess.PIPE:
        # communicate() can only feed a pipe we opened ourselves.
        kwargs["stdin"] = subprocess.PIPE
    kwargs.pop("timeout", None)
    proc = subprocess.Popen(cmd, **kwargs)

    baseline = _dir_size(measure_dir) if measure_dir is not None else 0
    start = last_progress = time.monotonic()
    last_seen = 0
    first = True
    while True:
        try:
            out, err = proc.communicate(
                input=stdin_data if first else None, timeout=STATUS_INTERVAL
            )
            break
        except subprocess.TimeoutExpired:
            first = False
        now = time.monotonic()
        growth = max(0, _dir_size(measure_dir) - baseline) if measure_dir is not None else 0
        if growth > last_seen:
            last_seen, last_progress = growth, now
        line = f"still {status} ... {_fmt_elapsed(now - start)}"
        if measure_dir is not None and growth > 0:
            line += f" ({_fmt_bytes(growth)} written to {measure_dir.name})"
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        if limit is not None and now - last_progress >= limit:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            raise SnapshotError(
                f"git {' '.join(cmd[1:])} made no measurable progress for "
                f"{limit:g}s and was killed (stalled?)"
            ) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)



#: Variables git uses to pick the repository it operates on; a run launched from
#: a hook or CI step inherits them, and they override --git-dir.  A copy of
#: ``git_remote_seafile.git_util._GIT_REPO_ENV_VARS`` (this file must run
#: without the package installed); tests/test_git_util.py fails if they differ.
_REPO_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_CEILING_DIRECTORIES",
)


def git(
    git_dir: Path,
    work_tree: Path | None,
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    stdin: str | None = None,
    status: str | None = None,
    measure_dir: Path | None = None,
    live_stderr: bool = False,
    stall_limit: "float | None | _UnsetStall" = _UNSET_STALL,
) -> subprocess.CompletedProcess[str]:
    """Run git against an explicit --git-dir/--work-tree pair.

    Passing both explicitly is what keeps this out of the source repository's
    own ``.git``: git never consults it, and never writes to it -- but
    ``--git-dir`` alone does not guarantee that.  A run launched from a git
    hook, alias, or CI step inherits ``GIT_INDEX_FILE``/``GIT_DIR`` in the
    environment, and ``GIT_INDEX_FILE`` has *no* command-line override: index
    operations would go to the caller's index, writing the whole tree
    (ignored files included) into whatever repository launched us.  The
    library's own ``clean_git_env`` exists for exactly this and gc.py uses
    it; the snapshot tool does the same scrubbing here.

    ``GIT_TERMINAL_PROMPT=0`` plus a detached stdin turn a credential prompt
    under a scheduler into a fast, clean failure instead of a hang holding
    the vault lock.

    ``status``/``measure_dir``/``stall_limit`` are passed to :func:`_run` for
    steps that run long enough to look dead (staging, pushing, restoring).  With
    ``live_stderr`` the child's stderr is inherited, so a push shows git's and
    the remote helper's own output as it happens -- including the helper's
    notices.  Callers must tolerate ``proc.stderr`` being ``None`` on failure
    (the text already reached the user directly).
    """
    cmd = ["git", f"--git-dir={git_dir}"]
    if work_tree is not None:
        cmd.append(f"--work-tree={work_tree}")
    cmd.extend(args)
    env = dict(os.environ)
    for var in _REPO_ENV_VARS:
        env.pop(var, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Every path this tool hands git is meant literally.  Without this, the
    # entries fed through `--pathspec-from-file` are *pathspecs*: a file named
    # `a[b].txt` also matches an unrelated `ab.txt` and both get staged, and a
    # name that reads as magic (`*.log`, `:x`) can make `git add` fail outright
    # rather than stage the file.  The exclude pre-filter has already decided
    # exactly which paths may be staged, so glob semantics can only add
    # mistakes -- and a mistake here means an excluded file's bytes reaching the
    # vault, which is the one thing the pre-filter exists to prevent.
    env["GIT_LITERAL_PATHSPECS"] = "1"
    # A child that runs the remote helper (push, fetch, clone) inherits the
    # helper's own per-command ceiling, which defaults to 300 s and is meant for
    # an interactive `git push`.  This tool's no-progress watchdog is the guard
    # for long steps, so the helper's wall-clock ceiling is lifted to the same
    # horizon -- unless the user set one, which always wins.
    env.setdefault("SEAFILE_GIT_TIMEOUT", str(int(STALL_LIMIT)))
    proc = _run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        stdout=subprocess.PIPE,
        stderr=None if live_stderr else subprocess.PIPE,
        status=status,
        measure_dir=measure_dir,
        stall_limit=stall_limit,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        hint = ""
        if "index.lock" in detail and "exist" in detail:
            hint = (
                "\nA git process left its lock behind (the earlier run was probably killed). "
                "If no git process is running on this vault, delete the lock file named "
                "above and re-run; the vault itself is not damaged."
            )
        raise SnapshotError(
            f"git {' '.join(args)} failed (rc={proc.returncode})\n{detail}{hint}"
        )
    return proc
