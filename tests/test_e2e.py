"""test_e2e.py - end-to-end tests: real `git` driving the real helper.

These run against an in-memory Seafile API stub (see e2e_harness.py) over a
loopback socket.  No network, no real server, no credentials.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import (  # noqa: E402
    SeafileStub,
    commit_file,
    init_repo,
    make_helper_env,
    run_git,
    run_helper,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class E2ETestCase(unittest.TestCase):
    """Shared stub + helper environment for the whole class."""

    @classmethod
    def setUpClass(cls):
        cls.stub = SeafileStub().start()
        cls._tmp = tempfile.TemporaryDirectory(prefix="grs-e2e-")
        cls.work = pathlib.Path(cls._tmp.name)
        cls.env = make_helper_env(cls.work / "bin", cls.stub.base_url, REPO_ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.stub.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.stub.reset()

    def url(self, name: str) -> str:
        return f"seafile://testlib/{name}"


class TestPushFetchRoundTrip(E2ETestCase):
    def test_push_then_clone(self):
        src = init_repo(self.work / "src1", self.env)
        commit_file(src, "README.txt", "hello from the stub\n", "initial commit", self.env)
        run_git(["remote", "add", "origin", self.url("repo1")], src, self.env)

        run_git(["push", "-u", "origin", "main"], src, self.env)

        dst = self.work / "clone1"
        run_git(["clone", self.url("repo1"), str(dst)], self.work, self.env)

        self.assertEqual((dst / "README.txt").read_text(encoding="utf-8").replace("\r\n", "\n"),
                         "hello from the stub\n")
        self.assertIn("initial commit", run_git(["log", "--oneline"], dst, self.env).stdout)
        # The push really stored a pack + index on the "server".
        self.assertTrue(any(p.endswith(".pack") for p in self.stub.packs("/repo1")))
        self.assertTrue(any(p.endswith(".idx") for p in self.stub.packs("/repo1")))

    def test_incremental_fetch_pulls_only_new_commits(self):
        src = init_repo(self.work / "src2", self.env)
        commit_file(src, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url("repo2")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

        dst = self.work / "clone2"
        run_git(["clone", self.url("repo2"), str(dst)], self.work, self.env)

        commit_file(src, "b.txt", "two\n", "c2", self.env)
        run_git(["push", "origin", "main"], src, self.env)
        run_git(["pull", "--ff-only", "origin", "main"], dst, self.env)

        self.assertTrue((dst / "b.txt").is_file())
        self.assertIn("c2", run_git(["log", "--oneline"], dst, self.env).stdout)


class TestFaultInjection(E2ETestCase):
    """Failure paths a healthy live server will never show on demand."""

    def test_listing_failure_surfaces_as_error(self):
        self.stub.faults["dir"] = 500
        proc = run_git(["ls-remote", self.url("repo3")], self.work, self.env, check=False)
        self.assertNotEqual(proc.returncode, 0, "git should report the helper failure")
        combined = proc.stdout + proc.stderr
        self.assertTrue("500" in combined or "fatal" in combined.lower(),
                        f"expected a visible error, got:\n{combined}")


class TestNestedBranchRefs(E2ETestCase):
    """refs/heads/<a>/<b> must be advertised, fetched, and survive gc.

    Regression guard for the Tier-1 data-loss bug found by review: cmd_list
    iterates a single level under refs/heads and ignores ``type == "dir"``
    entries, so a slash-named branch is never advertised.  It is then neither
    fetched by a clone nor considered reachable by compact_repository, which
    prunes its objects as garbage -- silently and irreversibly.
    """

    def test_nested_branch_is_advertised_and_cloned(self):
        src = init_repo(self.work / "src_nested", self.env)
        commit_file(src, "a.txt", "base\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url("nested")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

        run_git(["checkout", "-q", "-b", "feature/auth"], src, self.env)
        commit_file(src, "b.txt", "feature\n", "feature commit", self.env)
        run_git(["push", "-u", "origin", "feature/auth"], src, self.env)

        # 1. The remote must advertise the nested ref.
        ls = run_git(["ls-remote", self.url("nested")], self.work, self.env)
        self.assertIn(
            "refs/heads/feature/auth",
            ls.stdout,
            f"nested branch not advertised by the helper:\n{ls.stdout}",
        )

        # 2. A fresh clone must contain it, with its commit.
        dst = self.work / "clone_nested"
        run_git(["clone", self.url("nested"), str(dst)], self.work, self.env)
        branches = run_git(["branch", "-r"], dst, self.env).stdout
        self.assertIn(
            "origin/feature/auth",
            branches,
            f"nested branch missing after clone:\n{branches}",
        )
        log = run_git(["log", "--oneline", "origin/feature/auth"], dst, self.env).stdout
        self.assertIn("feature commit", log)


class TestGcPreservesNestedRefs(E2ETestCase):
    """gc must not treat a nested branch's objects as garbage.

    compact_repository mirrors remote refs into a scratch bare repository so
    that ``git repack -a -d`` knows which objects are reachable.  While that
    mirror was single-level, any ref with a slash in its name was omitted, its
    objects looked unreachable, and repack deleted them -- the unrecoverable
    half of the nested-ref bug.
    """

    def test_gc_keeps_nested_branch_objects(self):
        src = init_repo(self.work / "src_gc", self.env)
        commit_file(src, "a.txt", "base\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url("gcrepo")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

        run_git(["checkout", "-q", "-b", "topic/x"], src, self.env)
        commit_file(src, "c.txt", "topic\n", "topic only commit", self.env)
        run_git(["push", "-u", "origin", "topic/x"], src, self.env)

        packs = [p for p in self.stub.packs("/gcrepo") if p.endswith(".pack")]
        self.assertGreaterEqual(len(packs), 2, f"expected >=2 packs to compact, got {packs}")

        res = run_helper(["gc", self.url("gcrepo")], self.work, self.env)
        self.assertIn("Compacted", res.stdout + res.stderr, f"gc did not run:\n{res.stdout}\n{res.stderr}")

        # After compaction the nested branch, and the commit unique to it, must
        # still be retrievable by a fresh clone.
        dst = self.work / "clone_gc"
        run_git(["clone", self.url("gcrepo"), str(dst)], self.work, self.env)
        log = run_git(["log", "--oneline", "origin/topic/x"], dst, self.env).stdout
        self.assertIn("topic only commit", log)


class TestGcRefusesUnsafeCompaction(E2ETestCase):
    """gc must never delete packfiles it cannot prove are superseded.

    compact_repository mirrors remote refs into a scratch bare repo so that
    ``git repack -a -d`` can tell reachable objects from garbage.  If the ref
    listing fails, the mirror comes up empty, every object looks unreachable,
    and gc deletes the lot -- while reporting success.
    """

    def test_gc_refuses_when_ref_listing_is_empty(self):
        src = init_repo(self.work / "src_gcunsafe", self.env)
        commit_file(src, "a.txt", "base\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url("gcunsafe")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)
        commit_file(src, "b.txt", "more\n", "second commit", self.env)
        run_git(["push", "origin", "main"], src, self.env)

        before = sorted(p for p in self.stub.packs("/gcunsafe") if p.endswith(".pack"))
        self.assertGreaterEqual(len(before), 2, f"need >=2 packs to compact, got {before}")

        # A transient failure on the refs listing makes a populated repo look
        # ref-less.  gc must not treat that as "everything is unreachable".
        self.stub.faults["dir_404"] = "refs/"
        res = run_helper(["gc", self.url("gcunsafe")], self.work, self.env, check=False)

        after = sorted(p for p in self.stub.packs("/gcunsafe") if p.endswith(".pack"))
        self.assertEqual(after, before, "gc destroyed remote packfiles after an empty ref listing")
        self.assertNotEqual(res.returncode, 0, f"gc reported success:\n{res.stdout}\n{res.stderr}")


class TestRefsListingFailureIsNotSilent(E2ETestCase):
    """A failed refs listing must not be reported as an empty repository.

    Seafile answers 404 for a directory that does not exist, which is the
    correct answer for a brand-new repo.  But the same status is produced by a
    transient failure, and the helper cannot tell them apart -- so a populated
    repo silently looks empty and clone/fetch/ls-remote all "succeed" while
    doing nothing.
    """

    def _seed(self, name: str) -> None:
        src = init_repo(self.work / f"src_{name}", self.env)
        commit_file(src, "a.txt", "hello\n", "initial commit", self.env)
        run_git(["remote", "add", "origin", self.url(name)], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

    def test_ls_remote_fails_loudly_when_refs_listing_fails(self):
        self._seed("refs404a")
        self.stub.faults["dir_404"] = "refs/"
        proc = run_git(["ls-remote", self.url("refs404a")], self.work, self.env, check=False)
        self.assertNotEqual(
            proc.returncode, 0,
            f"ls-remote claimed success on a failed refs listing:\n{proc.stdout}",
        )

    def test_clone_does_not_silently_produce_an_empty_repo(self):
        self._seed("refs404b")
        self.stub.faults["dir_404"] = "refs/"
        proc = run_git(
            ["clone", self.url("refs404b"), str(self.work / "clone_refs404")],
            self.work, self.env, check=False,
        )
        self.assertNotEqual(
            proc.returncode, 0,
            f"clone claimed success while the refs listing had failed:\n{proc.stderr}",
        )

    def test_genuinely_empty_remote_still_clones_as_empty(self):
        """Guard against a false positive: a new, empty remote is not an error.

        The empty-listing check must only fire when the object store proves the
        repository is not empty, otherwise a brand-new remote becomes unusable.
        """
        proc = run_git(
            ["clone", self.url("brandnew"), str(self.work / "clone_brandnew")],
            self.work, self.env, check=False,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"cloning a new empty remote failed:\n{proc.stderr}",
        )


class TestAutoGcDoesNotDeadlock(E2ETestCase):
    """`seafile.autogc` must actually compact, not stall on its own lock.

    cmd_push held RemoteLock and then called compact_repository, which acquires
    the same lock again.  RemoteLock is not reentrant, so the inner acquire read
    back the caller's own unexpired lease, waited out the full timeout and
    failed -- and that failure was swallowed.  Auto-GC therefore never compacted
    anything, and every threshold-crossing push stalled for the whole timeout.
    """

    def test_push_compacts_once_threshold_is_reached(self):
        src = init_repo(self.work / "src_autogc", self.env)
        run_git(["config", "seafile.autogc", "true"], src, self.env)
        run_git(["config", "seafile.gcthreshold", "2"], src, self.env)

        commit_file(src, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url("autogc")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

        # Second push brings the pack count to the configured threshold.
        commit_file(src, "b.txt", "two\n", "c2", self.env)
        run_git(["push", "origin", "main"], src, self.env)

        packs = sorted(p for p in self.stub.packs("/autogc") if p.endswith(".pack"))
        self.assertEqual(len(packs), 1, f"auto-GC did not compact the remote: {packs}")

        # The repository must still be intact and cloneable after compaction.
        dst = self.work / "clone_autogc"
        run_git(["clone", self.url("autogc"), str(dst)], self.work, self.env)
        self.assertIn("c2", run_git(["log", "--oneline"], dst, self.env).stdout)


if __name__ == "__main__":
    unittest.main()
