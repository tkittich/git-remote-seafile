"""test_lock.py - Distributed lease lock: acquisition, fencing, ordering, status."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import time
import unittest
from unittest.mock import MagicMock, patch

from git_remote_seafile.config import RemoteConfig
from git_remote_seafile.lock import (
    LEASE_HELD,
    LEASE_LOST,
    LEASE_UNKNOWN,
    RemoteLock,
    RepositoryLockedError,
)

LEGACY_LOCK = "/path/.git-lock.json"

# Every RemoteLock below passes `settle=0`: the settlement window is exercised
# on its own in TestLockSettlementWindow, and everywhere else it would only add
# a real second of sleeping to each acquisition.


def _ticket(nonce: str, owner: str = "alice", machine: str = "nodeA", **overrides) -> str:
    payload = {
        "owner": owner,
        "machine": machine,
        "nonce": nonce,
        "timestamp": time.time(),
        "order_ts": time.time(),
        "lease": 60,
        "pid": 1234,
    }
    payload.update(overrides)
    return json.dumps(payload)


class _SharedLockStore:
    """Stateful stand-in for the Seafile file API shared by several lock clients.

    The MagicMock-based lock tests configure per-path return values up front,
    which cannot express a *sequence* of events (acquire, lapse, takeover).
    This stores real bytes keyed by path so multiple RemoteLock instances see
    one consistent remote.
    """

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.mtime_override: dict[str, int] = {}

    @staticmethod
    def _norm(path: str) -> str:
        return "/" + path.strip("/")

    def upload(self, repo_id, parent, filename, content, replace=True, progress_callback=None):
        self.files[self._norm(f"{parent}/{filename}")] = bytes(content)
        return True

    def get_text(self, repo_id, path):
        data = self.files.get(self._norm(path))
        return data.decode("utf-8") if data is not None else None

    def delete(self, repo_id, path):
        return self.files.pop(self._norm(path), None) is not None

    def list_dir(self, repo_id, path):
        prefix = self._norm(path).rstrip("/") + "/"
        out = []
        for key in sorted(self.files):
            if key.startswith(prefix):
                name = key[len(prefix):]
                out.append({
                    "type": "file",
                    "name": name,
                    "mtime": self.mtime_override.get(key, int(time.time())),
                })
        return out

    def expire_all(self, seconds: float) -> None:
        """Age every lock artifact past its lease, as if time simply passed."""
        for key in list(self.files):
            if ".git-lock" in key:
                payload = json.loads(self.files[key])
                payload["timestamp"] = payload.get("timestamp", time.time()) - seconds
                payload["order_ts"] = payload.get("order_ts", time.time()) - seconds
                self.files[key] = json.dumps(payload).encode("utf-8")

    def put_ticket(self, path: str, payload: dict) -> None:
        self.files[self._norm(path)] = json.dumps(payload).encode("utf-8")

    def client(self, token: str) -> MagicMock:
        c = MagicMock()
        c.token = token
        c.upload_file.side_effect = self.upload
        c.get_file_text.side_effect = self.get_text
        c.delete_entry.side_effect = self.delete
        c.list_dir.side_effect = self.list_dir
        return c


class TestConcurrencyAndLocking(unittest.TestCase):
    def test_lock_acquire_and_release(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60, settle=0)
        with lock:
            self.assertTrue(lock.acquired)
            # Exactly one artifact is written: the ticket. The v0.1-v0.6
            # legacy .git-lock.json mirror is gone.
            self.assertEqual(mock_client.upload_file.call_count, 1)
        self.assertFalse(lock.acquired)
        mock_client.delete_entry.assert_called_once_with(
            "repo1", f"/path/.git-lock.d/{lock._nonce}.json"
        )

    def test_orphaned_legacy_mirror_is_cleaned_up_on_acquire(self):
        """acquire() deletes a relic .git-lock.json so repositories self-clean."""
        store = _SharedLockStore()
        store.files[LEGACY_LOCK] = json.dumps({
            "owner": "long-gone", "machine": "ancient",
            "timestamp": time.time(), "lease": 60,
        }).encode("utf-8")

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertNotIn(LEGACY_LOCK, store.files)

    def test_active_foreign_ticket_blocks(self):
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/alice-nonce.json", json.loads(_ticket("alice-nonce")))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()
        self.assertIn("locked by 'alice'", str(ctx.exception))

    def test_expired_ticket_is_reaped_on_acquire(self):
        """A ticket past its lease is lawfully reaped and cannot block anyone."""
        store = _SharedLockStore()
        store.put_ticket(
            "/path/.git-lock.d/bob-nonce.json",
            json.loads(_ticket("bob-nonce", owner="bob", machine="crashed-laptop")),
        )
        store.expire_all(120)

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertNotIn("/path/.git-lock.d/bob-nonce.json", store.files)

    def test_corrupt_ticket_payload_is_skipped(self):
        """Corrupt JSON left by a cut mid-upload neither blocks nor crashes.

        A bystander must not reap a ticket it cannot parse -- only the holder
        (whose nonce names the file) or its expiry may remove one -- so the
        garbage file is ignored and left alone.
        """
        store = _SharedLockStore()
        store.files["/path/.git-lock.d/garbage.json"] = b"MALFORMED_NON_JSON{{{"

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertIn("/path/.git-lock.d/garbage.json", store.files)

    def test_release_only_deletes_its_own_ticket(self):
        """A lease can lapse mid-operation and someone else then holds the lock.

        Releasing must delete only our (already reaped) ticket path and leave
        the new holder's ticket untouched.
        """
        store = _SharedLockStore()
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60, settle=0)
        a.acquire()
        self.assertTrue(a.acquired)
        store.expire_all(120)

        b = RemoteLock(store.client("token-B"), "repo1", "/path", timeout=5, lease=60, settle=0)
        with patch("sys.stderr", io.StringIO()):
            b.acquire()
        self.assertTrue(b.acquired)

        with patch("sys.stderr", io.StringIO()):
            a.release()

        self.assertFalse(a.acquired)
        self.assertTrue(b.acquired, "B's lock must survive A's release")
        self.assertIn(f"/path/.git-lock.d/{b._nonce}.json", store.files)

    def test_release_removes_its_own_lock(self):
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()
        self.assertTrue(lock.acquired)

        lock.release()

        self.assertFalse(lock.acquired)
        self.assertNotIn(f"/path/.git-lock.d/{lock._nonce}.json", store.files)

    def test_lock_acquire_retries_and_fails_closed_on_list_dir_error(self):
        """Lock acquisition fails closed on listing errors rather than assuming no contenders (N-3)."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None
        mock_client.list_dir.side_effect = RuntimeError("HTTP 500 internal server error")

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0.1, lease=10, settle=0)
        with self.assertRaises(RepositoryLockedError):
            lock.acquire()
        self.assertFalse(lock.acquired)

    def test_lock_payload_does_not_leak_the_api_token(self):
        """The payload is stored in the repository, so it must not carry the token.

        The previous implementation wrote the token's first eight characters
        into it.
        """
        secret = "s3cr3t-token-value"
        mock_client = MagicMock()
        mock_client.token = secret
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        payload = mock_client.upload_file.call_args[0][3].decode("utf-8")
        self.assertNotIn("s3cr3t", payload)
        self.assertEqual(
            json.loads(payload)["owner"],
            hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8],
        )

    @patch("git_remote_seafile.lock.get_git_config_int")
    def test_lock_reads_git_config_defaults(self, mock_get_cfg):
        mock_get_cfg.side_effect = lambda key, default: {
            "seafile.locktimeout": 30,
            "seafile.locklease": 90,
            "seafile.locksettle": 4,
        }.get(key, default)
        mock_client = MagicMock()
        lock = RemoteLock(mock_client, "repo1", "/path")
        self.assertEqual(lock.timeout, 30)
        self.assertEqual(lock.lease, 90)
        self.assertEqual(lock.settle, 4)

    def test_the_settlement_window_comes_from_the_session_config(self):
        """RemoteConfig is the one place these three values are resolved."""
        cfg = RemoteConfig(lock_timeout=7, lock_lease=8, lock_settle=9,
                           auto_gc=False, gc_threshold=20)
        lock = RemoteLock(MagicMock(), "repo1", "/path", config=cfg)
        self.assertEqual(lock.timeout, 7)
        self.assertEqual(lock.lease, 8)
        self.assertEqual(lock.settle, 9)

    def test_a_negative_settlement_window_is_clamped(self):
        """A window that would elapse before it started is not a window."""
        lock = RemoteLock(MagicMock(), "repo1", "/path", settle=-3)
        self.assertEqual(lock.settle, 0.0)

    def test_ticket_with_malformed_timestamp_is_reaped(self):
        """A ticket whose timestamp is garbage is treated as expired, not fatal."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/alice-nonce.json", {
            "owner": "alice",
            "machine": "nodeA",
            "nonce": "alice-nonce",
            "timestamp": "INVALID_TIMESTAMP",
            "lease": "NOT_A_NUMBER",
        })

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertNotIn("/path/.git-lock.d/alice-nonce.json", store.files)

    def test_a_future_timestamp_with_a_garbage_lease_is_still_reaped(self):
        """The invariant that keeps acquire()'s own malformed-winner guard dead.

        `_is_lock_active` parses `timestamp` and `lease` inside one `try`, so a
        failure on *either* zeroes **both**.  A ticket claiming to expire in an
        hour but carrying a non-numeric lease would otherwise look active, and
        would then be the winner `acquire()` parses for its own expiry message
        -- the `except (ValueError, TypeError)` that cannot fire today.  It is
        reaped instead.  This test is what pins that, so the guard's deadness is
        a recorded property rather than an assumption.
        """
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/alice-nonce.json", {
            "owner": "alice",
            "machine": "nodeA",
            "nonce": "alice-nonce",
            "timestamp": time.time() + 3600,  # would be active ...
            "lease": "NOT_A_NUMBER",          # ... if the except did not zero both
        })

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        self.assertTrue(lock.acquired, "a malformed lease must not make a ticket active")
        self.assertNotIn("/path/.git-lock.d/alice-nonce.json", store.files)

    def test_the_read_only_scan_does_surface_a_malformed_ticket(self):
        """Why `acquire()` keeps a guard it cannot reach.

        With `reap=False` -- the lock_status path -- tickets are appended
        without the active check, so a malformed one *does* come back.  That is
        the guard's premise, and it is real; `acquire()` scanning only with
        `reap=True` is the sole reason the guard is dead.  Without this test the
        comment in `acquire()` would be an unfalsifiable claim.
        """
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/alice-nonce.json", {
            "owner": "alice",
            "machine": "nodeA",
            "nonce": "alice-nonce",
            "timestamp": "INVALID_TIMESTAMP",
            "lease": "NOT_A_NUMBER",
        })

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        tickets = lock._scan_tickets(time.time(), reap=False)

        self.assertEqual([t["nonce"] for t in tickets], ["alice-nonce"])
        # ... and the fields the guard exists to parse really are unparseable,
        # so an unguarded float() here would raise out of acquire().
        with self.assertRaises(ValueError):
            float(tickets[0]["timestamp"])

    def test_dead_pid_fast_reclaim_on_local_machine(self):
        """A lock held by the same machine with a dead PID should be fast-reclaimed."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/crashed-nonce.json", json.loads(_ticket(
            "crashed-nonce",
            owner="crashed-local-user",
            machine=socket.gethostname(),
            pid=999999,
        )))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        err = io.StringIO()
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=False):
            with patch("sys.stderr", err):
                lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertIn("Reclaiming stale lock from dead local process", err.getvalue())

    def test_alive_pid_on_local_machine_blocks(self):
        """A lock held by the same machine with an alive PID must not be stolen."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/alive-nonce.json", json.loads(_ticket(
            "alive-nonce",
            owner="running-local-user",
            machine=socket.gethostname(),
            pid=os.getpid(),
        )))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=True):
            with self.assertRaises(RepositoryLockedError):
                lock.acquire()

    def test_dead_pid_fast_reclaim_ignores_different_machine_id(self):
        """When machine hostname matches but machine_id differs (e.g. container fleet), lock is not stolen."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/crashed-nonce.json", json.loads(_ticket(
            "crashed-nonce",
            owner="crashed-container-user",
            machine=socket.gethostname(),
            machine_id="other-container-node-uuid",
            pid=999999,
        )))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=False):
            with self.assertRaises(RepositoryLockedError):
                lock.acquire()

    def test_ticket_based_ordering_earliest_wins(self):
        """When multiple tickets exist, the earliest ticket wins."""
        store = _SharedLockStore()
        now = time.time()
        store.put_ticket("/path/.git-lock.d/ticketA.json", json.loads(_ticket(
            "ticketA", timestamp=now - 10, order_ts=now - 10,
        )))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()
        self.assertIn("locked by 'alice'", str(ctx.exception))

    def test_ticket_based_ordering_is_not_mtime_based_for_modern_tickets(self):
        """All-order_ts tickets order by acquisition time even when mtimes lie.

        renew() rewrites the ticket file, so the holder's mtime is always the
        newest; ordering on mtime would let a later waiter win the scan while
        the holder is still active.  The holder here renewed last (newest
        mtime) but acquired first (earliest order_ts) and must win.
        """
        store = _SharedLockStore()
        now = int(time.time())
        store.put_ticket("/path/.git-lock.d/holder-nonce.json", {
            "owner": "holder", "machine": "nodeA", "nonce": "holder-nonce",
            "timestamp": float(now - 300), "order_ts": float(now - 300),
            "lease": 600, "pid": 111,
        })
        store.put_ticket("/path/.git-lock.d/waiter-nonce.json", {
            "owner": "waiter", "machine": "nodeB", "nonce": "waiter-nonce",
            "timestamp": float(now - 30), "order_ts": float(now - 30),
            "lease": 600, "pid": 222,
        })
        # The holder renewed just now; the waiter's ticket is older on disk.
        store.mtime_override["/path/.git-lock.d/holder-nonce.json"] = now
        store.mtime_override["/path/.git-lock.d/waiter-nonce.json"] = now - 200

        lock = RemoteLock(store.client("token-x"), "repo1", "/path", timeout=0, lease=600, settle=0)
        tickets = lock._scan_tickets(float(now), reap=False)
        tickets.sort(key=lambda t: t.get("_order_key", (0, 0, "")))
        self.assertEqual(tickets[0]["nonce"], "holder-nonce")

    def test_verify_ownership_fails_after_lease_lapse_and_takeover(self):
        """The N-4 fence must reject a holder whose ticket was reaped by a new owner.

        renew() used to re-upload the ticket blindly: after A's lease lapsed
        and B reaped it and took over, A's verify_ownership() resurrected the
        ticket and returned True -- so gc would have gone on to delete remote
        packfiles inside B's push.
        """
        store = _SharedLockStore()
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60, settle=0)
        a.acquire()
        self.assertTrue(a.acquired)

        # A stops renewing; every lock artifact ages past its lease.
        store.expire_all(120)

        # B arrives, reaps A's expired ticket, and acquires.
        b = RemoteLock(store.client("token-B"), "repo1", "/path", timeout=5, lease=60, settle=0)
        err = io.StringIO()
        with patch("sys.stderr", err):
            b.acquire()
        self.assertTrue(b.acquired)
        self.assertNotIn(
            f"/path/.git-lock.d/{a._nonce}.json",
            store.files,
            "B must have reaped A's expired ticket",
        )

        # A resumes and runs the fence before any destructive step.
        with patch("sys.stderr", io.StringIO()):
            a_ok = a.verify_ownership()
        self.assertFalse(a_ok)
        # A's failed renewal must not have created any lock artifact: exactly
        # B's ticket remains.
        self.assertEqual(
            [k for k in store.files if ".git-lock.d" in k],
            [f"/path/.git-lock.d/{b._nonce}.json"],
        )

    def test_lock_lease_renew(self):
        """Lease renewal rewrites the ticket with a fresh timestamp."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()
        # renew() reads the ticket back before rewriting it (the takeover
        # fence), so the client must serve what acquire() uploaded.
        mock_client.get_file_text.return_value = mock_client.upload_file.call_args[0][3].decode("utf-8")
        upload_count_before = mock_client.upload_file.call_count

        res = lock.renew()
        self.assertTrue(res)
        self.assertEqual(mock_client.upload_file.call_count, upload_count_before + 1)

    def test_renew_state_distinguishes_lost_from_unknown(self):
        """A takeover and an unreadable ticket are different states (D7).

        renew() collapses both to False, but the fence that reports to the user
        must be able to tell them apart: "another client took over" versus
        "we could not read our own ticket".
        """
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None
        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        # A read failure is UNKNOWN, not LOST.
        mock_client.get_file_text.side_effect = RuntimeError("connection reset")
        with patch("sys.stderr", io.StringIO()):
            self.assertFalse(lock.verify_ownership())
        self.assertEqual(lock.ownership_state(), LEASE_UNKNOWN)

        # A foreign nonce is a genuine takeover.
        mock_client.get_file_text.side_effect = None
        mock_client.get_file_text.return_value = json.dumps({"nonce": "someone-else"})
        with patch("sys.stderr", io.StringIO()):
            self.assertFalse(lock.verify_ownership())
        self.assertEqual(lock.ownership_state(), LEASE_LOST)

        # A ticket that is ours again means we still hold the lease.
        mock_client.get_file_text.return_value = json.dumps({"nonce": lock._nonce})
        self.assertTrue(lock.verify_ownership())
        self.assertEqual(lock.ownership_state(), LEASE_HELD)

    def test_renew_fails_when_the_ticket_is_gone(self):
        """A missing ticket means the lease lapsed and was reaped; renew must fail."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()
        err = io.StringIO()
        with patch("sys.stderr", err):
            self.assertFalse(lock.renew())
        self.assertIn("Lock ticket is gone", err.getvalue())

    def test_remote_lock_maybe_renew(self):
        """maybe_renew only triggers renew when interval has passed."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()
        mock_client.get_file_text.return_value = mock_client.upload_file.call_args[0][3].decode("utf-8")
        upload_count = mock_client.upload_file.call_count

        # Call immediately: within interval, returns False without uploading
        self.assertFalse(lock.maybe_renew(interval=20.0))
        self.assertEqual(mock_client.upload_file.call_count, upload_count)

        # Simulate time passage
        lock._last_renewed = time.monotonic() - 25.0
        self.assertTrue(lock.maybe_renew(interval=20.0))
        self.assertEqual(mock_client.upload_file.call_count, upload_count + 1)

    def test_lock_status_and_unlock(self):
        """get_status and unlock inspect and clear ticket state."""
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)

        status_unlocked = lock.get_status()
        self.assertFalse(status_unlocked["locked"])

        lock.acquire()
        status_locked = lock.get_status()
        self.assertTrue(status_locked["locked"])
        self.assertEqual(status_locked["protocol"], "ticket")
        self.assertEqual(status_locked["owner"], lock._owner_id())

        self.assertTrue(lock.unlock())
        self.assertFalse(lock.get_status()["locked"])
        self.assertEqual(
            [k for k in store.files if ".git-lock.d" in k],
            [],
        )

    def test_abandoned_ticket_cleaned_up_on_acquire_timeout(self):
        """When acquire() times out, its candidate ticket must be deleted immediately."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/competitor-nonce.json", json.loads(_ticket("competitor-nonce")))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        with self.assertRaises(RepositoryLockedError):
            lock.acquire()

        self.assertFalse(lock.acquired)
        self.assertTrue(bool(lock._nonce))
        # The abandoned candidate ticket must be gone; the winner's remains.
        self.assertNotIn(f"/path/.git-lock.d/{lock._nonce}.json", store.files)
        self.assertIn("/path/.git-lock.d/competitor-nonce.json", store.files)

    def test_windows_is_pid_alive_handles_access_denied(self):
        """On Windows, OpenProcess failing with ERROR_ACCESS_DENIED (5) must treat PID as alive."""
        from git_remote_seafile.lock import _is_pid_alive
        with patch("sys.platform", "win32"):
            mock_kernel32 = MagicMock()
            mock_kernel32.OpenProcess.return_value = 0
            mock_kernel32.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED
            mock_ctypes = MagicMock()
            mock_ctypes.windll.kernel32 = mock_kernel32
            with patch.dict("sys.modules", {"ctypes": mock_ctypes, "ctypes.wintypes": MagicMock()}):
                self.assertTrue(_is_pid_alive(1234))


