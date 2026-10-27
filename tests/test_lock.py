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
    LEASE_PREEMPTED,
    LEASE_UNKNOWN,
    RemoteLock,
    RepositoryLockedError,
    _queue_key,
)


# Every RemoteLock below passes `settle=0`: the settlement window is exercised
# on its own in TestLockSettlementWindow, and everywhere else it would only add
# a real second of sleeping to each acquisition.

#: How far in the past `_ticket` stamps the fixture it builds.
#:
#: A ticket from this helper models one *already on the remote* -- uploaded by a
#: peer that got there first -- so its FIFO key must be strictly earlier than
#: the acquire() that discovers it.  "Strictly" is the part that has to be
#: written down, because the wall clock cannot be relied on to supply it: on
#: Windows ``time.time()`` is backed by ``GetSystemTimeAsFileTime`` with a
#: 15.625 ms granularity through Python 3.12 (3.13 moved to
#: ``GetSystemTimePreciseAsFileTime``), so a ticket stamped at call time and an
#: acquire() a few hundred microseconds later land in the *same* tick.  The
#: queue then falls through the tied ``order_ts`` and the always-tied integer
#: mtime to the nonce, where our own random uuid4 can sort before the peer's
#: literal nonce and win a lock the test says must be refused.  CI saw exactly
#: that shape: intermittent "RepositoryLockedError not raised" failures on the
#: windows-latest 3.10/3.11/3.12 legs only.  One second is far wider than any
#: tick and still leaves the 60 s lease well inside its window.
_FIXTURE_TICKET_AGE = 1.0
_FOREIGN_MACHINE = f"foreign-{socket.gethostname()}"


