"""The blob store against the stub server: put, get, cat, references, the marker (STORE.md R1-R7, D2, D4, D5)."""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import SeafileStub  # noqa: E402

from git_remote_seafile.blob import cache as cache_mod  # noqa: E402
from git_remote_seafile.blob import refs, store  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402

DATA = os.urandom(5000)
SHA = hashlib.sha256(DATA).hexdigest()
ROOT = "/blobs-here"


class StoreCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = SeafileStub().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.stop()

    def setUp(self) -> None:
        self.stub.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = pathlib.Path(self._tmp.name)
        self.cache = cache_mod.Cache(self.work / "cache")
        self.client = SeafileClient(server_url=self.stub.base_url, token="t")
        self.rid = self.client.get_repo_id("testlib")
        self.store = self.make()
        self.source = self.work / "data.bin"
        self.source.write_bytes(DATA)

    def make(self, root: str = ROOT) -> store.Store:
        client = SeafileClient(server_url=self.stub.base_url, token="t")
        return store.Store(client, self.rid, root, self.cache)

    def files(self) -> "dict[str, bytes]":
        return dict(self.stub.files.get(self.rid, {}))

    def ref(self, sha: str = SHA) -> refs.Ref:
        return refs.full(f"sha256:{sha}")


class RoundTripTests(StoreCase):
    def test_put_then_get_from_an_empty_cache_returns_the_exact_bytes(self):
        put = self.store.put(self.source)
        self.assertTrue(put.uploaded)
        self.assertEqual((str(put.ref), put.size), (f"sha256:{SHA}", len(DATA)))
        self.assertEqual(self.files()[f"{ROOT}/blobs/{SHA[:2]}/{SHA}"], DATA)
        fresh = store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, ROOT,
                            cache_mod.Cache(self.work / "another-machine"))
        path, cached = fresh.get(put.ref)
        self.assertFalse(cached)
        self.assertEqual(path.read_bytes(), DATA)

    def test_nothing_but_the_marker_and_the_object_is_left_behind(self):
        self.store.put(self.source)
        names = sorted(self.files())
        self.assertEqual(names, [f"{ROOT}/.seafile-store", f"{ROOT}/blobs/{SHA[:2]}/{SHA}"])

    def test_a_second_put_of_the_same_bytes_uploads_nothing(self):
        self.store.put(self.source)
        before = dict(self.files())
        with patch.object(self.store.client, "upload_file", side_effect=AssertionError("uploaded again")):
            again = self.store.put(self.source)
        self.assertFalse(again.uploaded)
        self.assertEqual(self.files(), before)

    def test_a_get_that_hits_the_cache_asks_the_server_nothing(self):
        put = self.store.put(self.source)
        self.store.get(put.ref)
        self.stub.raw_hits.clear()
        with patch.object(self.store.client, "list_dir", side_effect=AssertionError("asked the server")):
            path, cached = self.store.get(put.ref)
        self.assertTrue(cached)
        self.assertEqual(path.read_bytes(), DATA)
        self.assertEqual(self.stub.raw_hits, [])

    def test_a_zero_byte_object_round_trips(self):
        empty = self.work / "empty"
        empty.write_bytes(b"")
        put = self.store.put(empty)
        self.assertEqual(put.size, 0)
        fresh = store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, ROOT,
                            cache_mod.Cache(self.work / "c2"))
        self.assertEqual(fresh.get(put.ref)[0].read_bytes(), b"")

    def test_a_missing_object_is_not_in_the_store_and_nothing_is_cached(self):
        self.store.put(self.source)
        with self.assertRaises(store.StoreError) as caught:
            self.store.get(self.ref(hashlib.sha256(b"nothing").hexdigest()))
        self.assertIn("is not in the store", str(caught.exception))
        self.assertEqual(self.cache.objects(), [])

    def test_exists_and_stat(self):
        self.store.put(self.source)
        self.assertTrue(self.store.exists(self.ref()))
        self.assertEqual(self.store.stat(self.ref()), len(DATA))
        self.assertFalse(self.store.exists(self.ref(hashlib.sha256(b"x").hexdigest())))


