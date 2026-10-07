"""test_refs.py - unit tests for recursive remote ref discovery.

The end-to-end suite proves the behaviour against real git; these tests pin the
walker's own contract (nesting depth, empty entries, missing namespaces) so a
regression is caught in milliseconds rather than via a subprocess.
"""

from __future__ import annotations

import unittest

from git_remote_seafile.refs import REF_NAMESPACES, iter_refs


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


if __name__ == "__main__":
    unittest.main()
