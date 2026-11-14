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
from git_remote_seafile.diagnose import MIB, SPLIT_FLOOR_BYTES, size_refusal, split_cap_bytes
from git_remote_seafile.helper import _PackVerifyFailed, _PendingUpdate

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
        # Half would be under the floor: the floor is tried once instead of giving up (T17)...
        self.assertEqual(split_cap_bytes(6 * MIB, None), SPLIT_FLOOR_BYTES)
        self.assertEqual(split_cap_bytes(40 * MIB, 6 * MIB), SPLIT_FLOOR_BYTES)
        # ...and a refusal at the floor (or a cap already there) ends the retries.
        self.assertIsNone(split_cap_bytes(SPLIT_FLOOR_BYTES, None))
        self.assertIsNone(split_cap_bytes(40 * MIB, SPLIT_FLOOR_BYTES))
        self.assertIsNone(split_cap_bytes(3 * MIB, None))


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
            return (self.sha(), b"P" * size, b"idx")

        self.remote = {}      # what the server holds: name -> size

        def upload(_rid, _dir, name, content, **_kw):
            if name.endswith(".pack") and len(content) > limit:
                raise SeafileAPIError(REFUSED)
            self.remote[name] = len(content)
            return True

        def delete(_rid, path):
            self.remote.pop(path.rsplit("/", 1)[-1], None)
            return True

        self.h.client.upload_file.side_effect = upload
        self.h.client.delete_entry.side_effect = delete
        # The helper lists the pack folder before its first upload and never removes a name that
        # was there then (fable-B1); a refused attempt's names are deleted, so the retry's other
        # names are never "held".
        self.h.client.list_dir.side_effect = lambda rid, path: [
            {"name": n, "size": sz} for n, sz in self.remote.items()]
        return create

    def sha(self):
        """Each attempt builds other objects, so it has another name: 'a', then 'b' (the real thing)."""
        return chr(ord("a") + max(len(self.caps), 1) - 1) * 40

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
        # Kept beside the remote that refused (ROADMAP Z31), not in the repository-wide key.
        run.assert_called_once_with(["config", "--local", "remote.origin.seafilegcmaxpackmib", "8"])
        self.assertIn("remote.origin.seafilegcmaxpackmib is now 8 in this repository", err)
        self.assertIn("later pushes to this remote start there", err)
        self.assertIn("Notice: this push went through in packs of at most 10.0 MiB", err)

    def test_a_push_by_url_has_no_remote_to_keep_the_cap_for(self):
        """Git hands the helper the URL as the remote's name when it was pushed to by URL."""
        self.h.remote_name = "seafile://lib/repo"
        err, run = self.push(limit=15 * MIB)
        run.assert_not_called()
        self.assertIn("set seafile.gcmaxpackmib to 8 to start there next time", err)

    def test_a_reset_is_retried_but_not_remembered(self):
        self.h.client.upload_file.side_effect = None
        err = io.StringIO()
        calls = {"n": 0}

        def create(objects, staged_dir=None, max_pack_bytes=None, renew=None):
            self.caps.append(max_pack_bytes)
            return (chr(ord("a") + len(self.caps) - 1) * 40, b"P" * min(PACK, max_pack_bytes or PACK), b"idx")

        def upload(_rid, _dir, name, content, **_kw):
            if name.endswith(".pack"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise SeafileAPIError("Failed to upload p to /d: ConnectionError (POST https://s)")
            return True

        self.h.client.upload_file.side_effect = upload
        self.h.client.list_dir.side_effect = lambda rid, path: [
            {"name": f"pack-{chr(ord('a') + len(self.caps) - 1) * 40}.pack", "size": 20 * MIB},
            {"name": f"pack-{chr(ord('a') + len(self.caps) - 1) * 40}.idx", "size": 3}] if calls["n"] else []
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

    def test_a_name_the_remote_held_before_is_not_removed_by_a_refused_attempt(self):
        """fable2 (AA36): the ``held_before`` filter of the refusal cleanup had no test."""
        held = f"pack-{'a' * 40}.idx"
        create = self.build(limit=0)          # installs its own listing: replace it after
        self.h.client.list_dir.side_effect = lambda rid, path: [{"name": held, "size": 3}]
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile", side_effect=create),                 patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SeafileAPIError):
                self.h._upload_push_pack(self.lock, self.pending, None)
        deleted = [c.args[1] for c in self.h.client.delete_entry.call_args_list]
        self.assertNotIn(f"/git-repo/objects/pack/{held}", deleted)
        self.assertIn(f"/git-repo/objects/pack/pack-{'a' * 40}.pack", deleted)

    def test_a_failed_verification_removes_only_what_it_proved_wrong_or_this_push_added(self):
        """fable2 (AA36): the same filter in the verification cleanup."""
        held = f"pack-{'a' * 40}.idx"
        listings = [[{"name": held, "size": 3}],                          # before the first upload
                    [{"name": held, "size": 3},
                     {"name": f"pack-{'a' * 40}.pack", "size": 5}]]       # the verification: wrong size
        def listing(rid, path):
            if "objects/pack" not in path:
                return []
            return listings.pop(0) if len(listings) > 1 else listings[0]

        self.h.client.list_dir.side_effect = listing
        self.h.client.upload_file.side_effect = None
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile",
                      return_value=("a" * 40, b"P" * PACK, b"idx")),                 patch("sys.stderr", io.StringIO()):
            with self.assertRaises(_PackVerifyFailed):
                self.h._upload_push_pack(self.lock, self.pending, None)
        deleted = [c.args[1] for c in self.h.client.delete_entry.call_args_list]
        self.assertEqual(deleted, [f"/git-repo/objects/pack/pack-{'a' * 40}.pack"])

    def test_a_pack_the_remote_already_holds_is_not_uploaded_again(self):
        """fable-B1 (AA37): ``replace=True`` over a held name, landing truncated, destroyed it."""
        names = [f"pack-{'a' * 40}.idx", f"pack-{'a' * 40}.pack"]
        create = self.build(limit=10 * PACK)
        self.h.client.list_dir.side_effect = lambda rid, path: [
            {"name": names[0], "size": 3}, {"name": names[1], "size": PACK}]
        err = io.StringIO()
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile", side_effect=create),                 patch("sys.stderr", err):
            self.h._upload_push_pack(self.lock, self.pending, None)
        self.h.client.upload_file.assert_not_called()
        self.assertIn("already on the remote; not uploaded again", err.getvalue())

    def test_only_the_missing_half_of_a_held_pair_is_uploaded(self):
        create = self.build(limit=10 * PACK)
        idx = f"pack-{'a' * 40}.idx"
        self.h.client.list_dir.side_effect = lambda rid, path: (
            [{"name": idx, "size": 3}]
            + ([{"name": f"pack-{'a' * 40}.pack", "size": PACK}] if self.h.client.upload_file.called else []))
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile", side_effect=create),                 patch("sys.stderr", io.StringIO()):
            self.h._upload_push_pack(self.lock, self.pending, None)
        uploaded = [c.args[2] for c in self.h.client.upload_file.call_args_list]
        self.assertEqual(uploaded, [f"pack-{'a' * 40}.pack"])

    def test_a_held_pack_with_a_missing_index_is_not_overwritten_either(self):
        create = self.build(limit=10 * PACK)
        held = f"pack-{'a' * 40}.pack"
        self.h.client.list_dir.side_effect = lambda rid, path: [{"name": held, "size": PACK}] + (
            [{"name": f"pack-{'a' * 40}.idx", "size": 3}] if self.h.client.upload_file.called else [])
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile", side_effect=create),                 patch("sys.stderr", io.StringIO()):
            self.h._upload_push_pack(self.lock, self.pending, None)
        uploaded = [c.args[2] for c in self.h.client.upload_file.call_args_list]
        self.assertEqual(uploaded, [f"pack-{'a' * 40}.idx"])

    def test_a_lease_that_is_not_ours_removes_nothing_after_a_refusal(self):
        """fable-A1: a name another push adopted since the listing is not this push's to delete."""
        create = self.build(limit=0)
        self.lock.verify_ownership.return_value = False
        err = io.StringIO()
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile", side_effect=create),                 patch("sys.stderr", err):
            with self.assertRaises(SeafileAPIError):
                self.h._upload_push_pack(self.lock, self.pending, None)
        self.h.client.delete_entry.assert_not_called()
        self.assertIn("no longer holds the lock", err.getvalue())

    def test_a_lease_that_is_not_ours_removes_nothing_after_a_failed_verification(self):
        listings = [[], [{"name": f"pack-{'a' * 40}.pack", "size": 5}]]
        self.h.client.list_dir.side_effect = lambda rid, path: (
            [] if "objects/pack" not in path else listings.pop(0) if len(listings) > 1 else listings[0])
        self.h.client.upload_file.side_effect = None
        self.lock.verify_ownership.return_value = False
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile",
                      return_value=("a" * 40, b"P" * PACK, b"idx")),                 patch("sys.stderr", io.StringIO()):
            with self.assertRaises(_PackVerifyFailed):
                self.h._upload_push_pack(self.lock, self.pending, None)
        self.h.client.delete_entry.assert_not_called()

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
