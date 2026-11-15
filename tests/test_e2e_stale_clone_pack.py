"""fable-B1 (review 68e81e5): a failed verify listing must not delete a pack the remote relied on.

A pack's name is the hash of its object set.  dev1 pushes ``main`` (pack P); dev2 clones, commits
and pushes (pack Q, built on P); dev1, who has not fetched, pushes a lightweight tag on its old
tip: the remote's tip is unknown to dev1, nothing is excluded, and the pack is P again, under P's
name.  If the listing that verifies the upload then fails once, "what this push posted, this push
removes" removed P, and a fresh clone died with "remote did not send all necessary objects".
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest
from urllib.parse import unquote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, commit_file, init_repo, run_git  # noqa: E402
from test_e2e import E2ETestCase  # noqa: E402


class TestAFailedVerifyListingRemovesNothingTheRemoteHeld(E2ETestCase):
    def _stale_clone_and_the_first_pack(self, name: str, new_commit: bool = True):
        dev1 = init_repo(self.work / f"dev1-{name}", self.env)
        (dev1 / "big.bin").write_bytes(os.urandom(120000))
        commit_file(dev1, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url(name)], dev1, self.env)
        run_git(["push", "-u", "origin", "main"], dev1, self.env)
        first = set(self.stub.packs(f"/{name}"))
        dev2 = self.work / f"dev2-{name}"
        run_git(["clone", "-q", self.url(name), str(dev2)], self.work, self.env)
        commit_file(dev2, "b.txt", "two\n", "c2", self.env)
        run_git(["push", "origin", "main"], dev2, self.env)
        if new_commit:
            commit_file(dev1, "c.txt", "three\n", "c3", self.env)   # a pack of its own name
        run_git(["tag", "v1"], dev1, self.env)  # on dev1's tip, which the remote does not know
        return dev1, first

    def test_a_listing_that_cannot_be_read_deletes_nothing(self):
        name = "verify"
        dev1, first = self._stale_clone_and_the_first_pack(name)
        armed = {"on": False}

        def arm(path: str) -> bool:
            # The upload-link request for objects/pack comes with each pack upload; the first
            # pack listing after it is the verification.
            if "upload-link" in path and "objects/pack" in unquote(path):
                armed["on"] = True
            return False

        def is_the_verify_listing(path: str) -> bool:
            return bool(armed["on"]) and "/dir/" in path and "objects/pack" in unquote(path)

        self.stub.get_hooks.append((arm, lambda: None))
        self.stub.get_hooks.append((is_the_verify_listing, lambda: self.stub.faults.update(dir=502)))
        proc = run_git(["push", "origin", "v1"], dev1, self.env, check=False)
        self.stub.get_hooks.clear()
        self.stub.faults.pop("dir", None)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("could not be verified", proc.stderr)
        self.assertEqual(sorted(first - set(self.stub.packs(f"/{name}"))), [],
                         "a pack the remote held before the push was removed")
        clone = run_git(["clone", "-q", self.url(name), str(self.work / f"fresh-{name}")], self.work,
                        self.env, check=False)
        self.assertEqual(clone.returncode, 0, clone.stderr)

    def test_a_pack_the_remote_holds_is_not_sent_again(self):
        """AA37 (fable-B1): the same name again was uploaded over the held pack before anything
        was verified; a truncated upload then destroyed it.  Now nothing is sent."""
        name = "held"
        dev1, first = self._stale_clone_and_the_first_pack(name, new_commit=False)
        sent: list[str] = []
        self.stub.get_hooks.append(
            (lambda path: "upload-link" in path and "objects/pack" in unquote(path),
             lambda: sent.append("upload-link")))
        proc = run_git(["push", "origin", "v1"], dev1, self.env, check=False)
        self.stub.get_hooks.clear()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sent, [], "a pack the remote held was uploaded again")
        self.assertIn("already on the remote; not uploaded again", proc.stderr)
        clone = run_git(["clone", "-q", self.url(name), str(self.work / f"fresh-{name}")], self.work,
                        self.env, check=False)
        self.assertEqual(clone.returncode, 0, clone.stderr)

    def test_a_held_pack_at_another_size_is_uploaded_over_not_trusted(self):
        """fable-G0.1 (review 134c8af): a name skipped as held was never verified.  One a failed
        attempt left truncated made the retry write the ref and say ok; a clone then died in
        index-pack.  A held name is skipped only at the size the push would send."""
        name = "damaged"
        dev1, first = self._stale_clone_and_the_first_pack(name, new_commit=False)
        files = self.stub.files[REPO_ID]
        pack = next(k for k in files if k.endswith(".pack") and f"/{name}/" in k)
        files[pack] = files[pack][: len(files[pack]) // 2]
        proc = run_git(["push", "origin", "v1"], dev1, self.env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Uploading packfile", proc.stderr)
        clone = run_git(["clone", "-q", self.url(name), str(self.work / f"fresh-{name}")], self.work,
                        self.env, check=False)
        self.assertEqual(clone.returncode, 0, clone.stderr)


if __name__ == "__main__":
    unittest.main()
