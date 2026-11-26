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

from git_remote_seafile import gitcred, umbrella  # noqa: E402
from git_remote_seafile.blob import cache as cache_mod  # noqa: E402
from git_remote_seafile.blob import cli, gcreport, pins, record, store, tree  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402
from git_remote_seafile.snapshot import restore_only  # noqa: E402

POSIX = sys.platform != "win32"


class ReadBackProofTests(StoreCase):
    """D8: over http a put is proved by reading it back; a failed proof leaves nothing under the hash name."""

    def _lying_download(self, client: SeafileClient):
        def fake(repo_id, path, destination, **kwargs):
            pathlib.Path(destination).write_bytes(bytes(reversed(DATA)))          # the right size, other bytes
            return True
        return patch.object(client, "download_file_to", fake)

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

    def test_the_marker_must_read_back_as_written(self):
        with patch.object(self.store.client, "get_file_bytes", return_value=b"{}"):
            with self.assertRaises(store.StoreError) as caught:
                self.store.put(self.source)
        self.assertIn("did not read back", str(caught.exception))


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
        real = store.Store.get_to

        def spy(self_, ref, destination, progress=None):
            staged.append(destination.name)
            return real(self_, ref, destination, progress)

        with patch.object(store.Store, "get_to", spy):
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


class SmallFixTests(unittest.TestCase):
    def test_a_token_with_a_line_feed_is_never_handed_to_git(self):
        with patch.object(gitcred, "_run", side_effect=AssertionError("must not run")):
            self.assertFalse(gitcred.approve("https://seafile.example.org", "seafile-token", "tok\nhost=evil.example"))

    def test_a_drive_letter_behind_dot_slash_is_refused(self):
        values, problems = restore_only.parse(["./C:/evil"])
        self.assertEqual(values, [])
        self.assertEqual(len(problems), 1)

    def test_the_umbrella_keeps_the_callers_argv_and_answers_version(self):
        from git_remote_seafile import cli as helper_cli
        keep = list(sys.argv)
        word = sorted(set(helper_cli._HANDLERS) - {"snapshot"})[0]
        with patch.object(helper_cli, "main", return_value=0) as main:
            self.assertEqual(umbrella.main([word]), 0)
        main.assert_called_once()
        self.assertEqual(sys.argv, keep)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(umbrella.main(["--version"]), 0)
        self.assertIn("git-remote-seafile", out.getvalue())

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
