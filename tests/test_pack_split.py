"""test_pack_split.py - a server that refuses a pack's size gets smaller packs, not a failed push."""

from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

try:
    from _helpers import make_helper
except ImportError:  # package-style: python -m unittest tests.test_pack_split
    from tests._helpers import make_helper

from git_remote_seafile.client import SeafileAPIError, TransferAborted
from git_remote_seafile.diagnose import MIB, size_refusal, split_cap_bytes
from git_remote_seafile.helper import _PendingUpdate

PACK = 40 * MIB
REFUSED = "Failed to upload pack-x.pack to /git-repo/objects/pack: HTTP 413 (POST https://s) <html>Too Large"


class TestSizeRefusal(unittest.TestCase):
    def test_a_413_is_a_refusal_whatever_the_size(self):
        self.assertEqual(size_refusal(SeafileAPIError(REFUSED), 1), "413")

    def test_a_reset_counts_only_for_a_body_big_enough_to_be_the_limit(self):
        reset = SeafileAPIError("Failed to upload p to /d: ConnectionError(reset) (POST https://s)")
        self.assertEqual(size_refusal(reset, 64 * MIB), "reset")
        self.assertIsNone(size_refusal(reset, 1 * MIB))

    def test_other_failures_are_not_refusals(self):
        self.assertIsNone(size_refusal(SeafileAPIError("HTTP 500 Connection pool"), 64 * MIB))
        self.assertIsNone(size_refusal(SeafileAPIError("HTTP 403 forbidden"), 64 * MIB))

    def test_the_cap_halves_to_whole_mib_and_stops_at_the_floor(self):
        self.assertEqual(split_cap_bytes(40 * MIB, None), 20 * MIB)
        self.assertEqual(split_cap_bytes(40 * MIB, 10 * MIB), 5 * MIB)   # a smaller cap is the base
        self.assertEqual(split_cap_bytes(40 * MIB + 5, None), 20 * MIB)
        self.assertIsNone(split_cap_bytes(6 * MIB, None))                  # 3 MiB is under the floor


