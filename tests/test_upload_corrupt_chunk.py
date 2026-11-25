"""A chunk the server stored one byte too long is noticed at once, and the object is uploaded again under a new name.

Measured on CE 13.0: a chunk's reply carries no size, so the server's parser bug (a read ending between the CR and the LF
before the closing boundary) is invisible until the next chunk answers ``400 Upload offset is invalid``.  The client asks what
the server holds after each chunk, and the callers that choose temporary names start the object again.
"""

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

from git_remote_seafile import client as client_mod  # noqa: E402
from git_remote_seafile.blob import cache as cache_mod  # noqa: E402
from git_remote_seafile.blob import store  # noqa: E402
from git_remote_seafile.client import SeafileClient, UploadCorruptedError  # noqa: E402

DATA = os.urandom(10_000)          # chunks of 3000: 0-2999, 3000-5999, 6000-8999, 9000-9999 (the last)


class CorruptChunkTests(unittest.TestCase):
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
        self.client = SeafileClient(server_url=self.stub.base_url, token="t")
        self.rid = self.client.get_repo_id("testlib")
        for patcher in (patch.object(client_mod, "CHUNKED_UPLOAD_MIN_BYTES", 1000),
                        patch.object(client_mod, "UPLOAD_CHUNK_BYTES", 3000),
                        patch.object(client_mod.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_middle_chunk_stored_long_stops_the_upload_at_once(self):
        self.stub.faults["chunk_plus_one_at"] = 3000
        with self.assertRaises(UploadCorruptedError) as caught:
            self.client.upload_file(self.rid, "/d", "obj.part-aa", io.BytesIO(DATA), replace=True)
        self.assertIn("one byte too long", str(caught.exception))
        self.assertEqual(len(self.stub.partial[(self.rid, "/d/obj.part-aa")]), 6001)          # nothing more was sent

    def test_the_last_chunk_stored_long_is_caught_by_the_size_of_the_finished_file(self):
        self.stub.faults["chunk_plus_one_at"] = 9000
        with self.assertRaises(UploadCorruptedError):
            self.client.upload_file(self.rid, "/d", "obj.part-aa", io.BytesIO(DATA), replace=True)

    def test_the_blob_store_starts_the_object_again_under_a_new_name(self):
        cache = cache_mod.Cache(self.work / "cache")

        def factory() -> store.Store:
            return store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, "/s", cache, factory=factory)

        base = factory()
        source = self.work / "big.bin"
        source.write_bytes(DATA)
        self.stub.faults["chunk_plus_one_at"] = 3000
        done = base.put(source)
        self.assertTrue(done.uploaded)
        self.assertEqual(done.ref.hex, hashlib.sha256(DATA).hexdigest())
        names = {n: d for n, d in self.stub.files[self.rid].items() if "/blobs/" in n}
        self.assertEqual(list(names.values()), [DATA])          # one object, the right bytes, no leftover temporary

    def test_the_blob_store_gives_up_after_the_restarts_and_says_so(self):
        cache = cache_mod.Cache(self.work / "cache")
        base = store.Store(SeafileClient(server_url=self.stub.base_url, token="t"), self.rid, "/s", cache)
        source = self.work / "big.bin"
        source.write_bytes(DATA)
        with patch.object(self.client.__class__, "uploaded_bytes", lambda *a, **k: len(DATA) + 5):
            with self.assertRaises(store.StoreError) as caught:
                base.put(source)
        self.assertIn("wrongly", str(caught.exception))
        self.assertEqual([n for n in self.stub.files.get(self.rid, {}) if "/blobs/" in n], [])


if __name__ == "__main__":
    unittest.main()
