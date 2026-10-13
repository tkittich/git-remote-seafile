"""lock.py - Cooperative mutex locking for git-remote-seafile."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
import uuid
from typing import Any
from .client import SeafileClient
from .config import RemoteConfig, default_lock_settle, get_git_config_int


class RepositoryLockedError(Exception):
    pass


#: Outcome of re-verifying the lease.  Both non-HELD values fence destructive
#: steps, but they are different events and the user must be told which one
#: happened: LEASE_LOST means another client provably holds the lock (our
#: ticket is gone or carries a foreign nonce), while LEASE_UNKNOWN means we
#: could not read or rewrite our own ticket, so ownership is merely unproven.
#: Reporting a network blip as "lost lock ownership" sends the user hunting for
#: a competing push that never happened.
LEASE_HELD = "held"
LEASE_LOST = "lost"
LEASE_UNKNOWN = "unknown"
#: Our own ticket is still in the queue and still ours, but another active
#: ticket has been granted the lock ahead of it.  Kept distinct from LEASE_LOST
#: on purpose: LOST means our ticket is *gone*, so the user should go looking
#: for the client that reaped it, while PREEMPTED means our ticket is still
#: there and the queue says someone else holds -- an ordering failure, not a
#: lapse, and the two send an operator to different places.
LEASE_PREEMPTED = "preempted"


def describe_ownership_failure(state: str) -> str:
    """Phrase a failed ownership re-check in terms of what actually happened."""
    if state == LEASE_LOST:
        return "another client took over the lock"
    if state == LEASE_PREEMPTED:
        return "another client was granted the lock while this ticket was still held"
    if state == LEASE_UNKNOWN:
        return "the lock ticket could not be read or rewritten, so ownership is unproven"
    return "lock ownership could not be verified"


#: How often a *waiting* client re-sends its ticket to keep the lease fresh.
#: Bounded by a third of the lease, so a queue that is slow to drain cannot let
#: a waiter's own ticket expire out from under it.
_TICKET_REFRESH_SECONDS = 20.0

#: How long a queue scan stays good for the fence in verify_ownership().
#:
#: The fence runs before *every* destructive step, and the delete path calls it
#: once per deleted ref, so re-listing the queue on each call turns an N-ref
#: `push --delete` into N listings.  Rate-limiting the scan costs the fence
#: nothing that matters: the takeover it exists to catch is one where another
#: client holds while our own ticket is still alive, and that condition lasts
#: as long as the other client holds it -- a scan from under a second ago
#: answers it.  The lease itself is still re-proven on every single call, since
#: renew() reads and rewrites the ticket; a ticket that has been *reaped* is
#: therefore caught immediately rather than once per window.
_FENCE_REUSE_SECONDS = 1.0


def _queue_key(ticket: dict[str, Any]) -> tuple:
    """Sort key for the ticket queue: granted holders first, then FIFO.

    The holder rank has to be the *primary* key rather than one more element of
    the FIFO tuple.  ``order_ts`` is a client-side estimate of server time
    (local clock + the median offset implied by the ``Date`` header, which has
    one-second resolution), which is all an expiry check needs -- approximate
    shared time -- but it cannot decide who *holds*, because two estimates half
    a second apart are indistinguishable from a genuinely earlier arrival.  A
    newcomer whose estimate came out behind an active holder's therefore sorted
    ahead of it and acquired the lock while the holder was mid-push.

    Among tickets at the same rank the FIFO tuple still decides, so the order
    stays total and every client sorts the same set the same way.
    """
    return (0 if ticket.get("holding") else 1, *ticket.get("_order_key", (0.0, 0.0, "")))


def _f(val: Any) -> float:
    """Coerce a JSON value to float, treating garbage as 0.0 (never raise)."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


_LOCAL_MACHINE_ID: str = hex(uuid.getnode())


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            # A dedicated WinDLL instance with use_last_error=True is the
            # documented way to read the failing call's error code:
            # ctypes.windll.kernel32.GetLastError() can return a stale value
            # because ctypes' own internal foreign calls between OpenProcess
            # and this call may clobber the thread's last error -- and a stale
            # read here misclassifies a *live* holder as dead, which lets the
            # fast-reclaim path steal an active lock.  Proper prototypes also
            # matter: without restype, HANDLE (64-bit) truncates to c_int and
            # a truncated handle can read as 0 (same steal) or close an
            # unrelated kernel object in CloseHandle.
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_ACCESS_DENIED = 5
            ERROR_INVALID_PARAMETER = 87
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
            )
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = (
                ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            )
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
            )
            if not handle:
                err = ctypes.get_last_error()
                # Access denied means the process exists but is protected --
                # alive for our purposes.  An invalid PID reads as dead.
                if err == ERROR_ACCESS_DENIED:
                    return True
                if err == ERROR_INVALID_PARAMETER:
                    return False
                # Unknown failure: answer "alive" and let the lease expire
                # rather than reclaim on a guess.
                return True
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return True


