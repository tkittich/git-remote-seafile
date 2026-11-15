"""AA39 (review 9fe723f, fable-G9.1): a ref this clone fetched before, listed but unreadable, is an error.

Left out of the advertisement it made ``git fetch --prune`` delete the branch here and exit 0 (a
``--mirror`` clone lost the branch itself), and the advertised-refs record forgot it.  A ref the
record never held keeps the warning-and-skip, so one planted file cannot make a repository
unfetchable.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, commit_file, init_repo, run_git  # noqa: E402
from test_e2e import E2ETestCase  # noqa: E402


class TestAnUnreadableRefTheCloneKnowsIsNotPruned(E2ETestCase):
    def _mirror(self, tag: str):
        a = init_repo(self.work / f"a-{tag}", self.env)
        commit_file(a, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url(tag)], a, self.env)
        run_git(["push", "-u", "origin", "main"], a, self.env)
        run_git(["checkout", "-q", "-b", "dev"], a, self.env)
        commit_file(a, "b.txt", "two\n", "c2", self.env)
        run_git(["push", "origin", "dev"], a, self.env)
        mirror = self.work / f"mirror-{tag}.git"
        run_git(["clone", "-q", "--mirror", self.url(tag), str(mirror)], self.work, self.env)
        return a, mirror

    def _refs(self, repo):
        return run_git(["for-each-ref", "--format=%(refname)"], repo, self.env).stdout.split()

    def test_a_known_ref_that_cannot_be_read_stops_the_fetch_and_keeps_the_branch(self):
        _, mirror = self._mirror("known")
        for fault in ("raw_404_on", "raw_empty_on"):
            self.stub.faults[fault] = "refs/heads/dev"
            proc = run_git(["fetch", "--prune", "origin"], mirror, self.env, check=False)
            self.stub.faults.pop(fault)
            self.assertNotEqual(proc.returncode, 0, f"{fault}: {proc.stderr}")
            self.assertIn("could not be read, and this clone fetched it before", proc.stderr)
            self.assertIn("refs/heads/dev", self._refs(mirror), fault)

    def test_a_known_ref_whose_body_is_not_an_object_id_is_not_pruned_either(self):
        """fable-G9.1 (review 134c8af): AA39 covered an empty or missing body only; a proxy's
        200 page, half an id or a file over 4 KiB was left out and ``--prune`` took the branch."""
        _, mirror = self._mirror("badbody")
        key = "/badbody/refs/heads/dev"
        good = self.stub.files[REPO_ID][key]
        for label, body in (("html", b"<html>502 Bad Gateway</html>"), ("half an id", good[:10]),
                            ("5 KiB", b"x" * 5000)):
            self.stub.files[REPO_ID][key] = body
            proc = run_git(["fetch", "--prune", "origin"], mirror, self.env, check=False)
            self.assertNotEqual(proc.returncode, 0, f"{label}: {proc.stderr}")
            self.assertIn("refs/heads/dev", self._refs(mirror), label)
        self.stub.files[REPO_ID][key] = good

    def test_a_namespace_that_vanishes_beside_surviving_tags_stops_the_fetch(self):
        """fable-A1 (review 134c8af): ``refs/heads`` renamed in the web UI left the tags, the
        advertisement was not empty, and ``fetch --prune`` deleted every branch and exited 0."""
        a, mirror = self._mirror("renamed")
        run_git(["tag", "v1"], a, self.env)
        run_git(["push", "origin", "v1"], a, self.env)
        run_git(["fetch", "origin"], mirror, self.env)      # the record now holds the tag too
        files = self.stub.files[REPO_ID]
        for key in [k for k in files if "/renamed/refs/heads/" in k]:
            files[key.replace("/refs/heads/", "/refs/heads-old/")] = files.pop(key)
        proc = run_git(["fetch", "--prune", "origin"], mirror, self.env, check=False)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lists no branch", proc.stderr)
        self.assertIn("refs/heads/dev", self._refs(mirror))

    def test_a_repository_with_tags_and_no_branch_it_never_had_is_fine(self):
        a = init_repo(self.work / "a-tagsonly", self.env)
        commit_file(a, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url("tagsonly")], a, self.env)
        run_git(["tag", "v1"], a, self.env)
        run_git(["push", "origin", "v1"], a, self.env)
        mirror = self.work / "mirror-tagsonly.git"
        run_git(["clone", "-q", "--mirror", self.url("tagsonly"), str(mirror)], self.work, self.env)
        proc = run_git(["fetch", "--prune", "origin"], mirror, self.env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_ref_the_clone_never_saw_is_still_only_a_warning(self):
        a, mirror = self._mirror("planted")
        run_git(["checkout", "-q", "-b", "planted"], a, self.env)
        commit_file(a, "c.txt", "three\n", "c3", self.env)
        run_git(["push", "origin", "planted"], a, self.env)
        self.stub.faults["raw_404_on"] = "refs/heads/planted"
        proc = run_git(["fetch", "--prune", "origin"], mirror, self.env, check=False)
        self.stub.faults.pop("raw_404_on")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("is listed but its content could not be read", proc.stderr)
        self.assertIn("refs/heads/dev", self._refs(mirror))


if __name__ == "__main__":
    unittest.main()
