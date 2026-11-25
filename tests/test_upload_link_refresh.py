"""An upload link expires an hour after it was issued, in the middle of an upload; the client asks for another."""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import SeafileStub  # noqa: E402

from git_remote_seafile import client as client_mod  # noqa: E402
from git_remote_seafile.client import SeafileAPIError, SeafileClient  # noqa: E402

DATA = os.urandom(10_000)          # chunks of 3000: 0-2999, 3000-5999, 6000-8999, 9000-9999


class UploadLinkExpiryTests(unittest.TestCase):
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
        for patcher in (patch.object(client_mod, "CHUNKED_UPLOAD_MIN_BYTES", 1000),
                        patch.object(client_mod, "UPLOAD_CHUNK_BYTES", 3000)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def upload(self) -> None:
        self.assertTrue(self.client.upload_file(self.rid, "/d", "obj.bin", io.BytesIO(DATA), replace=True))

    def test_an_upload_whose_link_expires_every_chunk_still_finishes_whole(self):
        self.stub.faults["expire_link_after"] = 1          # each link answers one chunk, then 403
        self.upload()
        held = self.stub.files[self.rid]["/d/obj.bin"]
        self.assertEqual(hashlib.sha256(held).hexdigest(), hashlib.sha256(DATA).hexdigest())
        self.assertGreaterEqual(self.stub.link_serial, 4)

    def test_an_expired_link_that_drops_the_connection_instead_of_answering_403_is_replaced_too(self):
        """Measured on CE 13.0: a large body sent to an expired link is not read and the request hangs."""
        self.stub.faults["expire_link_after"] = 1
        self.stub.faults["expire_link_drop"] = 1
        self.upload()
        held = self.stub.files[self.rid]["/d/obj.bin"]
        self.assertEqual(hashlib.sha256(held).hexdigest(), hashlib.sha256(DATA).hexdigest())

    def test_a_link_older_than_the_limit_is_replaced_before_a_chunk_is_sent_on_it(self):
        with patch.object(client_mod, "UPLOAD_LINK_MAX_AGE", -1.0)          # negative: a clock that ticks every 15 ms (Windows) can read equal twice, and 0 is then not older:
            self.upload()
        self.assertEqual(self.stub.files[self.rid]["/d/obj.bin"], DATA)
        self.assertGreaterEqual(self.stub.link_serial, 4)

    def test_a_link_that_keeps_expiring_fails_with_the_status_once_the_refreshes_are_spent(self):
        self.stub.faults["expire_link_after"] = 1
        with patch.object(client_mod, "UPLOAD_LINK_REFRESHES", 1):
            with self.assertRaises(SeafileAPIError) as caught:
                self.upload()
        self.assertIn("HTTP 403", str(caught.exception))

    def test_an_upload_that_never_expires_asks_for_one_link(self):
        self.upload()
        self.assertEqual(self.stub.link_serial, 1)


if __name__ == "__main__":
    unittest.main()