class ReferenceTests(StoreCase):
    def test_a_short_reference_names_the_one_object_it_matches(self):
        self.store.put(self.source)
        got, _ = self.store.get(refs.parse(f"sha256:{SHA[:8]}"))
        self.assertEqual(got.read_bytes(), DATA)

    def test_an_unknown_short_reference_is_an_error_not_a_guess(self):
        self.store.put(self.source)
        with self.assertRaises(store.StoreError):
            self.store.get(refs.parse(f"sha256:{SHA[:2]}ffff"))


class HonestyTests(StoreCase):
    def test_a_file_that_changes_between_hashing_and_the_upload_publishes_nothing(self):
        real = self.store.client.upload_file

        def changing(rid, parent, name, content, *args, **kwargs):
            if ".part-" in name:                                    # the object's upload, not the marker's
                self.source.write_bytes(bytes(reversed(DATA)))     # same size, other bytes
            return real(rid, parent, name, content, *args, **kwargs)

        with patch.object(self.store.client, "upload_file", changing):
            with self.assertRaises(store.StoreError) as caught:
                self.store.put(self.source)
        self.assertIn("changed while it was being uploaded", str(caught.exception))
        self.assertFalse([n for n in self.files() if f"/{SHA}" in n], self.files())

    def test_a_failed_commit_does_not_leave_a_wrong_sized_object_under_the_hash_name(self):
        """The rename landed on the free name but the server holds other bytes there (review R6 N7)."""
        key = f"{ROOT}/blobs/{SHA[:2]}/{SHA}"
        self.store.check_root(create=True)

        def landed_wrong(client, rid, directory, temp, sha, size):
            self.stub.files[rid][key] = DATA + b"\r"          # the object is there, one byte too long
            return False

        with patch("git_remote_seafile.snapshot.payload_upload._commit_object", landed_wrong):
            with self.assertRaises(store.StoreError):
                self.store.put(self.source)
        self.assertNotIn(key, self.files())          # not left for `exists` to vouch for

    def test_a_damaged_object_under_the_name_is_replaced_by_the_bytes_it_promises(self):
        self.store.put(self.source)
        key = f"{ROOT}/blobs/{SHA[:2]}/{SHA}"
        self.stub.files[self.rid][key] = DATA[:100]
        put = self.store.put(self.source)
        self.assertTrue(put.uploaded)
        self.assertEqual(self.files()[key], DATA)
        self.assertEqual([n for n in self.files() if "(1)" in n], [])

    def test_a_download_that_does_not_hash_to_the_reference_fails_and_caches_nothing(self):
        self.store.put(self.source)
        key = f"{ROOT}/blobs/{SHA[:2]}/{SHA}"
        self.stub.files[self.rid][key] = bytes(reversed(DATA))     # right size, wrong bytes
        with self.assertRaises(store.StoreError) as caught:
            self.store.get(self.ref())
        self.assertIn("could not fetch", str(caught.exception))
        self.assertEqual(self.cache.objects(), [])
        self.assertEqual([p.name for p in self.cache.partial_dir().glob("*") if p.suffix != ".lock"], [])   # a lock file stays (D4)

    def test_a_folder_under_an_objects_name_is_not_an_object(self):
        self.store.put(self.source)
        key = f"{ROOT}/blobs/{SHA[:2]}/{SHA}"
        del self.stub.files[self.rid][key]
        self.stub.files[self.rid][f"{key}/inside"] = b"x"
        self.assertFalse(self.store.exists(self.ref()))


class ConcurrentGetTests(StoreCase):
    def test_two_gets_of_one_object_in_one_process_fetch_it_once_and_both_succeed(self):
        import threading
        import time
        put = self.store.put(self.source)
        fresh_cache = cache_mod.Cache(self.work / "shared")
        mine = store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, ROOT, fresh_cache)
        yours = store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, ROOT, fresh_cache)
        real = SeafileClient.download_file_to
        calls: "list[int]" = []

        def slow(self_, *args, **kwargs):
            calls.append(1)
            time.sleep(0.3)                      # long enough for the other thread to arrive
            return real(self_, *args, **kwargs)

        results: "list[object]" = []

        def run(which: store.Store) -> None:
            try:
                results.append(which.get(put.ref)[0].read_bytes())
            except Exception as exc:             # a failure is a result the assertions show
                results.append(exc)

        with patch.object(SeafileClient, "download_file_to", slow):
            threads = [threading.Thread(target=run, args=(mine,)), threading.Thread(target=run, args=(yours,))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, [DATA, DATA])
        self.assertEqual(len(calls), 1)


