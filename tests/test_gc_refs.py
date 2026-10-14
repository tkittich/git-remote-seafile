"""test_gc_refs.py - gc must never lose a ref it could not see, against real git.

The unit tests in test_gc.py mock ``subprocess.run`` wholesale, so they cannot
say what git does with the scratch mirror.  These run the real helper and real
git against the in-memory Seafile stub.  Each one fails without the fix it pins:
the loose-file mirror dropped a branch whose name collides on a case-folding or
Windows filesystem, and ``iter_refs`` skipped a ref it could not read.
"""

from __future__ import annotations

import io
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

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

from git_remote_seafile.client import SeafileAPIError  # noqa: E402
from git_remote_seafile.refs import iter_refs  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _GcFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = SeafileStub().start()
        cls._tmp = tempfile.TemporaryDirectory(prefix="grs-gcrefs-")
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

    def packs(self, name: str) -> list[str]:
        return sorted(p for p in self.stub.packs(f"/{name}") if p.endswith(".pack"))

    def repo_with_branches(self, name: str, branches: list[str], tag: str | None = None):
        """A remote holding main plus one branch (own commit) per name, over >= 2 packs."""
        src = init_repo(self.work / f"src_{name}", self.env)
        commit_file(src, "a.txt", "base\n", "base commit", self.env)
        run_git(["remote", "add", "origin", self.url(name)], src, self.env)
        run_git(["push", "-u", "origin", "main"], src, self.env)
        tips = {}
        for branch in branches:
            run_git(["checkout", "-q", "-b", branch, "main"], src, self.env)
            commit_file(src, f"{branch.replace('/', '-')}.txt", branch, f"only on {branch}", self.env)
            run_git(["push", "origin", branch], src, self.env)
            tips[branch] = run_git(["rev-parse", "HEAD"], src, self.env).stdout.strip()
        run_git(["checkout", "-q", "main"], src, self.env)
        if tag:
            run_git(["tag", tag], src, self.env)
            run_git(["push", "origin", tag], src, self.env)
        self.assertGreaterEqual(len(self.packs(name)), 2, self.packs(name))
        return src, tips


class TestGcMirrorHasNoFilesystemToCollideOn(_GcFixture):
    def test_branches_whose_names_collide_locally_both_survive_gc(self):
        _src, tips = self.repo_with_branches("collide", ["topic-a", "topic-c"])
        files = self.stub.files[REPO_ID]
        # Two refs that are different on the server and one file on Windows and
        # macOS: only the second commit is reachable through the second name.
        for old, new in (("topic-a", "Feature"), ("topic-c", "feature")):
            files[f"/collide/refs/heads/{new}"] = files.pop(f"/collide/refs/heads/{old}")

        res = run_helper(["gc", self.url("collide")], self.work, self.env, check=False)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("Compacted", res.stdout + res.stderr)

        # Objects, not refs, are what gc could have destroyed: a bare clone
        # carries every object the remote still has whatever the refs say.
        dst = self.work / "clone_collide"
        run_git(["clone", "--bare", self.url("collide"), str(dst)], self.work, self.env)
        for branch, sha in tips.items():
            probe = run_git(["cat-file", "-e", f"{sha}^{{commit}}"], dst, self.env, check=False)
            self.assertEqual(probe.returncode, 0, f"{branch}'s commit {sha} was pruned by gc")

    def test_a_device_named_branch_survives_gc(self):
        _src, tips = self.repo_with_branches("device", ["only-here"])
        files = self.stub.files[REPO_ID]
        files["/device/refs/heads/NUL"] = files.pop("/device/refs/heads/only-here")

        res = run_helper(["gc", self.url("device")], self.work, self.env, check=False)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        dst = self.work / "clone_device"
        run_git(["clone", "--bare", self.url("device"), str(dst)], self.work, self.env)
        sha = tips["only-here"]
        probe = run_git(["cat-file", "-e", f"{sha}^{{commit}}"], dst, self.env, check=False)
        self.assertEqual(probe.returncode, 0, "the NUL branch's commit was pruned by gc")


