

_KERNEL32: "object | None" = None


def _win_kernel32():
    """Build (once) a properly prototyped kernel32 for the pid probe."""
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (
            ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong,
        )
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
        )
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        )
        _KERNEL32 = kernel32
    return _KERNEL32


def process_start_token(pid: int) -> str | None:
    """An opaque value that changes if ``pid`` is later given to another process.

    A pid alone cannot say whether the run that took the lock is the process
    now holding that number: Windows recycles them quickly, so a crashed
    nightly run's pid soon belongs to something long-lived, which then looked
    "alive" and blocked every later run.  The process's creation time is what
    tells them apart.  ``None`` where it cannot be read (the lock then behaves
    as before).
    """
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = _win_kernel32()
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
                ok = kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user),
                )
                return str(created.value) if ok else None
            finally:
                kernel32.CloseHandle(handle)
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # the command name is in parentheses and may hold spaces: split after it
        fields = stat_line.rpartition(")")[2].split()
        return fields[19]  # field 22 of proc(5): start time in clock ticks since boot
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` is a running process *on this machine*.

    ``os.kill(pid, 0)`` is the POSIX idiom, but on Windows ``os.kill`` is
    ``TerminateProcess`` for every signal except the two console events -- so
    probing a pid that way would *kill* it.  Windows goes through the documented
    API instead, and treats "cannot tell" as alive so a lock is never stolen on
    a guess.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # Same prototype discipline as git_remote_seafile.lock._is_pid_alive:
        # ctypes.windll.kernel32.GetLastError() can return a stale value
        # (ctypes' own foreign calls may clobber the thread's last error
        # between OpenProcess failing and this read), and without restype the
        # 64-bit HANDLE truncates to c_int -- either defect can read a *live*
        # lock holder as dead and let a concurrent run steal its lock.  Built
        # and prototyped once at import, not per call (SNAP-10): every probe
        # was paying the four function-pointer reconfigurations.
        kernel32 = _win_kernel32()
        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            # A live process owned by another (elevated) user denies
            # OpenProcess outright; treating that as "dead" would let a
            # concurrent run steal its lock.  Only "no such process" proves
            # death -- anything else reads as alive (see the docstring).
            return ctypes.get_last_error() != error_invalid_parameter  # type: ignore[attr-defined,unused-ignore]
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        # A pid too large for the OS's pid_t (the stale-lock fixture uses
        # 2**32-1 to mean "certainly dead") cannot name a live process.  This
        # killed every POSIX CI run at v0.8.0 with an OverflowError before the
        # reclaim could happen; Windows masked it, because OpenProcess takes a
        # DWORD and simply fails for such a pid.
        return False
    return True


class VaultLock:
    """An exclusive lock so two snapshots cannot interleave.

    The lock records the pid and hostname that took it, so one left behind by a
    killed run can be recognised and reclaimed instead of deadlocking the vault
    until someone deletes the file by hand.  Reclamation only happens when the
    recorded host is *this* host: a pid from another machine means nothing here,
    and guessing would be worse than refusing.
    """

    def __init__(self, git_dir: Path) -> None:
        self.path = git_dir / "seafile-snapshot.lock"
        self._fd: int | None = None

    def holder(self) -> tuple[str, int, str | None] | None:
        """The lock file's recorded holder, read without touching it (--status)."""
        return self._holder()

    def _holder(self) -> tuple[str, int, str | None] | None:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        parts = text.split()
        if len(parts) < 2:
            return None
        try:
            return parts[0], int(parts[1]), (parts[2] if len(parts) > 2 else None)
        except ValueError:
            return None

    def _reclaim_if_stale(self) -> bool:
        holder = self._holder()
        if holder is None:
            # An empty or unreadable lock file is a run that died between
            # creating it and writing its identity.  A *live* run's lock is
            # written immediately after creation, so a fresh unparseable lock
            # may be a run mid-write -- require some age before reclaiming.
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            if age < _UNPARSEABLE_LOCK_GRACE_SECONDS:
                return False
            try:
                self.path.unlink()
            except OSError:
                # Not just FileNotFoundError: a Windows scanner holding a
                # transient read lock raises PermissionError, and crashing the
                # reclaim over it would defeat the point of reclaiming.  Leave
                # the file; the next run tries again.
                return False
            print("reclaiming an unparseable lock file left behind earlier "
                  f"(that run is gone): {self.path}")
            return True
        host, pid, started = holder
        if host != socket.gethostname():
            return False
        if pid_is_alive(pid):
            # A live pid is the holder only if it is the *same process*: one
            # whose start time differs is a recycled pid, and the run that took
            # this lock is gone.  Without a recorded or readable start time we
            # keep the old, cautious answer.
            now = process_start_token(pid) if started else None
            if not started or now is None or now == started:
                return False
        try:
            self.path.unlink()
        except OSError:
            # Same breadth as __exit__'s retry loop: a sharing violation is a
            # "not this time", not a crash.
            return False
        print(
            f"reclaiming a stale lock left by pid {pid} on this machine "
            f"(that run is gone)"
        )
        return True

    def __enter__(self) -> "VaultLock":
        for attempt in (1, 2):
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if attempt == 2 or not self._reclaim_if_stale():
                    holder = self._holder()
                    who = (
                        f"{holder[0]} pid {holder[1]}"
                        if holder
                        else "an unknown run"
                    )
                    raise SnapshotError(
                        f"another snapshot is running ({who}); lock: {self.path}.\n"
                        f"If you are certain that run is dead, delete the lock "
                        f"file and retry."
                    ) from None
        else:  # pragma: no cover - the loop always breaks or raises
            raise SnapshotError(f"could not take the lock: {self.path}")
        token = process_start_token(os.getpid())
        try:
            os.write(
                self._fd,
                f"{socket.gethostname()} {os.getpid()}{' ' + token if token else ''}".encode(),
            )
            os.fsync(self._fd)
        except OSError as exc:
            # A full disk or a failing share: without this the descriptor
            # leaked and an empty lock file stayed behind until its grace
            # period ran out, blocking the next run for that long.
            os.close(self._fd)
            self._fd = None
            try:
                self.path.unlink()
            except OSError:
                pass
            raise SnapshotError(
                f"could not record the lock holder in {self.path}: {exc}"
            ) from exc
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # A Windows scanner can hold the just-closed file for a moment, and a
        # sharing violation here would report a *successful* run as failed.
        # Retry briefly; if it still will not unlink, leave it -- the stale
        # reclaim above recognises it on the next run.
        for _ in range(5):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.05)
