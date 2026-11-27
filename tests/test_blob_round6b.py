"""Round 6, part 2 (Gemini, DeepSeek, Qwen): one test for each guard the reviewers showed could be deleted unseen.

Every test here fails when the fix or guard it names is removed (checked by hand, see IMPL_REVIEW_TRIAGE_R6.md part 2).
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_blob_cli import CliCase  # noqa: E402
from test_blob_store import DATA, ROOT, SHA, StoreCase  # noqa: E402
from test_blob_tree import TreeCase  # noqa: E402

from git_remote_seafile import gitcred  # noqa: E402
from git_remote_seafile.blob import cache as cache_mod  # noqa: E402
from git_remote_seafile.blob import cli, gcreport, pins, record, store, tree  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402
from git_remote_seafile.snapshot import restore_only  # noqa: E402

POSIX = sys.platform != "win32"


class ReadBackProofTests(StoreCase):
    """D8: over http a put is proved by reading it back; a failed proof leaves nothing under the hash name."""

    def _lying_download(self, client: SeafileClient):
        def fake(repo_id, path, destination, **kwargs):
            kwargs["hasher"].update(bytes(reversed(DATA)))          # the right size, other bytes (the stream is what is hashed)
            return True
        return patch.object(client, "download_file_to", fake)

    def _short_download(self, client: SeafileClient):
        def fake(repo_id, path, destination, **kwargs):
            kwargs["hasher"].update(DATA[:-3])          # a body that ended early, with no error
            return True
        return patch.object(client, "download_file_to", fake)

    def test_a_dropped_connection_during_the_read_back_is_a_transport_failure_that_deletes_nothing(self):
        # Round 9 (DeepSeek P4, Gemini P4): it was exit 1, "a fact about the store, do not retry"; it is exit 3, "run it again"
        from git_remote_seafile.client import SeafileConnectionError
        self.store.verify_by_download = True
        with patch.object(self.store.client, "download_file_to", side_effect=SeafileConnectionError("reset by peer")):
            with self.assertRaises(store.StoreTransportError):
                self.store.put(self.source)
        self.assertIn(f"{ROOT}/blobs/{SHA[:2]}/{SHA}", self.files())          # the object was left as it is

    def test_a_body_that_ended_early_proves_nothing_and_deletes_nothing(self):
        # Round 9 (Opus P5): a short read-back counted as a proven mismatch and put deleted the object at its hash name,
        # which may be another writer's verified object.  A short read is a failed read.
        self.store.verify_by_download = True
        with self._short_download(self.store.client):
            with self.assertRaises(store.StoreError) as caught:
                self.store.put(self.source)
        self.assertNotIsInstance(caught.exception, store.ReadBackMismatch)
        self.assertIn("came back", str(caught.exception))
        self.assertIn(f"{ROOT}/blobs/{SHA[:2]}/{SHA}", self.files())          # still there

    def test_a_failed_read_back_raises_and_removes_the_object(self):
        self.store.verify_by_download = True
        with self._lying_download(self.store.client):
            with self.assertRaises(store.StoreError) as caught:
                self.store.put(self.source)
        self.assertIn("did not read back", str(caught.exception))
        self.assertNotIn(f"{ROOT}/blobs/{SHA[:2]}/{SHA}", self.files())          # the next put must not skip it

    def test_without_the_flag_the_same_download_is_never_looked_at(self):
        self.store.verify_by_download = False
        with self._lying_download(self.store.client):
            self.assertTrue(self.store.put(self.source).uploaded)

    def test_a_short_url_over_http_turns_the_proof_on(self):
        env = {"SEAFILE_TOKEN": "t", "SEAFILE_SERVER": self.stub.base_url}
        with patch.dict(os.environ, env):
            opened = cli.open_store("seafile://testlib/short-form", str(self.work / "c2"))
        self.assertTrue(self.stub.base_url.startswith("http://"))
        self.assertTrue(opened.verify_by_download)


class PutGuardTests(StoreCase):
    def test_the_listing_after_the_upload_must_show_the_size(self):
        real = store.Store._listed_size
        calls = []

        def lying(self_, ref, *, fresh=False):
            calls.append(fresh)
            return len(DATA) + 1 if fresh and len(calls) > 1 else real(self_, ref, fresh=fresh)

        with patch.object(store.Store, "_listed_size", lying):
            with self.assertRaises(store.StoreError) as caught:
                self.store.put(self.source)
        self.assertIn("another size", str(caught.exception))

    def test_a_pinned_put_asks_the_server_not_a_listing_it_remembers(self):
        self.store.put(self.source)
        other = self.make()
        with other.presence_cached():
            self.assertEqual(other._listed_size(self.ref()), len(DATA))
            del self.stub.files[self.rid][f"{ROOT}/blobs/{SHA[:2]}/{SHA}"]          # gone behind the cached listing
            self.assertTrue(other.put(self.source, pin="keep").uploaded)

    def test_a_pin_name_is_checked_before_the_marker_is_written(self):
        with self.assertRaises(store.StoreError):
            self.store.put(self.source, pin="ls")
        self.assertEqual(self.files(), {})

    def test_a_dry_run_into_a_vault_is_refused(self):
        self.stub.files.setdefault(self.rid, {})[f"{ROOT}/refs/heads/main"] = b"x"
        with self.assertRaises(store.StoreError):
            self.store.would_put(self.source)

    def test_a_short_reference_does_not_match_a_folder_named_like_an_object(self):
        self.store.put(self.source)
        key = f"{ROOT}/blobs/{SHA[:2]}/{SHA}"
        del self.stub.files[self.rid][key]
        self.stub.files[self.rid][f"{key}/inside"] = b"x"
        with self.assertRaises(store.StoreError):
            self.store.resolve(refs_short(SHA[:10]))

    def test_a_marker_that_is_not_a_marker_when_read_back_is_refused(self):
        with patch.object(self.store.client, "get_file_bytes", return_value=b"{}"):
            with self.assertRaises(store.StoreError) as caught:
                self.store.put(self.source)
        self.assertIn("another format", str(caught.exception))

    def test_two_first_puts_both_succeed_and_the_winners_marker_stays(self):
        """D18 row 6: the loser's marker lands as a copy and the name holds the winner's (review R7 Gemini P3, DeepSeek P1)."""
        import json
        real = self.store.client.upload_file
        winner = json.dumps({"format": 1, "id": "w" * 32}).encode()
        state = {"first": True}

        def racing(repo_id, parent, name, data, **kwargs):
            if state["first"] and name == ".seafile-store":
                state["first"] = False
                real(repo_id, parent, name, winner, replace=False)          # the other writer got there first
            return real(repo_id, parent, name, data, **kwargs)

        self.stub.faults["replace_copies"] = True
        with patch.object(self.store.client, "upload_file", racing):
            self.assertTrue(self.store.put(self.source).uploaded)
        self.assertIn(f"{ROOT}/ (1).seafile-store", self.files())          # the loser's marker, as a copy
        self.assertEqual(json.loads(self.files()[f"{ROOT}/.seafile-store"])["id"], "w" * 32)


def refs_short(text: str):
    from git_remote_seafile.blob import refs
    return refs.parse(f"sha256:{text}")


class RootRefusalTests(StoreCase):
    def test_a_root_named_payload_is_refused(self):
        with self.assertRaises(store.StoreError) as caught:
            self.make("/vault/payload").put(self.source)
        self.assertIn("payload", str(caught.exception))

    def test_a_root_under_the_view_folder_of_a_vault_is_refused(self):
        self.stub.files.setdefault(self.rid, {})["/vault/refs/heads/main"] = b"x"
        with self.assertRaises(store.StoreError) as caught:
            self.make("/vault-view/store").put(self.source)
        self.assertIn("view", str(caught.exception))
        self.assertEqual([n for n in self.files() if "/store/" in n], [])

    def test_a_view_folder_beside_nothing_is_an_ordinary_folder(self):
        self.assertTrue(self.make("/photos-view/store").put(self.source).uploaded)

    def test_a_store_beside_a_vault_at_the_library_root_is_refused(self):
        self.stub.files.setdefault(self.rid, {})["/refs/heads/main"] = b"x"
        with self.assertRaises(store.StoreError):
            self.make("/store").put(self.source)


class FetchTests(StoreCase):
    def test_a_failed_get_to_leaves_no_half_file_and_the_rerun_works(self):
        out = self.work / "out.bin"

        def breaks(src, dst, length=0):
            dst.write(b"x" * 10)
            raise OSError(28, "No space left on device")

        self.store.put(self.source)
        with patch.object(store.shutil, "copyfileobj", breaks):
            with self.assertRaises(store.StoreError):
                self.store.get_to(self.ref(), out)
        self.assertFalse(out.exists())
        self.store.get_to(self.ref(), out)
        self.assertEqual(out.read_bytes(), DATA)

    def test_a_kept_partial_survives_three_dropped_transfers(self):
        self.store.put(self.source)
        fresh = self.make()
        fresh.cache = cache_mod.Cache(self.work / "other-cache")

        def drops(repo_id, path, destination, **kwargs):
            with open(destination, "ab") as handle:
                handle.write(DATA[:100])
            raise ConnectionError("dropped")

        with patch.object(fresh.client, "download_file_to", drops):
            with self.assertRaises(store.StoreError):
                fresh.get(self.ref())
        self.assertTrue((fresh.cache.partial_dir() / f"{SHA}.part").exists())

    def test_cat_stages_outside_the_cache_and_cleans_up_on_ctrl_c(self):
        self.store.put(self.source)
        fresh = self.make()
        fresh.cache = cache_mod.Cache(self.work / "other-cache")
        seen = []

        def interrupted(repo_id, path, destination, **kwargs):
            seen.append(pathlib.Path(destination))
            pathlib.Path(destination).write_bytes(b"half")
            raise KeyboardInterrupt

        with patch.object(fresh.client, "download_file_to", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                fresh.cat(self.ref(), io.BytesIO())
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0].parent, fresh.cache.partial_dir())
        self.assertFalse(seen[0].exists())


class CacheTests(StoreCase):
    def test_an_explicit_size_evicts_what_was_just_handed_out(self):
        self.store.put(self.source)
        self.store.get(self.ref())
        self.assertEqual(self.cache.evict(1), [])          # the grace period protects a file just used
        self.assertEqual(len(self.cache.evict(1, grace=0)), 1)

    @unittest.skipUnless(POSIX, "the file a lock names is checked on POSIX only")
    def test_a_lock_file_that_is_no_longer_the_one_opened_is_not_unlinked(self):
        folder = self.cache.partial_dir()
        folder.mkdir(parents=True)
        lock = folder / f"{SHA}.lock"
        lock.write_bytes(b"")
        with patch.object(cache_mod, "_names_the_open_file", return_value=False):
            self.assertEqual(self.cache._reap_lock(lock), 0)
        self.assertTrue(lock.exists())
        self.assertEqual(self.cache._reap_lock(lock), 1)

    @unittest.skipUnless(POSIX, "needs POSIX descriptors")
    def test_names_the_open_file_tells_a_replaced_file_from_the_same_one(self):
        path = self.work / "f"
        path.write_bytes(b"a")
        fd = os.open(path, os.O_RDWR)
        try:
            self.assertTrue(cache_mod._names_the_open_file(fd, path))
            path.unlink()
            path.write_bytes(b"b")
            self.assertFalse(cache_mod._names_the_open_file(fd, path))
        finally:
            os.close(fd)

    def test_admit_never_chmods_what_a_link_at_the_name_points_to(self):
        victim = self.work / "victim.txt"
        victim.write_bytes(b"mine")
        os.chmod(victim, stat.S_IREAD)
        self.store.put(self.source)
        final = self.cache.path_of(self.ref())
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(victim, final)
        except (OSError, NotImplementedError):
            self.skipTest("cannot make a symlink here")
        before = os.stat(victim).st_mode
        self.store.get(self.ref())
        self.assertEqual(os.stat(victim).st_mode, before)

    @unittest.skipUnless(os.name == "nt", "Windows replaces a read-only file only after a chmod")
    def test_a_failed_replace_seals_the_old_file_again(self):
        self.store.put(self.source)
        self.store.get(self.ref())
        final = self.cache.path_of(self.ref())
        part = self.work / "next.part"
        part.write_bytes(DATA)
        with patch.object(cache_mod.os, "replace", side_effect=PermissionError), patch.object(cache_mod.time, "sleep"):
            with self.assertRaises(PermissionError):
                self.cache.admit(self.ref(), part)
        self.assertFalse(os.stat(final).st_mode & stat.S_IWRITE)


class PinTests(StoreCase):
    def test_an_empty_pin_file_is_an_error_not_a_pin_of_nothing(self):
        self.store.put(self.source, pin="keep")
        for name in [n for n in self.files() if n.endswith(".refs")]:
            self.stub.files[self.rid][name] = b""
        with self.assertRaises(store.StoreError) as caught:
            pins.read_all(self.store)
        self.assertIn("empty", str(caught.exception))

    def test_a_pin_file_that_cannot_be_read_stops_the_read(self):
        self.store.put(self.source, pin="keep")
        with patch.object(self.store.client, "get_file_bytes", return_value=None):
            with self.assertRaises(store.StoreError) as caught:
                pins.read_all(self.store)
        self.assertIn("cannot be read", str(caught.exception))

    def test_a_pin_must_read_back_as_written(self):
        self.store.check_root(create=True)
        with patch.object(self.store.client, "get_file_bytes", return_value=b"sha256:" + b"00" * 32 + b"\n"):
            with self.assertRaises(store.StoreError) as caught:
                pins.write(self.store, "keep", [self.ref()])
        self.assertIn("read back", str(caught.exception))

    def test_ls_is_not_a_pin_name(self):
        with self.assertRaises(store.StoreError):
            pins.check_name("ls")


class GcReportTests(StoreCase):
    def test_the_report_names_what_is_not_the_stores(self):
        self.store.put(self.source, pin="keep")
        files = self.stub.files[self.rid]
        files[f"{ROOT}/stray.txt"] = b"x"                                       # in the store's root
        files[f"{ROOT}/pins/loose.txt"] = b"x"                                  # a file where a pin folder belongs
        files[f"{ROOT}/pins/keep/inner/x"] = b"x"                               # a folder inside a pin
        misplaced = "ab" + SHA[2:]                                              # an object name in the wrong fan-out
        files[f"{ROOT}/blobs/cd/{misplaced}"] = b"x"
        report = gcreport.scan(self.store)
        self.assertIn("stray.txt", report.foreign)
        self.assertIn("pins/loose.txt", report.foreign)
        self.assertIn("pins/keep/inner", report.foreign)
        self.assertIn(f"blobs/cd/{misplaced}", report.foreign)
        self.assertNotIn(misplaced, report.objects)

    def test_a_reference_named_twice_is_one_object_of_the_pin(self):
        self.store.put(self.source, pin="keep")
        folder = f"{ROOT}/pins/keep/"
        name = next(n for n in self.files() if n.startswith(folder))
        self.stub.files[self.rid][name] = self.files()[name] * 2
        self.assertEqual(gcreport.scan(self.store).pins["keep"], 1)


class TreeGuardTests(TreeCase):
    def _record(self, lines: "list[tuple[str, bytes]]") -> pathlib.Path:
        base = self.make()
        entries = []
        for path, data in lines:
            temp = self.work / "obj.bin"
            temp.write_bytes(data)
            done = base.put(temp)
            entries.append(record.Entry(path, done.size, done.ref))
        target = self.work / "case.txt"
        # render refuses two spellings of one Unicode path, not two cases, so a record may hold both
        target.write_text(record.render(entries), encoding="utf-8", newline="\n")
        return target

    def test_paths_that_differ_only_in_case_are_refused_on_a_volume_that_folds_case(self):
        rec = self._record([("A.txt", b"same"), ("a.txt", b"same")])
        out = self.work / "out"
        with patch.object(tree, "_folds_case", return_value=True):
            report = tree.get_tree(self.make("c"), rec, out)
        self.assertEqual(report.restored, 0)
        self.assertEqual(len(report.problems), 2)
        self.assertTrue(all("only in case" in p for p in report.problems))
        self.assertEqual([p.name for p in out.iterdir()], [])

    @unittest.skipIf(os.name == "nt" or sys.platform == "darwin", "this volume folds case")
    def test_they_are_both_restored_where_the_volume_keeps_case(self):
        rec = self._record([("A.txt", b"one"), ("a.txt", b"two")])
        with patch.object(tree, "_folds_case", return_value=False):
            report = tree.get_tree(self.make("c"), rec, self.work / "out")
        self.assertEqual((report.restored, report.problems), (2, []))

    @unittest.skipUnless(os.name == "nt", "NTFS folds case")
    def test_the_probe_finds_that_ntfs_folds_case(self):
        self.assertTrue(tree._folds_case(self.work, False))

    def test_a_junction_counts_as_a_link(self):
        plain = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x10)
        junction = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x10 | 0x400)
        with patch.object(tree.os, "lstat", return_value=plain):
            self.assertFalse(tree.is_link_or_junction(self.work))
        with patch.object(tree.os, "lstat", return_value=junction):
            self.assertTrue(tree.is_link_or_junction(self.work))

    @unittest.skipUnless(os.name == "nt", "mklink /J")
    def test_a_real_junction_is_not_walked_or_written_through(self):
        import subprocess
        outside = self.work / "outside"
        outside.mkdir()
        (self.src / "j").mkdir(exist_ok=True)
        (self.src / "j").rmdir()
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.src / "j"), str(outside)], capture_output=True)
        if made.returncode != 0:
            self.skipTest("cannot make a junction here")
        (outside / "secret.bin").write_bytes(b"outside")
        files, skipped, _ = tree._walk(self.src)
        self.assertFalse(any(p.name == "secret.bin" for p in files))
        self.assertTrue(any("j" in s for s in skipped))

    def test_without_hard_links_a_name_taken_in_the_window_is_not_replaced(self):
        temp, target = self.work / "temp.bin", self.work / "target.bin"
        temp.write_bytes(b"ours")
        target.write_bytes(b"theirs")
        with patch.object(tree.os, "link", side_effect=OSError(1, "no links here")), \
                patch.object(pathlib.Path, "exists", return_value=False), \
                patch.object(pathlib.Path, "is_symlink", return_value=False):
            self.assertFalse(tree._place(temp, target))
        self.assertEqual(target.read_bytes(), b"theirs")

    def test_without_hard_links_a_free_name_is_filled_and_the_temporary_removed(self):
        temp, target = self.work / "temp.bin", self.work / "target.bin"
        temp.write_bytes(b"ours")
        with patch.object(tree.os, "link", side_effect=OSError(1, "no links here")):
            self.assertTrue(tree._place(temp, target))
        self.assertEqual(target.read_bytes(), b"ours")
        self.assertFalse(temp.exists())

    def test_the_staging_name_does_not_depend_on_the_file_name(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        staged = []
        real = store.place_new

        def spy(stage, target):
            staged.append(stage.name)
            return real(stage, target)

        with patch.object(store, "place_new", spy):
            tree.get_tree(self.make("c"), self.work / "blobs.txt", self.work / "out")
        self.assertTrue(staged)
        self.assertTrue(all(n.startswith(".sfblob-part-") and len(n) == len(".sfblob-part-") + 16 for n in staged))

    def test_a_path_holding_other_bytes_of_the_same_size_is_not_in_place(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        out = self.work / "out"
        (out / "sub").mkdir(parents=True)
        (out / "sub" / "b.bin").write_bytes(b"x" * len(self.contents["sub/b.bin"]))
        report = tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        self.assertTrue(any("sub/b.bin" in p and "already there" in p for p in report.problems))
        self.assertEqual((out / "sub" / "b.bin").read_bytes(), b"x" * len(self.contents["sub/b.bin"]))

    def test_a_folder_that_cannot_be_read_is_a_problem_not_a_silent_gap(self):
        real = os.walk

        def failing(top, followlinks=False, onerror=None):
            if onerror:
                onerror(OSError(13, "Permission denied", str(pathlib.Path(top) / "secret")))
            return real(top, followlinks=followlinks, onerror=onerror)

        with patch.object(tree.os, "walk", failing):
            report = tree.put_tree(self.make(), self.src, None)
        self.assertTrue(any(p.startswith("secret") and "cannot be read" in p for p in report.problems))

    @unittest.skipIf(os.name == "nt", "a backslash in a file name is a POSIX thing")
    def test_a_backslash_in_a_posix_name_is_a_problem_not_a_rewrite(self):
        (self.src / "a\\b.txt").write_bytes(b"x")
        report = tree.put_tree(self.make(), self.src, None)
        self.assertTrue(any("a\\b.txt" in p and "backslash" in p for p in report.problems))
        self.assertNotIn("a/b.txt", [e.path for e in report.entries])

    def test_a_bad_pin_name_is_refused_before_the_marker_is_written(self):
        with self.assertRaises(store.StoreError):
            tree.put_tree(self.make(), self.src, None, pin="ls")
        self.assertEqual(self.stub.files.get(self.rid, {}), {})


class CommandGuardTests(CliCase):
    def _main(self, *argv: str) -> "tuple[int, str, str]":
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_an_empty_record_verifies_nothing_and_not_the_whole_store(self):
        self.run_cli("put", str(self.source))
        empty = self.work / "empty.txt"
        empty.write_text("", encoding="utf-8")
        code, out, _ = self.run_cli("verify", "--from", str(empty))
        self.assertEqual(code, 0)
        self.assertIn("0 of 0", out)

    def test_a_command_not_built_yet_says_so_even_after_a_global_flag(self):
        code, _, err = self._main("--store", self.url, "offload")
        self.assertEqual(code, 1)
        self.assertIn("not in this version", err)

    def test_clear_and_size_together_are_a_usage_error(self):
        self.assertEqual(self.run_cli("cache", "--clear", "--size", "1G")[0], 2)

    def test_pin_ls_names_one_pin_or_all_and_refuses_extra_words(self):
        self.run_cli("put", "--pin", "alpha", str(self.source))
        self.run_cli("pin", "beta", f"sha256:{SHA}")
        _, out, _ = self.run_cli("pin", "ls")
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        code, out, _ = self.run_cli("pin", "ls", "alpha")
        self.assertEqual(code, 0)
        self.assertIn("alpha", out)
        self.assertNotIn("beta", out)
        self.assertEqual(self.run_cli("pin", "ls", "no-such")[0], 1)
        self.assertEqual(self.run_cli("pin", "ls", "alpha", "beta")[0], 2)

    def test_put_to_a_pin_named_ls_is_refused_and_writes_nothing(self):
        code, _, _ = self.run_cli("put", "--pin", "ls", str(self.source))
        self.assertEqual(code, 1)
        self.assertEqual(self.stub.files.get(self.stub_rid(), {}), {})

    def test_the_pro_warning_reaches_standard_error_in_gc(self):
        self.run_cli("put", "--pin", "p", str(self.source))
        with patch.object(SeafileClient, "hidden_history_risk", return_value="pins may be hidden"):
            code, out, err = self.run_cli("gc")
        self.assertEqual(code, 0)
        self.assertIn("Warning: pins may be hidden", err)
        self.assertNotIn("Warning", out)

    def stub_rid(self) -> str:
        return next(iter(self.stub.files), "")

    # -- the 0.11.1 items folded in --------------------------------------------------------------------------------

    def test_status_says_what_the_store_holds_and_what_to_run_next(self):
        self.run_cli("put", "--pin", "keep", str(self.source))
        other = self.work / "other.bin"
        other.write_bytes(os.urandom(3000))
        self.run_cli("put", str(other))
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        for text in ("store    :", "objects  : 2", "1 object(s) no pin names", "cache    :", "sfblob pin <name> <reference>"):
            self.assertIn(text, out)
        code, out, _ = self.run_cli("status", "--json")
        data = __import__("json").loads(out)
        self.assertEqual((data["objects"], data["pins"], data["unused"], data["missing"], data["writable"]), (2, 1, 1, 0, True))

    def test_status_exits_1_and_names_the_fix_when_a_pinned_object_is_gone(self):
        self.run_cli("put", str(self.source))
        self.run_cli("pin", "later", "sha256:" + "cd" * 32)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("sfblob put <the file> --pin", out)

    def test_verify_subset_partitions_the_objects_by_fan_out_folder(self):
        for n in range(8):
            (self.work / f"f{n}.bin").write_bytes(os.urandom(500 + n))
            self.run_cli("put", str(self.work / f"f{n}.bin"))
        seen = []
        for part in (1, 2, 3):
            code, out, _ = self.run_cli("verify", "--subset", f"{part}/3", "--json")
            self.assertEqual(code, 0)
            seen.append({row["ref"] for row in __import__("json").loads(out)["objects"]})
        self.assertEqual(len(set().union(*seen)), 8)          # together: every object
        self.assertEqual(sum(len(s) for s in seen), 8)          # and no object twice
        for bad in ("0/3", "4/3", "x", "1/300"):
            self.assertEqual(self.run_cli("verify", "--subset", bad)[0], 2, bad)

    def test_pin_from_record_pins_every_object_the_record_names(self):
        folder = self.work / "tree"
        folder.mkdir()
        for n in range(3):
            (folder / f"f{n}.bin").write_bytes(os.urandom(400 + n))
        record = self.work / "blobs.txt"
        self.assertEqual(self.run_cli("put", "-r", str(folder), "--record", str(record))[0], 0)
        self.assertEqual(self.run_cli("pin", "tree", "--from-record", str(record))[0], 0)
        self.assertIn("tree  3 object(s)", self.run_cli("pin", "ls")[1])

    def test_a_dropped_connection_exits_3_and_a_fact_about_the_store_exits_1(self):
        from git_remote_seafile.client import SeafileConnectionError
        self.run_cli("put", str(self.source))
        with patch.object(SeafileClient, "list_dir", side_effect=SeafileConnectionError("timed out")):
            code, _, err = self.run_cli("ls")
        self.assertEqual(code, 3)
        self.assertIn("run it again", err)
        self.assertEqual(self.run_cli("exists", "sha256:" + "ef" * 32)[0], 1)

    def test_json_failures_are_one_document_with_a_code_and_whether_to_retry(self):
        import json
        from git_remote_seafile.client import SeafileConnectionError
        self.run_cli("put", str(self.source))
        with patch.object(SeafileClient, "list_dir", side_effect=SeafileConnectionError("timed out")):
            code, out, err = self.run_cli("ls", "--json")
        error = json.loads(out)["error"]
        self.assertEqual((code, error["code"], error["retryable"], error["exit"]), (3, "network", True, 3))
        self.assertIn("run it again", err)          # the text is still on standard error
        code, out, _ = self.run_cli("stat", "sha256:" + "ab" * 32, "--json")
        error = json.loads(out)["error"]
        self.assertEqual((code, error["code"], error["retryable"]), (1, "store", False))
        code, out, _ = self.run_cli("verify", "--subset", "9/3", "--json")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "usage"))
        self.assertEqual(json.loads(out)["ok"], False)

    def test_without_json_a_failure_writes_nothing_to_standard_output(self):
        code, out, _ = self.run_cli("stat", "sha256:" + "ab" * 32)
        self.assertEqual((code, out), (1, ""))

    def test_a_connection_error_that_escapes_a_command_exits_3_too(self):
        from git_remote_seafile.client import SeafileConnectionError
        with patch.object(cli, "open_store", side_effect=SeafileConnectionError("refused")):
            self.assertEqual(self.run_cli("ls")[0], 3)


class SmallFixTests(unittest.TestCase):
    def test_a_token_with_a_line_feed_is_never_handed_to_git(self):
        with patch.object(gitcred, "_run", side_effect=AssertionError("must not run")):
            self.assertFalse(gitcred.approve("https://seafile.example.org", "seafile-token", "tok\nhost=evil.example"))

    def test_a_drive_letter_behind_dot_slash_is_refused(self):
        values, problems = restore_only.parse(["./C:/evil"])
        self.assertEqual(values, [])
        self.assertEqual(len(problems), 1)

    def test_the_snapshot_tool_answers_version(self):
        from git_remote_seafile.snapshot import cli as snapshot_cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(snapshot_cli.main(["--version"]), 0)
        self.assertIn("git-remote-seafile", out.getvalue())

    def test_the_hidden_history_warning_says_what_the_caller_calls_it(self):
        client = SeafileClient(server_url="http://127.0.0.1:1", token="t")
        with patch.object(client, "_json_or_none", return_value=None):
            default = client.hidden_history_risk("rid")
            named = client.hidden_history_risk("rid", noun="pins")
        self.assertIn("refs", default or "")
        self.assertIn("pins", named or "")
        self.assertNotIn("refs", named or "")

    def test_git_is_asked_for_a_credential_once_when_the_config_asks_for_it(self):
        client = SeafileClient(server_url="http://127.0.0.1:1", token="t")
        asked = []

        def from_config(path, require):
            client._config_wants_git = True
            return False, None, None

        def from_git(require):
            asked.append(1)
            return False

        with patch.object(client, "_credentials_from_env", return_value=False), \
                patch.object(client, "_credentials_from_config_file", side_effect=from_config), \
                patch.object(client, "_credentials_from_accounts_db", return_value=False), \
                patch.object(client, "_credentials_from_git", side_effect=from_git):
            with contextlib.suppress(Exception):
                client._load_credentials(require_token=False)
        self.assertEqual(len(asked), 1)


class RoundSevenTests(StoreCase):
    """Round 7 (Opus): the read-back that failed, the marker's feature lists, one folder's case, a BOM, a truncated pin."""

    def test_a_read_that_failed_proves_nothing_and_deletes_nothing(self):
        from git_remote_seafile.client import SeafileAPIError
        self.store.verify_by_download = True
        with patch.object(self.store.client, "download_file_to", side_effect=SeafileAPIError("timed out")):
            with self.assertRaises(store.StoreError) as caught:
                self.store.put(self.source)
        self.assertIn("verify --deep", str(caught.exception))
        self.assertIn(f"{ROOT}/blobs/{SHA[:2]}/{SHA}", self.files())          # possibly another writer's verified object

    def _marker(self, **extra):
        import json
        self.store.put(self.source)
        key = f"{ROOT}/.seafile-store"
        data = json.loads(self.files()[key])
        data.update(extra)
        self.stub.files[self.rid][key] = json.dumps(data).encode()
        return self.make()

    def test_an_unknown_ro_compat_feature_lets_a_store_be_read_but_not_written(self):
        other = self._marker(ro_compat=["tombstones"])
        self.assertTrue(other.exists(self.ref()))
        with self.assertRaises(store.StoreError):
            other.put(self.source)          # even an object already there: a put is a write
        with self.assertRaises(store.StoreError) as caught:
            other.put(self.source, pin="keep")
        self.assertIn("not write", str(caught.exception))
        with self.assertRaises(store.StoreError):
            pins.remove(other, "keep")
        self.assertEqual(self.files().get(f"{ROOT}/pins"), None)

    def test_an_unknown_incompat_feature_refuses_everything(self):
        other = self._marker(incompat=["newlayout"])
        with self.assertRaises(store.StoreError) as caught:
            other.exists(self.ref())
        self.assertIn("newer sfblob", str(caught.exception))

    def test_a_feature_value_that_is_not_a_list_of_names_refuses_the_store(self):
        for bad in ("packs", {"packs": 1}, 5, [1, 2], None):
            other = self._marker(incompat=bad)
            with self.assertRaises(store.StoreError, msg=repr(bad)) as caught:
                other.exists(self.ref())
            self.assertIn("cannot read", str(caught.exception))

    def test_a_dry_run_into_a_read_only_store_is_refused(self):
        other = self._marker(ro_compat=["tombstones"])
        with self.assertRaises(store.StoreError):
            other.would_put(self.source)

    def test_an_unknown_compat_feature_is_ignored(self):
        other = self._marker(compat=["hint"])
        self.assertTrue(other.put(self.source).ref.hex == SHA)

    def test_the_pins_api_on_a_fresh_store_object_still_honours_the_marker(self):
        """``pins.write`` and ``pins.remove`` read the marker themselves (review R8 Gemini P5): no earlier call primes them."""
        self._marker(ro_compat=["tombstones"])
        for call in (lambda s: pins.write(s, "bypass", [self.ref()]), lambda s: pins.remove(s, "bypass")):
            with self.assertRaises(store.StoreError):
                call(self.make())
        self._marker(incompat=["newlayout"])
        with self.assertRaises(store.StoreError):
            pins.write(self.make(), "bypass", [self.ref()])

    def test_a_marker_that_cannot_be_read_says_how_to_start_afresh(self):
        self.store.put(self.source)
        self.stub.files[self.rid][f"{ROOT}/.seafile-store"] = b"not json at all"
        with self.assertRaises(store.StoreError) as caught:
            self.make().exists(self.ref())
        self.assertIn("remove", str(caught.exception))

    def test_a_pin_body_shorter_than_the_listing_says_is_refused(self):
        self.store.put(self.source, pin="keep")
        real = self.store.client.get_file_bytes
        with patch.object(self.store.client, "get_file_bytes", lambda *a, **k: (real(*a, **k) or b"")[:-1]):
            with self.assertRaises(store.StoreError) as caught:
                pins.read_all(self.store)
        self.assertIn("listing says", str(caught.exception))

    def test_the_report_keeps_a_newer_versions_entries_apart_from_foreign_ones(self):
        self.store.put(self.source)
        files = self.stub.files[self.rid]
        files[f"{ROOT}/gc-pending.json"] = b"{}"
        files[f"{ROOT}/gc/run1/x"] = b"x"
        files[f"{ROOT}/blobs/{SHA[:2]}/{SHA}.gc-ab12"] = b"x"
        report = gcreport.scan(self.store)
        self.assertEqual(sorted(report.newer), sorted(["gc-pending.json", "gc", f"blobs/{SHA[:2]}/{SHA}.gc-ab12"]))
        self.assertEqual(report.foreign, [])


class RecordRoundSevenTests(unittest.TestCase):
    def test_a_path_cannot_start_with_a_byte_order_mark(self):
        with self.assertRaises(record.RecordError):
            record.normalise_path("\ufeffdata.bin")

    def test_a_line_with_more_fields_says_a_newer_version_may_have_written_it(self):
        with self.assertRaises(record.RecordError) as caught:
            record.parse("a.bin\t5\tsha256:" + "ab" * 32 + "\textra\n")
        self.assertIn("newer sfblob", str(caught.exception))


class CaseSpellingTests(TreeCase):
    @unittest.skipUnless(os.name == "nt", "needs a volume that finds a file under another case")
    def test_a_file_found_under_another_spelling_is_not_in_place(self):
        data = b"same bytes"
        (self.work / "A.txt").write_bytes(data)
        import hashlib
        from git_remote_seafile.blob import refs
        entry = record.Entry("a.txt", len(data), refs.full("sha256:" + hashlib.sha256(data).hexdigest()))
        self.assertFalse(tree._in_place(self.work / "a.txt", entry))
        entry = record.Entry("A.txt", len(data), entry.ref)
        self.assertTrue(tree._in_place(self.work / "A.txt", entry))


class DryRunRefusalTests(StoreCase):
    """A dry run refuses what the real run refuses (Gemini, DeepSeek and Qwen found three holes in the first fix)."""

    def test_under_a_vault_two_levels_up(self):
        self.stub.files.setdefault(self.rid, {})["/vault/refs/heads/main"] = b"x"
        with self.assertRaises(store.StoreError):
            self.make("/vault/store").would_put(self.source)

    def test_under_the_view_folder_of_a_vault(self):
        self.stub.files.setdefault(self.rid, {})["/vault/refs/heads/main"] = b"x"
        with self.assertRaises(store.StoreError):
            self.make("/vault-view/store").would_put(self.source)

    def test_in_a_folder_that_is_not_empty_and_not_a_store(self):
        self.stub.files.setdefault(self.rid, {})[f"{ROOT}/somebody-elses.txt"] = b"x"
        with self.assertRaises(store.StoreError):
            self.store.would_put(self.source)

    def test_inside_another_store(self):
        self.store.put(self.source)
        with self.assertRaises(store.StoreError):
            self.make(f"{ROOT}/inner").would_put(self.source)

    def test_an_empty_new_folder_is_fine_and_nothing_is_written(self):
        self.assertTrue(self.store.would_put(self.source).uploaded)
        self.assertEqual(self.files(), {})


class TreeRoundSevenTests(TreeCase):
    def _record(self, lines):
        base = self.make()
        entries = []
        for path, data in lines:
            temp = self.work / "obj.bin"
            temp.write_bytes(data)
            done = base.put(temp)
            entries.append(record.Entry(path, done.size, done.ref))
        target = self.work / "case.txt"
        target.write_text(record.render(entries), encoding="utf-8", newline="\n")
        return target

    def test_a_dry_run_writes_no_probe_file(self):
        rec = self._record([("A.txt", b"one"), ("a.txt", b"two")])
        out = self.work / "out"
        out.mkdir()
        with patch.object(pathlib.Path, "write_bytes", side_effect=AssertionError("wrote in a dry run")):
            tree.get_tree(self.make("c"), rec, out, dry_run=True)
        self.assertEqual(list(out.iterdir()), [])

    def test_a_probe_that_cannot_be_made_counts_the_names_as_colliding(self):
        rec = self._record([("A.txt", b"one"), ("a.txt", b"two")])
        out = self.work / "out"
        with patch.object(pathlib.Path, "write_bytes", side_effect=OSError("read-only")):
            report = tree.get_tree(self.make("c"), rec, out)
        self.assertEqual(report.restored, 0)
        self.assertEqual(len(report.problems), 2)

    def test_a_file_and_a_folder_of_the_same_name_in_one_record_are_refused_everywhere(self):
        rec = self._record([("foo", b"a file"), ("foo/bar", b"in a folder")])
        report = tree.get_tree(self.make("c"), rec, self.work / "out")
        self.assertEqual(report.restored, 1)          # foo/bar cannot be told apart from the clash: the file is refused
        self.assertTrue(any(p.startswith("foo:") and "folder" in p for p in report.problems))

    def test_a_folder_and_a_file_that_differ_only_in_case_are_refused_where_case_folds(self):
        rec = self._record([("Dir/child.txt", b"one"), ("dir", b"two")])
        with patch.object(tree, "_folds_case", return_value=True):
            report = tree.get_tree(self.make("c"), rec, self.work / "out")
        self.assertEqual(report.restored, 0)
        self.assertTrue(any("only in case" in p for p in report.problems))

    def test_get_to_stages_beside_the_destination_and_places_without_replacing(self):
        base = self.make()
        temp = self.work / "obj.bin"
        temp.write_bytes(b"payload bytes")
        done = base.put(temp)
        out = self.work / "got.bin"
        names = []
        real = store.place_new

        def spy(stage, target):
            names.append(stage.name)
            return real(stage, target)

        with patch.object(store, "place_new", spy):
            base.get_to(done.ref, out)
        self.assertEqual(out.read_bytes(), b"payload bytes")
        self.assertTrue(names and names[0].startswith(".sfblob-part-"))
        self.assertEqual([p.name for p in self.work.iterdir() if p.name.startswith(".sfblob-part-")], [])

    def test_get_to_that_loses_a_race_for_the_name_keeps_the_other_file(self):
        base = self.make()
        temp = self.work / "obj.bin"
        temp.write_bytes(b"payload bytes")
        done = base.put(temp)
        out = self.work / "got.bin"
        with patch.object(store, "place_new", lambda stage, target: (target.write_bytes(b"theirs"), False)[1]):
            with self.assertRaises(store.StoreError):
                base.get_to(done.ref, out)
        self.assertEqual(out.read_bytes(), b"theirs")


class ReportAndPinRoundSevenTests(StoreCase):
    def test_the_markers_copy_a_pin_comment_and_a_wrong_fanout_name(self):
        self.store.put(self.source, pin="keep")
        files = self.stub.files[self.rid]
        files[f"{ROOT}/ (1).seafile-store"] = b"{}"                         # the copy's name as the server makes it
        files[f"{ROOT}/blobs/zz/note.txt"] = b"x"                          # a folder that is not a fan-out name
        wrong = "ab" + SHA[2:]
        files[f"{ROOT}/blobs/cd/{wrong}.part-deadbeef"] = b"x"             # a temporary in the wrong fan-out folder
        report = gcreport.scan(self.store)
        self.assertNotIn(" (1).seafile-store", report.foreign)
        self.assertIn("blobs/zz", report.foreign)
        self.assertIn(f"blobs/cd/{wrong}.part-deadbeef", report.foreign)
        self.assertEqual(report.temporaries, [])

    def test_a_pin_file_may_carry_comment_lines_and_u2028_is_not_a_line_break(self):
        ref = "sha256:" + "ab" * 32
        text = "# pinned by a later version\n" + ref + chr(0x2028) + ref + "\n"
        with self.assertRaises(store.StoreError):          # U+2028 stays in the line, so these are not two references
            pins.parse(text, "where")
        self.assertEqual([str(r) for r in pins.parse("# a comment\n" + ref + "\n", "where")], [ref])

    def test_gc_lists_the_unused_objects_and_a_missing_pin_target_is_exit_1(self):
        # exercised through the command line in test_blob_cli; here the report's own lists
        self.store.put(self.source)
        report = gcreport.scan(self.store)
        self.assertEqual(report.unused, [SHA])


class LandingNameTests(unittest.TestCase):
    """The name a 301's Location gives is what callers delete by: it must be a plain name (review R8 DeepSeek P2, Gemini P10)."""

    def _answer(self, location: str):
        class Resp:
            status_code = 301
            headers = {"Location": location}
            text = '"success"'

        client = SeafileClient(server_url="http://127.0.0.1:1", token="t")
        client._send_waiting_out_a_throttle = lambda call: Resp()          # type: ignore[method-assign]
        return client.rename_entry_result("r", "/d/a", "b")

    def test_a_plus_in_a_name_stays_a_plus(self):
        self.assertEqual(self._answer("http://h/api2/repos/r/file/?p=/d/a+b.txt"), (200, "a+b.txt"))
        self.assertEqual(self._answer("http://h/api2/repos/r/file/?p=/d/a%2Bb%20c.txt"), (200, "a+b c.txt"))

    def test_a_landing_that_is_not_a_plain_name_is_refused(self):
        for bad in ("/blobs/ab/..", "/blobs/ab/.", "/", "", "/d/a%2Fb"):
            self.assertEqual(self._answer(f"http://h/api2/repos/r/file/?p={bad}"), (301, None), bad)