class TestGcRefusesWhenARefCannotBeRead(_GcFixture):
    def test_one_unreadable_ref_stops_gc_with_the_packs_intact(self):
        self.repo_with_branches("flaky", ["dev", "wip"])
        before = self.packs("flaky")
        self.stub.faults["file_404_on"] = "refs/heads/dev"
        res = run_helper(["gc", self.url("flaky")], self.work, self.env, check=False)
        self.assertEqual(self.packs("flaky"), before, "gc deleted packs after an unreadable ref")
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("refs/heads/dev", res.stdout + res.stderr)

    def test_a_namespace_that_lists_as_missing_stops_gc(self):
        self.repo_with_branches("nohd", ["dev"], tag="v1")
        before = self.packs("nohd")
        # refs/tags still lists, so the old "no refs at all" guard stays quiet.
        self.stub.faults["dir_404"] = "refs/heads"
        res = run_helper(["gc", self.url("nohd")], self.work, self.env, check=False)
        self.assertEqual(self.packs("nohd"), before, "gc deleted packs after refs/heads 404'd")
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_a_nested_directory_that_vanishes_stops_gc(self):
        self.repo_with_branches("nest", ["feature/auth", "feature/ui"])
        before = self.packs("nest")
        self.stub.faults["dir_404"] = "refs/heads/feature"
        res = run_helper(["gc", self.url("nest")], self.work, self.env, check=False)
        self.assertEqual(self.packs("nest"), before)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)


class TestListingSaysWhatItCouldNotRead(_GcFixture):
    def test_a_ref_that_cannot_be_read_is_named_not_silently_dropped(self):
        self.repo_with_branches("advert", ["dev", "wip"])
        self.stub.faults["file_404_on"] = "refs/heads/dev"
        res = run_git(["ls-remote", self.url("advert")], self.work, self.env, check=False)
        self.assertIn("refs/heads/dev", res.stderr)
        self.assertIn("could not be read", res.stderr)
        self.assertIn("refs/heads/wip", res.stdout)


class TestIterRefsUnit(unittest.TestCase):
    """The walker's contract without a server: strict raises, lax warns."""

    class _Client:
        def __init__(self, tree, missing=()):
            self.tree, self.missing = tree, set(missing)

        def list_dir(self, repo_id, path):
            if path in self.missing:
                return []
            return [
                {"type": "dir" if kind == "dir" else "file", "name": name}
                for name, kind in self.tree.get(path, {}).items()
            ]

        def dir_exists(self, repo_id, path):
            return path not in self.missing

        def get_file_text(self, repo_id, path):
            parent, _, name = path.rpartition("/")
            val = self.tree.get(parent, {}).get(name)
            return val[len("file:"):] if val and val.startswith("file:") else None

    TREE = {
        "/r/refs": {"heads": "dir"},
        "/r/refs/heads": {"main": "file:aaa", "feature": "dir"},
        "/r/refs/heads/feature": {"auth": "file:bbb"},
    }

    def test_strict_raises_for_a_known_directory_that_is_missing(self):
        client = self._Client(self.TREE, missing=["/r/refs/heads/feature"])
        with self.assertRaises(SeafileAPIError):
            list(iter_refs(client, "rid", "/r", "refs/heads", strict=True))

    def test_lax_warns_and_keeps_the_rest(self):
        client = self._Client(self.TREE, missing=["/r/refs/heads/feature"])
        buf = io.StringIO()
        with redirect_stderr(buf):
            got = dict(iter_refs(client, "rid", "/r", "refs/heads"))
        self.assertEqual(got, {"refs/heads/main": "aaa"})
        self.assertIn("/r/refs/heads/feature", buf.getvalue())

    def test_strict_raises_when_the_namespace_root_is_missing_but_listed(self):
        client = self._Client(self.TREE, missing=["/r/refs/heads"])
        with self.assertRaises(SeafileAPIError):
            list(iter_refs(client, "rid", "/r", "refs/heads", strict=True))

    def test_a_namespace_nobody_created_is_simply_empty(self):
        client = self._Client({"/r/refs": {"heads": "dir"}, "/r/refs/heads": {"main": "file:aaa"}})
        self.assertEqual(list(iter_refs(client, "rid", "/r", "refs/tags", strict=True)), [])

    def test_strict_raises_for_an_unreadable_ref_and_lax_names_it(self):
        tree = {"/r/refs": {"heads": "dir"}, "/r/refs/heads": {"main": "file:aaa", "dev": "dir"}}
        tree["/r/refs/heads"]["bad"] = "file:"  # listed, empty content
        client = self._Client(tree, missing=["/r/refs/heads/dev"])
        with self.assertRaises(SeafileAPIError):
            list(iter_refs(client, "rid", "/r", "refs/heads", strict=True))


if __name__ == "__main__":
    unittest.main()