class RemoteLock:
    """Cooperative distributed lease lock stored on Seafile.

    Prevents race conditions if multiple machines attempt to push to the
    same repository concurrently. Includes automatic lease expiration so
    stale locks from crashed processes do not permanently block pushes.
    Uses the ticket-based distributed protocol (.git-lock.d/<nonce>.json)
    with server-side timestamp ordering and fast stale-lock reclamation for
    dead local processes.

    A win is confirmed by a second read of the queue one settlement window
    later (one second; not a user setting), so a peer that is still
    uploading during the first read cannot win a lock this client also
    claims.

    This protocol coordinates exclusively through the ticket queue.  The
    v0.1–v0.3 single-file mirror (.git-lock.json) is gone, and a helper that
    old cannot see these tickets at all: do not mix versions on one repository.
    """

    def __init__(
        self,
        client: SeafileClient,
        repo_id: str,
        repo_path: str,
        timeout: int | None = None,
        lease: int | None = None,
        config: RemoteConfig | None = None,
        settle: float | None = None,
    ):
        self.client = client
        self.repo_id = repo_id
        self.repo_path = repo_path.rstrip("/")
        self.lock_dir = f"{self.repo_path}/.git-lock.d"
        if config is not None:
            if timeout is None:
                timeout = config.lock_timeout
            if lease is None:
                lease = config.lock_lease
            if settle is None:
                settle = config.lock_settle
        if timeout is None:
            timeout = get_git_config_int("seafile.locktimeout", 15)
        if lease is None:
            lease = get_git_config_int("seafile.locklease", 60)
        if settle is None:
            settle = default_lock_settle()
        self.timeout = timeout
        self.lease = lease
        #: Settlement window in seconds; 0 disables the confirming re-scan.
        self.settle = max(0.0, float(settle))
        self.acquired = False
        self._identity: tuple[str, str] | None = None
        self._nonce: str | None = None
        self._last_renewed: float = 0.0
        #: Why the last renew()/verify_ownership() held or failed; see
        #: ownership_state() and describe_ownership_failure().
        self._last_ownership_state: str = LEASE_UNKNOWN
        # FIFO key captured once at acquire time and preserved by renew():
        # renewal rewrites the ticket file, which bumps its mtime, so the queue
        # must not be ordered on mtime for tickets that carry this field.
        self._order_ts: float = 0.0
        #: True once acquire() has been granted the lock and published the
        #: ``holding`` marker on our ticket.  Every renewal preserves it: the
        #: renewal rewrites the ticket body, so a marker dropped there would
        #: disappear exactly when the fence needs it.
        self._holding: bool = False
        #: Cache for verify_ownership()'s queue scan, so a delete loop that
        #: fences once per ref does not list the queue once per ref.
        self._preemptor_cached: dict[str, Any] | None = None
        self._preemptor_checked_at: float | None = None

    def _owner_id(self) -> str:
        """A stable, non-secret identifier for the credentials in use."""
        tok = getattr(self.client, "token", None)
        if not tok:
            return "user"
        return hashlib.sha256(str(tok).encode("utf-8")).hexdigest()[:8]

    def _get_server_time(self) -> float:
        if isinstance(self.client, SeafileClient):
            return self.client.get_server_time()
        return time.time()

    def _ticket_payload(self, now: float, holding: bool) -> dict[str, Any]:
        """The body of our lock ticket.

        One builder for all three writers -- the upload that enters the queue,
        the rewrite that marks the winner, and every renewal.  They each used to
        carry their own copy of this dict, and that is exactly how a field can
        be added to the winning ticket and then silently dropped by the next
        renewal: the marker would be gone by the time the fence read it, and no
        single-shot test would notice, because the ticket still looks perfectly
        well-formed.
        """
        owner, machine = (
            self._identity if self._identity else (self._owner_id(), socket.gethostname())
        )
        return {
            "owner": owner,
            "machine": machine,
            "machine_id": _LOCAL_MACHINE_ID,
            "nonce": self._nonce,
            "timestamp": now,
            "order_ts": self._order_ts,
            "lease": self.lease,
            "pid": os.getpid(),
            "holding": holding,
            "held_since": now if holding else 0.0,
        }

    def _is_lock_active(self, info: dict[str, Any], now: float, quiet: bool = False) -> bool:
        """Check if lock payload is active. Returns False if expired or dead local PID.

        ``quiet`` suppresses the dead-PID reclaim notice.  That message belongs
        to *acquire*, which is about to take the lock over and should say so;
        verify_ownership() only asks whether a ticket still counts, and it asks
        once per destructive step, so announcing a reclaim there would print
        the same line over and over for a decision nobody is making.
        """
        lock_machine = info.get("machine", "")
        lock_machine_id = info.get("machine_id")
        lock_pid = info.get("pid")
        try:
            lock_pid_int = int(lock_pid) if lock_pid is not None else 0
        except (ValueError, TypeError):
            lock_pid_int = 0

        # Reclaim a lock whose owner is provably gone: same machine, and a PID
        # that is no longer alive.  Both the hostname *and* machine_id (when
        # available) are checked, so a container, or a fleet of machines sharing
        # a default hostname, cannot make another host's live lock look dead
        # here.
        same_machine = (lock_machine == socket.gethostname())
        if same_machine and lock_machine_id is not None:
            same_machine = (str(lock_machine_id) == _LOCAL_MACHINE_ID)

        if same_machine and lock_pid_int > 0:
            if not _is_pid_alive(lock_pid_int):
                if not quiet:
                    sys.stderr.write(
                        f"Reclaiming stale lock from dead local process (PID {lock_pid_int}).\n"
                    )
                    sys.stderr.flush()
                return False

        try:
            lock_time = float(info.get("timestamp", 0))
            lock_lease = float(info.get("lease", self.lease))
        except (ValueError, TypeError):
            lock_time = 0.0
            lock_lease = 0.0

        expires_at = lock_time + lock_lease
        return now < expires_at

    def _scan_tickets(self, now: float, reap: bool = True) -> list[dict[str, Any]]:
        active_tickets: list[dict[str, Any]] = []
        entries = self.client.list_dir(self.repo_id, self.lock_dir)
        if not isinstance(entries, list):
            entries = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            if not name.endswith(".json"):
                continue
            ticket_path = f"{self.lock_dir}/{name}"
            raw = self.client.get_file_text(self.repo_id, ticket_path)
            if not raw:
                continue
            try:
                t_info = json.loads(raw)
            except Exception:
                continue
            if not isinstance(t_info, dict):
                continue

            t_nonce = t_info.get("nonce") or name[:-5]
            t_info["nonce"] = t_nonce

            if reap and t_nonce != self._nonce:
                if not self._is_lock_active(t_info, now):
                    # Stale or dead local PID; clean up ticket file
                    try:
                        self.client.delete_entry(self.repo_id, ticket_path)
                    except Exception:
                        pass
                    continue

            t_info["_mtime"] = _f(entry.get("mtime"))
            t_info["_path"] = ticket_path
            active_tickets.append(t_info)

        # FIFO ordering: each ticket's own acquisition time (``order_ts``).
        # renew() rewrites the ticket file, so an mtime-ordered queue would move
        # a healthy holder to the back of the queue on every refresh.  A ticket
        # with no ``order_ts`` (written by a client older than v0.6.3, which the
        # docs say must not be mixed with this one) falls back, *for that
        # ticket*, to its own timestamp and then its mtime -- there is no
        # queue-wide mode switch to reason about.
        #
        # The nonce is the final tie-break, and for our own ticket it is a
        # random uuid4.  That is safe -- the key is total, and once both
        # tickets are visible (the settlement window's job) every contender
        # sorts the same set the same way and so agrees on the winner -- but it
        # is not fair, and on Windows through Python 3.12 it is reached far
        # more often than the clock's 15.625 ms granularity suggests: two
        # acquisitions inside one tick tie on order_ts *and* on the integer
        # mtime, leaving the nonce to decide.  Fairness below the clock's
        # resolution is not on offer, so the tie-break stays; what must not
        # change is the determinism, because that is what stops a tie from
        # producing two winners.
        for t in active_tickets:
            mtime = _f(t.get("_mtime"))
            stamp = _f(t.get("order_ts")) or _f(t.get("timestamp")) or mtime
            t["_order_key"] = (stamp, mtime, t["nonce"])

        return active_tickets

    def _candidates(
        self,
        tickets: list[dict[str, Any]],
        lock_payload: dict[str, Any],
        nonce: str,
        now: float,
    ) -> list[dict[str, Any]]:
        """The queue with our own ticket merged in, ordered earliest-first.

        Our ticket is merged rather than trusted to the scan: the upload has
        already succeeded, so we *know* it exists, and a scan that does not
        show it (a listing that has not caught up, a mock that returns
        nothing) must not be read as "we are not in the queue" -- that would
        send the caller round the retry loop for a ticket that is already
        there.
        """
        if not any(t.get("nonce") == nonce for t in tickets):
            my_ticket = dict(lock_payload)
            my_ticket["_mtime"] = 0.0
            my_ticket["_order_key"] = (self._order_ts, 0.0, nonce)
            my_ticket["_path"] = f"{self.lock_dir}/{nonce}.json"
            tickets.append(my_ticket)
        tickets.sort(key=_queue_key)
        return tickets

    def acquire(self) -> None:
        start_time = time.monotonic()
        hostname = socket.gethostname()
        owner = self._owner_id()
        nonce = uuid.uuid4().hex
        self._identity = (owner, hostname)
        self._nonce = nonce
        self._order_ts = self._get_server_time()

        # The wait is bounded by `timeout` *since the queue last moved*, not
        # since this call began.  Measured against a real server, six clients
        # pushing at once each wait for the ones ahead of them: the sixth waited
        # 13.4 s of a 15 s budget, and a seventh would have been told "locked"
        # while the queue was draining normally.  A holder changing, or this
        # ticket moving up, is progress and restarts the clock; a queue that is
        # stuck behind one holder still times out after `timeout`.  An absolute
        # cap keeps a pathological queue from holding a push forever.
        anchor = start_time
        last_winner: str | None = None
        last_position: int | None = None
        hard_cap = max(self.timeout * 10, 120.0) if self.timeout > 0 else 0.0
        last_upload: float | None = None
        refresh = min(_TICKET_REFRESH_SECONDS, max(1.0, self.lease / 3.0))

        def timed_out() -> bool:
            mono = time.monotonic()
            if mono - anchor >= self.timeout:
                return True
            return bool(hard_cap) and mono - start_time >= hard_cap

        try:
            while True:
                now = self._get_server_time()

                # 1. Upload ticket.  We are not a holder yet: the marker is
                # published only once the queue agrees we won (below), so that
                # "holding" always means "granted" and never "asked".
                self._holding = False
                lock_payload = self._ticket_payload(now, holding=False)
                # Uploaded once, and again only to keep the lease from lapsing
                # while queued.  Every upload is a commit in the library's
                # history, so re-sending an unchanged ticket on every poll made a
                # waiting client the loudest thing in the queue.
                if last_upload is None or time.monotonic() - last_upload >= refresh:
                    payload_bytes = json.dumps(lock_payload).encode("utf-8")
                    try:
                        self.client.upload_file(
                            self.repo_id,
                            self.lock_dir,
                            f"{nonce}.json",
                            payload_bytes,
                            replace=True,
                        )
                    except Exception as ex:
                        if isinstance(ex, RepositoryLockedError):
                            raise
                        if timed_out():
                            raise RepositoryLockedError(f"Failed to acquire lock: {ex}") from ex
                        time.sleep(2)
                        continue
                    last_upload = time.monotonic()

                # 3. Check candidate tickets in .git-lock.d
                try:
                    tickets = self._scan_tickets(now)
                except Exception as ex:
                    if isinstance(ex, RepositoryLockedError):
                        raise
                    if timed_out():
                        raise RepositoryLockedError(f"Failed to scan lock tickets: {ex}") from ex
                    sys.stderr.write(f"Transient error scanning lock tickets ({ex}); retrying...\n")
                    sys.stderr.flush()
                    time.sleep(2)
                    continue

                queue = self._candidates(tickets, lock_payload, nonce, now)
                winner = queue[0]

                if winner.get("nonce") == nonce and self.settle > 0:
                    # D21 settlement window.  A single scan cannot be trusted:
                    # a peer that starts before us can still be uploading when
                    # we read the queue, so we can see only ourselves and win,
                    # while the peer commits a moment later with an *earlier*
                    # order_ts, reads the queue (now showing both), wins too,
                    # and we both push.  order_ts is captured before the upload
                    # precisely so that the queue order is stable once both
                    # tickets are visible -- so wait one round-trip's worth of
                    # time for any in-flight peer to land, then read it again
                    # and let the same comparison decide for both of us.
                    time.sleep(self.settle)
                    try:
                        settled = self._scan_tickets(self._get_server_time())
                    except Exception as ex:
                        if timed_out():
                            raise RepositoryLockedError(
                                f"Failed to confirm lock ownership: {ex}"
                            ) from ex
                        sys.stderr.write(
                            f"Transient error confirming lock ownership ({ex}); retrying...\n"
                        )
                        sys.stderr.flush()
                        continue
                    queue = self._candidates(settled, lock_payload, nonce, now)
                    winner = queue[0]

                if winner.get("nonce") == nonce:
                    # Publish the holding marker before claiming anything.  This
                    # is the fix for the ordering race: from here on the queue
                    # sorts us ahead of every newcomer, because "was granted"
                    # now outranks "estimated an earlier server time".  A
                    # rewrite that fails leaves us unmarked, which is exactly
                    # the pre-fix behaviour and no worse, so retry rather than
                    # push with a marker no other client can see.
                    try:
                        self.client.upload_file(
                            self.repo_id,
                            self.lock_dir,
                            f"{nonce}.json",
                            json.dumps(self._ticket_payload(now, holding=True)).encode("utf-8"),
                            replace=True,
                        )
                    except Exception as ex:
                        if isinstance(ex, RepositoryLockedError):
                            raise
                        if timed_out():
                            raise RepositoryLockedError(
                                f"Failed to publish lock ownership marker: {ex}"
                            ) from ex
                        sys.stderr.write(
                            f"Transient error publishing lock ownership marker ({ex}); retrying...\n"
                        )
                        sys.stderr.flush()
                        time.sleep(2)
                        continue
                    self._holding = True
                    self._preemptor_cached = None
                    self._preemptor_checked_at = None
                    self.acquired = True
                    self._last_renewed = time.monotonic()
                    return
                else:
                    # Another ticket won
                    winner_owner = winner.get("owner", "another user")
                    winner_mach = winner.get("machine", "another machine")
                    try:
                        w_time = float(winner.get("timestamp", 0))
                        w_lease = float(winner.get("lease", self.lease))
                    except (ValueError, TypeError):
                        # Cannot fire while acquire() only scans with reap=True,
                        # and that is worth stating rather than leaving a reader
                        # to wonder: _is_lock_active parses these same two
                        # fields with the same call, and its single `except`
                        # zeroes *both* on any failure -- so a ticket with a
                        # malformed field is already reaped and never becomes a
                        # winner.  Verified: a ticket with a future timestamp
                        # and a garbage lease is reaped, not treated as active.
                        #
                        # Kept because the premise is real one call away:
                        # _scan_tickets(reap=False) -- the lock_status path --
                        # appends tickets *without* the active check, so a
                        # malformed one does come back that way.  This is the
                        # only thing between such a ticket and a ValueError
                        # escaping acquire() if the scan is ever reused here.
                        w_time = 0.0
                        w_lease = 0.0
                    winner_exp = w_time + w_lease
                    position = next(
                        (i for i, t in enumerate(queue) if t.get("nonce") == nonce), len(queue)
                    )
                    winner_nonce = winner.get("nonce")
                    if (last_winner is not None and winner_nonce != last_winner) or (
                        last_position is not None and position < last_position
                    ):
                        anchor = time.monotonic()
                    last_winner, last_position = winner_nonce, position
                    if timed_out():
                        raise RepositoryLockedError(
                            f"Repository is locked by '{winner_owner}' on '{winner_mach}'. "
                            f"Lock expires in {max(0, int(winner_exp - now))}s."
                        )
                    sys.stderr.write(f"Repository locked by ticket from {winner_mach}; waiting for lock...\n")
                    sys.stderr.flush()
                    time.sleep(2)
                    continue
        finally:
            if not self.acquired and self._nonce:
                try:
                    self.client.delete_entry(self.repo_id, f"{self.lock_dir}/{self._nonce}.json")
                except Exception:
                    pass

    def renew_state(self) -> str:
        """Re-verify and refresh the lease, returning why it held or failed.

        Returns LEASE_HELD, LEASE_LOST or LEASE_UNKNOWN.  The ticket is read
        back before it is rewritten.  The nonce names the ticket file and only
        this process ever writes it, so a ticket that is gone or carries a
        foreign nonce means this lease lapsed and was reaped -- most plausibly by
        another client that now holds the lock.  Re-uploading anyway would
        resurrect the ticket and claim ownership we no longer have, so this
        fails closed.  A read or write *failure* is reported as UNKNOWN, not
        LOST: we cannot prove either way, and the two send the user to different
        places.
        """
        if not self.acquired or not self._nonce:
            state = LEASE_LOST
        else:
            state = self._refresh_lease()
        self._last_ownership_state = state
        return state

    def _refresh_lease(self) -> str:
        try:
            current = self.client.get_file_text(self.repo_id, f"{self.lock_dir}/{self._nonce}.json")
        except Exception as ex:
            sys.stderr.write(f"Warning: could not re-read lock ticket ({ex}); ownership unproven.\n")
            sys.stderr.flush()
            return LEASE_UNKNOWN

        if not current:
            sys.stderr.write(
                "Warning: Lock ticket is gone; this lease lapsed and another client may hold the lock. "
                "Not renewing.\n"
            )
            sys.stderr.flush()
            return LEASE_LOST

        try:
            current_nonce = json.loads(current).get("nonce")
        except Exception:
            sys.stderr.write(
                "Warning: Lock ticket is unreadable; refusing to renew rather than guess at ownership.\n"
            )
            sys.stderr.flush()
            return LEASE_UNKNOWN

        if current_nonce != self._nonce:
            sys.stderr.write("Warning: Lock ticket was taken over; not renewing.\n")
            sys.stderr.flush()
            return LEASE_LOST

        now = self._get_server_time()
        # Built by the one builder, with `holding` carried through.  A renewal
        # that rewrote this ticket without the marker would quietly un-grant the
        # lock while we still hold it, and the next newcomer's scan would be
        # free to take it.
        payload_bytes = json.dumps(self._ticket_payload(now, holding=self._holding)).encode("utf-8")
        try:
            self.client.upload_file(
                self.repo_id,
                self.lock_dir,
                f"{self._nonce}.json",
                payload_bytes,
                replace=True,
            )
            self._last_renewed = time.monotonic()
            return LEASE_HELD
        except Exception as ex:
            sys.stderr.write(f"Warning: Failed to renew lock lease: {ex}\n")
            sys.stderr.flush()
            return LEASE_UNKNOWN

    def renew(self) -> bool:
        """True iff the lease is still held.  See renew_state() for the reason."""
        return self.renew_state() == LEASE_HELD

    def ownership_state(self) -> str:
        """The outcome of the most recent renew()/verify_ownership().

        One of LEASE_HELD / LEASE_LOST / LEASE_UNKNOWN; LEASE_UNKNOWN before any
        check has run.
        """
        return self._last_ownership_state

    def maybe_renew(self, interval: float = 20.0) -> bool:
        """Renew the held lock lease if at least `interval` seconds have elapsed."""
        if not self.acquired or not self._nonce:
            return False
        now_mono = time.monotonic()
        if now_mono - self._last_renewed >= interval:
            return self.renew()
        return False

    def _queue_preemptor(self) -> dict[str, Any] | None:
        """The ticket that has been granted the lock ahead of ours, if any.

        Read-only: the queue is listed, but nothing here is reaped or rewritten.
        A *verification* step that deleted a ticket would be doing acquisition's
        job, and reaping from inside a push is how a fence turns into a second
        way to lose a lock.

        Only tickets that are still active count, so a holder that crashed
        cannot fence anyone out: the next acquire() would reap that same ticket,
        and a dead lease must not read as a live preemption.

        The answer is cached for ``_FENCE_REUSE_SECONDS``; see that constant for
        why the window costs the fence nothing that matters.
        """
        now_mono = time.monotonic()
        if (
            self._preemptor_checked_at is not None
            and now_mono - self._preemptor_checked_at < _FENCE_REUSE_SECONDS
        ):
            return self._preemptor_cached

        now = self._get_server_time()
        tickets = self._scan_tickets(now, reap=False)
        active = [t for t in tickets if self._is_lock_active(t, now, quiet=True)]
        mine = next((t for t in active if t.get("nonce") == self._nonce), None)
        if mine is None:
            # Our ticket is gone, or its lease has run out.  Neither is an
            # ordering failure and neither is something this scan can name: the
            # lease read in renew() is the authority on both, and it reports
            # LEASE_LOST for the first and re-takes the ticket for the second.
            result = None
        else:
            first = min(active, key=_queue_key)
            result = None if first.get("nonce") == self._nonce else first

        self._preemptor_cached = result
        self._preemptor_checked_at = now_mono
        return result

    def verify_ownership(self) -> bool:
        """Verify that this process still actively holds the lock on the remote.

        Two questions, and the second is the one a ticket read cannot answer.
        Reading our own ticket back proves the *lease* is ours -- it is gone or
        carries a foreign nonce once another client reaps it, so a lapsed lease
        cannot be resurrected here.  It says nothing about whether some other
        client has since been *granted* the lock: our ticket would still read
        back as ours, intact and unexpired, while the queue had already given
        the lock away.  So the queue is scanned too, and a ticket that sorts
        ahead of ours fails this with LEASE_PREEMPTED.

        Callers must fence destructive steps (gc pack deletion, push ref writes)
        on this result; when it is False they should read ownership_state() to
        tell the three cases apart -- a takeover, a preemption, or an unreadable
        ticket -- and report the right one.
        """
        if not self.acquired or not self._nonce:
            self._last_ownership_state = LEASE_LOST
            return False

        try:
            preemptor = self._queue_preemptor()
        except Exception as ex:
            # Fail closed, and as UNKNOWN rather than LOST: a queue we could not
            # read proves nothing about who holds the lock.
            self._last_ownership_state = LEASE_UNKNOWN
            sys.stderr.write(
                f"Warning: could not re-read the lock queue ({ex}); ownership unproven.\n"
            )
            sys.stderr.flush()
            return False

        if preemptor is not None:
            self._last_ownership_state = LEASE_PREEMPTED
            sys.stderr.write(
                f"Warning: lock preempted by '{preemptor.get('owner', 'another user')}' on "
                f"'{preemptor.get('machine', 'another machine')}'; not continuing.\n"
            )
            sys.stderr.flush()
            return False

        return self.renew()

    def release(self) -> None:
        """Release the lock by deleting this client's own ticket.

        Only the ticket named by our nonce is ever deleted, so if the lease
        lapsed and another client reaped it, this is a no-op and their lock
        state is untouched.

        Unlike ``unlock()`` this stays best-effort and silent on failure, and
        deliberately so: it runs at the end of a push that has already
        succeeded, and a lock artifact we failed to delete expires on its own.
        Raising here would turn a completed push into a reported failure, which
        trades a real result for a tidier queue.
        """
        if not self.acquired:
            return
        self.acquired = False
        # Drop the grant with the ticket, and the cached scan with it: both
        # describe a lock we no longer hold, and a later acquire() on this
        # instance must not read a stale "no preemptor" answer.
        self._holding = False
        self._preemptor_cached = None
        self._preemptor_checked_at = None
        if self._nonce:
            try:
                self.client.delete_entry(self.repo_id, f"{self.lock_dir}/{self._nonce}.json")
            except Exception:
                pass

    def get_status(self) -> dict[str, Any]:
        """Inspect current repository lock status.

        Read-only: tickets are neither reaped nor rewritten.  The winner is
        the earliest ticket whose lease is *still active*; if every ticket has
        expired (or its local PID is dead) the earliest one is still reported,
        but flagged ``stale`` -- a plain push would reap it and take the lock,
        and ``lock-status`` should say so rather than present a dead lock as a
        live one that happens to expire in 0 seconds.

        A ticket that has been *granted* the lock (``holding``) outranks every
        ticket that has merely asked for it, so the reported winner is the
        client that actually holds it rather than whichever contender happens to
        carry the earliest server-time estimate.  ``holding`` is reported too,
        because "locked, but by a ticket that was never granted" is the shape of
        a queue that needs ``--force``.

        A listing failure is reported as ``{"locked": None}`` rather than
        ``{"locked": False}``: the two send an operator to different places,
        and a transient server error silently reading as "UNLOCKED" is exactly
        the false confidence ``unlock --force`` feeds on.  Callers must treat
        ``None`` as "could not tell" (the CLI exits non-zero on it).
        """
        now = self._get_server_time()
        try:
            tickets = self._scan_tickets(now, reap=False)
        except Exception as ex:
            return {"locked": None, "error": str(ex)}
        if tickets:
            tickets.sort(key=_queue_key)
            active = [t for t in tickets if self._is_lock_active(t, now, quiet=True)]
            stale = not active
            winner = active[0] if active else tickets[0]
            try:
                w_time = float(winner.get("timestamp", 0))
                w_lease = float(winner.get("lease", self.lease))
            except (ValueError, TypeError):
                w_time = 0.0
                w_lease = 0.0
            exp = w_time + w_lease
            return {
                "locked": True,
                "protocol": "ticket",
                "owner": winner.get("owner", "unknown"),
                "machine": winner.get("machine", "unknown"),
                "machine_id": winner.get("machine_id"),
                "pid": winner.get("pid"),
                "nonce": winner.get("nonce"),
                "expires_in": max(0, int(exp - now)),
                "expires_at": exp,
                "stale": stale,
                "holding": bool(winner.get("holding")),
            }
        return {"locked": False}

    def unlock(self, force: bool = False) -> bool:
        """Break or release the lock on the repository.

        Returns True when a lock actually existed and was cleared, False when
        there was nothing to release -- so the CLI can say "nothing to
        release" instead of reporting success for a no-op.

        A release that was *attempted and failed* is neither of those, and
        raises ``RepositoryLockedError``.  ``delete_entry`` answers a refused
        delete with ``False`` rather than an exception, so checking only for
        exceptions is not enough; both are checked.  The failure this guards
        against is an operator being told "Forcibly unlocked" over a queue
        that is still there, whose next move is to push.

        What is deleted depends on ``force``.  Without it, the caller has
        proven ownership of the winning ticket, and only that ticket is
        deleted: the rest of the queue belongs to other clients -- a waiter
        whose next scan would otherwise conclude its lease lapsed -- and the
        nonce discipline this module keeps everywhere else ("only our own
        ticket is ever deleted") is preserved.  ``--force`` is the emergency
        break-the-glass path: it sweeps every ticket in the directory, which
        is what recovering from an abandoned queue requires, and why it is
        gated behind an explicit flag.
        """
        status = self.get_status()
        if status.get("locked") is None:
            # "Could not tell" must not read as "nothing to release": the
            # caller (the CLI) reports the error and exits non-zero.
            raise RepositoryLockedError(
                f"Could not inspect the lock queue ({status.get('error', 'unknown error')}); "
                "refusing to unlock on an unverifiable state. Retry, or use "
                "--force to clear the queue without a status check."
            )
        if not status.get("locked"):
            return False

        if not force:
            owner = self._owner_id()
            hostname = socket.gethostname()
            if status.get("owner") != owner or status.get("machine") != hostname:
                raise RepositoryLockedError(
                    f"Repository is locked by '{status.get('owner')}' on '{status.get('machine')}'. "
                    "Use --force to break the lock."
                )
            # Owner proven: delete only the winning ticket, not the queue.
            winner_nonce = status.get("nonce")
            if not winner_nonce:
                # The queue reports a holder but names no ticket to delete.
                # Saying "unlocked" here would be a claim with nothing behind
                # it, which is the whole point of this method's contract.
                raise RepositoryLockedError(
                    "The lock queue reports a holder but names no ticket, so there is "
                    "nothing to delete. Use --force to clear the queue."
                )
            self._delete_tickets([f"{winner_nonce}.json"])
            self.acquired = False
            return True

        # Force: sweep every ticket in the directory.  The listing gets its
        # own error handling so that a queue we could not read is reported as
        # that, rather than as "nothing to release".
        try:
            entries = self.client.list_dir(self.repo_id, self.lock_dir)
        except Exception as ex:
            raise RepositoryLockedError(
                f"Could not list the lock queue to force-unlock ({ex}); nothing was deleted."
            ) from ex
        if not isinstance(entries, list):
            raise RepositoryLockedError(
                "Could not list the lock queue to force-unlock (unexpected response); "
                "nothing was deleted."
            )

        names = [
            str(entry.get("name", ""))
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("name", "")).endswith(".json")
        ]
        if not names:
            # The status check saw a holder but the queue is now empty: another
            # client released it in between.  "Nothing to release" is the
            # honest answer, and the CLI prints exactly that.
            return False

        self._delete_tickets(names)
        self.acquired = False
        return True

    def _delete_tickets(self, names: list[str]) -> None:
        """Delete lock tickets by name, raising if any of them survives.

        ``delete_entry`` reports a refused delete by returning ``False``
        instead of raising, so a bare ``try/except`` around it is not enough:
        the return value has to be checked too.  Every failure is collected
        rather than raised on the first one, because a *partial* sweep is the
        state an operator most needs described -- some tickets gone, others
        still holding the lock.
        """
        failed: list[tuple[str, str]] = []
        for name in names:
            try:
                deleted = self.client.delete_entry(self.repo_id, f"{self.lock_dir}/{name}")
            except Exception as ex:
                failed.append((name, str(ex)))
                continue
            if not deleted:
                failed.append((name, "the server refused the delete"))

        still_failed: list[str] = []
        for name, reason in failed:
            # A refused delete and a ticket another client already removed are
            # indistinguishable from ``delete_entry``'s answer -- both are
            # False.  Ask the queue instead of guessing: if the ticket is gone
            # the lock really is released, and reporting a failure there would
            # send the operator to ``--force`` on a repository that is already
            # free.
            if self._ticket_is_gone(name):
                continue
            still_failed.append(f"{name} ({reason})")

        if still_failed:
            raise RepositoryLockedError(
                f"Unlock removed {len(names) - len(still_failed)} of {len(names)} lock "
                f"ticket(s); {len(still_failed)} could not be deleted: "
                + "; ".join(still_failed)
            )

    def _ticket_is_gone(self, name: str) -> bool:
        """True when the ticket is no longer in the queue.

        An unreadable queue is *not* evidence that the ticket is gone, so a
        failure to read keeps the caller's failure: this method only ever
        turns a reported failure into a success, never the other way round.
        """
        try:
            return self.client.get_file_text(self.repo_id, f"{self.lock_dir}/{name}") is None
        except Exception:
            return False

    def __enter__(self) -> RemoteLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