class RenameRedirectTests(unittest.TestCase):
    def _client(self, response):
        client = SeafileClient(server_url="http://127.0.0.1:1", token="t")
        client._send_waiting_out_a_throttle = lambda call: response          # type: ignore[method-assign]
        return client

    def test_a_301_names_the_landing_in_its_location_and_is_not_followed(self):
        seen = {}

        class Resp:
            status_code = 301
            headers = {"Location": "http://host/api2/repos/r/file/?p=/dir/name%20(1).bin"}
            text = '"success"'

        client = SeafileClient(server_url="http://127.0.0.1:1", token="t")

        def fake_send(call):
            class Session:
                def post(self_, url, **kwargs):
                    seen.update(kwargs)
                    return Resp()
            client.session = Session()          # type: ignore[assignment]
            return call()

        client._send_waiting_out_a_throttle = fake_send          # type: ignore[method-assign]
        self.assertEqual(client.rename_entry_result("r", "/dir/tmp.bin", "name.bin"), (200, "name (1).bin"))
        self.assertIs(seen.get("allow_redirects"), False)

    def test_a_301_with_no_usable_location_is_not_a_success(self):
        class Resp:
            status_code = 301
            headers: "dict[str, str]" = {}
            text = ""

        self.assertEqual(self._client(Resp()).rename_entry_result("r", "/d/a", "b"), (301, None))


