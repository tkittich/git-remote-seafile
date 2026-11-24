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
        code, out, _ = self.run_cli("put", str(self.source))
        self.assertEqual(code, 0)
        self.assertIn(f"sha256:{SHA}  {len(DATA)}  {self.source}", out)
        self.assertIn("not protected from `gc`", out)

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
        code, out, _ = self.run_cli("put", "-r", str(folder), "--exclude", "*.tmp")
        self.assertEqual(code, 0)
        self.assertIn("1 excluded", out)
        self.assertIn("1 file(s)", out)

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
        self.assertEqual(code, 1)
        self.assertIn("not a reference", err)

    def test_the_commands_of_a_later_version_say_so(self):
        for word in ("gc", "pin", "offload", "verify"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main([word])
            self.assertEqual(code, 2)
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
