"""The chunked upload with a hashing reader (RESUME.md D1, round 4): the lost reply, per-attempt digests.

The stub keeps what a real server was measured to keep (CE 13.0.29): a chunk stored whose reply was lost is held,
and a resend of it answers 400; after the last chunk the held count is 0 and the file is listed.
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
from git_remote_seafile.client import HashingReader, SeafileAPIError, SeafileClient, UploadSourceChangedError  # noqa: E402

DATA = os.urandom(10_000)          # chunks of 3000: 0-2999, 3000-5999, 6000-8999, 9000-9999 (the last)


class ChunkedUploadFaultTests(unittest.TestCase):
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
        self.client = SeafileClient(server_url=self.stub.base_url, token="t")
        self.rid = self.client.get_repo_id("testlib")
        for patcher in (patch.object(client_mod, "CHUNKED_UPLOAD_MIN_BYTES", 1000),
                        patch.object(client_mod, "UPLOAD_CHUNK_BYTES", 3000),
                        patch.object(client_mod.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def upload(self, name: str = "obj.part-ab", data: bytes = DATA) -> HashingReader:
        reader = HashingReader(io.BytesIO(data), len(data))
        self.assertTrue(self.client.upload_file(self.rid, "/d", name, reader, replace=True))
        return reader

    def held(self, name: str = "obj.part-ab") -> "bytes | None":
        return self.stub.files.get(self.rid, {}).get(f"/d/{name}")

    def test_a_clean_upload_has_the_digest_of_the_file(self):
        reader = self.upload()
        self.assertEqual(self.held(), DATA)
        self.assertEqual(reader.hexdigest(), hashlib.sha256(DATA).hexdigest())
        self.assertFalse(reader.rewound)

    def test_a_middle_chunk_stored_with_its_reply_lost_is_not_resent_and_the_object_is_whole(self):
        self.stub.faults["lost_reply_start"] = 3000
        reader = self.upload()
        self.assertEqual(self.held(), DATA)
        self.assertEqual(reader.hexdigest(), hashlib.sha256(DATA).hexdigest())
        self.assertTrue(reader.complete)
        self.assertFalse(reader.rewound)

    def test_the_last_chunk_stored_with_its_reply_lost_is_resolved_by_the_listing(self):
        self.stub.faults["lost_reply_start"] = 9000
        reader = self.upload()
        self.assertEqual(self.held(), DATA)
        self.assertEqual(reader.hexdigest(), hashlib.sha256(DATA).hexdigest())

    def test_a_single_chunk_upload_whose_reply_was_lost_is_resolved_the_same_way(self):
        data = DATA[:2500]
        self.stub.faults["lost_reply_start"] = 0
        reader = self.upload(data=data)
        self.assertEqual(self.held(), data)
        self.assertEqual(reader.hexdigest(), hashlib.sha256(data).hexdigest())

    def test_a_probe_that_cannot_answer_fails_the_upload_and_does_not_resend(self):
        self.stub.faults["lost_reply_start"] = 3000
        self.stub.faults["uploaded_bytes"] = 500
        with self.assertRaises(SeafileAPIError) as caught:
            self.upload()
        self.assertIn("could not say whether it holds the chunk", str(caught.exception))
        self.assertIsNone(self.held())                                  # never completed
        self.assertEqual(len(self.stub.partial[(self.rid, "/d/obj.part-ab")]), 6000)   # and nothing resent

    def test_a_chunk_the_server_never_stored_is_sent_again(self):
        import requests
        real = self.client._transfer_session
        posts = {"n": 0}

        def wrapped(url: str) -> object:
            session = real(url)
            post = session.post

            def counted(*args: object, **kwargs: object) -> object:
                posts["n"] += 1
                if posts["n"] == 2:
                    raise requests.ConnectionError("reset before anything was stored")
                return post(*args, **kwargs)

            session.post = counted   # type: ignore[method-assign]
            return session

        with patch.object(self.client, "_transfer_session", wrapped):
            reader = self.upload()
        self.assertEqual(posts["n"], 5)                    # four chunks and one resend
        self.assertEqual(self.held(), DATA)
        self.assertEqual(reader.hexdigest(), hashlib.sha256(DATA).hexdigest())


class AttemptDigestTests(unittest.TestCase):
    """One digest per attempt: the server may keep the first attempt although the second is answered."""

    def reader(self, data: bytes) -> "tuple[HashingReader, io.BytesIO]":
        fh = io.BytesIO(data)
        return HashingReader(fh, len(data)), fh

    def send(self, reader: HashingReader, fh: io.BytesIO, offset: int, length: int) -> None:
        fh.seek(offset)
        reader.begin_attempt(offset, length)
        while fh.tell() < offset + length:       # the uploader reads exactly the span, never past it
            reader.read(min(700, offset + length - fh.tell()))

    def test_two_attempts_of_the_same_bytes_leave_the_digest_of_those_bytes(self):
        data = os.urandom(3000)
        reader, fh = self.reader(data)
        self.send(reader, fh, 0, 1000)
        self.send(reader, fh, 0, 1000)        # the retry
        reader.accept()
        self.send(reader, fh, 1000, 2000)
        reader.accept()
        self.assertEqual(reader.hexdigest(), hashlib.sha256(data).hexdigest())
        self.assertFalse(reader.rewound)

    def test_attempts_that_read_different_bytes_are_refused_not_published(self):
        data = os.urandom(2000)
        reader, fh = self.reader(data)
        self.send(reader, fh, 0, 1000)
        fh.seek(0)
        fh.write(b"\x00" * 10)                # the file changes between the attempts
        self.send(reader, fh, 0, 1000)
        with self.assertRaises(UploadSourceChangedError):
            reader.accept()

    def test_an_attempt_cut_in_the_middle_does_not_count(self):
        data = os.urandom(2000)
        reader, fh = self.reader(data)
        fh.seek(0)
        reader.begin_attempt(0, 1000)
        reader.read(300)                       # a connection cut after 300 bytes
        self.send(reader, fh, 0, 1000)
        reader.accept()
        self.send(reader, fh, 1000, 1000)
        reader.accept()
        self.assertEqual(reader.hexdigest(), hashlib.sha256(data).hexdigest())

    def test_a_span_the_reader_never_read_whole_cannot_be_vouched_for(self):
        reader, fh = self.reader(os.urandom(1000))
        reader.begin_attempt(0, 1000)
        reader.read(100)
        with self.assertRaises(UploadSourceChangedError):
            reader.accept()

    def test_the_changed_error_is_not_an_api_error(self):
        """_upload retries SeafileAPIError; this one must reach the caller."""
        self.assertFalse(issubclass(UploadSourceChangedError, SeafileAPIError))


if __name__ == "__main__":
    unittest.main()