class RestoreCostTests(TreeCase):
    def test_a_restore_copies_each_file_once_and_lists_each_folder_once(self):
        tree.put_tree(self.make(), self.src, self.work / "blobs.txt")
        copies, listings = [], []
        real_copy, real_listdir = store.shutil.copyfileobj, os.listdir

        def counting_copy(src, dst, length=0):
            copies.append(1)
            return real_copy(src, dst, length)

        def counting_listdir(path="."):
            listings.append(str(path))
            return real_listdir(path)

        out = self.work / "out"
        with patch.object(store.shutil, "copyfileobj", counting_copy):
            first = tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        self.assertEqual(first.restored, 4)
        self.assertEqual(len(copies), 4)          # cache to stage, once per file; the stage is linked, not copied again
        with patch.object(tree.os, "listdir", counting_listdir):
            again = tree.get_tree(self.make("c"), self.work / "blobs.txt", out)
        self.assertEqual(again.in_place, 4)
        mine = [p for p in listings if str(p).startswith(str(out))]          # other code lists folders too (name probes)
        self.assertLessEqual(len(mine), 3)          # three folders, listed once each (it was once per file)

    @unittest.skipUnless(os.name == "nt", "needs a volume that finds a folder under another case")
    def test_a_folder_on_the_way_spelled_otherwise_is_not_in_place(self):
        import hashlib

        from git_remote_seafile.blob import refs
        (self.work / "Sub").mkdir()
        (self.work / "Sub" / "file.txt").write_bytes(b"same bytes")
        entry = record.Entry("sub/file.txt", 10, refs.full("sha256:" + hashlib.sha256(b"same bytes").hexdigest()))
        self.assertFalse(tree._in_place(self.work / "sub" / "file.txt", entry, None, (self.work, "sub/file.txt")))
        entry = record.Entry("Sub/file.txt", 10, entry.ref)
        self.assertTrue(tree._in_place(self.work / "Sub" / "file.txt", entry, None, (self.work, "Sub/file.txt")))


