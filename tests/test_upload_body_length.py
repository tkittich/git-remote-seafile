"""The upload endpoint answers 400 when the last network read of a body is only its final CRLF; the client avoids the lengths where that happens.

Measured on a CE 13.0 container behind its nginx (reads of 4096): of 4,200 consecutive body lengths only those 2 past a multiple of
4096 failed, for any content; sending the last byte alone is fine, the last two alone are not.  The test stub answers the same for
those lengths, so a client that could send one fails here.  The client keeps clear of every length 2 past a multiple of 512.
"""

from __future__ import annotations

import io
import os
import pathlib
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import SeafileStub  # noqa: E402

from git_remote_seafile import client as client_mod  # noqa: E402
from git_remote_seafile.client import SeafileAPIError, SeafileClient, StreamingMultipartFile  # noqa: E402

FIELDS = {"parent_dir": "/d", "replace": "1"}
NAME = "obj.bin"


def _default_length_boundary() -> str:
    return f"----GRSBoundary{uuid.uuid4().hex}"


def _size_for_residue_2(name: str = NAME) -> int:
    """A file size whose request body, framed with a boundary of the usual length, is 2 past a multiple of 4096."""
    framing = StreamingMultipartFile(FIELDS, "file", name, io.BytesIO(b""), 0, boundary=_default_length_boundary()).total_len
    return (2 - framing) % 4096 + 4096


class FramingTests(unittest.TestCase):
    def test_no_file_size_gives_the_refused_body_length(self):
        for size in range(0, 9000):
            body = StreamingMultipartFile(FIELDS, "file", NAME, io.BytesIO(b"x" * size), size)
            self.assertNotEqual(body.total_len % 512, 2, size)          # 512 covers every read size that is a multiple of it

    def test_a_size_that_would_be_refused_is_framed_with_a_longer_boundary(self):
        size = _size_for_residue_2()
        plain = StreamingMultipartFile(FIELDS, "file", NAME, io.BytesIO(b"x" * size), size, boundary=_default_length_boundary())
        self.assertEqual(plain.total_len % 4096, 2)          # the length the server refuses, with a boundary of the usual length
        framed = StreamingMultipartFile(FIELDS, "file", NAME, io.BytesIO(b"x" * size), size)
        self.assertNotEqual(framed.total_len % 512, 2)
        self.assertGreater(len(framed.boundary), len(plain.boundary) - 1)
        data = framed.read(-1)
        self.assertEqual(len(data), framed.total_len)          # what is sent is as long as what was announced
        self.assertTrue(data.endswith(f"--{framed.boundary}--\r\n".encode()))

    def test_a_boundary_the_caller_chose_is_kept(self):
        size = _size_for_residue_2()
        kept = StreamingMultipartFile(FIELDS, "file", NAME, io.BytesIO(b"x" * size), size, boundary="B" * 36)
        self.assertEqual(kept.boundary, "B" * 36)


class AgainstTheServersRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = SeafileStub().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.stop()

    def setUp(self) -> None:
        self.stub.reset()
        self.client = SeafileClient(server_url=self.stub.base_url, token="t")
        self.rid = self.client.get_repo_id("testlib")

    def test_a_small_upload_of_the_refused_length_lands(self):
        size = _size_for_residue_2("obj.bin")
        data = os.urandom(size)
        self.assertTrue(self.client.upload_file(self.rid, "/d", "obj.bin", data, replace=True))
        self.assertEqual(self.stub.files[self.rid]["/d/obj.bin"], data)

    def test_the_same_through_a_file_object(self):
        size = _size_for_residue_2("obj.bin")
        data = os.urandom(size)
        self.assertTrue(self.client.upload_file(self.rid, "/d", "obj.bin", io.BytesIO(data), replace=True))
        self.assertEqual(self.stub.files[self.rid]["/d/obj.bin"], data)

    def test_a_chunk_of_the_refused_length_lands_too(self):
        chunk = _size_for_residue_2("obj.bin")
        data = os.urandom(chunk * 2 + 100)
        with patch.object(client_mod, "CHUNKED_UPLOAD_MIN_BYTES", 1000), patch.object(client_mod, "UPLOAD_CHUNK_BYTES", chunk):
            self.assertTrue(self.client.upload_file(self.rid, "/d", "obj.bin", io.BytesIO(data), replace=True))
        self.assertEqual(self.stub.files[self.rid]["/d/obj.bin"], data)

    def test_an_upload_the_server_stored_one_byte_too_long_is_noticed_and_sent_again(self):
        """The same parser bug also stores a body one byte long and answers 200 (measured); the reply names the size."""
        data = os.urandom(5000)
        self.stub.faults["store_plus_one"] = 1
        self.assertTrue(self.client.upload_file(self.rid, "/d", "obj.bin", data, replace=True))
        self.assertEqual(self.stub.files[self.rid]["/d/obj.bin"], data)

    def test_a_wrong_sized_copy_that_a_retry_would_not_replace_is_removed_first(self):
        data = os.urandom(5000)
        self.stub.faults["store_plus_one"] = 1
        self.assertTrue(self.client.upload_file(self.rid, "/d", "obj.bin", io.BytesIO(data), replace=False))
        names = sorted(n for n in self.stub.files[self.rid] if n.startswith("/d/obj"))
        self.assertEqual([self.stub.files[self.rid][n] for n in names], [data])          # one copy, the right one

    def test_a_reply_that_names_no_file_never_deletes_the_parent_folder_or_another_file(self):
        """The cleanup of a wrong-sized ``replace=False`` upload deletes only a name that is ours (review R6 N4)."""
        self.stub.files.setdefault(self.rid, {})["/d/other-writer.refs"] = b"theirs"
        self.stub.faults["reply_without_name"] = 1
        self.assertTrue(self.client.upload_file(self.rid, "/d", "mine.refs", io.BytesIO(b"x" * 100), replace=False))
        self.assertEqual(self.stub.files[self.rid]["/d/other-writer.refs"], b"theirs")          # still there
        self.assertEqual(self.stub.files[self.rid]["/d/mine.refs"], b"x" * 100)

    def test_a_400_is_sent_again_with_a_different_framing_and_lands(self):
        data = os.urandom(5000)
        self.stub.faults["upload_400_once"] = 1
        self.assertTrue(self.client.upload_file(self.rid, "/d", "obj.bin", io.BytesIO(data), replace=True))
        self.assertEqual(self.stub.files[self.rid]["/d/obj.bin"], data)

    def test_a_refusal_that_repeats_is_still_an_error(self):
        self.stub.faults["upload"] = 400
        with self.assertRaises(SeafileAPIError):
            self.client.upload_file(self.rid, "/d", "obj.bin", b"x" * 100, replace=True)

    def test_each_attempt_is_framed_with_a_longer_boundary(self):
        lengths = []
        for pad in range(3):
            body = StreamingMultipartFile(FIELDS, "file", NAME, io.BytesIO(b"x" * 100), 100, boundary_pad=pad)
            lengths.append(len(body.boundary))
        self.assertEqual(lengths[1] - lengths[0], 1)
        self.assertEqual(lengths[2] - lengths[1], 1)

    def test_the_stub_really_refuses_that_length_so_these_tests_can_fail(self):
        size = _size_for_residue_2("obj.bin")
        real = StreamingMultipartFile

        def usual_boundary(*args, **kwargs):
            kwargs["boundary"] = _default_length_boundary()          # no length avoidance: what the client did before
            return real(*args, **kwargs)

        with self.assertRaises(SeafileAPIError) as caught:
            with patch.object(client_mod, "StreamingMultipartFile", usual_boundary):
                self.client.upload_file(self.rid, "/d", "obj.bin", io.BytesIO(os.urandom(size)), replace=True)
        self.assertIn("HTTP 400", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
