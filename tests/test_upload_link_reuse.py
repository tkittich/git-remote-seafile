"""One upload link per folder for a caller that sends many small files, and one more try after a refusal."""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import SeafileStub  # noqa: E402

from git_remote_seafile import client as client_mod  # noqa: E402
from git_remote_seafile.client import SeafileAPIError, SeafileClient  # noqa: E402


class LinkReuseTests(unittest.TestCase):
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

    def put(self, name: str, folder: str = "/d") -> None:
        self.assertTrue(self.client.upload_file(self.rid, folder, name, b"x" * 100, replace=True))

    def test_by_default_every_upload_asks_for_its_own_link(self):
        for i in range(3):
            self.put(f"a{i}")
        self.assertEqual(self.stub.link_serial, 3)

    def test_a_caller_that_reuses_links_asks_once_per_folder(self):
        self.client.reuse_upload_links = True
        for i in range(3):
            self.put(f"a{i}")
        self.put("b0", "/e")
        self.assertEqual(self.stub.link_serial, 2)
        self.assertEqual(sorted(self.stub.files[self.rid]), ["/d/a0", "/d/a1", "/d/a2", "/e/b0"])

    def test_a_reused_link_older_than_the_limit_is_asked_for_again(self):
        self.client.reuse_upload_links = True
        self.put("a0")
        client_mod_limit = client_mod.UPLOAD_LINK_MAX_AGE
        try:
            client_mod.UPLOAD_LINK_MAX_AGE = -1.0
            self.put("a1")
        finally:
            client_mod.UPLOAD_LINK_MAX_AGE = client_mod_limit
        self.assertEqual(self.stub.link_serial, 2)

    def test_a_reused_link_that_has_expired_is_replaced_and_the_upload_still_lands(self):
        self.client.reuse_upload_links = True
        self.stub.faults["expire_link_after"] = 1
        self.put("a0")
        self.put("a1")          # the cached link answers 403 now: one more try on a fresh one
        self.assertEqual(self.stub.files[self.rid]["/d/a1"], b"x" * 100)
        self.assertEqual(self.stub.link_serial, 2)

    def test_a_refusal_that_repeats_is_still_an_error(self):
        self.stub.faults["upload"] = 400
        with self.assertRaises(SeafileAPIError) as caught:
            self.put("a0")
        self.assertIn("HTTP 400", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