class TestSplitOnRefusal(unittest.TestCase):
    def setUp(self):
        self.h = make_helper()
        self.h.client = MagicMock()
        self.h.repo_id = "repo1"
        self.h.repo_path = "/git-repo"
        self.h._refs_cache = {}
        self.lock = MagicMock()
        self.lock.maybe_renew.return_value = False
        self.pending = [_PendingUpdate("refs/heads/main", "1" * 40, None, False)]
        self.caps = []

    def build(self, limit):
        """create_packfile fake: one pack at the cap (or the whole 40 MiB); the server takes <= limit."""
        def create(objects, staged_dir=None, max_pack_bytes=None, renew=None):
            self.caps.append(max_pack_bytes)
            size = min(PACK, max_pack_bytes or PACK)
            return ("a" * 40, b"P" * size, b"idx")

        def upload(_rid, _dir, name, content, **_kw):
            if name.endswith(".pack") and len(content) > limit:
                raise SeafileAPIError(REFUSED)
            return True

        self.h.client.upload_file.side_effect = upload
        self.h.client.list_dir.side_effect = lambda rid, path: [
            {"name": f"pack-{'a' * 40}.pack", "size": min(PACK, self.caps[-1] or PACK)},
            {"name": f"pack-{'a' * 40}.idx", "size": 3},
        ]
        return create

    def push(self, limit, cap=None):
        err = io.StringIO()
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]), \
                patch("git_remote_seafile.helper.create_packfile", side_effect=self.build(limit)), \
                patch("git_remote_seafile.helper.run_git", return_value=(b"", b"", 0)) as run, \
                patch("sys.stderr", err):
            self.h._upload_push_pack(self.lock, self.pending, cap)
        return err.getvalue(), run

    def test_a_refused_pack_is_rebuilt_smaller_until_the_server_takes_it(self):
        err, run = self.push(limit=15 * MIB)
        self.assertEqual(self.caps, [None, 20 * MIB, 10 * MIB])
        self.assertIn("the server refused an upload of 40.0 MiB (HTTP 413)", err)
        self.assertIn("building smaller packs (at most 20.0 MiB)", err)

    def test_a_413_keeps_the_cap_that_worked_in_the_repository(self):
        err, run = self.push(limit=15 * MIB)
        # The cap that worked was 10 MiB; a fifth is kept to spare, as doctor --upload-limit does.
        run.assert_called_once_with(["config", "--local", "seafile.gcmaxpackmib", "8"])
        self.assertIn("seafile.gcmaxpackmib is now 8 in this repository", err)
        self.assertIn("Notice: this push went through in packs of at most 10.0 MiB", err)

    def test_a_reset_is_retried_but_not_remembered(self):
        self.h.client.upload_file.side_effect = None
        err = io.StringIO()
        calls = {"n": 0}

        def create(objects, staged_dir=None, max_pack_bytes=None, renew=None):
            self.caps.append(max_pack_bytes)
            return ("a" * 40, b"P" * min(PACK, max_pack_bytes or PACK), b"idx")

        def upload(_rid, _dir, name, content, **_kw):
            if name.endswith(".pack"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise SeafileAPIError("Failed to upload p to /d: ConnectionError (POST https://s)")
            return True

        self.h.client.upload_file.side_effect = upload
        self.h.client.list_dir.side_effect = lambda rid, path: [
            {"name": f"pack-{'a' * 40}.pack", "size": 20 * MIB}, {"name": f"pack-{'a' * 40}.idx", "size": 3}]
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]), \
                patch("git_remote_seafile.helper.create_packfile", side_effect=create), \
                patch("git_remote_seafile.helper.run_git") as run, patch("sys.stderr", err):
            self.h._upload_push_pack(self.lock, self.pending, None)
        run.assert_not_called()
        self.assertIn("the connection was closed", err.getvalue())
        self.assertIn("set seafile.gcmaxpackmib to 16 to start there next time", err.getvalue())

    def test_nothing_of_a_refused_attempt_is_left_on_the_server(self):
        self.push(limit=15 * MIB)
        deleted = [c.args[1] for c in self.h.client.delete_entry.call_args_list]
        self.assertIn(f"/git-repo/objects/pack/pack-{'a' * 40}.pack", deleted)
        self.assertIn(f"/git-repo/objects/pack/pack-{'a' * 40}.idx", deleted)

    def test_a_server_that_takes_nothing_gives_up_with_the_servers_own_error(self):
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]), \
                patch("git_remote_seafile.helper.create_packfile", side_effect=self.build(limit=0)), \
                patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SeafileAPIError) as ctx:
                self.h._upload_push_pack(self.lock, self.pending, None)
        self.assertIn("HTTP 413", str(ctx.exception))
        self.assertLessEqual(len(self.caps), 7)

    def test_an_upload_that_fails_for_another_reason_is_not_split(self):
        self.h.client.upload_file.side_effect = SeafileAPIError("Failed to upload p to /d: HTTP 403 quota")
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]), \
                patch("git_remote_seafile.helper.create_packfile",
                      return_value=("a" * 40, b"P" * PACK, b"idx")) as create, \
                patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SeafileAPIError):
                self.h._upload_push_pack(self.lock, self.pending, None)
        self.assertEqual(create.call_count, 1)

    def test_an_aborted_transfer_is_never_mistaken_for_a_refusal(self):
        self.h.client.upload_file.side_effect = TransferAborted("aborting the upload: lease lost")
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]), \
                patch("git_remote_seafile.helper.create_packfile",
                      return_value=("a" * 40, b"P" * PACK, b"idx")) as create, \
                patch("sys.stderr", io.StringIO()):
            with self.assertRaises(TransferAborted):
                self.h._upload_push_pack(self.lock, self.pending, None)
        self.assertEqual(create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
