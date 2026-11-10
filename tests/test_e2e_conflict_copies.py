"""test_e2e_conflict_copies.py - ``(SFConflict ...)`` copies, end to end.

ROADMAP Z33 and X7: the stub could not make the copy that simultaneous writers leave on a real
server (measured, CE 13.0.28: the path keeps one value and ``<name> (SFConflict <user> <date>)``
the other), so the ref conflict-copy warning and the lock tickets' copies were only ever tested
against hand-built listings.  ``stub.conflict_copy_for`` makes every upload to a matching path
leave one.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, commit_file, init_repo, run_git  # noqa: E402
from test_e2e import E2ETestCase  # noqa: E402


class TestConflictCopiesThroughGit(E2ETestCase):
    def _repo(self, name: str) -> pathlib.Path:
        src = init_repo(self.work / f"src-{name}", self.env)
        commit_file(src, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url(name)], src, self.env)
        return src

    def copies(self) -> list[str]:
        return [k for k in self.stub.files.get(REPO_ID, {}) if "(SFConflict " in k]

    def test_a_copy_beside_the_ref_is_a_warning_and_the_push_is_recorded(self):
        src = self._repo("cc-ref")
        self.stub.conflict_copy_for = "refs/heads/main"
        proc = run_git(["push", "-u", "origin", "main"], src, self.env)
        self.assertIn("another client wrote refs/heads/main at the same time", proc.stderr)
        self.assertIn("(SFConflict e2e@example.com", proc.stderr)
        self.assertEqual(len(self.copies()), 1, self.copies())
        # What the warning promises: the push is recorded and the repository is usable.
        self.stub.conflict_copy_for = ""
        commit_file(src, "b.txt", "two\n", "c2", self.env)
        run_git(["push", "origin", "main"], src, self.env)
        dst = self.work / "clone-cc-ref"
        run_git(["clone", self.url("cc-ref"), str(dst)], self.work, self.env)
        self.assertTrue((dst / "b.txt").is_file())

    def test_a_clean_push_makes_no_copy_and_no_warning(self):
        src = self._repo("cc-clean")
        proc = run_git(["push", "-u", "origin", "main"], src, self.env)
        self.assertNotIn("SFConflict", proc.stderr)
        self.assertEqual(self.copies(), [])

    def test_a_push_removes_the_conflict_copies_of_its_own_lock_ticket(self):
        """ROADMAP Z47: the copies stayed on the server after every push (5 of 5 measured); the
        holder now deletes the ones it has seen and those made since, at release."""
        src = self._repo("cc-ticket")
        self.stub.conflict_copy_for = ".git-lock.d"
        run_git(["push", "-u", "origin", "main"], src, self.env)
        self.assertTrue(self.stub.conflict_copies, "the stub made no ticket copy: nothing was tested")
        self.assertEqual(self.copies(), [], "ticket copies were left on the server")
        commit_file(src, "b.txt", "two\n", "c2", self.env)
        proc = run_git(["push", "origin", "main"], src, self.env)
        self.assertNotIn("locked by", proc.stderr)
        self.assertEqual(self.copies(), [])
        dst = self.work / "clone-cc-ticket"
        run_git(["clone", self.url("cc-ticket"), str(dst)], self.work, self.env)
        self.assertTrue((dst / "b.txt").is_file())


class TestTheStubCopyCanNumberATakenName(E2ETestCase):
    """ROADMAP Z33: ``copy_duplicate`` makes a server-side copy onto a taken name leave ``<name> (1)``."""

    def _client(self):
        from git_remote_seafile.client import SeafileClient

        return SeafileClient(server_url=self.stub.base_url, token="t")

    def _seed(self):
        files = self.stub.files.setdefault(REPO_ID, {})
        files["/src/obj"] = b"one"
        files["/dst/obj"] = b"already here"
        return files

    def test_the_default_overwrites_as_before(self):
        files = self._seed()
        self.assertTrue(self._client().copy_entries(REPO_ID, "/src", ["obj"], "/dst"))
        self.assertEqual(files["/dst/obj"], b"one")
        self.assertNotIn("/dst/obj (1)", files)

    def test_with_the_fault_a_taken_name_is_numbered(self):
        files = self._seed()
        self.stub.faults["copy_duplicate"] = 1
        client = self._client()
        self.assertTrue(client.copy_entries(REPO_ID, "/src", ["obj"], "/dst"))
        self.assertEqual((files["/dst/obj"], files["/dst/obj (1)"]), (b"already here", b"one"))
        self.assertTrue(client.copy_entries(REPO_ID, "/src", ["obj"], "/dst"))
        self.assertEqual(files["/dst/obj (2)"], b"one")


if __name__ == "__main__":
    unittest.main()