class ResumeAcrossRunsTests(StoreCase):
    def test_a_partial_an_earlier_run_left_is_continued_and_proved_by_the_hash(self):
        put = self.store.put(self.source)
        fresh = store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, ROOT,
                            cache_mod.Cache(self.work / "resume"))
        folder = fresh.cache.partial_dir()
        folder.mkdir(parents=True)
        (folder / f"{SHA}.part").write_bytes(DATA[:1000])
        seen: "list[int]" = []
        real = SeafileClient.download_file_to

        def watching(self_, repo, path, dest, progress_callback=None, max_bytes=None, resume_from=0):
            seen.append(resume_from)
            return real(self_, repo, path, dest, progress_callback, max_bytes, resume_from)

        with patch.object(SeafileClient, "download_file_to", watching):
            path, _ = fresh.get(put.ref)
        self.assertEqual(seen, [1000])
        self.assertEqual(path.read_bytes(), DATA)

    def test_a_partial_of_other_bytes_is_caught_by_the_hash_and_the_fetch_starts_again(self):
        put = self.store.put(self.source)
        fresh = store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, ROOT,
                            cache_mod.Cache(self.work / "resume2"))
        folder = fresh.cache.partial_dir()
        folder.mkdir(parents=True)
        (folder / f"{SHA}.part").write_bytes(b"\x00" * 1000)         # not a prefix of the object
        path, _ = fresh.get(put.ref)
        self.assertEqual(path.read_bytes(), DATA)

    def test_a_partial_longer_than_the_object_is_discarded(self):
        put = self.store.put(self.source)
        fresh = store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, ROOT,
                            cache_mod.Cache(self.work / "resume3"))
        folder = fresh.cache.partial_dir()
        folder.mkdir(parents=True)
        (folder / f"{SHA}.part").write_bytes(b"\x01" * (len(DATA) + 50))
        self.assertEqual(fresh.get(put.ref)[0].read_bytes(), DATA)


class LayoutTests(unittest.TestCase):
    def test_a_stores_folders_are_layout_only_under_a_store_root(self):
        from git_remote_seafile.client import _is_layout_dir
        self.assertTrue(_is_layout_dir("/s", "blobs", "/s", True))
        self.assertTrue(_is_layout_dir("/s/blobs", "ab", "/s", True))
        self.assertTrue(_is_layout_dir("/s", "pins", "/s", True))
        self.assertTrue(_is_layout_dir("/s/pins", "keep-me", "/s", True))
        self.assertFalse(_is_layout_dir("/s/blobs", "zz", "/s", True))          # not two hex characters
        self.assertFalse(_is_layout_dir("/s", "somebodys", "/s", True))

    def test_a_store_at_the_top_of_a_library_keeps_its_first_character(self):
        from git_remote_seafile.client import _is_layout_dir
        for root in ("", "/"):
            with self.subTest(root=root):
                self.assertTrue(_is_layout_dir("/blobs", "ab", root, True))          # was read as "lobs/ab"
                self.assertTrue(_is_layout_dir("/lfs", "ab", root, False))
                self.assertFalse(_is_layout_dir("/blobs", "zz", root, True))

    def test_a_rename_that_times_out_is_a_connection_error_not_a_traceback(self):
        import requests
        from git_remote_seafile.client import SeafileAPIError, SeafileConnectionError
        client = SeafileClient(server_url="https://seafile.example.com", token="t")
        with patch.object(requests.Session, "send", side_effect=requests.ReadTimeout("slow")):
            with self.assertRaises(SeafileConnectionError) as caught:
                client.rename_entry_result("rid", "/a/b", "c")
        self.assertIsInstance(caught.exception, SeafileAPIError)          # every `except SeafileAPIError` sees it

    def test_any_other_repository_is_unchanged(self):
        from git_remote_seafile.client import _is_layout_dir
        for root, store_flag in (("/s", False), (None, True), (None, False)):
            with self.subTest(root=root, store=store_flag):
                self.assertFalse(_is_layout_dir("/s", "blobs", root, store_flag))
                self.assertFalse(_is_layout_dir("/s", "pins", root, store_flag))
        self.assertFalse(_is_layout_dir("/other", "blobs", "/s", True))        # outside the store root

    def test_a_store_tells_its_client_where_its_root_is(self):
        client = SeafileClient(server_url="https://seafile.example.com", token="t")
        store.Store(client, "rid", "/the/root", cache_mod.Cache(pathlib.Path(tempfile.gettempdir()) / "unused"))
        self.assertEqual((client.layout_root, client.store_layout), ("/the/root", True))


