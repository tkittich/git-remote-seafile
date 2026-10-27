"""test_refs.py - unit tests for recursive remote ref discovery.

The end-to-end suite proves the behaviour against real git; these tests pin the
walker's own contract (nesting depth, empty entries, missing namespaces) so a
regression is caught in milliseconds rather than via a subprocess.
"""

from __future__ import annotations

import unittest

from git_remote_seafile.refs import REF_NAMESPACES, iter_refs, is_valid_ref_name


class FakeClient:
    """Stand-in exposing only the methods iter_refs relies on.

    ``tree`` maps a remote directory path to its entries; an entry value of
    ``"dir"`` marks a subdirectory, ``"file:<sha>"`` marks a ref file.
    ``existing_dirs`` names directories that exist on the server while being
    empty (a state the flat ``tree`` cannot express).
    """

    def __init__(self, tree: dict[str, dict[str, str]], existing_dirs: set[str] | None = None):
        self.tree = tree
        self.existing_dirs = existing_dirs or set()

    def list_dir(self, repo_id, path):
        return [
            {"type": "dir" if kind == "dir" else "file", "name": name}
            for name, kind in self.tree.get(path, {}).items()
        ]

    def get_file_text(self, repo_id, path):
        parent, _, name = path.rpartition("/")
        val = self.tree.get(parent, {}).get(name)
        return val[len("file:"):] if val and val.startswith("file:") else None

    def dir_exists(self, repo_id, path):
        return path in self.existing_dirs or any(
            k.startswith(path.rstrip("/") + "/") for k in self.tree
        )


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

        seen_threads = set()
        lock = threading.Lock()
        # Two reads must be in flight at the same moment, or the barrier
        # breaks: a serial walk fails deterministically here instead of
        # passing or failing on a loaded runner's scheduling.
        barrier = threading.Barrier(2, timeout=10)

        class ConcurrentClient(FakeClient):
            def get_file_text(self, repo_id, path):
                with lock:
                    seen_threads.add(threading.get_ident())
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass  # the later reads (fewer than a pair left) just go through
                return super().get_file_text(repo_id, path)

        tree = {
            "/git-repo/refs/heads": {f"branch-{i}": f"file:sha{i}" for i in range(8)}
        }
        got = dict(iter_refs(ConcurrentClient(tree), "rid", "/git-repo", "refs/heads", max_workers=4))
        self.assertEqual(len(got), 8)
        self.assertGreater(len(seen_threads), 1)
        self.assertFalse(barrier.broken and len(seen_threads) < 2)

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


class RefNameDifferentialTests(unittest.TestCase):
    """`is_valid_ref_name` against `git check-ref-format`, the specification.

    The hand-written rules in refs.py are a reimplementation of a git command;
    this runs both over every combination of awkward name fragments and demands
    they agree.  It found the trailing-slash and one-level cases.
    """

    ATOMS = [
        "a", "b.c", "", ".", " ", "a b", "a~1", "a^", "a:", "a?", "a*", "a[", "a\\",
        "a..b", "a@{b", "@", "a@b", ".a", "a.", "a.lock", "a/", "/a", "", "a	b",
        "-a", "{", "a{b", "@{", "b.lock",
    ]

    def test_agrees_with_git_check_ref_format(self):
        import itertools
        import shutil
        import subprocess

        if shutil.which("git") is None:
            self.skipTest("git not available")
        candidates = set()
        for atom in self.ATOMS:
            candidates.add("refs/heads/" + atom)
            candidates.add(atom)
        for a, b in itertools.product(self.ATOMS[:14], repeat=2):  # pairs of the first 14: keep the git calls ~300
            candidates.add(f"refs/heads/{a}/{b}")
        disagreements = []
        for name in sorted(candidates):
            git_says = subprocess.run(
                ["git", "check-ref-format", name], capture_output=True
            ).returncode == 0
            if git_says != is_valid_ref_name(name):
                disagreements.append((name, git_says))
        self.assertEqual(disagreements, [], "is_valid_ref_name differs from git check-ref-format")



