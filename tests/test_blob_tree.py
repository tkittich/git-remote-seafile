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

    def test_parallel_jobs_make_one_client_per_thread_not_one_per_file(self):
        """Review R6 N5: a new client per file meant a new login, sessions and upload link for every file."""
        many = self.work / "many-files"
        many.mkdir()
        for i in range(24):
            (many / f"f{i:02}.txt").write_bytes(os.urandom(50) + bytes([i]))
        made, real = [], store.Store.clone

        def counting(self_):
            made.append(1)
            return real(self_)

        with patch.object(store.Store, "clone", counting):
            report = tree.put_tree(self.make(), many, self.work / "many.txt", jobs=4)
        self.assertEqual(report.problems, [])
        self.assertLessEqual(len(made), 4, f"{len(made)} clients for 24 files at 4 jobs")
        made.clear()
        with patch.object(store.Store, "clone", counting):
            got = tree.get_tree(self.make("c"), self.work / "many.txt", self.work / "many-out", jobs=4)
        self.assertEqual(got.problems, [])
        self.assertLessEqual(len(made), 4)

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


class RoundFiveTests(TreeCase):
    def test_a_users_own_part_file_is_never_deleted_by_a_restore(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        out = self.work / "restored"
        out.mkdir()
        for name in ("a.bin.sfblob-part", "a.bin.sfblob-part-0000", "a.bin.sfblob-tmp"):
            (out / name).write_bytes(b"mine " + name.encode())
        report = tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        self.assertEqual((report.restored, report.problems), (4, []))
        for name in ("a.bin.sfblob-part", "a.bin.sfblob-part-0000", "a.bin.sfblob-tmp"):
            self.assertEqual((out / name).read_bytes(), b"mine " + name.encode())
        self.assertEqual([p.name for p in out.rglob("*") if ".sfblob-part-" in p.name and p.name != "a.bin.sfblob-part-0000"], [])

    def test_a_file_that_appears_during_the_fetch_is_not_replaced(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        out = self.work / "restored"
        real = store.Store.get_to

        def late(self_, ref, destination, progress=None):
            result = real(self_, ref, destination, progress)
            target = out / "a.bin"
            if destination.name.startswith("a.bin."):
                target.write_bytes(b"arrived meanwhile")
            return result

        with patch.object(store.Store, "get_to", late):
            report = tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        self.assertEqual((out / "a.bin").read_bytes(), b"arrived meanwhile")
        self.assertTrue(any("a.bin" in p and "appeared" in p for p in report.problems))
        self.assertEqual([p.name for p in out.iterdir() if "sfblob-part" in p.name], [])

    def test_the_record_file_inside_the_folder_is_not_uploaded_and_runs_agree(self):
        record_path = self.src / "blobs.txt"
        first = tree.put_tree(self.make(), self.src, record_path)
        text = record_path.read_text(encoding="utf-8")
        second = tree.put_tree(self.make(), self.src, record_path)
        self.assertNotIn("blobs.txt", [e.path for e in record.parse(text)])
        self.assertEqual(record_path.read_text(encoding="utf-8"), text)
        self.assertIn("blobs.txt: the record file itself", second.skipped)      # the first run found none to leave out
        self.assertEqual(first.problems, [])
        self.assertEqual(second.uploaded, 0)
        out = self.work / "restored"
        self.assertEqual(tree.get_tree(self.make("c"), record_path, out).problems, [])
        self.assertEqual(tree.get_tree(self.make("c"), record_path, out).in_place, 4)

    def test_a_size_the_hash_overrules_is_a_warning_and_the_file_is_placed(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        path = self.work / "blobs.txt"
        path.write_text(path.read_text(encoding="utf-8").replace("\t100\t", "\t999\t"), encoding="utf-8")
        out = self.work / "restored"
        report = tree.get_tree(self.make("c"), path, out)
        self.assertEqual((report.restored, report.problems), (4, []))
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("sub/b.bin", report.warnings[0])
        self.assertEqual((out / "sub" / "b.bin").read_bytes(), self.contents["sub/b.bin"])

    def test_two_files_whose_names_differ_only_in_unicode_form_are_a_problem_not_one_line(self):
        nfc, nfd = "caf\u00e9.bin", "caf\u0065\u0301.bin"
        folder = self.work / "twins"
        folder.mkdir()
        (folder / nfc).write_bytes(b"same")
        try:
            (folder / nfd).write_bytes(b"same")
        except OSError:
            self.skipTest("this volume cannot hold both spellings")
        if len(list(folder.iterdir())) != 2:
            self.skipTest("this volume folds the two spellings into one file")
        report = tree.put_tree(self.make(), folder, self.work / "twins.txt")
        self.assertEqual(len(report.problems), 1)
        self.assertIn("Unicode", report.problems[0])
        self.assertFalse((self.work / "twins.txt").exists())


class PresenceCacheTests(TreeCase):
    def test_a_folder_run_lists_each_fan_out_folder_once_for_its_pre_checks(self):
        tree.put_tree(self.make(), self.src, None)
        before = self.stub.dir_calls
        again = tree.put_tree(self.make(), self.src, None)
        self.assertEqual(again.uploaded, 0)
        distinct = {hashlib.sha256(d).hexdigest()[:2] for d in self.contents.values()}
        self.assertLessEqual(self.stub.dir_calls - before, len(distinct) + 3)          # + the marker check and the root

    def test_a_single_put_always_asks_the_server(self):
        base = self.make()
        one = self.src / "a.bin"
        base.put(one)
        before = self.stub.dir_calls
        base.put(one)
        base.put(one)
        self.assertGreaterEqual(self.stub.dir_calls - before, 2)          # a fresh listing for each

    def test_a_damaged_object_is_still_repaired_by_a_single_put_right_after_a_folder_run(self):
        base = self.make()
        tree.put_tree(base, self.src, None)
        name = next(n for n in self.stub.files[self.rid] if n.endswith(hashlib.sha256(self.contents["a.bin"]).hexdigest()))
        self.stub.files[self.rid][name] = b"short"
        self.assertTrue(base.put(self.src / "a.bin").uploaded)


class RestoreGuardTests(TreeCase):
    """``get --from`` has the guards the snapshot restore has: refused names, links on the way, long Windows paths."""

    def setUp(self) -> None:
        super().setUp()
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")

    def test_a_name_the_volume_refuses_is_reported_and_the_rest_is_restored(self):
        from git_remote_seafile.snapshot import restore_names
        refuse = lambda rel: "has a character this volume refuses" if "deeper" in rel else None          # noqa: E731
        with patch.object(restore_names, "destination_name_problem", return_value=refuse):
            report = tree.get_tree(self.make("c"), self.work / "blobs.txt", self.work / "out")
        self.assertEqual(report.restored, 3)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("sub/deeper/c.bin", report.problems[0])
        self.assertIn("not restored", report.problems[0])
        self.assertFalse((self.work / "out" / "sub" / "deeper").exists())

    def test_a_link_on_the_way_is_never_written_through(self):
        out, elsewhere = self.work / "out", self.work / "elsewhere"
        out.mkdir()
        elsewhere.mkdir()
        try:
            os.symlink(elsewhere, out / "sub", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this machine cannot make a link")
        report = tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        self.assertEqual(sorted(p.split(":")[0] for p in report.problems), ["sub/b.bin", "sub/deeper/c.bin"])
        self.assertTrue(all("is a link" in p for p in report.problems))
        self.assertEqual(list(elsewhere.iterdir()), [])          # nothing went through the link
        self.assertEqual(report.restored, 2)

    @unittest.skipUnless(os.name == "nt", "the 260-character limit is Windows'")
    def test_a_path_past_260_characters_is_restored_on_windows(self):
        long_name = "/".join(["d" * 60] * 5 + ["file.bin"])          # about 320 characters below the destination
        entries = [record.Entry(long_name, len(self.contents["a.bin"]),
                                next(e.ref for e in record.parse((self.work / "blobs.txt").read_text(encoding="utf-8")) if e.path == "a.bin"))]
        rec = self.work / "long.txt"
        rec.write_text(record.render(entries), encoding="utf-8")
        report = tree.get_tree(self.make("c"), rec, self.work / "out")
        self.assertEqual((report.restored, report.problems), (1, []))
        from git_remote_seafile.snapshot import payload_restore
        self.assertEqual(payload_restore.extended_path(self.work / "out" / long_name).read_bytes(), self.contents["a.bin"])


class DryRunTests(TreeCase):
    def test_a_dry_run_put_changes_nothing_and_says_what_it_would_do(self):
        report = tree.put_tree(self.make(), self.src, self.work / "blobs.txt", dry_run=True)
        self.assertEqual(self.stub.files.get(self.rid, {}), {})              # not even the marker
        self.assertFalse((self.work / "blobs.txt").exists())
        self.assertTrue(report.dry_run)
        self.assertEqual((len(report.entries), report.problems), (4, []))
        self.assertGreaterEqual(report.uploaded, 3)

    def test_a_dry_run_after_a_real_put_would_upload_nothing(self):
        tree.put_tree(self.make(), self.src, None)
        before = dict(self.stub.files[self.rid])
        report = tree.put_tree(self.make(), self.src, self.work / "blobs.txt", dry_run=True)
        self.assertEqual((report.uploaded, report.present), (0, 4))
        self.assertEqual(self.stub.files[self.rid], before)
        self.assertFalse((self.work / "blobs.txt").exists())

    def test_a_dry_run_get_writes_nothing_creates_no_folder_and_touches_no_cache(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        out = self.work / "restored"
        report = tree.get_tree(self.make("fresh"), self.work / "blobs.txt", out, dry_run=True)
        self.assertEqual((report.restored, report.in_place, report.problems), (4, 0, []))
        self.assertFalse(out.exists())
        self.assertFalse((self.work / "fresh").exists())

    def test_a_dry_run_get_still_reports_a_path_that_holds_other_bytes(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        out = self.work / "restored"
        out.mkdir()
        (out / "a.bin").write_bytes(b"mine")
        report = tree.get_tree(self.make("fresh"), self.work / "blobs.txt", out, dry_run=True)
        self.assertEqual(len(report.problems), 1)
        self.assertEqual((out / "a.bin").read_bytes(), b"mine")


class ExcludeTests(TreeCase):
    def test_excluded_paths_are_not_uploaded_and_not_recorded(self):
        report = tree.put_tree(self.make(), self.src, self.work / "blobs.txt", exclude=["sub/*"])
        self.assertEqual(report.excluded, 2)
        paths = [e.path for e in record.parse((self.work / "blobs.txt").read_text(encoding="utf-8"))]
        self.assertEqual(paths, ["a.bin", "same-as-a.bin"])

    def test_a_pattern_without_a_slash_matches_across_folders_as_the_shell_tools_do(self):
        report = tree.put_tree(self.make(), self.src, None, exclude=["*c.bin"])
        self.assertEqual(report.excluded, 1)

    def test_a_pattern_matches_whatever_unicode_form_either_side_uses(self):
        self.assertTrue(tree.excluded_by("caf\u00e9/x.bin", ["caf\u0065\u0301/*"]))
        self.assertTrue(tree.excluded_by("caf\u0065\u0301/x.bin", ["caf\u00e9/*"]))
        self.assertTrue(tree.excluded_by("a\\b.bin", ["a/*"]))

    def test_excluding_everything_writes_an_empty_record_and_uploads_nothing(self):
        report = tree.put_tree(self.make(), self.src, self.work / "blobs.txt", exclude=["*"])
        self.assertEqual((report.excluded, len(report.entries)), (4, 0))
        self.assertEqual((self.work / "blobs.txt").read_text(encoding="utf-8"), "")

    def test_get_from_leaves_excluded_paths_alone(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        out = self.work / "restored"
        report = tree.get_tree(self.make("fresh"), self.work / "blobs.txt", out, exclude=["sub/*"])
        self.assertEqual((report.restored, report.excluded), (2, 2))
        self.assertFalse((out / "sub").exists())

    def test_no_delete_a_restore_never_removes_what_is_not_in_the_record(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        out = self.work / "restored"
        out.mkdir()
        (out / "mine.txt").write_bytes(b"my file")
        tree.get_tree(self.make("fresh"), self.work / "blobs.txt", out)
        self.assertEqual((out / "mine.txt").read_bytes(), b"my file")


if __name__ == "__main__":
    unittest.main()