class _LateCommitStore(_SharedLockStore):
    """A store where one ticket becomes visible only after N listings.

    Models the D21 race directly: two clients acquire concurrently, and the
    listing each reads before the other's upload has committed shows only that
    client's own ticket.
    """

    def __init__(self, hidden_name: str | None = None, visible_after: int = 0):
        super().__init__()
        self.hidden_name = hidden_name
        self.visible_after = visible_after
        self.listings = 0

    def list_dir(self, repo_id, path):
        self.listings += 1
        entries = super().list_dir(repo_id, path)
        if self.hidden_name and self.listings <= self.visible_after:
            entries = [e for e in entries if e["name"] != self.hidden_name]
        return entries


class TestLockSettlementWindow(unittest.TestCase):
    """D21: the first read of the queue is not enough to claim ownership.

    Two clients can both win if each reads the queue before the other's upload
    has committed: A sees only itself and claims the lock; B commits with an
    *earlier* order_ts, reads the queue, sees both, and claims it too.  The
    window gives any in-flight peer time to land, then reads again, so both
    clients compare the same set and the same one wins.
    """

    def _store_with_earlier_peer(self) -> _LateCommitStore:
        store = _LateCommitStore("bob-nonce.json", visible_after=1)
        store.put_ticket(
            "/path/.git-lock.d/bob-nonce.json",
            json.loads(_ticket("bob-nonce", owner="bob", order_ts=time.time() - 5)),
        )
        return store

    def test_a_win_on_the_first_read_is_not_yet_a_win(self):
        store = self._store_with_earlier_peer()

        lock = RemoteLock(store.client("token123"), "repo1", "/path",
                          timeout=0, lease=60, settle=0.05)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()

        self.assertFalse(lock.acquired)
        self.assertIn("locked by 'bob'", str(ctx.exception))
        # Our losing candidate is cleaned up; the winner's ticket survives.
        self.assertEqual(
            sorted(store.files), ["/path/.git-lock.d/bob-nonce.json"]
        )

    def test_without_the_window_the_first_read_decides(self):
        """The guard is what prevents the double win -- disable it and it returns.

        Bob's ticket carries the *earlier* order_ts and is still in flight when
        we read, so with the window off we claim the lock; Bob commits, reads
        the queue, sees both tickets, and wins the same comparison.  Two
        holders, one repository.  This pins why `seafile.locksettle 0` is a
        trade rather than a free speed-up: it is the difference between this
        test and the one above.
        """
        store = self._store_with_earlier_peer()

        lock = RemoteLock(store.client("token123"), "repo1", "/path",
                          timeout=0, lease=60, settle=0)
        lock.acquire()

        self.assertTrue(lock.acquired)

    def test_a_peer_that_started_later_does_not_take_the_lock(self):
        """The window must not hand the lock to whoever happens to be listed."""
        store = _LateCommitStore()
        store.put_ticket(
            "/path/.git-lock.d/bob-nonce.json",
            json.loads(_ticket("bob-nonce", owner="bob", order_ts=time.time() + 5)),
        )

        lock = RemoteLock(store.client("token123"), "repo1", "/path",
                          timeout=0, lease=60, settle=0.05)
        lock.acquire()

        self.assertTrue(lock.acquired)
        # Two reads, not a loop: the queue is read once to decide and once to
        # confirm.  That is the whole cost of the guard.
        self.assertEqual(store.listings, 2)

    def test_the_window_can_be_switched_off_with_a_single_read(self):
        store = _LateCommitStore()
        lock = RemoteLock(store.client("token123"), "repo1", "/path",
                          timeout=0, lease=60, settle=0)
        lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertEqual(store.listings, 1)


