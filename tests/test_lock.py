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

from git_remote_seafile.lock import RemoteLock, RepositoryLockedError

LEGACY_LOCK = "/path/.git-lock.json"


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

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
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

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60)
        lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertNotIn(LEGACY_LOCK, store.files)

    def test_active_foreign_ticket_blocks(self):
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/alice-nonce.json", json.loads(_ticket("alice-nonce")))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60)
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

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60)
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

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60)
        lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertIn("/path/.git-lock.d/garbage.json", store.files)

    def test_release_only_deletes_its_own_ticket(self):
        """A lease can lapse mid-operation and someone else then holds the lock.

        Releasing must delete only our (already reaped) ticket path and leave
        the new holder's ticket untouched.
        """
        store = _SharedLockStore()
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60)
        a.acquire()
        self.assertTrue(a.acquired)
        store.expire_all(120)

        b = RemoteLock(store.client("token-B"), "repo1", "/path", timeout=5, lease=60)
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
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60)
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

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0.1, lease=10)
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

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()

        payload = mock_client.upload_file.call_args[0][3].decode("utf-8")
        self.assertNotIn("s3cr3t", payload)
        self.assertEqual(
            json.loads(payload)["owner"],
            hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8],
        )

    @patch("git_remote_seafile.lock.get_git_config_int")
    def test_lock_reads_git_config_defaults(self, mock_get_cfg):
        mock_get_cfg.side_effect = lambda key, default: 30 if "timeout" in key else 90
        mock_client = MagicMock()
        lock = RemoteLock(mock_client, "repo1", "/path")
        self.assertEqual(lock.timeout, 30)
        self.assertEqual(lock.lease, 90)

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

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60)
        lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertNotIn("/path/.git-lock.d/alice-nonce.json", store.files)

    def test_dead_pid_fast_reclaim_on_local_machine(self):
        """A lock held by the same machine with a dead PID should be fast-reclaimed."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/crashed-nonce.json", json.loads(_ticket(
            "crashed-nonce",
            owner="crashed-local-user",
            machine=socket.gethostname(),
            pid=999999,
        )))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60)
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

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60)
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

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60)
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

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60)
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

        lock = RemoteLock(store.client("token-x"), "repo1", "/path", timeout=0, lease=600)
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
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60)
        a.acquire()
        self.assertTrue(a.acquired)

        # A stops renewing; every lock artifact ages past its lease.
        store.expire_all(120)

        # B arrives, reaps A's expired ticket, and acquires.
        b = RemoteLock(store.client("token-B"), "repo1", "/path", timeout=5, lease=60)
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

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()
        # renew() reads the ticket back before rewriting it (the takeover
        # fence), so the client must serve what acquire() uploaded.
        mock_client.get_file_text.return_value = mock_client.upload_file.call_args[0][3].decode("utf-8")
        upload_count_before = mock_client.upload_file.call_count

        res = lock.renew()
        self.assertTrue(res)
        self.assertEqual(mock_client.upload_file.call_count, upload_count_before + 1)

    def test_renew_fails_when_the_ticket_is_gone(self):
        """A missing ticket means the lease lapsed and was reaped; renew must fail."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
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

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
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
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60)

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

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60)
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


if __name__ == "__main__":
    unittest.main()