def _ticket(nonce: str, owner: str = "alice", machine: str | None = None, **overrides) -> str:
    stamped = time.time() - _FIXTURE_TICKET_AGE
    payload = {
        "owner": owner,
        "machine": machine if machine is not None else _FOREIGN_MACHINE,
        "nonce": nonce,
        "timestamp": stamped,
        "order_ts": stamped,
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
            # Two writes, and both are the ticket: the upload that enters the
            # queue (unmarked) and the rewrite that marks it as the granted
            # holder.  The v0.1-v0.6 legacy .git-lock.json mirror is gone.
            self.assertEqual(mock_client.upload_file.call_count, 2)
        self.assertFalse(lock.acquired)
        mock_client.delete_entry.assert_called_once_with(
            "repo1", f"/path/.git-lock.d/{lock._nonce}.json"
        )

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
        }.get(key, default)
        mock_client = MagicMock()
        lock = RemoteLock(mock_client, "repo1", "/path")
        self.assertEqual(lock.timeout, 30)
        self.assertEqual(lock.lease, 90)
        self.assertEqual(lock.settle, 1)  # fixed: not a git config key

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

    def test_recycled_pid_on_local_machine_reclaims(self):
        """A lock held by the same machine where PID is alive but start_time differs is reclaimed."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/recycled-nonce.json", json.loads(_ticket(
            "recycled-nonce",
            owner="old-local-user",
            machine=socket.gethostname(),
            pid=12345,
            start_time="1000",
        )))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        err = io.StringIO()
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=True):
            with patch("git_remote_seafile.lock._process_creation_time", return_value="2000"):
                with patch("sys.stderr", err):
                    lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertIn("recycled PID 12345", err.getvalue())

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

    def test_dead_pid_reclaim_requires_the_same_install_id(self):
        """Same hostname and MAC, different install: the shared-bridge container pair.

        machine_id is the MAC (``uuid.getnode``), which a bridge gives to every
        container on it, and the hostname can be set equal as well; only the
        per-install token knows the two apart.  Reaping on a dead local PID
        would let one container take the other's *live* lock, so the ticket's
        install id must match ours.  An old ticket without the field keeps the
        machine_id-only behavior -- the tests above pin that.
        """
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/sister.json", json.loads(_ticket(
            "sister",
            owner="sister-container-user",
            machine=socket.gethostname(),
            install_id="the-other-container",
            pid=999999,
        )))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        with patch("git_remote_seafile.lock._LOCAL_INSTALL_ID", "this-install"):
            with patch("git_remote_seafile.lock._is_pid_alive", return_value=False):
                with self.assertRaises(RepositoryLockedError):
                    lock.acquire()

    def test_dead_pid_reclaims_when_the_install_id_matches(self):
        """The install id is an additional proof, not a replacement: a match reclaims."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/crashed.json", json.loads(_ticket(
            "crashed",
            owner="crashed-local-user",
            machine=socket.gethostname(),
            install_id="this-install",
            pid=999999,
        )))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        err = io.StringIO()
        with patch("git_remote_seafile.lock._LOCAL_INSTALL_ID", "this-install"):
            with patch("git_remote_seafile.lock._is_pid_alive", return_value=False):
                with patch("sys.stderr", err):
                    lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertIn("Reclaiming stale lock from dead local process", err.getvalue())

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

    def test_a_stalled_wall_clock_still_yields_to_a_ticket_already_on_the_remote(self):
        """Regression: Windows <=3.12 stamps time.time() in 15.625 ms steps.

        Every other test here lets the clock run, which on this machine puts the
        peer ticket and our own acquisition in different ticks and hides the
        bug.  Pinning ``time.time()`` to a single value reproduces what Python
        3.10-3.12 on Windows does to *every* acquisition: a peer ticket and the
        acquire() that follows it receive the same timestamp, the queue falls
        through the tied ``order_ts`` and the tied integer mtime to the nonce,
        and a uuid4 that happens to sort before the peer's name wins a lock it
        must lose.  CI caught this as an intermittent failure on the
        windows-latest 3.10/3.11/3.12 legs, never on 3.13+ or another OS.

        The peer is named ``zulu-`` on purpose: 'z' sorts above every hex digit,
        so the pre-fix code lost this test on *every* run rather than on the
        ~60% of runs where the uuid4 happens to sort first.

        The patch is global rather than scoped to the lock module because
        ``_ticket`` reads the same ``time`` module, and the point of the test is
        that both sides of the comparison see one frozen clock.
        """
        store = _SharedLockStore()
        with patch("time.time", return_value=1_800_000_000.0):
            store.put_ticket(
                "/path/.git-lock.d/zulu-nonce.json", json.loads(_ticket("zulu-nonce"))
            )

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

    def test_an_expired_unreaped_ticket_is_not_resurrected_over_a_live_peer(self):
        """The reap can fail (a transient delete error), leaving A's expired
        ticket in place next to B's live one.  A rewriting it with a fresh
        timestamp and the grant marker would put A back ahead of B."""
        store = _SharedLockStore()
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60, settle=0)
        a.acquire()
        store.expire_all(120)
        a_path = f"/path/.git-lock.d/{a._nonce}.json"
        before = store.files[a_path]
        store.put_ticket(
            "/path/.git-lock.d/b-nonce.json",
            json.loads(_ticket("b-nonce", owner="bob", machine="nodeB", holding=True)),
        )
        with patch("sys.stderr", io.StringIO()) as err:
            state = a.renew_state()
        self.assertEqual(state, LEASE_LOST)
        self.assertIn("expired", err.getvalue())
        self.assertEqual(store.files[a_path], before, "the expired ticket was rewritten")

    def test_an_expired_ticket_with_nobody_waiting_still_renews(self):
        """A slow holder that nobody is queued behind is not punished."""
        store = _SharedLockStore()
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60, settle=0)
        a.acquire()
        store.expire_all(120)
        self.assertEqual(a.renew_state(), LEASE_HELD)

    def test_plain_unlock_leaves_a_live_local_holder_alone_and_clears_a_dead_one(self):
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token-U"), "repo1", "/path", timeout=1, lease=60, settle=0)
        mine = dict(owner=lock._owner_id(), machine=socket.gethostname())

        # os.getppid() is a live process other than this one.
        store.put_ticket(
            "/path/.git-lock.d/live.json",
            json.loads(_ticket("live", pid=os.getppid(), **mine)),
        )
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.unlock()
        self.assertIn("live process", str(ctx.exception))
        self.assertIn("/path/.git-lock.d/live.json", store.files)

        # The same ticket from a process that is gone is the stale lock the
        # command exists for.
        store.put_ticket(
            "/path/.git-lock.d/live.json",
            json.loads(_ticket("live", pid=4294967295, **mine)),
        )
        self.assertTrue(lock.unlock())
        self.assertNotIn("/path/.git-lock.d/live.json", store.files)

    def test_plain_unlock_refuses_a_lock_from_another_install(self):
        """Same owner and hostname, different install: staleness cannot be judged here.

        The dead-PID check that makes a plain unlock safe runs in *this* PID
        namespace; a ticket written by another install (the shared-MAC
        container pair) is not covered by it, so the unlock must refuse and
        leave --force as the deliberate override.
        """
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token-U"), "repo1", "/path", timeout=1, lease=60, settle=0)
        mine = dict(owner=lock._owner_id(), machine=socket.gethostname())
        store.put_ticket(
            "/path/.git-lock.d/sister.json",
            json.loads(_ticket("sister", pid=4294967295, install_id="the-other-container", **mine)),
        )
        with patch("git_remote_seafile.lock._LOCAL_INSTALL_ID", "this-install"):
            with self.assertRaises(RepositoryLockedError) as ctx:
                lock.unlock()
        self.assertIn("different install of git-remote-seafile", str(ctx.exception))
        self.assertIn("/path/.git-lock.d/sister.json", store.files)

        self.assertTrue(lock.unlock(force=True))
        self.assertNotIn("/path/.git-lock.d/sister.json", store.files)

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

    def test_renew_write_failure_is_unknown_not_held(self):
        """A renew that cannot REWRITE the ticket is LEASE_UNKNOWN.

        The read side distinguishing takeover from unreadable ticket is
        tested above; this is the third state: the ticket is readable and
        ours, but the re-upload fails (connection dropped mid-renew).  The
        lease can no longer be *proven*, and verify_ownership() must answer
        False so the fence holds -- but the state is UNKNOWN, not LOST,
        because no other client provably holds anything.
        """
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None
        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        # Ticket reads back as ours...
        mock_client.get_file_text.return_value = json.dumps({"nonce": lock._nonce})
        # ...but the rewrite fails.
        def broken_upload(*args, **kwargs):
            raise RuntimeError("connection reset during write")
        mock_client.upload_file.side_effect = broken_upload
        with patch("sys.stderr", io.StringIO()):
            self.assertFalse(lock.renew())
        self.assertEqual(lock.ownership_state(), LEASE_UNKNOWN)

    def test_maybe_renew_rewrites_the_ticket_through_the_client(self):
        """The renewal path the push-side progress callback relies on works.

        cmd_push registers on_upload_progress -> lock.maybe_renew(20.0) so a
        long pack upload cannot outlive its lease; its wiring is pinned in
        test_helper.py (G-4).  This pins the lock side of that contract: a
        forced renewal rewrites the ticket through the client and leaves
        ownership verifiable.
        """
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=60, lease=600, settle=0)
        lock.acquire()
        self.assertTrue(lock.acquired)

        lock.maybe_renew(0)  # interval 0: force the renewal path now
        after = store.get_text("repo1", f"/path/.git-lock.d/{lock._nonce}.json")
        # The rewrite carried the same nonce, and ownership still verifies.
        self.assertEqual(json.loads(after)["nonce"], lock._nonce)
        self.assertTrue(lock.verify_ownership())

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

    def test_a_failed_renewal_is_not_retried_on_every_progress_callback(self):
        """After one failed attempt the next is a full interval away.

        The upload progress callback fires per 16 KiB block.  Pacing on success
        only made a lock that could not be renewed re-check its ticket (and
        print a warning) for every block of every transfer.
        """
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None
        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()
        lock._last_renewed = time.monotonic() - 25.0
        with patch.object(lock, "renew", return_value=False) as renew:
            for _ in range(500):
                lock.maybe_renew(20.0)
        self.assertEqual(renew.call_count, 1)

    def test_a_short_lease_is_renewed_at_a_third_of_it(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None
        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=9, settle=0)
        lock.acquire()
        mock_client.get_file_text.return_value = mock_client.upload_file.call_args[0][3].decode("utf-8")
        lock._last_renewed = time.monotonic() - 4.0  # past 9/3, well inside the caller's 20 s
        with patch.object(lock, "renew", return_value=True) as renew:
            lock.maybe_renew(20.0)
        self.assertEqual(renew.call_count, 1)

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
        # unlocking again is a no-op, and says so: the CLI uses this to print
        # "nothing to release" instead of a success that did nothing
        self.assertFalse(lock.unlock())

    def test_non_force_unlock_deletes_only_the_winner_ticket(self):
        """Non-force unlock deletes the ticket the authorization covered -- not the queue.

        unlock() used to sweep every .json in .git-lock.d even without --force,
        destroying a *waiter's* active ticket: the waiter's next renew_state()
        would then report LEASE_LOST and abort a push that was never contested.
        The ownership check authorizes deleting the winning ticket only;
        --force remains the queue sweep.
        """
        import socket
        store = _SharedLockStore()
        # The winner belongs to this machine/user, so non-force is authorized.
        # It must also be the earliest *active* ticket -- get_status()'s winner
        # is active[0] -- so the foreign waiter's ticket is stamped older but
        # expired, and a live one younger than ours.
        # owner must match _owner_id() = sha256(token)[:8]; the fixture client
        # uses token "token123", so derive the owner the same way.
        import hashlib
        owner_id = hashlib.sha256(b"token123").hexdigest()[:8]
        # The winner must be *active* to be get_status()'s winner, and a
        # same-machine ticket is only active when its PID is alive -- use this
        # test process's own pid (README: the fixture hostnames nodeA/nodeB
        # are not usable here because the winner must match the real machine).
        winner = _ticket("winner-nonce", owner=owner_id, machine=socket.gethostname(),
                         timestamp=time.time() - 10, order_ts=time.time() - 10,
                         lease=60, pid=os.getpid())
        store.put_ticket("/path/.git-lock.d/winner-nonce.json", json.loads(winner))
        store.put_ticket("/path/.git-lock.d/winner-nonce.json", json.loads(winner))
        expired_waiter = _ticket("expired-waiter", owner="bob", machine="elsewhere",
                                 timestamp=time.time() - 3600, order_ts=time.time() - 3600,
                                 lease=60, pid=999999)
        store.put_ticket("/path/.git-lock.d/expired-waiter.json", json.loads(expired_waiter))
        live_waiter = _ticket("live-waiter", owner="carol", machine="third-mach",
                              timestamp=time.time() + 30, order_ts=time.time() + 30,
                              lease=600, pid=999999)
        store.put_ticket("/path/.git-lock.d/live-waiter.json", json.loads(live_waiter))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        self.assertTrue(lock.unlock(force=False))

        remaining = sorted(k for k in store.files if ".git-lock.d" in k)
        self.assertEqual(remaining,
                         ["/path/.git-lock.d/expired-waiter.json",
                          "/path/.git-lock.d/live-waiter.json"],
                         "non-force unlock must not touch other clients' tickets")

    def test_force_unlock_sweeps_the_whole_queue(self):
        """--force is the break-the-glass path: every ticket goes."""
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/a.json",
                         json.loads(_ticket("a", owner="alice", machine="nodeA")))
        store.put_ticket("/path/.git-lock.d/b.json",
                         json.loads(_ticket("b", owner="bob", machine="nodeB")))

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        self.assertTrue(lock.unlock(force=True))
        self.assertEqual([k for k in store.files if ".git-lock.d" in k], [])

    def test_force_unlock_reports_a_refused_delete(self):
        """A delete the server refuses must not be reported as a successful unlock.

        ``delete_entry`` answers a refused delete with ``False`` rather than an
        exception, and ``unlock()`` used to ignore its return value entirely --
        so ``unlock --force`` printed "Forcibly unlocked" over a queue that was
        still there, and the operator's next move is to push.
        """
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/a.json",
                         json.loads(_ticket("a", owner="alice", machine="nodeA")))
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)

        with patch.object(lock.client, "delete_entry", return_value=False):
            with self.assertRaises(RepositoryLockedError) as ctx:
                lock.unlock(force=True)

        message = str(ctx.exception)
        self.assertIn("0 of 1", message)
        self.assertIn("a.json", message)
        self.assertEqual(
            [k for k in store.files if ".git-lock.d" in k],
            ["/path/.git-lock.d/a.json"],
            "the ticket survived, so the unlock must not have claimed it went",
        )

    def test_force_unlock_reports_a_partial_sweep(self):
        """A sweep that removed one ticket and not the other says exactly that.

        The partial state is the one an operator most needs described: some
        tickets gone, others still holding the lock.  Collapsing it to "ok"
        (or to a bare failure) loses the half that matters.
        """
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/a.json",
                         json.loads(_ticket("a", owner="alice", machine="nodeA")))
        store.put_ticket("/path/.git-lock.d/b.json",
                         json.loads(_ticket("b", owner="bob", machine="nodeB")))
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        real_delete = store.delete

        def refuse_b(repo_id, path):
            if path.endswith("b.json"):
                return False
            return real_delete(repo_id, path)

        with patch.object(lock.client, "delete_entry", side_effect=refuse_b):
            with self.assertRaises(RepositoryLockedError) as ctx:
                lock.unlock(force=True)

        message = str(ctx.exception)
        self.assertIn("1 of 2", message)
        self.assertIn("b.json", message)
        self.assertNotIn("a.json", message, "only the survivor belongs in the failure list")
        self.assertEqual([k for k in store.files if ".git-lock.d" in k],
                         ["/path/.git-lock.d/b.json"])

    def test_force_unlock_succeeds_when_the_ticket_is_already_gone(self):
        """A ticket another client removed is not a failure.

        ``delete_entry`` reports a refused delete and an already-absent ticket
        the same way -- ``False`` -- so the sweep re-reads the queue instead of
        guessing.  This test has no teeth against the *original* code (which
        returned True regardless); its job is to stop the fix overcorrecting
        into a failure that would send the operator to ``--force`` on a
        repository that is already free.
        """
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        locked = {"locked": True, "owner": "alice", "machine": "nodeA", "nonce": "a"}
        entry = [{"type": "file", "name": "a.json", "mtime": int(time.time())}]

        with patch.object(RemoteLock, "get_status", return_value=locked), \
                patch.object(lock.client, "list_dir", return_value=entry), \
                patch.object(lock.client, "delete_entry", return_value=False), \
                patch.object(lock.client, "get_file_text", return_value=None):
            self.assertTrue(lock.unlock(force=True))

    def test_force_unlock_raises_when_the_queue_cannot_be_listed(self):
        """A queue we could not read is 'nothing was deleted', not a success.

        The listing needs its own error handling: without it a failed list
        falls straight through to the ``return True`` that follows the sweep,
        which is the shape the original code had.
        """
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        locked = {"locked": True, "owner": "alice", "machine": "nodeA", "nonce": "a"}

        with patch.object(RemoteLock, "get_status", return_value=locked), \
                patch.object(lock.client, "list_dir", side_effect=RuntimeError("listing blew up")):
            with self.assertRaises(RepositoryLockedError) as ctx:
                lock.unlock(force=True)

        message = str(ctx.exception)
        self.assertIn("listing blew up", message)
        self.assertIn("nothing was deleted", message)

    def test_force_unlock_says_nothing_to_release_when_the_queue_is_empty(self):
        """An empty queue is 'nothing to release', not a successful sweep.

        The status check saw a holder, but another client released it in
        between; the CLI prints "nothing to release" and exits 0, which is
        true of the queue as the sweep found it.
        """
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        locked = {"locked": True, "owner": "alice", "machine": "nodeA", "nonce": "a"}

        with patch.object(RemoteLock, "get_status", return_value=locked), \
                patch.object(lock.client, "list_dir", return_value=[]):
            self.assertFalse(lock.unlock(force=True))

    def test_non_force_unlock_reports_a_refused_delete(self):
        """The owner's own release must not claim success on a refused delete.

        The non-force path swallowed every exception and returned True, so the
        CLI printed "Unlocked repository" while our own ticket was still in the
        queue holding the lock.
        """
        store = _SharedLockStore()
        owner_id = hashlib.sha256(b"token123").hexdigest()[:8]
        winner = _ticket("winner-nonce", owner=owner_id, machine=socket.gethostname(),
                         timestamp=time.time() - 10, order_ts=time.time() - 10,
                         lease=60, pid=os.getpid())
        store.put_ticket("/path/.git-lock.d/winner-nonce.json", json.loads(winner))
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)

        with patch.object(lock.client, "delete_entry", return_value=False):
            with self.assertRaises(RepositoryLockedError) as ctx:
                lock.unlock(force=False)

        self.assertIn("winner-nonce.json", str(ctx.exception))
        self.assertEqual([k for k in store.files if ".git-lock.d" in k],
                         ["/path/.git-lock.d/winner-nonce.json"])

    def test_non_force_unlock_raises_when_the_winner_names_no_ticket(self):
        """A holder with no ticket to delete is not a release.

        The queue said locked, but the winner carried no nonce, so there was
        nothing to delete; answering "unlocked" would be a claim with nothing
        behind it.
        """
        store = _SharedLockStore()
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)
        owner_id = hashlib.sha256(b"token123").hexdigest()[:8]
        locked = {"locked": True, "owner": owner_id, "machine": socket.gethostname(),
                  "nonce": None}

        with patch.object(RemoteLock, "get_status", return_value=locked):
            with self.assertRaises(RepositoryLockedError) as ctx:
                lock.unlock(force=False)

        self.assertIn("names no ticket", str(ctx.exception))

    def test_unlock_refuses_to_act_on_an_unverifiable_queue(self):
        """A listing failure must not read as 'nothing to release'.

        The old behavior returned False for a get_status() that swallowed a
        listing error, so the CLI printed 'not locked; nothing to release'
        for a queue it never saw.  Now the unknown state raises, and the
        operator decides.
        """
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/foreign.json",
                         json.loads(_ticket("foreign", owner="alice", machine="nodeA")))
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)

        def broken_list(repo_id, path):
            raise RuntimeError("transient server error")
        # _scan_tickets reaches the store through the mock client built in
        # .client() -- side_effect was bound at construction, so patch the
        # client's method, not the store's.
        client = lock.client
        with patch.object(client, "list_dir", side_effect=broken_list):
            with self.assertRaises(RepositoryLockedError) as ctx:
                lock.unlock(force=False)
        self.assertIn("unverifiable", str(ctx.exception))

    def test_get_status_reports_a_listing_failure_as_unknown_not_unlocked(self):
        """get_status must distinguish 'cannot tell' from 'unlocked'.

        A transient server error during lock-status used to print 'UNLOCKED'
        and exit 0 -- exactly the false confidence 'unlock --force' feeds on.
        """
        store = _SharedLockStore()
        store.put_ticket("/path/.git-lock.d/a.json",
                         json.loads(_ticket("a", owner="alice", machine="nodeA")))
        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=5, lease=60, settle=0)

        def broken_list(repo_id, path):
            raise RuntimeError("transient server error")
        client = lock.client
        with patch.object(client, "list_dir", side_effect=broken_list):
            status = lock.get_status()
        self.assertIsNone(status["locked"])
        self.assertIn("transient server error", status.get("error", ""))

    def test_a_ticket_without_order_ts_is_ordered_by_its_own_timestamp(self):
        """There is one ordering key, applied per ticket.

        A ticket from a client older than v0.6.3 has no ``order_ts``.  It used
        to flip the *whole queue* onto mtime ordering; now only that ticket
        falls back (to its own timestamp, then its mtime), so a queue never
        changes mode depending on who happens to be in it.
        """
        store = _SharedLockStore()
        now = int(time.time())
        legacy_payload = json.loads(_ticket("legacy", owner="alice", machine="nodeA",
                         timestamp=now - 60, lease=600, pid=999999))
        del legacy_payload["order_ts"]
        modern = _ticket("modern", owner="bob", machine="nodeB",
                         timestamp=now - 120, order_ts=now - 120,
                         lease=600, pid=999999)
        store.put_ticket("/path/.git-lock.d/legacy.json", legacy_payload)
        store.put_ticket("/path/.git-lock.d/modern.json", json.loads(modern))
        # mtimes are irrelevant now: both tickets order on their own stamps
        # (legacy: timestamp now-60; modern: order_ts now-120).
        store.mtime_override["/path/.git-lock.d/legacy.json"] = now - 300
        store.mtime_override["/path/.git-lock.d/modern.json"] = now - 30

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()
        self.assertIn("locked by 'bob'", str(ctx.exception))

    def test_acquire_does_not_probe_for_the_legacy_mirror(self):
        """The v0.1-v0.3 mirror is gone; acquire() must not spend a request on it."""
        store = _SharedLockStore()
        client = store.client("token123")
        lock = RemoteLock(client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()
        probed = [c.args[1] for c in client.get_file_text.call_args_list if len(c.args) > 1]
        self.assertFalse(any(str(p).endswith(".git-lock.json") for p in probed), probed)

    def test_lock_status_flags_an_expired_queue_as_stale(self):
        """A dead lock must say so, not pose as one that expires in 0 seconds.

        get_status reads without reaping, so an expired ticket used to be
        presented as the current holder with ``Expires: in 0s`` -- indistinguishable
        from a live lock, with no hint that a plain push would simply reap it
        and take the lock.
        """
        store = _SharedLockStore()
        store.put_ticket(
            "/path/.git-lock.d/old.json",
            json.loads(_ticket("old", timestamp=time.time() - 3600, lease=60)),
        )
        lock = RemoteLock(store.client("tok"), "repo1", "/path", settle=0)

        status = lock.get_status()
        self.assertTrue(status["locked"])
        self.assertTrue(status["stale"])
        self.assertEqual(status["nonce"], "old")
        self.assertEqual(status["expires_in"], 0)

        # a live ticket on top makes the queue non-stale again
        store.put_ticket(
            "/path/.git-lock.d/live.json",
            json.loads(_ticket("live", timestamp=time.time(), lease=60)),
        )
        status = lock.get_status()
        self.assertTrue(status["locked"])
        self.assertFalse(status["stale"])
        self.assertEqual(status["nonce"], "live")

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
            mock_ctypes = MagicMock()
            # _is_pid_alive builds its own WinDLL("kernel32",
            # use_last_error=True) and reads the failure through
            # ctypes.get_last_error() -- the documented pattern: reading
            # GetLastError() through ctypes.windll can return a
            # stale value.  The mock therefore answers get_last_error().
            mock_ctypes.get_last_error.return_value = 5  # ERROR_ACCESS_DENIED
            mock_ctypes.WinDLL.return_value = mock_kernel32
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


class TestHoldingMarkerOutranksTheServerTimeEstimate(unittest.TestCase):
    """A1: a granted lock must not be preempted by a later arrival's clock.

    ``order_ts`` is a client-side *estimate* of server time -- the local clock
    plus the offset implied by the ``Date`` header, whose one-second resolution
    means two clients routinely disagree by a fraction of a second.  Ordering
    the whole queue on it made that estimate the thing that decides who holds
    the lock: a newcomer whose estimate came out half a second behind an active
    holder's sorted ahead of it and acquired the lock while the holder was
    still pushing, with its own ticket intact and unexpired.  Reading our own
    ticket back could not detect that -- it is still ours.

    The fix is a marker.  The winner publishes ``holding: true`` on its ticket,
    and the queue sorts granted holders ahead of every ticket that has merely
    asked, so "estimated an earlier time" no longer outranks "was granted".
    """

    def test_a_newcomer_with_an_earlier_estimate_cannot_preempt_the_holder(self):
        store = _SharedLockStore()
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60, settle=0)
        a.acquire()
        self.assertTrue(a.acquired)

        b = RemoteLock(store.client("token-B"), "repo1", "/path", timeout=0, lease=60, settle=0)
        # Half a second behind is well inside what the one-second Date header
        # can produce, and it is enough to have won the queue before the fix.
        with patch.object(b, "_get_server_time", return_value=a._order_ts - 0.5):
            with patch("sys.stderr", io.StringIO()):
                with self.assertRaises(RepositoryLockedError) as ctx:
                    b.acquire()

        self.assertIn("locked by", str(ctx.exception))
        # And A, mid-push throughout, is never told it lost the lock it holds.
        with patch("sys.stderr", io.StringIO()):
            self.assertTrue(a.verify_ownership())
        self.assertEqual(a.ownership_state(), LEASE_HELD)

    def test_the_winner_publishes_the_marker_and_every_renewal_keeps_it(self):
        """The renewal trap: renewal rewrites the whole ticket body.

        The initial upload, the post-win rewrite and every renewal each used to
        build their own copy of the ticket dict.  Adding the marker to one of
        them and not the others drops it on the first renewal -- and the ticket
        still looks perfectly well-formed, so nothing notices.
        """
        store = _SharedLockStore()
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60, settle=0)
        a.acquire()

        ticket_path = f"/path/.git-lock.d/{a._nonce}.json"
        first = json.loads(store.files[ticket_path])
        self.assertTrue(first["holding"], "the granted holder must mark its ticket")
        self.assertGreater(first["held_since"], 0)

        self.assertTrue(a.maybe_renew(0))  # interval 0: force the renewal path now
        after = json.loads(store.files[ticket_path])
        self.assertTrue(
            after["holding"],
            "a renewal that dropped the marker would un-grant a lock we still hold",
        )

    def test_verify_ownership_reports_preemption_while_our_ticket_is_alive(self):
        """The failure reading our own ticket back cannot see.

        This is the shape A1 produces once the settlement window misses a
        ticket: two clients both marked holding, each with a live ticket.  Our
        ticket reads back as ours, so renew() would happily renew it -- which
        is exactly why the fence has to look at the queue, and why the state is
        PREEMPTED rather than LOST: nothing was reaped, we were overtaken.
        """
        store = _SharedLockStore()
        a = RemoteLock(store.client("token-A"), "repo1", "/path", timeout=5, lease=60, settle=0)
        a.acquire()

        store.put_ticket("/path/.git-lock.d/bob-nonce.json", {
            "owner": "bob", "machine": "nodeB", "nonce": "bob-nonce",
            "timestamp": time.time(), "order_ts": a._order_ts - 1.0,
            "lease": 60, "pid": 4242, "holding": True,
        })

        err = io.StringIO()
        with patch("sys.stderr", err):
            self.assertFalse(a.verify_ownership())
        self.assertEqual(a.ownership_state(), LEASE_PREEMPTED)
        self.assertIn("preempted by 'bob'", err.getvalue())
        # A's own ticket is untouched -- a verification step never reaps.
        self.assertIn(f"/path/.git-lock.d/{a._nonce}.json", store.files)

    def test_a_crashed_holder_is_still_reaped_despite_the_marker(self):
        """The marker must not turn a dead holder into a permanent lock.

        A crashed holder's ticket keeps ``holding: true`` until its lease runs
        out, exactly as an unmarked one did, so reclaim is unchanged: expired
        lease, or a dead PID on this machine.
        """
        store = _SharedLockStore()
        now = time.time()
        store.put_ticket("/path/.git-lock.d/dead-nonce.json", {
            "owner": "crashed", "machine": "nodeZ", "nonce": "dead-nonce",
            "timestamp": now - 600, "order_ts": now - 600,
            "lease": 60, "pid": 424242, "holding": True,
        })

        lock = RemoteLock(store.client("token123"), "repo1", "/path", timeout=0, lease=60, settle=0)
        with patch("sys.stderr", io.StringIO()):
            lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertNotIn("/path/.git-lock.d/dead-nonce.json", store.files)

    def test_the_queue_scan_is_rate_limited_but_the_lease_is_not(self):
        """Fencing once per deleted ref must not list the queue once per ref.

        A `push --delete` of forty refs fences forty times, and each fence used
        to be a ticket read plus a ticket rewrite.  The queue scan is the extra
        read, so it is cached for a window; the lease is still re-proven on
        every call, because a *reaped* ticket has to be caught immediately
        rather than up to a window later.
        """
        store = _LateCommitStore()
        client = store.client("token123")
        lock = RemoteLock(client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock.acquire()

        store.listings = 0
        uploads_before = client.upload_file.call_count
        with patch("sys.stderr", io.StringIO()):
            self.assertTrue(lock.verify_ownership())
            self.assertTrue(lock.verify_ownership())

        self.assertEqual(store.listings, 1, "the second fence reused the cached scan")
        self.assertEqual(
            client.upload_file.call_count, uploads_before + 2,
            "the lease must be re-proven on every call, not once per window",
        )

    def test_the_sort_key_puts_a_granted_holder_ahead_of_an_earlier_estimate(self):
        """The marker is the *primary* key, not one more element of the tuple.

        If it were appended after ``order_ts`` it would only break ties, and a
        holder with a later estimate would still lose -- which is the bug.
        """
        holder = {"holding": True, "_order_key": (200.0, 0.0, "zulu")}
        newcomer = {"holding": False, "_order_key": (100.0, 0.0, "alpha")}
        self.assertIs(min([holder, newcomer], key=_queue_key), holder)

        # Among peers, and for tickets from before the marker existed, the FIFO
        # tuple still decides, so the order stays total and identical for all.
        first = {"_order_key": (100.0, 0.0, "alpha")}
        second = {"_order_key": (200.0, 0.0, "zulu")}
        self.assertIs(min([second, first], key=_queue_key), first)


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


class TestUnreadableTicketFailsClosed(unittest.TestCase):
    """A ticket that exists but does not parse is a read failure (ROADMAP T8).

    A 2xx that carried a truncated ticket body would otherwise read a *live*
    holder out of the queue, and a second client would acquire mid-push.
    Verification fails closed; acquisition stays lenient, because a planted
    unreadable file must not block the lock for ever.
    """

    def setUp(self):
        self.store = _SharedLockStore()
        self.lock = RemoteLock(self.store.client("token123"), "repo1", "/path",
                               timeout=5, lease=60, settle=0)

    def _plant_garbage(self):
        self.store.files["/path/.git-lock.d/deadbeef.json"] = b'{"trunc'

    def test_verify_ownership_answers_unknown_over_an_unreadable_queue(self):
        self.lock.acquired = True
        self.lock._nonce = "mine"
        self.lock._identity = ("own", "this-machine")
        self.lock._order_ts = 0.0
        self.lock._holding = True
        self.store.put_ticket("/path/.git-lock.d/mine.json", json.loads(
            _ticket("mine", owner="own", machine="this-machine")))
        self._plant_garbage()
        self.assertFalse(self.lock.verify_ownership())
        self.assertEqual(self.lock.ownership_state(), LEASE_UNKNOWN)

    def test_get_status_refuses_to_read_the_queue_as_unlocked(self):
        self._plant_garbage()
        status = self.lock.get_status()
        self.assertIsNone(status["locked"], "garbage must not read as UNLOCKED")
        self.assertIn("unreadable", status["error"])

    def test_acquire_still_succeeds_with_garbage_in_the_queue(self):
        self._plant_garbage()
        with self.lock:
            self.assertTrue(self.lock.acquired)
        self.assertIn("/path/.git-lock.d/deadbeef.json", self.store.files,
                      "acquire must not delete a ticket it cannot parse")

    def test_a_holder_with_a_torn_ticket_refuses_to_renew(self):
        self.lock.acquired = True
        self.lock._nonce = "mine"
        self.lock._identity = ("own", "this-machine")
        self.lock._order_ts = 0.0
        self.lock._holding = True
        self.store.files["/path/.git-lock.d/mine.json"] = b'{"trunc'
        state = self.lock.renew_state()
        self.assertEqual(state, LEASE_UNKNOWN)


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
        attempts: list[bool] = []
        real_upload = store.upload

        def flaky_upload(repo_id, parent, filename, content, replace=True,
                         progress_callback=None):
            attempts.append(json.loads(content)["holding"])
            if len(attempts) == 1:
                raise OSError("connection reset by peer")
            return real_upload(repo_id, parent, filename, content, replace=replace)

        client = store.client("token123")
        client.upload_file.side_effect = flaky_upload

        lock = RemoteLock(client, "repo1", "/path", timeout=30, lease=60, settle=0)
        with patch("git_remote_seafile.lock.time.sleep"):
            lock.acquire()

        self.assertTrue(lock.acquired)
        # Three writes, and the marker tells them apart: the entry upload that
        # was dropped, the retry that landed, and only then the rewrite that
        # publishes the grant.  A marker written on the *first* attempt would
        # claim the lock before the queue had agreed we won.
        self.assertEqual(
            attempts, [False, False, True],
            "the failed entry upload must be retried before the marker is published",
        )
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


class _FakeClock:
    """A monotonic clock and a sleep that advance together, with hooks.

    ``on_sleep`` runs after each advance so a test can change the remote at a
    chosen moment -- a holder finishing, a new contender arriving -- without
    the test taking real seconds.
    """

    def __init__(self, on_sleep=None):
        self.now = 1000.0
        self.on_sleep = on_sleep
        self.sleeps = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self)


