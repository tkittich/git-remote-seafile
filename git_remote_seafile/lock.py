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
from .config import RemoteConfig, get_git_config_int


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


def describe_ownership_failure(state: str) -> str:
    """Phrase a failed ownership re-check in terms of what actually happened."""
    if state == LEASE_LOST:
        return "another client took over the lock"
    if state == LEASE_UNKNOWN:
        return "the lock ticket could not be read or rewritten, so ownership is unproven"
    return "lock ownership could not be verified"


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
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
            if not handle:
                ERROR_ACCESS_DENIED = 5
                if kernel32.GetLastError() == ERROR_ACCESS_DENIED:
                    return True
                return False
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    STILL_ACTIVE = 259
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
    later (`seafile.locksettle`, default 1s), so a peer that is still
    uploading during the first read cannot win a lock this client also
    claims. Set it to 0 to skip that re-scan.

    The v0.1–v0.3 single-file mirror (.git-lock.json) was removed in v0.7.0:
    this protocol coordinates exclusively through the ticket queue, and
    acquire() deletes an orphaned mirror when it finds one so existing
    repositories self-clean. Do not mix this version with a v0.1–v0.3
    helper on the same repository — those clients predate the ticket
    protocol and cannot see its locks at all.
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
            # Literal, not a named constant: the doc-consistency guard reads
            # this call to check the documented default (see
            # tests/test_docs_consistency.py).
            settle = get_git_config_int("seafile.locksettle", 1)
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

    def _is_lock_active(self, info: dict[str, Any], now: float) -> bool:
        """Check if lock payload is active. Returns False if expired or dead local PID."""
        lock_machine = info.get("machine", "")
        lock_machine_id = info.get("machine_id")
        lock_pid = info.get("pid")
        try:
            lock_pid_int = int(lock_pid) if lock_pid is not None else 0
        except (ValueError, TypeError):
            lock_pid_int = 0

        # Sonnet H-5 / L-2: Dead local PID fast-reclaim
        # Verify both hostname and machine_id (when available) to prevent false reclaim
        # in container environments or fleets sharing default hostnames.
        same_machine = (lock_machine == socket.gethostname())
        if same_machine and lock_machine_id is not None:
            same_machine = (str(lock_machine_id) == _LOCAL_MACHINE_ID)

        if same_machine and lock_pid_int > 0:
            if not _is_pid_alive(lock_pid_int):
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

        # FIFO ordering.  When every ticket records its acquisition time
        # (``order_ts``), order by that: renew() rewrites the ticket file, so
        # an mtime-ordered queue would move a healthy holder to the back of
        # the queue on every refresh, letting the next waiter win the scan
        # while the holder is still active.  A scan that mixes ticket formats
        # falls back to mtime for all of them -- mtime is the one field every
        # format shares, and comparing an order_ts against an mtime across
        # formats is not a FIFO comparison at all.  (Differently-formatted
        # ticket clients contend through the scan itself; the old single-file
        # mirror that used to gate this is gone as of v0.7.0.)
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
        all_modern = bool(active_tickets) and all(_f(t.get("order_ts")) > 0 for t in active_tickets)
        for t in active_tickets:
            if all_modern:
                t["_order_key"] = (_f(t.get("order_ts")), _f(t.get("_mtime")), t["nonce"])
            elif t.get("_mtime"):
                t["_order_key"] = (_f(t.get("_mtime")), 0.0, t["nonce"])
            else:
                t["_order_key"] = (_f(t.get("timestamp")), 0.0, t["nonce"])

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
            # Match the scheme _scan_tickets chose for the tickets it saw, so
            # the appended candidate compares against them.
            others_modern = bool(tickets) and all(_f(t.get("order_ts")) > 0 for t in tickets)
            if others_modern:
                my_ticket["_order_key"] = (self._order_ts, 0.0, nonce)
            else:
                my_ticket["_order_key"] = (int(now), 0.0, nonce)
            my_ticket["_path"] = f"{self.lock_dir}/{nonce}.json"
            tickets.append(my_ticket)
        tickets.sort(key=lambda t: t.get("_order_key", (0, 0, "")))
        return tickets

    def acquire(self) -> None:
        start_time = time.monotonic()
        hostname = socket.gethostname()
        owner = self._owner_id()
        nonce = uuid.uuid4().hex
        self._identity = (owner, hostname)
        self._nonce = nonce
        self._order_ts = self._get_server_time()

        # One-time cleanup: an orphaned .git-lock.json is a relic of the
        # v0.1–v0.3 single-file protocol (removed in v0.7.0).  This protocol
        # never reads it, so rather than leave the clutter in every repository
        # forever, delete it when found.  Best-effort: a failure here must not
        # block the push -- the acquire loop below fails loudly on its own.
        legacy_path = f"{self.repo_path}/.git-lock.json"
        try:
            if self.client.get_file_text(self.repo_id, legacy_path):
                self.client.delete_entry(self.repo_id, legacy_path)
        except Exception:
            pass

        try:
            while True:
                now = self._get_server_time()

                # 1. Upload ticket
                lock_payload = {
                    "owner": owner,
                    "machine": hostname,
                    "machine_id": _LOCAL_MACHINE_ID,
                    "nonce": nonce,
                    "timestamp": now,
                    "order_ts": self._order_ts,
                    "lease": self.lease,
                    "pid": os.getpid(),
                }
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
                    if time.monotonic() - start_time >= self.timeout:
                        raise RepositoryLockedError(f"Failed to acquire lock: {ex}") from ex
                    time.sleep(2)
                    continue

                # 3. Check candidate tickets in .git-lock.d
                try:
                    tickets = self._scan_tickets(now)
                except Exception as ex:
                    if isinstance(ex, RepositoryLockedError):
                        raise
                    if time.monotonic() - start_time >= self.timeout:
                        raise RepositoryLockedError(f"Failed to scan lock tickets: {ex}") from ex
                    sys.stderr.write(f"Transient error scanning lock tickets ({ex}); retrying...\n")
                    sys.stderr.flush()
                    time.sleep(2)
                    continue

                winner = self._candidates(tickets, lock_payload, nonce, now)[0]

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
                        if time.monotonic() - start_time >= self.timeout:
                            raise RepositoryLockedError(
                                f"Failed to confirm lock ownership: {ex}"
                            ) from ex
                        sys.stderr.write(
                            f"Transient error confirming lock ownership ({ex}); retrying...\n"
                        )
                        sys.stderr.flush()
                        continue
                    winner = self._candidates(settled, lock_payload, nonce, now)[0]

                if winner.get("nonce") == nonce:
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
                    if time.monotonic() - start_time >= self.timeout:
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
        payload = {
            "owner": self._identity[0] if self._identity else self._owner_id(),
            "machine": self._identity[1] if self._identity else socket.gethostname(),
            "machine_id": _LOCAL_MACHINE_ID,
            "nonce": self._nonce,
            "timestamp": now,
            "order_ts": self._order_ts,
            "lease": self.lease,
            "pid": os.getpid(),
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
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
        return getattr(self, "_last_ownership_state", LEASE_UNKNOWN)

    def maybe_renew(self, interval: float = 20.0) -> bool:
        """Renew the held lock lease if at least `interval` seconds have elapsed."""
        if not self.acquired or not self._nonce:
            return False
        now_mono = time.monotonic()
        if now_mono - getattr(self, "_last_renewed", 0.0) >= interval:
            return self.renew()
        return False

    def verify_ownership(self) -> bool:
        """Verify that this process still actively holds the lock on the remote (N-4).

        Renewal reads the ticket back before rewriting it, so a lease that
        lapsed and was reaped by a new holder returns False here instead of
        being resurrected.  Callers must fence destructive steps (gc pack
        deletion, push ref writes) on this result; when it is False they should
        read ownership_state() to tell a takeover from an unreadable ticket and
        report the right one.
        """
        return self.renew()

    def release(self) -> None:
        """Release the lock by deleting this client's own ticket.

        Only the ticket named by our nonce is ever deleted, so if the lease
        lapsed and another client reaped it, this is a no-op and their lock
        state is untouched.
        """
        if not self.acquired:
            return
        self.acquired = False
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
        """
        now = self._get_server_time()
        try:
            tickets = self._scan_tickets(now, reap=False)
        except Exception:
            tickets = []
        if tickets:
            tickets.sort(key=lambda t: t.get("_order_key", (0, 0, "")))
            active = [t for t in tickets if self._is_lock_active(t, now)]
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
            }
        return {"locked": False}

    def unlock(self, force: bool = False) -> bool:
        """Break or release the lock on the repository.

        Returns True when a lock actually existed and was cleared, False when
        there was nothing to release -- so the CLI can say "nothing to
        release" instead of reporting success for a no-op.
        """
        status = self.get_status()
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

        # Clear tickets in lock_dir
        try:
            entries = self.client.list_dir(self.repo_id, self.lock_dir)
            if isinstance(entries, list):
                for entry in entries:
                    if entry.get("name", "").endswith(".json"):
                        self.client.delete_entry(self.repo_id, f"{self.lock_dir}/{entry['name']}")
        except Exception:
            pass

        self.acquired = False
        return True

    def __enter__(self) -> RemoteLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
