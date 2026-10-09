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
        if timeout is None:
            timeout = get_git_config_int("seafile.locktimeout", 15)
        if lease is None:
            lease = get_git_config_int("seafile.locklease", 60)
        self.timeout = timeout
        self.lease = lease
        self.acquired = False
        self._identity: tuple[str, str] | None = None
        self._nonce: str | None = None
        self._last_renewed: float = 0.0
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
        all_modern = bool(active_tickets) and all(_f(t.get("order_ts")) > 0 for t in active_tickets)
        for t in active_tickets:
            if all_modern:
                t["_order_key"] = (_f(t.get("order_ts")), _f(t.get("_mtime")), t["nonce"])
            elif t.get("_mtime"):
                t["_order_key"] = (_f(t.get("_mtime")), 0.0, t["nonce"])
            else:
                t["_order_key"] = (_f(t.get("timestamp")), 0.0, t["nonce"])

        return active_tickets

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

                if not any(t.get("nonce") == nonce for t in tickets):
                    my_ticket = dict(lock_payload)
                    my_ticket["_mtime"] = 0.0
                    # Match the scheme _scan_tickets chose for the tickets it
                    # saw, so the appended candidate compares against them.
                    others_modern = bool(tickets) and all(_f(t.get("order_ts")) > 0 for t in tickets)
                    if others_modern:
                        my_ticket["_order_key"] = (self._order_ts, 0.0, nonce)
                    else:
                        my_ticket["_order_key"] = (int(now), 0.0, nonce)
                    my_ticket["_path"] = f"{self.lock_dir}/{nonce}.json"
                    tickets.append(my_ticket)

                tickets.sort(key=lambda t: t.get("_order_key", (0, 0, "")))
                winner = tickets[0]

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

    def renew(self) -> bool:
        """Renew the lease on an actively held lock.

        The ticket is read back before it is rewritten.  The nonce names the
        ticket file and only this process ever writes it, so a ticket that is
        gone, unreadable, or carries a foreign nonce means this lease lapsed
        and was reaped -- most plausibly by another client that now holds the
        lock.  Re-uploading anyway would resurrect the ticket and claim
        ownership we no longer have; the fencing checks in cmd_push and gc
        delegate to this method precisely to detect that, so it must fail
        closed here.
        """
        if not self.acquired or not self._nonce:
            return False

        try:
            current = self.client.get_file_text(self.repo_id, f"{self.lock_dir}/{self._nonce}.json")
        except Exception as ex:
            sys.stderr.write(f"Warning: Failed to renew lock lease: {ex}\n")
            sys.stderr.flush()
            return False

        if not current:
            sys.stderr.write(
                "Warning: Lock ticket is gone; this lease lapsed and another client may hold the lock. "
                "Not renewing.\n"
            )
            sys.stderr.flush()
            return False

        try:
            current_nonce = json.loads(current).get("nonce")
        except Exception:
            sys.stderr.write(
                "Warning: Lock ticket is unreadable; refusing to renew rather than guess at ownership.\n"
            )
            sys.stderr.flush()
            return False

        if current_nonce != self._nonce:
            sys.stderr.write("Warning: Lock ticket was taken over; not renewing.\n")
            sys.stderr.flush()
            return False

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
            return True
        except Exception as ex:
            sys.stderr.write(f"Warning: Failed to renew lock lease: {ex}\n")
            sys.stderr.flush()
            return False

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
        deletion, push ref writes) on this result.
        """
        if not self.acquired or not self._nonce:
            return False
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
        """Inspect current repository lock status."""
        now = self._get_server_time()
        # Read-only query; do not reap stale tickets during status inspection.
        try:
            tickets = self._scan_tickets(now, reap=False)
        except Exception:
            tickets = []
        if tickets:
            tickets.sort(key=lambda t: t.get("_order_key", (0, 0, "")))
            winner = tickets[0]
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
            }
        return {"locked": False}

    def unlock(self, force: bool = False) -> bool:
        """Break or release the lock on the repository."""
        status = self.get_status()
        if not status.get("locked"):
            return True

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
