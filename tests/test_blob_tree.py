"""Folders: put -r with a record file, get --from (STORE.md D3, "Ease of use")."""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import SeafileStub  # noqa: E402

from git_remote_seafile.blob import cache as cache_mod  # noqa: E402
from git_remote_seafile.blob import record, store, tree  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402

ROOT = "/tree-store"


class TreeCase(unittest.TestCase):
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
        self.rid = SeafileClient(server_url=self.stub.base_url, token="t").get_repo_id("testlib")
        self.src = self.work / "src"
        (self.src / "sub" / "deeper").mkdir(parents=True)
        shared = os.urandom(3000)
        self.contents = {"a.bin": shared, "sub/b.bin": os.urandom(100), "sub/deeper/c.bin": b"", "same-as-a.bin": shared}
        for rel, data in self.contents.items():
            (self.src / rel).write_bytes(data)

    def make(self, cache_name: str = "cache") -> store.Store:
        cache = cache_mod.Cache(self.work / cache_name)

        def factory() -> store.Store:
            client = SeafileClient(server_url=self.stub.base_url, token="t")
            return store.Store(client, self.rid, ROOT, cache, factory=factory)

        return factory()


class PutTreeTests(TreeCase):
    def test_a_folder_is_uploaded_and_recorded_sorted_by_path(self):
        report = tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        self.assertEqual(report.problems, [])
        lines = (self.work / "blobs.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split("\t")[0] for line in lines], sorted(self.contents))
        by_path = {e.path: e for e in record.parse("\n".join(lines))}
        for rel, data in self.contents.items():
            self.assertEqual(by_path[rel].size, len(data))
            self.assertEqual(by_path[rel].ref.hex, hashlib.sha256(data).hexdigest())
        # two paths with the same bytes are one object: a.bin and same-as-a.bin
        self.assertEqual(report.uploaded + report.present, 4)
        self.assertEqual(len([n for n in self.stub.files[self.rid] if "/blobs/" in n]), 3)

    def test_running_it_again_uploads_nothing(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        first = (self.work / "blobs.txt").read_text(encoding="utf-8")
        again = tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        self.assertEqual(again.uploaded, 0)
        self.assertEqual((self.work / "blobs.txt").read_text(encoding="utf-8"), first)

    def test_parallel_jobs_give_the_same_record(self):
        one = tree.put_tree(self.make(), self.src, self.work / "one.txt", jobs=1)
        self.stub.reset()
        many = tree.put_tree(self.make(), self.src, self.work / "many.txt", jobs=4)
        self.assertEqual(one.problems + many.problems, [])
        self.assertEqual((self.work / "one.txt").read_bytes(), (self.work / "many.txt").read_bytes())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_link_is_skipped_and_said(self):
        os.symlink(self.src / "a.bin", self.src / "link.bin")
        report = tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        self.assertEqual(report.skipped, ["link.bin: not a regular file"])
        self.assertNotIn("link.bin", (self.work / "blobs.txt").read_text(encoding="utf-8"))

    def test_a_failed_run_writes_no_record(self):
        real = store.Store.put

        def failing(self_, path, progress=None):
            if path.name == "b.bin":
                raise store.StoreError("injected")
            return real(self_, path, progress)

        with patch.object(store.Store, "put", failing):
            report = tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        self.assertEqual(len(report.problems), 1)
        self.assertIn("sub/b.bin: injected", report.problems[0])
        self.assertFalse((self.work / "blobs.txt").exists())

    def test_a_missing_folder_is_an_error(self):
        with self.assertRaises(store.StoreError):
            tree.put_tree(self.make(), self.work / "nope", None)


class GetTreeTests(TreeCase):
    def setUp(self) -> None:
        super().setUp()
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")

    def test_every_path_is_restored_byte_exact_from_another_machine(self):
        out = self.work / "restored"
        report = tree.get_tree(self.make("other-cache"), self.work / "blobs.txt", out)
        self.assertEqual((report.restored, report.problems), (4, []))
        for rel, data in self.contents.items():
            self.assertEqual((out / rel).read_bytes(), data)
        self.assertEqual(list(out.rglob("*.sfblob-part")), [])

    def test_running_it_again_leaves_what_is_in_place_alone(self):
        out = self.work / "restored"
        tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        again = tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        self.assertEqual((again.restored, again.in_place, again.problems), (0, 4, []))

    def test_a_path_that_holds_other_bytes_is_never_overwritten_and_the_rest_is_still_restored(self):
        out = self.work / "restored"
        (out / "sub").mkdir(parents=True)
        (out / "sub" / "b.bin").write_bytes(b"my own work")
        report = tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("sub/b.bin", report.problems[0])
        self.assertEqual((out / "sub" / "b.bin").read_bytes(), b"my own work")
        self.assertEqual((out / "a.bin").read_bytes(), self.contents["a.bin"])

    def test_a_missing_object_is_reported_for_its_path_only(self):
        gone = hashlib.sha256(self.contents["sub/b.bin"]).hexdigest()
        for name in [n for n in self.stub.files[self.rid] if n.endswith(gone)]:
            del self.stub.files[self.rid][name]
        report = tree.get_tree(self.make("c"), self.work / "blobs.txt", self.work / "restored")
        self.assertEqual(report.restored, 3)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("sub/b.bin", report.problems[0])

    def test_a_bad_record_file_is_refused_before_anything_is_written(self):
        (self.work / "bad.txt").write_text("a\t1\n", encoding="utf-8")
        with self.assertRaises(store.StoreError) as caught:
            tree.get_tree(self.make("c"), self.work / "bad.txt", self.work / "out2")
        self.assertIn("line 1", str(caught.exception))
        self.assertFalse((self.work / "out2").exists())

    def test_parallel_restore_is_the_same_as_one_at_a_time(self):
        report = tree.get_tree(self.make("c"), self.work / "blobs.txt", self.work / "par", jobs=4)
        self.assertEqual((report.restored, report.problems), (4, []))


if __name__ == "__main__":
    unittest.main()
