"""test_e2e_listing_faults.py - one failed directory listing, at every position in a push.

ROADMAP Z38 (deepseek-v4.1-flash H2.3): the stub could fail every listing or a scoped
subset, but not exactly one, so a server that is healthy apart from a single wrong
answer was never tested.  One 502 on a listing switched a refusal off once (Z3) and no
test saw it.

The wrong answer used here is a 404 (``dir_fail_status``): a 5xx is retried by the
client and absorbed at every position (measured: 36 of 36 pushes went through), whereas
a 404 on a folder that exists is read as "not there", which is the answer a decision can
be wrongly made from.  Pro answers 403 or a 500 page for a missing folder (issue 6), so
the same reading follows.

The invariant, for the n-th listing of a first push and of an update, for every n:

* the push either fails, or succeeds and then the remote's tip is the local tip;
* after a failure a push repeated on a healthy server succeeds, tip equal;
* a fresh clone holds the pushed files and passes ``git fsck``.

Nothing says *which* of the first two a given n gives; either is honest.  What a push
cannot do is report success and leave the remote different from what was pushed.  On
the 0.9.23 code the first push fails at listing 26 and is repeated cleanly.  Not shown:
that this would have caught Z3 -- forcing ``_listed_as_file`` to answer False leaves it
green, because Z3 needed that listing to fail twice in a row.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import commit_file, init_repo, run_git  # noqa: E402
from test_e2e import E2ETestCase  # noqa: E402


class _ListingFaultCase(E2ETestCase):
    """The shared checks; a subclass supplies the push whose listings are failed in turn."""

    __test__ = False

    def _tip(self, repo: pathlib.Path) -> str:
        return run_git(["rev-parse", "HEAD"], repo, self.env).stdout.strip()

    def _remote_tip(self, name: str, repo: pathlib.Path) -> str:
        out = run_git(["ls-remote", self.url(name), "refs/heads/main"], repo, self.env).stdout
        return out.split()[0] if out.split() else ""

    def _listings_of(self, action) -> int:
        before = self.stub.dir_calls
        action()
        return self.stub.dir_calls - before

    def _push_failing_listing(self, n: int, src: pathlib.Path) -> int:
        target = self.stub.dir_calls + n
        self.stub.faults["dir_fail_nth"] = target
        self.stub.faults["dir_fail_status"] = 404
        try:
            rc = run_git(["push", "origin", "main"], src, self.env, check=False).returncode
        finally:
            self.stub.faults.pop("dir_fail_nth", None)
            self.stub.faults.pop("dir_fail_status", None)
        self.assertGreaterEqual(self.stub.dir_calls, target, f"listing {n} was never asked for: nothing was injected")
        return rc

    def _check_after(self, label: str, name: str, src: pathlib.Path, rc: int, expect_file: str) -> None:
        if rc == 0:
            self.assertEqual(self._remote_tip(name, src), self._tip(src),
                             f"{label}: the push reported success, but the remote's tip is not the pushed one")
        else:
            retry = run_git(["push", "origin", "main"], src, self.env, check=False)
            self.assertEqual(retry.returncode, 0, f"{label}: a push on a healthy server failed\n{retry.stderr}")
            self.assertEqual(self._remote_tip(name, src), self._tip(src), f"{label}: tip after the retry")
        dst = self.work / f"clone-{name}"
        run_git(["clone", self.url(name), str(dst)], self.work, self.env)
        self.assertTrue((dst / expect_file).is_file(), f"{label}: {expect_file} missing from a fresh clone")
        run_git(["fsck", "--no-dangling"], dst, self.env)

    def _source(self, name: str) -> pathlib.Path:
        src = init_repo(self.work / f"src-{name}", self.env)
        commit_file(src, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url(name)], src, self.env)
        return src


class TestOneFailedListingInAFirstPush(_ListingFaultCase):
    __test__ = True

    def test_every_position(self):
        probe = self._source("lf-probe")
        count = self._listings_of(lambda: run_git(["push", "-u", "origin", "main"], probe, self.env))
        self.assertGreater(count, 0, "a push lists nothing: the fault has nothing to hit")
        for n in range(1, count + 1):
            with self.subTest(listing=n):
                name = f"lf-first-{n}"
                src = self._source(name)
                rc = self._push_failing_listing(n, src)
                self._check_after(f"first push, listing {n}", name, src, rc, "a.txt")


class TestOneFailedListingInAnUpdate(_ListingFaultCase):
    __test__ = True

    def test_every_position(self):
        probe = self._source("lu-probe")
        run_git(["push", "-u", "origin", "main"], probe, self.env)
        commit_file(probe, "b.txt", "two\n", "c2", self.env)
        count = self._listings_of(lambda: run_git(["push", "origin", "main"], probe, self.env))
        self.assertGreater(count, 0)
        for n in range(1, count + 1):
            with self.subTest(listing=n):
                name = f"lf-update-{n}"
                src = self._source(name)
                run_git(["push", "-u", "origin", "main"], src, self.env)
                commit_file(src, "b.txt", "two\n", "c2", self.env)
                rc = self._push_failing_listing(n, src)
                self._check_after(f"update, listing {n}", name, src, rc, "b.txt")


class TestOneFailedListingNeverLetsADivergentPushWin(_ListingFaultCase):
    """Another machine's branch is not overwritten, whichever listing answers wrongly.

    A false "that ref is not there" is the answer a destructive decision is made from
    (Z2, Z3): machine B, whose history is unrelated to the branch machine A pushed, must
    be refused, not accepted because one listing said the branch was absent.
    """

    __test__ = True

    def _divergent(self, name: str) -> tuple[pathlib.Path, str]:
        a = self._source(name)
        run_git(["push", "-u", "origin", "main"], a, self.env)
        tip_a = self._tip(a)
        b = init_repo(self.work / f"other-{name}", self.env)
        commit_file(b, "b.txt", "other history\n", "b1", self.env)
        run_git(["remote", "add", "origin", self.url(name)], b, self.env)
        return b, tip_a

    def test_every_position(self):
        probe, _ = self._divergent("ld-probe")
        count = self._listings_of(
            lambda: run_git(["push", "origin", "main"], probe, self.env, check=False))
        self.assertGreater(count, 0)
        for n in range(1, count + 1):
            with self.subTest(listing=n):
                name = f"ld-{n}"
                b, tip_a = self._divergent(name)
                rc = self._push_failing_listing(n, b)
                self.assertNotEqual(rc, 0, f"listing {n}: a divergent push was accepted")
                self.assertEqual(self._remote_tip(name, b), tip_a,
                                 f"listing {n}: the branch machine A pushed was overwritten")


if __name__ == "__main__":
    unittest.main()