class TestOversizedRefFiles(unittest.TestCase):
    """A ref is one object id; a planted multi-megabyte file is not read."""

    class _Client(FakeClient):
        def list_dir(self, repo_id, path):
            entries = super().list_dir(repo_id, path)
            for entry in entries:
                if entry["name"] == "huge":
                    entry["size"] = 200 * 1024 * 1024
            return entries

        def get_file_text(self, repo_id, path):
            raise AssertionError("an oversized ref must not be downloaded")

    TREE = {"/r/refs/heads": {"huge": "file:aaa"}}

    def test_lax_warns_and_does_not_download_it(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            got = list(iter_refs(self._Client(self.TREE), "rid", "/r", "refs/heads"))
        self.assertEqual(got, [])
        self.assertIn("refs/heads/huge", buf.getvalue())

    def test_strict_raises(self):
        from git_remote_seafile.client import SeafileAPIError

        with self.assertRaises(SeafileAPIError):
            list(iter_refs(self._Client(self.TREE), "rid", "/r", "refs/heads", strict=True))



class TestConflictCopiesOfDottedRefs(unittest.TestCase):
    def test_the_marker_before_the_extension_is_recognised(self):
        from git_remote_seafile.refs import conflict_copies_of

        entries = [
            {"name": "v1.0.0"},
            {"name": "v1.0 (SFConflict alice 2026-10-14).0"},
            {"name": "release-2.1"},
            {"name": "release-2 (SFConflict bob 2026-10-14).1"},
            {"name": "other (SFConflict bob 2026-10-14)"},
        ]
        self.assertEqual(
            conflict_copies_of(entries, "v1.0.0"), ["v1.0 (SFConflict alice 2026-10-14).0"]
        )
        self.assertEqual(
            conflict_copies_of(entries, "release-2.1"), ["release-2 (SFConflict bob 2026-10-14).1"]
        )
        self.assertEqual(conflict_copies_of(entries, "main"), [])


if __name__ == "__main__":
    unittest.main()


class TestConflictCopies(unittest.TestCase):
    """A Seafile conflict copy is a symptom, and is named as one."""

    def test_helpers(self):
        from git_remote_seafile.refs import conflict_copies_of, is_conflict_copy

        self.assertTrue(is_conflict_copy("main (SFConflict bob 2026-Oct-13-05-44-44)"))
        self.assertFalse(is_conflict_copy("main"))
        entries = [
            {"name": "main"},
            {"name": "main (SFConflict bob 2026-Oct-13-05-44-44)"},
            {"name": "main2 (SFConflict bob 2026-Oct-13-05-44-45)"},
            {"name": "mainline"},
        ]
        self.assertEqual(
            conflict_copies_of(entries, "main"),
            ["main (SFConflict bob 2026-Oct-13-05-44-44)"],
        )

    def test_listing_skips_a_conflict_copy_with_a_specific_message(self):
        import io
        from unittest.mock import MagicMock, patch

        from git_remote_seafile.refs import iter_refs

        client = MagicMock()
        client.list_dir.return_value = [
            {"name": "main", "type": "file"},
            {"name": "main (SFConflict bob 2026-Oct-13-05-44-44)", "type": "file"},
        ]
        client.get_file_text.return_value = "a" * 40
        err = io.StringIO()
        with patch("sys.stderr", err):
            found = list(iter_refs(client, "repo1", "/r", "refs/heads"))
        self.assertEqual([name for name, _ in found], ["refs/heads/main"])
        self.assertIn("conflict copy", err.getvalue())
        self.assertNotIn("malformed", err.getvalue())



class TestEmptyListingOfAKnownDirectory(unittest.TestCase):
    """A directory that exists and answers empty is a read failure, not "no refs".

    The listing a glitch answers empty is the same listing a truncated answer
    comes from: advertising the rest as the whole repository is how a fetch
    quietly loses branches (REVIEW.iq3_s B1, reproduced at the listing layer).
    """

    HEADS = "/git-repo/refs/heads"
    REFS = "/git-repo/refs"

    def _tree_with_existing_empty_heads(self):
        # The parent listing shows heads as a directory; the directory itself
        # exists (200) and answers empty twice.
        return FakeClient(
            {self.REFS: {"heads": "dir"}},
            existing_dirs={self.HEADS},
        )

    def test_strict_listing_treats_it_as_a_read_failure(self):
        from git_remote_seafile.client import SeafileAPIError

        with self.assertRaises(SeafileAPIError) as caught:
            list(iter_refs(self._tree_with_existing_empty_heads(), "rid", "/git-repo",
                           "refs/heads", strict_listing=True))
        self.assertIn("twice answered empty", str(caught.exception))

    def test_strict_gc_walk_treats_it_as_a_read_failure(self):
        from git_remote_seafile.client import SeafileAPIError

        with self.assertRaises(SeafileAPIError):
            list(iter_refs(self._tree_with_existing_empty_heads(), "rid", "/git-repo",
                           "refs/heads", strict=True))

    def test_a_never_created_namespace_is_still_just_empty(self):
        # No directory in the parent listing, nothing existing: a brand-new
        # repository must keep advertising zero branches without a whisper.
        client = FakeClient({})
        self.assertEqual(list(iter_refs(client, "rid", "/git-repo", "refs/heads",
                                        strict_listing=True)), [])


class TestRefWalkDepthCap(unittest.TestCase):
    """A listing that nests forever must not drive the walk forever (C2)."""

    def test_bottomless_nesting_terminates_at_the_cap(self):
        import contextlib
        import io

        class Bottomless:
            """Every directory contains one more directory, never a ref."""

            def list_dir(self, repo_id, path):
                return [{"type": "dir", "name": "deeper"}]

            def get_file_text(self, repo_id, path):
                return None

            def dir_exists(self, repo_id, path):
                return True

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = list(iter_refs(Bottomless(), "rid", "/r", "refs/heads", strict=True))
        self.assertEqual(got, [])
        self.assertIn("nested deeper than", err.getvalue())
        self.assertIn("not advertised", err.getvalue())


class TestRefWalkListingCap(unittest.TestCase):
    """The depth cap stops a chain, not a fan-out (C2): three subdirectories in
    every directory is ~3**32 listings before the depth is reached."""

    class FanOut:
        def __init__(self):
            self.listings = 0

        def list_dir(self, repo_id, path):
            self.listings += 1
            return [{"type": "dir", "name": n} for n in ("a", "b", "c")]

        def get_file_text(self, repo_id, path):
            return None

        def dir_exists(self, repo_id, path):
            return True

    def test_a_fan_out_stops_after_a_bounded_number_of_listings(self):
        import contextlib
        import io

        from git_remote_seafile.refs import _MAX_REF_LISTINGS

        client = self.FanOut()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = list(iter_refs(client, "rid", "/r", "refs/heads"))
        self.assertEqual(got, [])
        self.assertLessEqual(client.listings, _MAX_REF_LISTINGS)
        self.assertIn("directories under refs/heads were listed", err.getvalue())
        self.assertIn("the refs not reached are not advertised", err.getvalue())

    def test_strict_walks_raise_instead_of_returning_a_short_answer(self):
        from git_remote_seafile.client import SeafileAPIError

        with self.assertRaises(SeafileAPIError) as ctx:
            list(iter_refs(self.FanOut(), "rid", "/r", "refs/heads", strict=True))
        self.assertIn("do not describe a repository", str(ctx.exception))


class TestRefReadsRenewTheCallersLease(unittest.TestCase):
    """Reading refs is ~33 ms each on a real server (B5): a few thousand outlast
    a 60 s lease that nothing renewed, and the push fails after its work is done."""

    class Counting:
        """``refs`` flat refs under refs/heads; tracks reads in flight."""

        def __init__(self, refs: int):
            import threading

            self.refs = refs
            self.in_flight = 0
            self.reads = 0
            self._lock = threading.Lock()

        def list_dir(self, repo_id, path):
            if path.endswith("/refs/heads"):
                return [{"type": "file", "name": f"b{i:04d}"} for i in range(self.refs)]
            return []

        def get_file_text(self, repo_id, path):
            import time

            with self._lock:
                self.in_flight += 1
                self.reads += 1
            time.sleep(0.001)
            with self._lock:
                self.in_flight -= 1
            return "a" * 40

        def dir_exists(self, repo_id, path):
            return True

    def test_the_lease_is_renewed_between_batches_and_never_beside_a_read(self):
        client = self.Counting(200)
        calls: list[int] = []

        def renew() -> None:
            calls.append(client.in_flight)

        got = list(iter_refs(client, "rid", "/r", "refs/heads", renew=renew))
        self.assertEqual(len(got), 200)
        self.assertEqual(client.reads, 200)
        # 200 refs, 8 workers x 8 per batch = 4 batches, plus the listing
        self.assertGreaterEqual(len(calls), 4, calls)
        self.assertEqual(set(calls), {0}, "renewal ran while a read was in flight")

    def test_a_single_threaded_walk_renews_before_each_read(self):
        client = self.Counting(5)
        calls: list[int] = []
        list(iter_refs(client, "rid", "/r", "refs/heads", max_workers=1,
                       renew=lambda: calls.append(1)))
        self.assertGreaterEqual(len(calls), 5)

    def test_no_callback_changes_nothing(self):
        client = self.Counting(20)
        self.assertEqual(len(list(iter_refs(client, "rid", "/r", "refs/heads"))), 20)
