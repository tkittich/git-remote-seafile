"""test_refs.py - unit tests for recursive remote ref discovery.

The end-to-end suite proves the behaviour against real git; these tests pin the
walker's own contract (nesting depth, empty entries, missing namespaces) so a
regression is caught in milliseconds rather than via a subprocess.
"""

from __future__ import annotations

import unittest

from git_remote_seafile.refs import REF_NAMESPACES, iter_refs, is_valid_ref_name


class FakeClient:
    """Stand-in exposing only the two methods iter_refs relies on.

    ``tree`` maps a remote directory path to its entries; an entry value of
    ``"dir"`` marks a subdirectory, ``"file:<sha>"`` marks a ref file.
    """

    def __init__(self, tree: dict[str, dict[str, str]]):
        self.tree = tree

    def list_dir(self, repo_id, path):
        return [
            {"type": "dir" if kind == "dir" else "file", "name": name}
            for name, kind in self.tree.get(path, {}).items()
        ]

    def get_file_text(self, repo_id, path):
        parent, _, name = path.rpartition("/")
        val = self.tree.get(parent, {}).get(name)
        return val[len("file:"):] if val and val.startswith("file:") else None


class TestIterRefs(unittest.TestCase):
    def test_flat_refs(self):
        tree = {"/git-repo/refs/heads": {"main": "file:aaa", "dev": "file:bbb"}}
        got = dict(iter_refs(FakeClient(tree), "rid", "/git-repo", "refs/heads"))
        self.assertEqual(got, {"refs/heads/main": "aaa", "refs/heads/dev": "bbb"})

    def test_nested_ref_is_discovered(self):
        """The core regression: a slash-named branch must not be invisible."""
        tree = {
            "/git-repo/refs/heads": {"main": "file:aaa", "feature": "dir"},
            "/git-repo/refs/heads/feature": {"auth": "file:ccc"},
        }
        got = dict(iter_refs(FakeClient(tree), "rid", "/git-repo", "refs/heads"))
        self.assertEqual(got["refs/heads/feature/auth"], "ccc")
        self.assertEqual(got["refs/heads/main"], "aaa")

    def test_deeply_nested_and_mixed_levels(self):
        tree = {
            "/git-repo/refs/heads": {"a": "dir"},
            "/git-repo/refs/heads/a": {"b": "dir", "top": "file:s1"},
            "/git-repo/refs/heads/a/b": {"c": "file:s2"},
        }
        got = dict(iter_refs(FakeClient(tree), "rid", "/git-repo", "refs/heads"))
        self.assertEqual(set(got), {"refs/heads/a/b/c", "refs/heads/a/top"})

    def test_empty_sha_is_skipped(self):
        tree = {"/git-repo/refs/heads": {"main": "file:aaa", "broken": "file:"}}
        got = dict(iter_refs(FakeClient(tree), "rid", "/git-repo", "refs/heads"))
        self.assertEqual(set(got), {"refs/heads/main"})

    def test_missing_namespace_yields_nothing(self):
        got = list(iter_refs(FakeClient({}), "rid", "/git-repo", "refs/heads"))
        self.assertEqual(got, [])

    def test_tags_namespace_and_nesting(self):
        tree = {
            "/git-repo/refs/tags": {"v1": "file:t1", "rel": "dir"},
            "/git-repo/refs/tags/rel": {"v2": "file:t2"},
        }
        got = dict(iter_refs(FakeClient(tree), "rid", "/git-repo", "refs/tags"))
        self.assertEqual(set(got), {"refs/tags/v1", "refs/tags/rel/v2"})

    def test_trailing_slash_on_base_path(self):
        tree = {"/git-repo/refs/heads": {"main": "file:aaa"}}
        got = dict(iter_refs(FakeClient(tree), "rid", "/git-repo/", "refs/heads"))
        self.assertEqual(got, {"refs/heads/main": "aaa"})

    def test_namespaces_constant_order(self):
        self.assertEqual(REF_NAMESPACES, ("refs/heads", "refs/tags"))

    def test_parallel_ref_enumeration_deterministic_order(self):
        """Refs are yielded in deterministic sorted order regardless of directory discovery order."""
        tree = {
            "/git-repo/refs/heads": {"zebra": "file:z", "alpha": "file:a", "middle": "file:m"}
        }
        got = list(iter_refs(FakeClient(tree), "rid", "/git-repo", "refs/heads", max_workers=4))
        self.assertEqual(got, [
            ("refs/heads/alpha", "a"),
            ("refs/heads/middle", "m"),
            ("refs/heads/zebra", "z"),
        ])

    def test_parallel_ref_enumeration_concurrency(self):
        """Multiple refs are fetched concurrently across threads."""
        import threading
        import time

        seen_threads = set()
        lock = threading.Lock()

        class ConcurrentClient(FakeClient):
            def get_file_text(self, repo_id, path):
                with lock:
                    seen_threads.add(threading.get_ident())
                time.sleep(0.01)
                return super().get_file_text(repo_id, path)

        tree = {
            "/git-repo/refs/heads": {f"branch-{i}": f"file:sha{i}" for i in range(8)}
        }
        got = dict(iter_refs(ConcurrentClient(tree), "rid", "/git-repo", "refs/heads", max_workers=4))
        self.assertEqual(len(got), 8)
        self.assertGreater(len(seen_threads), 1)

    def test_parallel_ref_enumeration_propagates_fetch_exception(self):
        """Exceptions during individual ref fetches propagate rather than being swallowed."""
        class FailingClient(FakeClient):
            def get_file_text(self, repo_id, path):
                if "broken" in path:
                    raise RuntimeError("simulated network failure")
                return super().get_file_text(repo_id, path)

        tree = {
            "/git-repo/refs/heads": {"good": "file:aaa", "broken": "file:bbb"}
        }
        with self.assertRaises(RuntimeError):
            list(iter_refs(FailingClient(tree), "rid", "/git-repo", "refs/heads", max_workers=2))
        with self.assertRaises(RuntimeError):
            list(iter_refs(FailingClient(tree), "rid", "/git-repo", "refs/heads", max_workers=1))

    def test_ref_name_with_legal_at_sign_is_accepted(self):
        """Legal @ characters (feature@v2, v1.0@release) are accepted per git check-ref-format."""
        self.assertTrue(is_valid_ref_name("refs/heads/feature@v2"))
        self.assertTrue(is_valid_ref_name("refs/tags/v1.0@final"))
        self.assertTrue(is_valid_ref_name("refs/heads/user@domain/task"))

        tree = {
            "/git-repo/refs/heads": {"feature@v2": "file:sha123"}
        }
        got = dict(iter_refs(FakeClient(tree), "rid", "/git-repo", "refs/heads"))
        self.assertEqual(got, {"refs/heads/feature@v2": "sha123"})

    def test_illegal_ref_names_are_rejected(self):
        """Bare @ (the HEAD alias) and @{ sequences / invalid chars are rejected.

        An "@" *inside* a component is legal per git-check-ref-format -- even the odd "refs/heads/@"
        passes there, so it must not be rejected here either.
        """
        self.assertFalse(is_valid_ref_name("@"))
        self.assertTrue(is_valid_ref_name("refs/heads/@"))
        self.assertFalse(is_valid_ref_name("refs/heads/foo@{bar}"))
        self.assertFalse(is_valid_ref_name("refs/heads/.hidden"))
        self.assertFalse(is_valid_ref_name("refs/heads/branch.lock"))
        self.assertFalse(is_valid_ref_name("refs/heads/branch..dots"))
        self.assertFalse(is_valid_ref_name("refs/heads/branch?mark"))
        self.assertFalse(is_valid_ref_name("refs/heads/branch*star"))
        self.assertFalse(is_valid_ref_name("refs/heads/branch[open"))
        self.assertFalse(is_valid_ref_name("refs/heads/branch\\back"))
        self.assertFalse(is_valid_ref_name("refs/heads/trailing."))


if __name__ == "__main__":
    unittest.main()