class CatAndGetToTests(StoreCase):
    def test_cat_streams_the_verified_bytes_and_leaves_no_cache_file(self):
        put = self.store.put(self.source)
        out = io.BytesIO()
        self.assertEqual(self.store.cat(put.ref, out), len(DATA))
        self.assertEqual(out.getvalue(), DATA)
        self.assertEqual(self.cache.objects(), [])

    def test_cat_writes_nothing_when_the_object_does_not_verify(self):
        put = self.store.put(self.source)
        self.stub.files[self.rid][f"{ROOT}/blobs/{SHA[:2]}/{SHA}"] = bytes(reversed(DATA))
        out = io.BytesIO()
        with self.assertRaises(store.StoreError):
            self.store.cat(put.ref, out)
        self.assertEqual(out.getvalue(), b"")

    def test_get_to_a_path_that_exists_is_refused_and_a_new_one_is_written(self):
        put = self.store.put(self.source)
        taken = self.work / "taken.bin"
        taken.write_bytes(b"mine")
        with self.assertRaises(store.StoreError):
            self.store.get_to(put.ref, taken)
        self.assertEqual(taken.read_bytes(), b"mine")
        new = self.work / "new.bin"
        self.store.get_to(put.ref, new)
        self.assertEqual(new.read_bytes(), DATA)


class RootTests(StoreCase):
    def test_a_folder_that_holds_a_vault_is_never_a_store_root(self):
        self.stub.files.setdefault(self.rid, {})[f"{ROOT}/payload/objects/aa/{'a' * 64}"] = b"x"
        with self.assertRaises(store.StoreError) as caught:
            self.store.put(self.source)
        self.assertIn("vault", str(caught.exception))

    def test_a_non_empty_folder_with_no_marker_is_not_adopted(self):
        self.stub.files.setdefault(self.rid, {})[f"{ROOT}/somebody-elses.txt"] = b"x"
        with self.assertRaises(store.StoreError) as caught:
            self.store.put(self.source)
        self.assertIn("never adopted", str(caught.exception))
        self.assertEqual(list(self.files()), [f"{ROOT}/somebody-elses.txt"])

    def test_a_folder_under_a_vault_is_refused(self):
        self.stub.files.setdefault(self.rid, {})["/parent/refs/heads/main"] = b"x"
        with self.assertRaises(store.StoreError):
            self.make("/parent/store").put(self.source)

    def test_a_store_is_never_created_inside_another_store(self):
        self.store.put(self.source)
        with self.assertRaises(store.StoreError) as caught:
            self.make(f"{ROOT}/inner").put(self.source)
        self.assertIn("inside another one", str(caught.exception))
        self.assertEqual([n for n in self.files() if "/inner/" in n], [])

    def test_a_store_two_levels_below_another_is_refused_too(self):
        self.store.put(self.source)
        with self.assertRaises(store.StoreError):
            self.make(f"{ROOT}/a/b").put(self.source)

    def test_get_on_a_folder_with_no_marker_says_so(self):
        with self.assertRaises(store.StoreError) as caught:
            self.store.get(self.ref(hashlib.sha256(b"y").hexdigest()))
        self.assertIn("not a blob store", str(caught.exception))

    def test_a_marker_of_another_format_is_refused(self):
        self.stub.files.setdefault(self.rid, {})[f"{ROOT}/.seafile-store"] = b'{"format": 99, "id": "x"}'
        with self.assertRaises(store.StoreError) as caught:
            self.store.put(self.source)
        self.assertIn("another format", str(caught.exception))

    def test_a_second_put_uses_the_marker_the_first_wrote(self):
        self.store.put(self.source)
        marker = self.files()[f"{ROOT}/.seafile-store"]
        other = self.work / "other.bin"
        other.write_bytes(b"second object")
        self.make().put(other)
        self.assertEqual(self.files()[f"{ROOT}/.seafile-store"], marker)
        self.assertEqual([n for n in self.files() if ".seafile-store" in n], [f"{ROOT}/.seafile-store"])


if __name__ == "__main__":
    unittest.main()
