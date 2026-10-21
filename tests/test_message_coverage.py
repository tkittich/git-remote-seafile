"""A test for each message that tests/test_message_style.py used to list as untested.

Each test triggers the message and quotes a distinctive part of its text, which
is what the style guard looks for.  The behaviour around the message is covered
where the code lives; these pin only that the user is told.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))

import seafile_snapshot as snap  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402
from git_remote_seafile.config import RemoteConfig  # noqa: E402
from git_remote_seafile.lock import RemoteLock  # noqa: E402
from git_remote_seafile.packs import fetch_pack_artifact  # noqa: E402
from git_remote_seafile.refs import iter_refs  # noqa: E402


def _stderr():
    err = io.StringIO()
    return err, patch("sys.stderr", err)


class TestClientAndConfigMessages(unittest.TestCase):
    def test_a_token_over_plain_http_is_warned_about(self):
        err, redirect = _stderr()
        with patch.dict("os.environ", {"SEAFILE_SERVER": "http://seafile.example.com",
                                       "SEAFILE_TOKEN": "tok"}, clear=True), redirect:
            SeafileClient()
        self.assertIn("is being sent to", err.getvalue())
        self.assertIn("over plain HTTP", err.getvalue())

    def test_an_implausible_setting_is_replaced_and_said_so(self):
        def config_int(key, default):
            return -1 if key == "seafile.locktimeout" else default

        err, redirect = _stderr()
        with patch("git_remote_seafile.config.get_git_config_int", side_effect=config_int), \
                patch("git_remote_seafile.config.get_git_config_bool", side_effect=lambda k, d: d), \
                redirect:
            cfg = RemoteConfig.load()
        self.assertEqual(cfg.lock_timeout, 15)
        self.assertIn("implausible seafile.* config ignored", err.getvalue())
        self.assertIn("seafile.locktimeout=-1", err.getvalue())


class TestLockLeaseMessages(unittest.TestCase):
    def _lock(self, ticket: dict | None):
        client = MagicMock()
        client.token = "tok"
        client.get_file_text.return_value = json.dumps(ticket) if ticket is not None else None
        lock = RemoteLock(client, "repo1", "/path", timeout=5, lease=60, settle=0)
        lock._nonce = "mine"
        lock.acquired = True
        return lock, client

    def _ticket(self, nonce="mine", age=1.0):
        stamped = time.time() - age
        return {"owner": "alice", "machine": "foreign-host", "nonce": nonce,
                "timestamp": stamped, "order_ts": stamped, "lease": 60, "pid": 1234}

    def test_a_failed_renewal_upload_is_reported(self):
        lock, client = self._lock(self._ticket())
        client.upload_file.side_effect = OSError("disk full")
        err, redirect = _stderr()
        with redirect:
            state = lock._refresh_lease()
        self.assertEqual(state, "unknown")
        self.assertIn("Failed to renew lock lease", err.getvalue())

    def test_a_ticket_with_a_foreign_nonce_is_not_renewed(self):
        lock, _ = self._lock(self._ticket(nonce="someone-else"))
        err, redirect = _stderr()
        with redirect:
            state = lock._refresh_lease()
        self.assertEqual(state, "lost")
        self.assertIn("was taken over; not renewing", err.getvalue())

    def test_an_expired_lease_with_an_unreadable_queue_is_unproven(self):
        lock, _ = self._lock(self._ticket(age=1000.0))
        err, redirect = _stderr()
        with patch.object(lock, "_scan_tickets", side_effect=OSError("boom")), redirect:
            state = lock._refresh_lease()
        self.assertEqual(state, "unknown")
        self.assertIn("the queue could not be read", err.getvalue())

    def test_a_preempting_ticket_is_named(self):
        lock, _ = self._lock(self._ticket())
        err, redirect = _stderr()
        preemptor = {"owner": "bob", "machine": "laptop-2"}
        with patch.object(lock, "_queue_preemptor", return_value=preemptor), redirect:
            self.assertFalse(lock.verify_ownership())
        self.assertIn("lock preempted by 'bob' on 'laptop-2'", err.getvalue())


class TestRefAndPackMessages(unittest.TestCase):
    def test_an_entry_of_unknown_type_is_named_not_dropped(self):
        client = MagicMock()
        client.list_dir.return_value = [
            {"name": "main", "type": "file"},
            {"name": "odd", "type": "symlink"},
        ]
        client.get_file_text.return_value = "a" * 40
        err, redirect = _stderr()
        with redirect:
            found = list(iter_refs(client, "repo1", "/r", "refs/heads"))
        self.assertEqual([name for name, _ in found], ["refs/heads/main"])
        self.assertIn("ignoring entry 'odd' of unknown type", err.getvalue())

    def test_an_unreadable_ref_is_not_advertised(self):
        client = MagicMock()
        client.list_dir.return_value = [{"name": "main", "type": "file"}]
        client.get_file_text.return_value = ""
        err, redirect = _stderr()
        with redirect:
            found = list(iter_refs(client, "repo1", "/r", "refs/heads"))
        self.assertEqual(found, [])
        self.assertIn("it is not advertised", err.getvalue())

    def test_a_failed_streaming_download_falls_back_and_says_so(self):
        import tempfile

        client = MagicMock()
        client.download_file_to.side_effect = OSError("reset")
        client.get_file_bytes.return_value = b"PACK"
        err, redirect = _stderr()
        with tempfile.TemporaryDirectory() as td, redirect:
            dest = pathlib.Path(td) / "pack-1.pack"
            reason = fetch_pack_artifact(client, "r1", "/p/pack-1.pack", dest, expected_size=4)
        self.assertIsNone(reason)
        self.assertIn("streaming download of pack-1.pack failed", err.getvalue())


class TestSnapshotToolMessages(unittest.TestCase):
    def test_a_marker_that_cannot_be_written_does_not_fail_the_run(self):
        client = MagicMock()
        client.mkdir_p.side_effect = OSError("read-only")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            snap._write_json_marker(client, "rid", "/base", "marker.json", {"a": 1})
        self.assertIn("could not write marker.json", err.getvalue())

    def test_a_single_other_branch_is_restored_with_a_note(self):
        proc = MagicMock(stdout="refs/remotes/origin/HEAD\nrefs/remotes/origin/snapshot\n")
        out = io.StringIO()
        with patch.object(snap, "_run", return_value=proc), contextlib.redirect_stdout(out):
            branch = snap._resolve_assumed_branch(pathlib.Path("clone"), "main")
        self.assertEqual(branch, "snapshot")
        self.assertIn("restoring its only branch", out.getvalue())


if __name__ == "__main__":
    unittest.main()
