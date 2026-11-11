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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, commit_file, init_repo, run_git  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
