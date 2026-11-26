"""The blob store's command line against the stub (STORE.md D3, CLI.md): output, JSON, exit codes, no prompts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import SeafileStub  # noqa: E402

from git_remote_seafile.blob import cli  # noqa: E402

DATA = os.urandom(4000)
SHA = hashlib.sha256(DATA).hexdigest()


class CliCase(unittest.TestCase):
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
        self.url = f"seafile://{self.stub.base_url}/testlib/cli-store"
        env = {"SEAFILE_TOKEN": "t", "SEAFILE_SERVER": self.stub.base_url, "SFBLOB_CACHE": str(self.work / "cache")}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SFBLOB_STORE", None)
        self.source = self.work / "data.bin"
        self.source.write_bytes(DATA)
        self.raw = b""

    def run_cli(self, *args: str, stdin: "bytes | None" = None) -> "tuple[int, str, str]":
        out, err = io.StringIO(), io.StringIO()
        out.buffer = io.BytesIO()           # type: ignore[attr-defined]
        feed = None
        if stdin is not None:
            feed = io.TextIOWrapper(io.BytesIO(stdin))
        with redirect_stdout(out), redirect_stderr(err), patch.object(sys, "stdin", feed if feed else sys.stdin):
            code = cli.main(self.with_store(args))
        self.raw = out.buffer.getvalue()    # type: ignore[attr-defined]
        return code, out.getvalue(), err.getvalue()

    def with_store(self, args: "tuple[str, ...]") -> "list[str]":
        return [args[0], "--store", self.url, *args[1:]]


class CommandTests(CliCase):
    def test_put_prints_the_reference_size_and_path_and_says_gc_does_not_know_it(self):
        code, out, err = self.run_cli("put", str(self.source))
        self.assertEqual(code, 0)
        self.assertEqual(out, f"sha256:{SHA}  {len(DATA)}  {self.source}\n")        # standard output is the answer only
        self.assertIn("not pinned", err)
        self.assertIn("--pin NAME", err)

    def test_put_warns_when_the_files_are_larger_than_the_quota_left_but_still_uploads(self):
        self.stub.faults["quota"] = (1000, 1000 + len(DATA) - 1)          # one byte too little room
        code, out, err = self.run_cli("put", str(self.source))
        self.assertEqual(code, 0)                                         # a warning, never a refusal
        self.assertIn("quota left", err)
        self.assertIn(f"sha256:{SHA}", out)

    def test_put_of_a_folder_warns_with_the_total_of_the_files(self):
        folder = self.work / "tree"
        folder.mkdir()
        for name in ("a", "b"):
            (folder / name).write_bytes(DATA)
        self.stub.faults["quota"] = (0, len(DATA) + 5)                    # room for one file, not two
        _, _, err = self.run_cli("put", "-r", str(folder))
        self.assertIn("quota left", err)

    def test_put_says_nothing_about_quota_when_the_files_fit_or_the_server_names_no_quota(self):
        self.stub.faults["quota"] = (0, len(DATA) * 10)
        self.assertNotIn("quota", self.run_cli("put", str(self.source))[2])
        self.stub.faults.pop("quota")
        self.assertNotIn("quota", self.run_cli("put", str(self.source), "--pin", "again")[2])

    def test_put_dry_run_does_not_ask_about_quota(self):
        self.stub.faults["quota"] = (0, 1)
        self.assertNotIn("quota", self.run_cli("put", str(self.source), "--dry-run")[2])

    def test_put_of_a_folder_without_r_says_which_command_takes_a_folder(self):
        folder = self.work / "folder"
        folder.mkdir()
        code, out, err = self.run_cli("put", str(folder))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("put -r", err)
        self.assertNotIn("Permission", err)

    def test_the_shared_flags_work_before_the_command_too(self):
        self.run_cli("put", str(self.source))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--store", self.url, "--json", "stat", f"sha256:{SHA}"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["size"], len(DATA))

    def test_version_is_answered(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("git-remote-seafile", out.getvalue())

    def test_get_dash_o_names_the_file_and_never_overwrites(self):
        self.run_cli("put", str(self.source))
        target = self.work / "report.bin"
        code, out, _ = self.run_cli("get", f"sha256:{SHA}", "-o", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(target.read_bytes(), DATA)
        self.assertEqual(self.run_cli("get", f"sha256:{SHA}", "-o", str(target))[0], 1)
        self.assertEqual(self.run_cli("get", f"sha256:{SHA}", f"sha256:{SHA}", "-o", str(self.work / "x"))[0], 2)

    def test_a_short_reference_is_said_in_full_on_standard_error(self):
        self.run_cli("put", str(self.source))
        code, out, err = self.run_cli("stat", f"sha256:{SHA[:10]}")
        self.assertEqual(code, 0)
        self.assertIn(f"sha256:{SHA[:10]} is sha256:{SHA}", err)
        self.assertIn("scripts and records should keep the full reference", err)
        self.assertNotIn("Note:", out)

    def test_json_lists_a_failed_item_and_a_malformed_reference_is_a_usage_error(self):
        self.run_cli("put", str(self.source))
        missing = "sha256:" + "ab" * 32
        code, out, _ = self.run_cli("get", f"sha256:{SHA}", missing, "--json")
        self.assertEqual(code, 1)
        rows = json.loads(out)["objects"]
        self.assertEqual(len(rows), 2)
        self.assertIn("path", rows[0])
        self.assertIn("not in the store", rows[1]["error"])
        self.assertEqual(self.run_cli("get", "abc123")[0], 2)

    def test_a_folder_put_without_a_record_file_prints_its_references(self):
        folder = self.work / "folder"
        folder.mkdir()
        (folder / "a.bin").write_bytes(DATA)
        code, out, err = self.run_cli("put", "-r", str(folder))
        self.assertEqual(code, 0)
        self.assertEqual(out, f"sha256:{SHA}  {len(DATA)}  a.bin\n")
        self.assertIn("1 file(s)", err)

    def test_ls_and_usage_show_what_the_store_holds(self):
        self.run_cli("put", str(self.source))
        other = self.work / "other.bin"
        other.write_bytes(b"x" * 10)
        self.run_cli("put", str(other))
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, 0)
        self.assertIn(f"sha256:{SHA}  {len(DATA)}", out)
        self.assertEqual(len(out.splitlines()), 2)
        code, out, _ = self.run_cli("usage", "--json")
        data = json.loads(out)
        self.assertEqual((data["objects"], data["bytes"]), (2, len(DATA) + 10))
        self.assertIn("cli-store", data["store"])

    def test_the_put_log_finds_a_reference_by_the_files_name_and_clearing_the_cache_keeps_it(self):
        self.run_cli("put", str(self.source))
        code, out, _ = self.run_cli("log", "data")
        self.assertEqual(code, 0)
        self.assertIn(f"sha256:{SHA}  {len(DATA)}  {self.source}", out)
        self.assertEqual(self.run_cli("log", "*.BIN")[0], 0)          # case-insensitive, shell-style
        self.assertEqual(self.run_cli("cache", "--clear")[0], 0)
        self.assertEqual(self.run_cli("log", "data")[0], 0)
        self.assertEqual(self.run_cli("log", "no-such-name")[0], 1)

    def test_no_log_writes_nothing_and_a_dry_run_logs_nothing(self):
        self.run_cli("put", str(self.source), "--no-log")
        self.run_cli("put", str(self.source), "--dry-run")
        self.assertEqual(self.run_cli("log")[0], 1)

    def test_a_folder_put_is_logged_with_each_files_path(self):
        folder = self.work / "folder"
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "a.bin").write_bytes(DATA)
        self.run_cli("put", "-r", str(folder), "--record", str(self.work / "r.txt"))
        code, out, _ = self.run_cli("log", "a.bin")
        self.assertEqual(code, 0)
        self.assertIn(os.path.join(str(folder), "sub", "a.bin"), out)

    def test_a_dry_run_folder_put_lists_every_file_and_what_it_would_do(self):
        folder = self.work / "folder"
        folder.mkdir()
        (folder / "new.bin").write_bytes(b"n" * 7)
        (folder / "old.bin").write_bytes(DATA)
        self.run_cli("put", str(folder / "old.bin"))
        code, out, _ = self.run_cli("put", "-r", str(folder), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("+ would upload  7  new.bin", out)
        self.assertIn(f"= already there  {len(DATA)}  old.bin", out)

    def test_a_cache_bound_is_kept_for_the_next_command(self):
        self.run_cli("put", str(self.source))
        self.run_cli("get", f"sha256:{SHA}")
        code, out, _ = self.run_cli("cache", "--size", "1G")
        self.assertIn("kept to 1.0 GiB", out)
        code, out, _ = self.run_cli("cache")
        self.assertIn("kept to 1.0 GiB", out)
        code, out, _ = self.run_cli("cache", "--size", "off")
        self.assertIn("no size bound", out)
        self.assertIn("no size bound", self.run_cli("cache")[1])

    def test_init_creates_the_store_validates_it_and_says_what_to_do_next(self):
        config = self.work / "gitconfig"          # never the developer's own ~/.gitconfig
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            code, out, _ = self.run_cli("init", self.url, "--global")
        self.assertEqual(code, 0)
        self.assertIn("store ready", out)
        self.assertIn("created", out)
        self.assertIn(self.url, config.read_text(encoding="utf-8"))
        self.assertIn(".seafile-store", " ".join(self.stub.files.get(self.stub_rid(), {})))
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            again = self.run_cli("init", self.url, "--global")[1]
        self.assertIn("it already was", again)

    def test_init_refuses_what_put_refuses(self):
        code, _, err = self.run_cli("init", f"seafile://{self.stub.base_url}/testlib")
        self.assertEqual(code, 2)
        self.assertIn("a store needs one", err)
        code, _, err = self.run_cli("init", f"seafile://{self.stub.base_url}/no-such-library/folder")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_a_skipped_path_is_a_warning_on_standard_error(self):
        folder = self.work / "folder"
        folder.mkdir()
        (folder / "a.bin").write_bytes(DATA)
        with patch.object(cli.tree, "_walk", return_value=([folder / "a.bin"], ["x.lnk: not a regular file"], [])):
            code, out, err = self.run_cli("put", "-r", str(folder))
        self.assertEqual(code, 0)
        self.assertIn("Warning: not uploaded (x.lnk: not a regular file)", err)
        self.assertNotIn("Warning", out)

    def test_init_says_when_git_config_could_not_save_the_store(self):
        import subprocess
        failed = subprocess.CompletedProcess([], 1, "", "boom")
        with patch.object(subprocess, "run", return_value=failed):
            code, out, err = self.run_cli("init", self.url, "--global")
        self.assertEqual(code, 0)
        self.assertIn("could not save the store with git config", err)

    def test_pin_ls_and_unpin_round_trip_and_put_pin_writes_the_pin_first(self):
        code, out, err = self.run_cli("put", str(self.source), "--pin", "keep")
        self.assertEqual(code, 0)
        self.assertNotIn("not pinned", err)          # a pinned put is protected: no warning (the real sentence, R8 DeepSeek P5)
        code, out, _ = self.run_cli("pin", "ls")
        self.assertIn("keep  1 object(s)", out)
        self.assertEqual(self.run_cli("unpin", "keep")[0], 0)
        self.assertEqual(self.run_cli("pin", "ls")[1], "")
        self.assertIn("there was no pin keep", self.run_cli("unpin", "keep")[1])

    def test_pin_names_full_references_and_says_when_an_object_is_absent(self):
        self.run_cli("put", str(self.source))
        self.assertEqual(self.run_cli("pin", "p1", f"sha256:{SHA}")[0], 0)
        self.assertEqual(self.run_cli("pin", "p2", f"sha256:{SHA[:10]}")[0], 2)
        code, _, err = self.run_cli("pin", "p3", "sha256:" + "ab" * 32)
        self.assertEqual(code, 1)
        self.assertIn("not in the store", err)
        self.assertEqual(self.run_cli("pin", "bad/name", f"sha256:{SHA}")[0], 1)
        listing = json.loads(self.run_cli("pin", "ls", "--json")[1])["pins"]
        self.assertEqual(sorted(listing), ["p1", "p3"])

    def test_pin_from_a_file_and_the_gc_report_counts_what_no_pin_names(self):
        other = self.work / "other.bin"
        other.write_bytes(b"y" * 10)
        self.run_cli("put", str(self.source))
        self.run_cli("put", str(other))
        listfile = self.work / "refs.txt"
        listfile.write_text(f"sha256:{SHA}\n", encoding="utf-8")
        self.assertEqual(self.run_cli("pin", "mine", "--from-file", str(listfile))[0], 0)
        code, out, _ = self.run_cli("gc", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data["objects"], data["unused"], data["removed"]), (2, 1, 0))
        self.assertEqual(data["pins"], {"mine": 1})
        before = dict(self.stub.files[self.stub_rid()])
        text = self.run_cli("gc")[1]
        self.assertIn("unused : 1 object(s)", text)
        self.assertIn("nothing changed", text)
        self.assertEqual(self.stub.files[self.stub_rid()], before)          # a report writes nothing

    def test_gc_apply_is_refused_in_this_version_and_gc_stops_on_a_pin_it_cannot_read(self):
        self.run_cli("put", str(self.source), "--pin", "keep")
        self.assertEqual(self.run_cli("gc", "--apply")[0], 2)
        for name in [n for n in self.stub.files[self.stub_rid()] if n.endswith(".refs")]:
            self.stub.files[self.stub_rid()][name] = b"not a reference\n"
        code, _, err = self.run_cli("gc")
        self.assertEqual(code, 1)
        self.assertIn("pins/keep/", err)
        self.assertIn("line 1", err)

    def test_gc_warns_about_a_pinned_object_that_is_not_in_the_store(self):
        self.run_cli("put", str(self.source))
        self.run_cli("pin", "later", "sha256:" + "cd" * 32)          # absent: the pin is written, the exit is 1
        code, out, err = self.run_cli("gc")
        self.assertEqual(code, 1)          # the store is not what its pins say: 0 means consistent
        self.assertIn("1 pinned object(s) are not in the store", err)          # a warning: stderr, stdout is the answer
        self.assertNotIn("Warning", out)

    def test_gc_json_names_the_missing_objects(self):
        self.run_cli("put", str(self.source))
        absent = "sha256:" + "cd" * 32
        self.run_cli("pin", "later", absent)
        code, out, err = self.run_cli("gc", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(__import__("json").loads(out)["missing_list"], [absent])
        self.assertIn(absent, err)

    def test_verify_says_present_missing_and_with_deep_reads_the_bytes(self):
        self.run_cli("put", str(self.source))
        code, out, _ = self.run_cli("verify")
        self.assertEqual(code, 0)
        self.assertIn("1 of 1 object(s) verified", out)
        self.assertIn("--deep", out)
        code, out, _ = self.run_cli("verify", "--deep")
        self.assertEqual(code, 0)
        self.assertIn("read and hashed", out)
        code, _, err = self.run_cli("verify", "sha256:" + "ab" * 32)
        self.assertEqual(code, 1)
        self.assertIn("missing", err)

    def test_verify_deep_catches_an_object_whose_bytes_are_not_its_name(self):
        self.run_cli("put", str(self.source))
        rid = self.stub_rid()
        name = next(n for n in self.stub.files[rid] if n.endswith(SHA))
        self.stub.files[rid][name] = b"z" * len(DATA)          # same size, other bytes
        self.assertEqual(self.run_cli("verify")[0], 0)          # present and the right size: the shallow check cannot tell
        code, _, err = self.run_cli("verify", "--deep")
        self.assertEqual(code, 1)
        self.assertIn(SHA, err)

    def test_verify_deep_reads_the_servers_copy_even_when_the_cache_holds_the_object(self):
        self.run_cli("put", str(self.source))
        self.assertEqual(self.run_cli("get", f"sha256:{SHA}", "-o", str(self.work / "out.bin"))[0], 0)          # now cached
        rid = self.stub_rid()
        name = next(n for n in self.stub.files[rid] if n.endswith(SHA))
        self.stub.files[rid][name] = b"z" * len(DATA)
        code, _, err = self.run_cli("verify", "--deep")
        self.assertEqual(code, 1)          # the cached copy is good; the server's is not, and the server's is what is judged
        self.assertIn(SHA, err)

    def test_verify_from_a_record_checks_each_entry_at_its_recorded_size(self):
        folder = self.work / "folder"
        folder.mkdir()
        (folder / "a.bin").write_bytes(DATA)
        record = self.work / "r.txt"
        self.run_cli("put", "-r", str(folder), "--record", str(record))
        self.assertEqual(self.run_cli("verify", "--from", str(record))[0], 0)
        record.write_text(record.read_text(encoding="utf-8").replace(f"\t{len(DATA)}\t", "\t1\t"), encoding="utf-8")
        code, _, err = self.run_cli("verify", "--from", str(record))
        self.assertEqual(code, 1)
        self.assertIn("expected 1", err)

    def test_a_bare_cache_command_reports_the_cache(self):
        code, out, _ = self.run_cli("cache")
        self.assertEqual(code, 0)
        self.assertIn("the cache holds 0 object(s)", out)

    def test_get_prints_a_cache_path_holding_the_bytes_and_a_short_reference_works(self):
        self.run_cli("put", str(self.source))
        for text in (f"sha256:{SHA}", f"sha256:{SHA[:8]}"):
            code, out, _ = self.run_cli("get", text)
            self.assertEqual(code, 0)
            self.assertEqual(pathlib.Path(out.strip()).read_bytes(), DATA)

    def test_path_cat_exists_stat(self):
        self.run_cli("put", str(self.source))
        code, out, _ = self.run_cli("path", f"sha256:{SHA}")
        self.assertEqual(pathlib.Path(out.strip()).read_bytes(), DATA)
        code, _, _ = self.run_cli("cat", f"sha256:{SHA[:10]}")
        self.assertEqual((code, self.raw), (0, DATA))
        code, out, _ = self.run_cli("stat", f"sha256:{SHA}")
        self.assertIn(f"{len(DATA)}", out)
        code, out, _ = self.run_cli("exists", f"sha256:{SHA}", f"sha256:{'0' * 64}")
        self.assertEqual(code, 1)
        self.assertIn("present", out)
        self.assertIn("missing", out)

    def test_json_is_one_document(self):
        code, out, _ = self.run_cli("put", "--json", str(self.source))
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["files"][0]["ref"], f"sha256:{SHA}")
        self.assertTrue(doc["files"][0]["uploaded"])
        code, out, _ = self.run_cli("exists", "--json", f"sha256:{SHA}")
        self.assertEqual(json.loads(out)["objects"], [{"exists": True, "ref": f"sha256:{SHA}"}])

    def test_put_reads_standard_input_with_a_dash(self):
        code, out, _ = self.run_cli("put", "-", stdin=DATA)
        self.assertEqual(code, 0)
        self.assertIn(f"sha256:{SHA}", out)

    def test_a_folder_round_trips_through_a_record_file(self):
        folder = self.work / "folder"
        (folder / "sub").mkdir(parents=True)
        (folder / "one.bin").write_bytes(DATA)
        (folder / "sub" / "two.bin").write_bytes(b"two")
        record = self.work / "blobs.txt"
        code, out, _ = self.run_cli("put", "-r", str(folder), "--record", str(record))
        self.assertEqual(code, 0)
        self.assertIn("2 file(s)", out)
        restored = self.work / "restored"
        code, out, _ = self.run_cli("get", "--from", str(record), "--to", str(restored))
        self.assertEqual(code, 0)
        self.assertEqual((restored / "one.bin").read_bytes(), DATA)
        self.assertEqual((restored / "sub" / "two.bin").read_bytes(), b"two")

    def test_get_to_a_folder_names_the_file_by_its_reference_and_never_overwrites(self):
        self.run_cli("put", str(self.source))
        out_dir = self.work / "out"
        self.assertEqual(self.run_cli("get", f"sha256:{SHA}", "--to", str(out_dir))[0], 0)
        self.assertEqual((out_dir / SHA).read_bytes(), DATA)
        code, _, err = self.run_cli("get", f"sha256:{SHA}", "--to", str(out_dir))
        self.assertEqual(code, 1)
        self.assertIn("does not overwrite", err)

    def test_a_dry_run_says_so_and_changes_nothing(self):
        folder = self.work / "folder"
        folder.mkdir()
        (folder / "one.bin").write_bytes(DATA)
        record = self.work / "blobs.txt"
        code, out, _ = self.run_cli("put", "-r", str(folder), "--record", str(record), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run, nothing changed", out)
        self.assertFalse(record.exists())
        self.assertEqual(self.stub.files.get(self.stub_rid(), {}), {})

    def test_exclude_leaves_paths_out_and_is_counted(self):
        folder = self.work / "folder"
        folder.mkdir()
        (folder / "keep.bin").write_bytes(DATA)
        (folder / "skip.tmp").write_bytes(b"x")
        code, out, err = self.run_cli("put", "-r", str(folder), "--exclude", "*.tmp")
        self.assertEqual(code, 0)
        self.assertIn("1 excluded", err)
        self.assertIn("1 file(s)", err)

    def test_dry_run_and_exclude_need_their_folder_forms(self):
        self.assertEqual(self.run_cli("put", str(self.source), "--exclude", "*.tmp")[0], 2)
        code, _, err = self.run_cli("get", f"sha256:{SHA}", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("need --from", err)

    def stub_rid(self) -> str:
        from e2e_harness import REPO_ID
        return REPO_ID

    def test_the_cache_command_evicts_and_clears(self):
        self.run_cli("put", str(self.source))
        self.run_cli("get", f"sha256:{SHA}")
        code, out, _ = self.run_cli("cache", "--clear")
        self.assertEqual(code, 0)
        self.assertIn("removed 1 object", out)


class FailureTests(CliCase):
    def test_no_store_is_a_usage_error_that_says_how_to_set_one(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch("git_remote_seafile.git_util.get_git_config", return_value=None):
            code = cli.main(["get", f"sha256:{SHA}"])
        self.assertEqual(code, 2)
        self.assertIn("SFBLOB_STORE", err.getvalue())

    def test_the_environment_names_the_store(self):
        self.run_cli("put", str(self.source))
        with patch.dict(os.environ, {"SFBLOB_STORE": self.url}):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["exists", f"sha256:{SHA}"])
        self.assertEqual(code, 0)

    def test_a_missing_object_is_exit_1_with_a_message_that_names_it(self):
        self.run_cli("put", str(self.source))
        code, _, err = self.run_cli("get", f"sha256:{'1' * 64}")
        self.assertEqual(code, 1)
        self.assertIn("is not in the store", err)

    def test_a_library_with_no_folder_is_refused_not_put_in_the_helpers_default_folder(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["put", "--store", f"seafile://{self.stub.base_url}/testlib", str(self.source)])
        self.assertEqual(code, 2)
        self.assertIn("a store needs one", err.getvalue())
        self.assertEqual(sum(len(v) for v in self.stub.files.values()), 0)

    def test_a_library_that_does_not_exist_is_a_message_not_a_traceback(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["put", "--store", f"seafile://{self.stub.base_url}/no-such-library/folder", str(self.source)])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertIn("seafile-blob:", err.getvalue())

    def test_a_bad_reference_is_named(self):
        code, _, err = self.run_cli("get", "not-a-reference")
        self.assertEqual(code, 2)          # a malformed reference is a usage error (CLI.md 2.3)
        self.assertIn("not a reference", err)

    def test_the_commands_of_a_later_version_say_so(self):
        for word in ("offload", "link", "read"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main([word])
            self.assertEqual(code, 1)          # not a mistake in the call, so not the usage code
            self.assertIn("not in this version yet", err.getvalue())

    def test_no_command_is_a_usage_error_and_nothing_ever_prompts(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main([]), 2)
        self.assertEqual(out.getvalue(), "")

    def test_record_without_a_folder_is_a_usage_error(self):
        code, _, err = self.run_cli("put", str(self.source), "--record", str(self.work / "r.txt"))
        self.assertEqual(code, 2)
        self.assertIn("--record needs -r", err)


if __name__ == "__main__":
    unittest.main()