class StageFileTests(TreeCase):
    def test_a_stage_file_left_by_an_interrupted_get_is_not_uploaded(self):
        (self.src / ".sfblob-part-0123456789abcdef").write_bytes(b"half a file")
        report = tree.put_tree(self.make(), self.src, None)
        self.assertTrue(any("stage file" in s for s in report.skipped))
        self.assertNotIn(".sfblob-part-0123456789abcdef", [e.path for e in report.entries])


class GatesMappingTests(unittest.TestCase):
    def test_an_edit_of_a_blob_module_reaches_the_blob_tests(self):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
        try:
            import gates
        finally:
            sys.path.pop(0)
        for changed in ("git_remote_seafile/blob/cli.py", "git_remote_seafile/blob/refs.py", "git_remote_seafile/units.py",
                        "git_remote_seafile/url.py", "git_remote_seafile/snapshot/payload_upload.py"):
            reached = gates.suite_for_changes([changed])
            self.assertTrue(reached is None or {"test_blob_cli", "test_blob_store", "test_blob_tree"} <= set(reached), changed)


class CommitSeenTests(unittest.TestCase):
    def test_a_folder_that_cannot_be_listed_stops_the_age_rule(self):
        from git_remote_seafile.snapshot import common, payload_guards

        class Broken:
            def list_dir(self, rid, path):
                raise ConnectionError("down")

        with self.assertRaises(common.SnapshotError):
            payload_guards.read_commit_seen(Broken(), "rid", "/base", 1_000_000.0)


if __name__ == "__main__":
    tempfile.gettempdir()
    unittest.main()
