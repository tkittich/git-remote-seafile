"""Fail each write a push makes, once, and require that nothing already stored is lost.

The review rounds kept finding the same shape by hand: something fails once midway and the run's
bookkeeping then loses data (fable-E1: one failed push re-parented over snapshots the remote never
had).  This does it mechanically for the helper's push: count the writes (POST and DELETE) a clean
push makes, then for each n fail the n-th with an HTTP 500 and check

* the remote still lists, with ``main`` at the old commit or the new one, never anywhere else;
* a push that said success really left ``main`` at the new commit (no false success);
* running the same push again, with the server healthy, succeeds and ends at the new commit;
* a clone of the final state has the new content and passes ``git fsck``.

The cap on ``n`` keeps the run short; the count a clean push makes is asserted to be inside it, so a
helper that starts making more writes cannot quietly escape the test.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, commit_file, init_repo, run_git  # noqa: E402
from test_e2e import E2ETestCase  # noqa: E402

MAX_WRITES = 20


class TestAWriteThatFailsOnceLosesNothing(E2ETestCase):
    def _tip(self, repo: pathlib.Path, name: str) -> str | None:
        out = run_git(["ls-remote", self.url(name)], repo, self.env, check=False)
        if out.returncode != 0:
            return None
        for line in out.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == "refs/heads/main":
                return sha
        return ""

    def _scenario(self, name: str, with_baseline: bool):
        """A clone ready to push ``new`` to ``name``; returns (src, old tip or None, new tip)."""
        src = init_repo(self.work / f"src-{name}", self.env)
        run_git(["remote", "add", "origin", self.url(name)], src, self.env)
        old = None
        if with_baseline:
            commit_file(src, "a.txt", "one\n", "c1", self.env)
            run_git(["push", "-u", "origin", "main"], src, self.env)
            old = run_git(["rev-parse", "HEAD"], src, self.env).stdout.strip()
        commit_file(src, "b.txt", "two\n", "c2", self.env)
        new = run_git(["rev-parse", "HEAD"], src, self.env).stdout.strip()
        return src, old, new

    def _count_writes_of_a_clean_push(self, with_baseline: bool) -> int:
        src, _old, _new = self._scenario(f"count-{int(with_baseline)}", with_baseline)
        self.stub.write_calls = 0
        run_git(["push", "-u", "origin", "main"], src, self.env)
        return self.stub.write_calls

    def _fail_each_write(self, with_baseline: bool):
        total = self._count_writes_of_a_clean_push(with_baseline)
        self.assertGreater(total, 0)
        self.assertLessEqual(total, MAX_WRITES, "the push makes more writes than the test fails; raise MAX_WRITES")
        for n in range(1, total + 1):
            with self.subTest(failing_write=n, of=total, baseline=with_baseline):
                name = f"fault-{int(with_baseline)}-{n}"
                src, old, new = self._scenario(name, with_baseline)
                self.stub.write_calls = 0
                self.stub.faults["fail_write_n"] = n
                try:
                    pushed = run_git(["push", "-u", "origin", "main"], src, self.env, check=False)
                finally:
                    self.stub.faults.pop("fail_write_n", None)
                tip = self._tip(src, name)
                if tip is None and not with_baseline and not any(
                        key.startswith(f"/{name}/refs/") for key in self.stub.files.get(REPO_ID, {})):
                    # A first push that stored its pack and failed before the ref: the helper refuses
                    # to call that an empty repository (DESIGN section 5, "packfiles with no listable
                    # ref"), loudly, and the same push again heals it.  Nothing is lost.
                    tip = ""
                self.assertIsNotNone(tip, "the remote cannot be listed after one failed write")
                self.assertIn(tip, {old or "", new}, "main is neither the old commit nor the new one")
                if pushed.returncode == 0:
                    self.assertEqual(tip, new, "the push said success but main is not the new commit")
                again = run_git(["push", "-u", "origin", "main"], src, self.env, check=False)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual(self._tip(src, name), new)
                # A HEAD that could not be set is said in the push's own words, with the command that
                # sets it; a repeated push has nothing to push and does not retry it.  Anything else
                # must leave a default branch.
                if "default branch (HEAD) could not be set" not in pushed.stderr:
                    self.assertIn(f"/{name}/HEAD", self.stub.files.get(REPO_ID, {}),
                                  "no default branch (HEAD) and no warning about it")
                clone = self.work / f"clone-{name}"
                run_git(["clone", "-b", "main", self.url(name), str(clone)], self.work, self.env)
                self.assertEqual((clone / "b.txt").read_text(encoding="utf-8").replace("\r\n", "\n"), "two\n")
                run_git(["fsck", "--no-progress"], clone, self.env)

    def test_the_second_push_of_a_repository(self):
        self._fail_each_write(with_baseline=True)

    def test_the_first_push_into_an_empty_library_path(self):
        self._fail_each_write(with_baseline=False)


if __name__ == "__main__":
    unittest.main()
