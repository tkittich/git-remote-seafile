"""ROADMAP AA10: a push of a nested branch makes ``refs/heads/<dir>/`` before it writes the ref.

If the ref write then fails, the empty folder is left behind.  The fetch listing treats a
folder its parent lists that answers empty twice as a read failure (the issue-6 shape, kept on
purpose), so every later ``git fetch`` and ``ls-remote`` stopped until somebody deleted the
folder in the web UI.  The stub models folders only on request (``explicit_dirs``).
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, commit_file, init_repo, run_git  # noqa: E402
from _helpers import make_helper  # noqa: E402
from test_e2e import E2ETestCase  # noqa: E402


class TestAFailedNestedPushLeavesNoFolderBehind(E2ETestCase):
    def _pushed_main(self, name: str) -> pathlib.Path:
        src = init_repo(self.work / f"src-{name}", self.env)
        commit_file(src, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url(name)], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)
        return src

    def _ls_remote(self, name: str, cwd: pathlib.Path):
        return run_git(["ls-remote", self.url(name)], cwd, self.env, check=False)

    def test_the_folder_a_failed_ref_write_made_is_removed(self):
        self.stub.explicit_dirs = True
        src = self._pushed_main("nested")
        run_git(["checkout", "-q", "-b", "feature/auth/login"], src, self.env)
        commit_file(src, "b.txt", "two\n", "c2", self.env)
        self.stub.faults["upload_fail_path"] = "refs/heads/feature/auth/login"
        proc = run_git(["push", "origin", "feature/auth/login"], src, self.env, check=False)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            sorted(d for d in self.stub.dirs.get(REPO_ID, ()) if "feature" in d), [],
            "an empty folder from the failed push is still on the server",
        )
        # What the folder used to cost: every later listing failed.
        listing = self._ls_remote("nested", src)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("refs/heads/main", listing.stdout)
        # And the same push works once the server does.
        self.stub.faults.pop("upload_fail_path")
        run_git(["push", "origin", "feature/auth/login"], src, self.env)
        self.assertIn("refs/heads/feature/auth/login", self._ls_remote("nested", src).stdout)

    def test_a_dropped_connection_leaves_no_folder_behind_either(self):
        """fable-G0.1: the cleanup ran only for SeafileAPIError; the upload link's connection
        dying is a requests exception, and the empty folder stayed."""
        self.stub.explicit_dirs = True
        src = self._pushed_main("dropped")
        run_git(["checkout", "-q", "-b", "feature/auth/login"], src, self.env)
        commit_file(src, "b.txt", "two\n", "c2", self.env)

        def drop_the_connection():
            raise RuntimeError("the stub drops the connection")

        self.stub.get_hooks.append(
            (lambda path: "upload-link" in path and "refs/heads/feature" in path.replace("%2F", "/"),
             drop_the_connection)
        )
        proc = run_git(["push", "origin", "feature/auth/login"], src, self.env, check=False)
        self.stub.get_hooks.clear()
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            sorted(d for d in self.stub.dirs.get(REPO_ID, ()) if "feature" in d), [],
            "an empty folder from the dropped connection is still on the server",
        )
        self.assertEqual(self._ls_remote("dropped", src).returncode, 0)

    def test_a_write_that_landed_but_was_not_answered_keeps_its_folder_and_ref(self):
        """fable-G0.3: the emptiness check is the only thing between the cleanup and a ref that
        did land.  The stub stores the ref and answers 500; the folder lists the ref, so it stays."""
        self.stub.explicit_dirs = True
        src = self._pushed_main("landed")
        run_git(["checkout", "-q", "-b", "feature/auth/login"], src, self.env)
        commit_file(src, "b.txt", "two\n", "c2", self.env)
        self.stub.faults["upload_lost_answer"] = "refs/heads/feature/auth/login"
        proc = run_git(["push", "origin", "feature/auth/login"], src, self.env, check=False)
        self.stub.faults.pop("upload_lost_answer")
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("/landed/refs/heads/feature/auth/login", self.stub.files.get(REPO_ID, {}),
                      "the cleanup deleted a ref that had landed")
        self.assertIn("refs/heads/feature/auth/login", self._ls_remote("landed", src).stdout)

    def test_a_folder_that_was_already_there_is_left_alone(self):
        self.stub.explicit_dirs = True
        src = self._pushed_main("kept")
        run_git(["checkout", "-q", "-b", "feature/one"], src, self.env)
        commit_file(src, "b.txt", "two\n", "c2", self.env)
        run_git(["push", "origin", "feature/one"], src, self.env)
        run_git(["checkout", "-q", "-b", "feature/two"], src, self.env)
        commit_file(src, "c.txt", "three\n", "c3", self.env)
        self.stub.faults["upload_fail_path"] = "refs/heads/feature/two"
        proc = run_git(["push", "origin", "feature/two"], src, self.env, check=False)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.stub.faults.pop("upload_fail_path")
        listing = self._ls_remote("kept", src)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("refs/heads/feature/one", listing.stdout)


class TestTheCleanupIsCarefulWhatItDeletes(unittest.TestCase):
    """fable-G0.3 (b), (c): a folder that was never made, and the fence."""

    def _helper(self, existing: set[str], lock_owned: bool = True):
        client = MagicMock()
        client.dir_exists.side_effect = lambda _rid, path: path in existing
        client.list_dir.return_value = []     # also what the server says for a folder it lacks
        client.delete_entry.return_value = True
        lock = MagicMock()
        lock.verify_ownership.return_value = lock_owned
        return make_helper(client=client), client, lock

    def test_a_folder_the_server_does_not_have_is_skipped_and_the_outer_one_still_goes(self):
        h, client, lock = self._helper({"/r/refs/heads/feature"})   # the inner mkdir never happened
        h._drop_empty_folders(["/r/refs/heads/feature", "/r/refs/heads/feature/auth"], lock)
        deleted = [c.args[1] for c in client.delete_entry.call_args_list]
        self.assertEqual(deleted, ["/r/refs/heads/feature"])

    def test_an_error_that_is_not_a_seafile_error_still_triggers_the_cleanup(self):
        """fable2-G0.3 (AA36): a dropped connection is a SeafileAPIError since AA27, which left the
        ``except Exception`` clause of ``_publish_ref`` covered by nothing; it still catches the rest."""
        h, client, lock = self._helper(set())
        client.get_file_text.return_value = None
        client.upload_file.side_effect = RuntimeError("not a requests or Seafile error")
        with patch.object(h, "_absent_ref_folders", return_value=["/r/refs/heads/feature"]),                 patch.object(h, "_drop_empty_folders") as drop:
            with self.assertRaises(RuntimeError):
                h._publish_ref("refs/heads/feature/x", "a" * 40, None, False, lock)
        drop.assert_called_once_with(["/r/refs/heads/feature"], lock)

    def test_a_lease_that_is_not_ours_deletes_nothing(self):
        h, client, lock = self._helper({"/r/refs/heads/feature"}, lock_owned=False)
        h._drop_empty_folders(["/r/refs/heads/feature"], lock)
        client.delete_entry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