class TestWaitingIsBoundedByProgressNotByTheClockAlone(unittest.TestCase):
    """The lock timeout restarts whenever the queue moves.

    Measured against a real Seafile server, six clients pushing at once each
    waited for those ahead: the sixth took 13.4 s of a 15 s budget, so a seventh
    would have been told "locked" while the queue was draining normally.
    """

    def _holder(self, store, name, age):
        store.put_ticket(
            f"/path/.git-lock.d/{name}.json",
            json.loads(_ticket(name, owner=name, machine="nodeA", lease=600,
                               timestamp=time.time() - age, order_ts=time.time() - age)),
        )

    def _run(self, store, clock, timeout=5):
        lock = RemoteLock(store.client("tok"), "repo1", "/path", timeout=timeout, lease=600, settle=0)
        with patch("git_remote_seafile.lock.time.monotonic", clock.monotonic),                 patch("git_remote_seafile.lock.time.sleep", clock.sleep):
            lock.acquire()
        return lock

    def test_a_queue_that_keeps_moving_outlasts_the_flat_timeout(self):
        store = _SharedLockStore()
        self._holder(store, "h1", 30)
        self._holder(store, "h2", 20)

        def advance(clock):
            # h1 finishes at ~4 s, h2 at ~8 s: each is past the 5 s budget
            # measured from the start, neither is past it from the last move.
            if clock.now - 1000.0 >= 4:
                store.files.pop("/path/.git-lock.d/h1.json", None)
            if clock.now - 1000.0 >= 8:
                store.files.pop("/path/.git-lock.d/h2.json", None)

        clock = _FakeClock(advance)
        lock = self._run(store, clock, timeout=5)
        self.assertTrue(lock.acquired)
        self.assertGreaterEqual(clock.now - 1000.0, 8, "the wait must really have exceeded the flat timeout")

    def test_a_queue_stuck_behind_one_holder_still_times_out(self):
        store = _SharedLockStore()
        self._holder(store, "h1", 30)
        clock = _FakeClock()
        with self.assertRaises(RepositoryLockedError):
            self._run(store, clock, timeout=5)
        self.assertLess(clock.now - 1000.0, 5 + 4, "a stuck queue must not be waited on past the timeout")

    def test_progress_cannot_hold_a_push_forever(self):
        store = _SharedLockStore()
        self._holder(store, "h0", 500)
        counter = {"n": 0}

        def churn(clock):
            # A new, earlier holder replaces the previous one every poll, so the
            # queue "moves" forever.  The absolute cap has to end it.
            counter["n"] += 1
            store.files.clear()
            self._holder(store, f"h{counter['n']}", 500 + counter["n"])

        clock = _FakeClock(churn)
        with self.assertRaises(RepositoryLockedError):
            self._run(store, clock, timeout=5)
        self.assertGreaterEqual(clock.now - 1000.0, 50)
        self.assertLess(clock.now - 1000.0, 130)

    def test_a_waiting_ticket_is_uploaded_once_not_on_every_poll(self):
        store = _SharedLockStore()
        self._holder(store, "h1", 30)
        client = store.client("tok")
        own_uploads: list[str] = []
        real_upload = store.upload

        def counting_upload(repo_id, parent, filename, content, **kw):
            if lock._nonce and filename == f"{lock._nonce}.json":
                own_uploads.append(filename)
            return real_upload(repo_id, parent, filename, content, **kw)

        client.upload_file.side_effect = counting_upload

        def release_at_30s(clock):
            if clock.now - 1000.0 >= 30:
                store.files.pop("/path/.git-lock.d/h1.json", None)

        clock = _FakeClock(release_at_30s)
        lock = RemoteLock(client, "repo1", "/path", timeout=60, lease=60, settle=0)
        with patch("git_remote_seafile.lock.time.monotonic", clock.monotonic),                 patch("git_remote_seafile.lock.time.sleep", clock.sleep):
            lock.acquire()
        self.assertTrue(lock.acquired)
        # ~15 polls over 30 s.  One initial upload, one lease refresh at 20 s,
        # and the grant marker: a handful, where it used to be one per poll.
        self.assertLessEqual(len(own_uploads), 4, own_uploads)
        self.assertGreaterEqual(len(own_uploads), 2, own_uploads)