class _FlakyScanStore(_SharedLockStore):
    """A store whose directory listing fails on selected calls.

    Which call fails is what distinguishes the two retry loops in `acquire()`:
    a failure on the *first* read is a transient scan error (reported as
    "Failed to scan lock tickets"), while a failure on the *confirming* read
    inside the settlement window is a different branch with its own message.
    """

    def __init__(self, fail_on: set[int]):
        super().__init__()
        self.fail_on = fail_on
        self.listings = 0

    def list_dir(self, repo_id, path):
        self.listings += 1
        if self.listings in self.fail_on:
            raise RuntimeError("HTTP 500 internal server error")
        return super().list_dir(repo_id, path)


class TestLockAcquireFailurePaths(unittest.TestCase):
    """The branches `acquire()` takes when the network is misbehaving.

    Each of these ends in a different message, and the message is the point:
    "Failed to acquire lock" (the upload never landed), "Failed to scan lock
    tickets" (we cannot see the queue), "Failed to confirm lock ownership" (the
    settlement re-read failed).  A wrong branch taken tells the user to debug a
    concurrency problem they do not have.
    """

    def test_a_transient_upload_failure_is_retried_and_then_wins(self):
        """A blip while depositing the ticket must not fail the push.

        The upload is the first thing `acquire()` does, so before this retry a
        single dropped connection was fatal to the whole push.
        """
        store = _SharedLockStore()
        attempts: list[str] = []
        real_upload = store.upload

        def flaky_upload(repo_id, parent, filename, content, replace=True,
                         progress_callback=None):
            attempts.append(filename)
            if len(attempts) == 1:
                raise OSError("connection reset by peer")
            return real_upload(repo_id, parent, filename, content, replace=replace)

        client = store.client("token123")
        client.upload_file.side_effect = flaky_upload

        lock = RemoteLock(client, "repo1", "/path", timeout=30, lease=60, settle=0)
        with patch("git_remote_seafile.lock.time.sleep"):
            lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertEqual(len(attempts), 2, "the failed upload must have been retried")
        self.assertIn(f"/path/.git-lock.d/{lock._nonce}.json", store.files)

    def test_an_upload_failure_that_outlasts_the_timeout_fails_closed(self):
        client = MagicMock()
        client.token = "token123"
        client.get_file_text.return_value = None
        client.upload_file.side_effect = OSError("connection reset by peer")

        lock = RemoteLock(client, "repo1", "/path", timeout=0, lease=60, settle=0)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()

        self.assertFalse(lock.acquired)
        # The transport error must survive into the message -- "failed to
        # acquire" alone leaves the user with nothing to act on.
        self.assertIn("Failed to acquire lock", str(ctx.exception))
        self.assertIn("connection reset", str(ctx.exception))

    def test_a_lock_error_from_the_upload_is_not_rewrapped(self):
        """Wrapping it would replace a precise reason with a vague one.

        A `RepositoryLockedError` carries the holder and the expiry; folding it
        into "Failed to acquire lock: ..." reads as a transport fault and sends
        the user looking in the wrong place.
        """
        client = MagicMock()
        client.token = "token123"
        client.get_file_text.return_value = None
        client.upload_file.side_effect = RepositoryLockedError("held by 'bob' on 'nodeB'")

        lock = RemoteLock(client, "repo1", "/path", timeout=0, lease=60, settle=0)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()

        self.assertEqual(str(ctx.exception), "held by 'bob' on 'nodeB'")

    def test_a_transient_failure_scanning_the_queue_is_retried(self):
        """The queue read fails once, then succeeds; the lock is still taken."""
        store = _FlakyScanStore(fail_on={1})

        lock = RemoteLock(store.client("token123"), "repo1", "/path",
                          timeout=30, lease=60, settle=0)
        with patch("git_remote_seafile.lock.time.sleep"), patch("sys.stderr", io.StringIO()):
            lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertGreaterEqual(store.listings, 2)

    def test_a_scan_failure_that_outlasts_the_timeout_names_the_scan(self):
        client = MagicMock()
        client.token = "token123"
        client.get_file_text.return_value = None
        client.list_dir.side_effect = RuntimeError("HTTP 500 internal server error")

        lock = RemoteLock(client, "repo1", "/path", timeout=0, lease=60, settle=0)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()

        self.assertIn("Failed to scan lock tickets", str(ctx.exception))

    def test_a_lock_error_from_the_scan_is_not_rewrapped(self):
        """Same contract as the upload path: a lock error keeps its identity.

        Without the re-raise it would be reported as "Failed to scan lock
        tickets: Repository is locked by ...", which reads as a listing fault
        and buries the holder's name in the middle of the sentence.
        """
        client = MagicMock()
        client.token = "token123"
        client.get_file_text.return_value = None
        client.list_dir.side_effect = RepositoryLockedError("held by 'bob' on 'nodeB'")

        lock = RemoteLock(client, "repo1", "/path", timeout=0, lease=60, settle=0)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()

        self.assertEqual(str(ctx.exception), "held by 'bob' on 'nodeB'")

    def test_a_transient_failure_during_the_settlement_rescan_is_retried(self):
        """The D21 confirming read is its own retry loop, not the scan's.

        Failing only the *confirming* read leaves the first read untouched, so
        this cannot be satisfied by the scan's retry path.
        """
        store = _FlakyScanStore(fail_on={2})

        lock = RemoteLock(store.client("token123"), "repo1", "/path",
                          timeout=30, lease=60, settle=0.05)
        with patch("git_remote_seafile.lock.time.sleep"), patch("sys.stderr", io.StringIO()):
            lock.acquire()

        self.assertTrue(lock.acquired)
        # 1: decide, 2: confirm (fails), 3: decide again, 4: confirm.
        self.assertGreaterEqual(store.listings, 4)

    def test_a_rescan_failure_that_outlasts_the_timeout_says_so(self):
        """Must not be reported as the queue being unreadable.

        The first read succeeded here -- we had already concluded we were the
        winner -- so "Failed to scan lock tickets" would misdescribe it.
        """
        store = _FlakyScanStore(fail_on=set(range(2, 100, 2)))  # every confirming read

        lock = RemoteLock(store.client("token123"), "repo1", "/path",
                          timeout=0, lease=60, settle=0.05)
        with patch("git_remote_seafile.lock.time.sleep"):
            with self.assertRaises(RepositoryLockedError) as ctx:
                lock.acquire()

        self.assertIn("Failed to confirm lock ownership", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
