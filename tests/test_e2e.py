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
    REPO_ID,
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

    def test_push_then_clone_sha256(self):
        """End-to-end push and clone of SHA-256 repository (N-1)."""
        src = self.work / "src_sha256"
        run_git(["init", "--object-format=sha256", "-b", "main", str(src)], self.work, self.env)
        commit_file(src, "README.txt", "hello sha256\n", "initial sha256 commit", self.env)
        run_git(["remote", "add", "origin", self.url("repo_sha256")], src, self.env)

        run_git(["push", "-u", "origin", "main"], src, self.env)

        dst = self.work / "clone_sha256"
        run_git(["clone", self.url("repo_sha256"), str(dst)], self.work, self.env)

        self.assertEqual(
            (dst / "README.txt").read_text(encoding="utf-8").replace("\r\n", "\n"),
            "hello sha256\n"
        )
        self.assertIn("initial sha256 commit", run_git(["log", "--oneline"], dst, self.env).stdout)
        # Verify local object format of cloned repository is sha256
        res = run_git(["rev-parse", "--show-object-format"], dst, self.env)
        self.assertEqual(res.stdout.strip(), "sha256")


class TestFaultInjection(E2ETestCase):
    """Failure paths a healthy live server will never show on demand."""

    def test_listing_failure_surfaces_as_error(self):
        self.stub.faults["dir"] = 500
        proc = run_git(["ls-remote", self.url("repo3")], self.work, self.env, check=False)
        self.assertNotEqual(proc.returncode, 0, "git should report the helper failure")
        combined = proc.stdout + proc.stderr
        self.assertTrue("500" in combined or "fatal" in combined.lower(),
                        f"expected a visible error, got:\n{combined}")


