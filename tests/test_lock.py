"""test_lock.py - Distributed lease lock: acquisition, fencing, ordering, status."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from git_remote_seafile.lock import RemoteLock, RepositoryLockedError

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
            self.assertEqual(mock_client.upload_file.call_count, 2)
        self.assertFalse(lock.acquired)
        mock_client.delete_entry.assert_any_call("repo1", "/path/.git-lock.json")

    def test_active_lock_timeout(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        active_lock = json.dumps({
            "owner": "alice",
            "machine": "nodeA",
            "timestamp": time.time(),
            "lease": 60,
        })
        mock_client.get_file_text.return_value = active_lock

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()
        self.assertIn("locked by 'alice'", str(ctx.exception))

    def test_expired_lease_override(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Lock created 120s ago with a 60s lease -> expired!
        expired_lock = json.dumps({
            "owner": "bob",
            "machine": "crashed-laptop",
            "timestamp": time.time() - 120,
            "lease": 60,
        })
        mock_client.get_file_text.return_value = expired_lock

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        err = io.StringIO()
        with patch("sys.stderr", err):
            lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertIn("Overriding expired lock", err.getvalue())

    def test_corrupt_lock_payload_handled_gracefully(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Corrupt JSON left by sudden network cut
        mock_client.get_file_text.return_value = "MALFORMED_NON_JSON{{{"

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()
        self.assertTrue(lock.acquired)

    def test_concurrent_threads_serialization(self):
        """Simulate two threads contending for a cooperative remote lock."""
        shared_remote_storage = {"lock": None}
        mock_client = MagicMock()

        def fake_get_file_text(repo_id, path):
            return shared_remote_storage["lock"]

        def fake_upload(repo_id, parent, filename, content, replace=True):
            if shared_remote_storage["lock"] is not None:
                # Collision simulation if already locked by another client
                info = json.loads(shared_remote_storage["lock"])
                new_info = json.loads(content)
                if time.time() < info["timestamp"] + info["lease"]:
                    if info.get("nonce") != new_info.get("nonce"):
                        raise Exception("Concurrent write rejected")
            shared_remote_storage["lock"] = content.decode("utf-8")

        def fake_delete(repo_id, path):
            shared_remote_storage["lock"] = None

        mock_client.get_file_text.side_effect = fake_get_file_text
        mock_client.upload_file.side_effect = fake_upload
        mock_client.delete_entry.side_effect = fake_delete

        results = []

        def worker(worker_id):
            client = MagicMock()
            client.token = f"user-{worker_id}"
            client.get_file_text.side_effect = fake_get_file_text
            client.upload_file.side_effect = fake_upload
            client.delete_entry.side_effect = fake_delete
            lock = RemoteLock(client, "repo1", "/path", timeout=5, lease=10)
            try:
                with lock:
                    results.append(f"{worker_id}_start")
                    time.sleep(0.05)
                    results.append(f"{worker_id}_end")
            except Exception as e:
                results.append(f"{worker_id}_error: {e}")

        _real_sleep = time.sleep
        with patch("time.sleep", side_effect=lambda s: _real_sleep(0.02)):
            t1 = threading.Thread(target=worker, args=(1,))
            t2 = threading.Thread(target=worker, args=(2,))
            t1.start()
            _real_sleep(0.01)  # Ensure t1 starts first
            t2.start()
            t1.join()
            t2.join()

        # Both workers should complete without error, strictly serialized
        self.assertEqual(len([r for r in results if "error" in r]), 0)
        self.assertEqual(len(results), 4)
        # Verify serialization: start -> end -> start -> end
        first_worker = results[0].split("_")[0]
        self.assertEqual(results[1], f"{first_worker}_end")

    def test_release_does_not_delete_a_lock_another_machine_took_over(self):
        """A lease can expire mid-operation, and someone else then holds it.

        Releasing unconditionally would delete *their* lock, letting two writers
        into the repository at once.
        """
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()
        self.assertTrue(lock.acquired)

        # Our lease expired while we worked; another machine took the lock.
        mock_client.get_file_text.return_value = json.dumps({
            "owner": "someone-else",
            "machine": "otherhost",
            "timestamp": time.time(),
            "lease": 60,
        })
        err = io.StringIO()
        with patch("sys.stderr", err):
            lock.release()

        # Must not delete the legacy lock taken over by another machine,
        # but must clean up its own ticket file (N-9)
        self.assertNotIn(
            unittest.mock.call("repo1", "/path/.git-lock.json"),
            mock_client.delete_entry.call_args_list,
        )
        mock_client.delete_entry.assert_called_with("repo1", f"/path/.git-lock.d/{lock._nonce}.json")
        self.assertFalse(lock.acquired)
        self.assertIn("otherhost", err.getvalue())

    def test_release_removes_its_own_lock(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()
        # The payload on the remote is our own.
        mock_client.get_file_text.return_value = mock_client.upload_file.call_args[0][3].decode("utf-8")
        lock.release()

        mock_client.delete_entry.assert_any_call("repo1", "/path/.git-lock.json")

    def test_release_does_not_delete_same_machine_lock_if_overridden(self):
        """When lease expires and same machine re-acquires with new nonce, release must not delete it."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock1 = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=1)
        lock1.acquire()
        self.assertTrue(lock1.acquired)

        # Process 2 on the same machine/account overrides after lease expiry
        payload1 = json.loads(mock_client.upload_file.call_args[0][3].decode("utf-8"))
        overriding_payload = dict(payload1)
        overriding_payload["nonce"] = "different-new-nonce-uuid"
        overriding_payload["timestamp"] = time.time() + 10
        mock_client.get_file_text.return_value = json.dumps(overriding_payload)

        err = io.StringIO()
        with patch("sys.stderr", err):
            lock1.release()

        # Must not delete the newly acquired legacy lock, but cleans up own ticket (N-9)
        self.assertNotIn(
            unittest.mock.call("repo1", "/path/.git-lock.json"),
            mock_client.delete_entry.call_args_list,
        )
        mock_client.delete_entry.assert_called_with("repo1", f"/path/.git-lock.d/{lock1._nonce}.json")
        self.assertFalse(lock1.acquired)

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

    def test_lock_handles_malformed_timestamp_without_crashing(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Timestamp is a string instead of float/int, lease is invalid
        mock_client.get_file_text.return_value = json.dumps({
            "owner": "alice",
            "machine": "nodeA",
            "timestamp": "INVALID_TIMESTAMP",
            "lease": "NOT_A_NUMBER",
        })

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        # Should override safely as expired instead of raising TypeError
        lock.acquire()
        self.assertTrue(lock.acquired)

    def test_lock_detects_post_write_collision(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Initial read says no lock, but verification read after upload sees competitor nonce
        call_count = [0]
        def fake_get_file_text(repo_id, path):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            return json.dumps({
                "owner": "competitor",
                "machine": "other-node",
                "nonce": "competitor-nonce-xyz",
                "timestamp": time.time(),
                "lease": 60,
            })

        mock_client.get_file_text.side_effect = fake_get_file_text
        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
        with self.assertRaises(RepositoryLockedError):
            lock.acquire()

    def test_dead_pid_fast_reclaim_on_local_machine(self):
        """A lock held by the same machine with a dead PID should be fast-reclaimed."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        dead_pid = 999999
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=False):
            stale_lock = json.dumps({
                "owner": "crashed-local-user",
                "machine": socket.gethostname(),
                "nonce": "crashed-nonce",
                "timestamp": time.time(),
                "lease": 60,
                "pid": dead_pid,
            })
            call_count = [0]
            def fake_read(repo_id, path):
                call_count[0] += 1
                if call_count[0] == 1:
                    return stale_lock
                return None
            mock_client.get_file_text.side_effect = fake_read
            lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
            err = io.StringIO()
            with patch("sys.stderr", err):
                lock.acquire()
            self.assertTrue(lock.acquired)
            self.assertIn("Reclaiming stale lock from dead local process", err.getvalue())

    def test_alive_pid_on_local_machine_blocks(self):
        """A lock held by the same machine with an alive PID must not be stolen."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        alive_pid = os.getpid()
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=True):
            mock_client.get_file_text.return_value = json.dumps({
                "owner": "running-local-user",
                "machine": socket.gethostname(),
                "nonce": "alive-nonce",
                "timestamp": time.time(),
                "lease": 60,
                "pid": alive_pid,
            })
            lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
            with self.assertRaises(RepositoryLockedError):
                lock.acquire()

    def test_dead_pid_fast_reclaim_ignores_different_machine_id(self):
        """When machine hostname matches but machine_id differs (e.g. container fleet), lock is not stolen."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        dead_pid = 999999
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=False):
            mock_client.get_file_text.return_value = json.dumps({
                "owner": "crashed-container-user",
                "machine": socket.gethostname(),
                "machine_id": "other-container-node-uuid",
                "nonce": "crashed-nonce",
                "timestamp": time.time(),
                "lease": 60,
                "pid": dead_pid,
            })
            lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
            with self.assertRaises(RepositoryLockedError):
                lock.acquire()

    def test_ticket_based_ordering_earliest_wins(self):
        """When multiple tickets exist, the earliest ticket wins."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        now = time.time()

        ticket_a = json.dumps({
            "owner": "alice",
            "machine": "nodeA",
            "nonce": "ticketA",
            "timestamp": now - 10,
            "lease": 60,
            "pid": 1234,
        })
        mock_client.get_file_text.side_effect = lambda repo_id, path: ticket_a if "ticketA" in path else None
        mock_client.list_dir.return_value = [
            {"name": "ticketA.json", "mtime": int(now - 10)},
        ]

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
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
        store.files["/path/.git-lock.d/holder-nonce.json"] = json.dumps({
            "owner": "holder", "machine": "nodeA", "nonce": "holder-nonce",
            "timestamp": float(now - 300), "order_ts": float(now - 300),
            "lease": 600, "pid": 111,
        }).encode("utf-8")
        store.files["/path/.git-lock.d/waiter-nonce.json"] = json.dumps({
            "owner": "waiter", "machine": "nodeB", "nonce": "waiter-nonce",
            "timestamp": float(now - 30), "order_ts": float(now - 30),
            "lease": 600, "pid": 222,
        }).encode("utf-8")
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
        ticket (and overwrote B's legacy mirror) and returned True -- so gc
        would have gone on to delete remote packfiles inside B's push.
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
        # A's failed renewal must not have touched B's legacy mirror.
        self.assertEqual(json.loads(store.files["/path/.git-lock.json"])["nonce"], b._nonce)

    def test_lock_lease_renew(self):
        """Lease renewal updates timestamps on both ticket and legacy lock."""
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
        self.assertEqual(mock_client.upload_file.call_count, upload_count_before + 2)

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
        self.assertEqual(mock_client.upload_file.call_count, upload_count + 2)

    def test_lock_status_and_unlock(self):
        """get_status and unlock methods inspect and clear locks."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        status_unlocked = lock.get_status()
        self.assertFalse(status_unlocked["locked"])

        lock.acquire()
        mock_client.get_file_text.return_value = mock_client.upload_file.call_args[0][3].decode("utf-8")
        status_locked = lock.get_status()
        self.assertTrue(status_locked["locked"])
        self.assertEqual(status_locked["owner"], lock._owner_id())

        self.assertTrue(lock.unlock())

    def test_abandoned_ticket_cleaned_up_on_acquire_timeout(self):
        """When acquire() times out, its candidate ticket must be deleted immediately."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Competitor holds lock
        mock_client.get_file_text.return_value = json.dumps({
            "owner": "competitor",
            "machine": "other-machine",
            "nonce": "competitor-nonce",
            "timestamp": time.time(),
            "lease": 60,
        })
        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
        with self.assertRaises(RepositoryLockedError):
            lock.acquire()

        self.assertFalse(lock.acquired)
        self.assertTrue(bool(lock._nonce))
        # Verify the client attempted to delete its own ticket in .git-lock.d/
        expected_ticket_path = f"/path/.git-lock.d/{lock._nonce}.json"
        mock_client.delete_entry.assert_any_call("repo1", expected_ticket_path)

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