class TestFetchFailureIsLoud(E2ETestCase):
    """A clone whose pack transfer fails must fail, not produce an empty repo.

    Regression guard for the fetch half of the silent-success class: cmd_fetch
    caught the error, wrote the protocol terminator and returned normally, so
    the helper exited 0 and `git clone` "succeeded" with an empty repository.
    The failure only surfaced later, as missing objects.

    The stub's `raw_pack` fault fails *only* the pack download, so the ref
    listing still succeeds and the fetch stage is isolated.
    """

    def test_clone_fails_when_the_pack_transfer_fails(self):
        src = init_repo(self.work / "src_fetchfail", self.env)
        commit_file(src, "a.txt", "hello\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url("fetchfail")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

        self.stub.faults["raw_pack"] = 500

        dest = self.work / "clone_fetchfail"
        proc = run_git(
            ["clone", self.url("fetchfail"), str(dest)], self.work, self.env, check=False
        )

        self.assertNotEqual(proc.returncode, 0, "git must not report a successful clone")
        combined = proc.stdout + proc.stderr

        # The helper must report the failure *itself*, naming the transfer that
        # failed.  Previously it wrote the protocol terminator and exited 0, so
        # git only discovered the damage later and blamed the object graph:
        # "fatal: remote did not send all necessary objects" -- which names
        # neither the pack nor the transfer that actually failed.
        self.assertIn("git-remote-seafile fatal error:", combined)
        self.assertIn("HTTP 500", combined)

    def test_a_healthy_clone_still_succeeds(self):
        # The control: with no fault injected the same sequence must work, so a
        # passing test above cannot be an artefact of the setup.
        src = init_repo(self.work / "src_fetchok", self.env)
        commit_file(src, "a.txt", "hello\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url("fetchok")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

        dest = self.work / "clone_fetchok"
        run_git(["clone", self.url("fetchok"), str(dest)], self.work, self.env)

        listing = run_git(["log", "--oneline"], dest, self.env)
        self.assertIn("base commit", listing.stdout)


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


class TestGcSha256Repositories(E2ETestCase):
    """gc must compact SHA-256 remotes, not only SHA-1 ones.

    The scratch bare repository used by compaction defaulted to SHA-1, so a
    SHA-256 remote's 64-hex packs were unreadable to it -- index-pack rejected
    them with "pack is corrupted (SHA1 mismatch)" and gc failed outright.  The
    verify-pack/index-pack calls also need the scratch repo as their *context*
    (-C) to pick up its object format, since they run with GIT_DIR scrubbed.
    """

    def test_gc_compacts_a_sha256_remote(self):
        src = self.work / "src_gc_sha256"
        run_git(["init", "--object-format=sha256", "-b", "main", str(src)], self.work, self.env)
        commit_file(src, "a.txt", "one\n", "c1", self.env)
        run_git(["remote", "add", "origin", self.url("gc_sha256")], src, self.env)
        run_git(["push", "-q", "-u", "origin", "main"], src, self.env)
        commit_file(src, "b.txt", "two\n", "c2", self.env)
        run_git(["push", "-q", "origin", "main"], src, self.env)

        before = sorted(p for p in self.stub.packs("/gc_sha256") if p.endswith(".pack"))
        self.assertGreaterEqual(len(before), 2, f"need >=2 packs to compact, got {before}")

        res = run_helper(["gc", self.url("gc_sha256")], self.work, self.env, check=False)
        self.assertEqual(res.returncode, 0, f"gc failed on a SHA-256 remote:\n{res.stdout}\n{res.stderr}")
        self.assertIn("Compacted", res.stdout + res.stderr)

        after = sorted(p for p in self.stub.packs("/gc_sha256") if p.endswith(".pack"))
        self.assertLess(len(after), len(before), f"gc did not reduce the pack count: {before} -> {after}")

        # The compacted remote must still clone back, as SHA-256, with history intact.
        dst = self.work / "clone_gc_sha256"
        run_git(["clone", self.url("gc_sha256"), str(dst)], self.work, self.env)
        self.assertEqual(
            run_git(["rev-parse", "--show-object-format"], dst, self.env).stdout.strip(),
            "sha256",
        )
        self.assertIn("c2", run_git(["log", "--oneline"], dst, self.env).stdout)


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


class TestForcePushOverDivergedHistory(E2ETestCase):
    """A force-push must work when the remote tip is unknown locally.

    cmd_push excludes the remote tip from the object set so the pack stays
    small, but over diverged history that tip is a commit this clone has never
    fetched.  "git rev-list --not <unknown>" then dies with "bad object", so a
    perfectly legitimate force-push fails.
    """

    def test_force_push_succeeds_when_remote_tip_is_not_held_locally(self):
        # Machine A creates the repository.
        a = init_repo(self.work / "src_fp_a", self.env)
        commit_file(a, "a.txt", "base\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url("diverged")], a, self.env)
        run_git(["push", "-u", "origin", "main"], a, self.env)

        # Machine B clones, commits and pushes.  A never learns of this commit.
        b = self.work / "clone_fp_b"
        run_git(["clone", self.url("diverged"), str(b)], self.work, self.env)
        commit_file(b, "b.txt", "from B\n", "commit from B", self.env)
        run_git(["push", "origin", "main"], b, self.env)

        # A has diverged from a tip it does not have, and force-pushes over it.
        commit_file(a, "a2.txt", "from A\n", "commit from A", self.env)
        proc = run_git(["push", "--force", "origin", "main"], a, self.env, check=False)

        self.assertEqual(proc.returncode, 0, f"force-push failed:\n{proc.stdout}\n{proc.stderr}")

        # The remote branch must now be exactly A's HEAD.
        a_head = run_git(["rev-parse", "HEAD"], a, self.env).stdout.strip()
        ls = run_git(["ls-remote", self.url("diverged"), "refs/heads/main"], self.work, self.env)
        self.assertIn(a_head, ls.stdout, f"remote main is not A's HEAD:\n{ls.stdout}")


class TestHarnessDoesNotNeedAmbientGitIdentity(E2ETestCase):
    """`commit_file` must not depend on the machine's git identity.

    A clone does not inherit the local ``user.name``/``user.email`` that
    ``init_repo()`` sets, and CI runners have no global identity -- so
    committing in a clone failed there with "Author identity unknown" while
    passing on developer machines and on GitHub's macOS images.

    Every other E2E test commits only in a repository that ``init_repo()``
    configured, which is why exactly one test failed, and only on Linux and
    Windows.  This test blanks every config source that could supply an
    identity, so the dependency cannot come back unnoticed.
    """

    def test_commit_in_a_clone_succeeds_without_any_ambient_identity(self):
        src = init_repo(self.work / "src_ident", self.env)
        commit_file(src, "a.txt", "base\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url("ident")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

        clone = self.work / "clone_ident"
        run_git(["clone", self.url("ident"), str(clone)], self.work, self.env)

        # Hide every config source that could supply an identity.
        empty_cfg = self.work / "empty-gitconfig"
        empty_cfg.write_text("", encoding="utf-8")
        clean_env = dict(self.env)
        clean_env["GIT_CONFIG_GLOBAL"] = str(empty_cfg)
        clean_env["GIT_CONFIG_SYSTEM"] = str(empty_cfg)

        self.assertEqual(
            run_git(["config", "user.email"], clone, clean_env, check=False).stdout.strip(),
            "",
            "precondition: the clone must have no identity of its own",
        )

        commit_file(clone, "b.txt", "from the clone\n", "commit from the clone", clean_env)

        self.assertIn(
            "commit from the clone",
            run_git(["log", "--oneline"], clone, clean_env).stdout,
        )


class TestConcurrentPushRaceCondition(E2ETestCase):
    """N-1: A push that lands while another client waits for the lock must not be lost."""

    def test_interleaved_push_during_lock_acquisition_rejected(self):
        # 1. Machine A initializes repo and pushes c0
        a = init_repo(self.work / "race_a", self.env)
        commit_file(a, "file.txt", "c0\n", "c0", self.env)
        run_git(["remote", "add", "origin", self.url("repo_race")], a, self.env)
        run_git(["push", "-u", "origin", "main"], a, self.env)

        # 2. Machine A creates a1 and pushes it to side branch 'a_tip' so objects exist on remote
        commit_file(a, "file.txt", "a1\n", "a1", self.env)
        a1_sha = run_git(["rev-parse", "HEAD"], a, self.env).stdout.strip()
        run_git(["push", "origin", "main:refs/heads/a_tip"], a, self.env)

        # 3. Machine B clones at c0 and creates b1
        b = self.work / "race_b"
        run_git(["clone", self.url("repo_race"), str(b)], self.work, self.env)
        commit_file(b, "b.txt", "b1\n", "b1", self.env)

        # 4. Install GET hook: the moment B requests an upload link for .git-lock.d,
        # move remote refs/heads/main to a1 (simulating A landing a push right then).
        hook_fired = []

        def on_lock_attempt():
            self.stub.files.setdefault(REPO_ID, {})["/repo_race/refs/heads/main"] = f"{a1_sha}\n".encode("utf-8")
            hook_fired.append(True)

        self.stub.get_hooks.append((
            lambda p: "upload-link" in p and ".git-lock.d" in p,
            on_lock_attempt,
        ))

        # 5. Machine B pushes to main. Must be rejected as non-fast-forward / fetch first!
        proc = run_git(["push", "origin", "main"], b, self.env, check=False)
        self.assertTrue(len(hook_fired) > 0, "Hook should have fired during push lock acquisition")
        self.assertNotEqual(proc.returncode, 0, "Push B should have been rejected as non-fast-forward")
        combined_err = (proc.stdout + proc.stderr).lower()
        self.assertTrue(
            "non-fast-forward" in combined_err or "fetch first" in combined_err,
            f"Expected non-fast-forward error, got:\n{proc.stdout}\n{proc.stderr}",
        )

        # 6. Verify remote refs/heads/main still points to a1 (A was NOT overwritten!)
        ls = run_git(["ls-remote", self.url("repo_race"), "refs/heads/main"], self.work, self.env)
        self.assertIn(a1_sha, ls.stdout, "A1 must not be overwritten by B1")


class TestStubRealismAndRefFault(E2ETestCase):
    """Verify stub Date headers, directory entry mtime/size, and N-2 per-path fault propagation."""

    def test_server_date_header_and_time_offset_sync(self):
        from git_remote_seafile.client import SeafileClient
        import time

        client = SeafileClient(server_url=self.stub.base_url, token="e2e-token")
        # Perform an API call to trigger response hook
        client.list_dir(REPO_ID, "/")
        self.assertTrue(len(client._server_time_offsets) > 0)
        server_now = client.get_server_time()
        self.assertAlmostEqual(server_now, time.time(), delta=3.0)

    def test_stub_listing_includes_mtime_and_size(self):
        # Put a file in the stub
        self.stub.files.setdefault(REPO_ID, {})["/test_repo/sample.txt"] = b"sample content"
        listing = self.stub.list_dir(REPO_ID, "/test_repo")
        self.assertEqual(len(listing), 1)
        entry = listing[0]
        self.assertEqual(entry["name"], "sample.txt")
        self.assertEqual(entry["type"], "file")
        self.assertIn("mtime", entry)
        self.assertIsInstance(entry["mtime"], int)
        self.assertIn("size", entry)
        self.assertEqual(entry["size"], len(b"sample content"))

    def test_single_ref_read_failure_surfaces_as_error(self):
        # 1. Initialize repo and push branches main and feature
        src = init_repo(self.work / "src_ref_fault", self.env)
        commit_file(src, "base.txt", "base\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url("repo_ref_fault")], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)

        run_git(["checkout", "-q", "-b", "feature"], src, self.env)
        commit_file(src, "feat.txt", "feat\n", "feat commit", self.env)
        run_git(["push", "-u", "origin", "feature"], src, self.env)

        # 2. Inject fault on 'feature' ref only
        self.stub.faults["file_500_on"] = "refs/heads/feature"

        # 3. Running ls-remote should fail loudly rather than silently dropping feature
        proc = run_git(["ls-remote", self.url("repo_ref_fault")], self.work, self.env, check=False)
        self.assertNotEqual(proc.returncode, 0, "ls-remote should fail when a ref read fails")
        combined = (proc.stdout + proc.stderr).lower()
        self.assertTrue("500" in combined or "fatal" in combined or "error" in combined,
                        f"Expected error in output:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
