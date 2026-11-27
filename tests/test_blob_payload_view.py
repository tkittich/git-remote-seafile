"""A git vault's payload objects read through the blob commands, read-only (PLAN_0.12 D1a, D1b)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_blob_cli import CliCase  # noqa: E402

from git_remote_seafile.blob import cli, payload_view, record, refs, store  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402
from git_remote_seafile.snapshot import payload_upload  # noqa: E402

BIG = os.urandom(6000)
SMALL = b"a small payload file"
VAULT = "/vault"


def obj(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PayloadFixture(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = f"seafile://{self.stub.base_url}/testlib/vault"
        self.rid = SeafileClient(server_url=self.stub.base_url, token="t").get_repo_id("testlib")
        files = self.stub.files.setdefault(self.rid, {})
        for data in (BIG, SMALL):
            files[f"{VAULT}/payload/objects/{obj(data)[:2]}/{obj(data)}"] = data
        files[f"{VAULT}/refs/heads/snapshot"] = b"x" * 41
        self.before = dict(files)

    def view(self, root: str = VAULT) -> payload_view.PayloadView:
        client = SeafileClient(server_url=self.stub.base_url, token="t")
        return payload_view.PayloadView(client, self.rid, root, store.cache_mod.Cache(self.work / "vcache"))


class PayloadViewTests(PayloadFixture):
    def test_the_read_verbs_work_on_a_vaults_payload_objects(self):
        view = self.view()
        ref = refs.full(f"sha256:{obj(BIG)}")
        self.assertEqual(view.stat(ref), len(BIG))
        self.assertTrue(view.exists(refs.full(f"sha256:{obj(SMALL)}")))
        self.assertFalse(view.exists(refs.full("sha256:" + "00" * 32)))
        path, cached = view.get(ref)
        self.assertEqual((path.read_bytes(), cached), (BIG, False))
        out = io.BytesIO()
        view.cat(refs.full(f"sha256:{obj(SMALL)}"), out)
        self.assertEqual(out.getvalue(), SMALL)
        self.assertEqual(sorted(size for _, size in view.objects()), sorted([len(BIG), len(SMALL)]))
        self.assertEqual(view.resolve(refs.parse(f"sha256:{obj(BIG)[:10]}")), ref)

    def test_nothing_can_be_written_and_nothing_was(self):
        view = self.view()
        source = self.work / "x.bin"
        source.write_bytes(b"new bytes")
        for call in (lambda: view.put(source), lambda: view.put(source, pin="p"), lambda: view.would_put(source),
                     lambda: view.require_writable()):
            with self.assertRaises(store.StoreError) as caught:
                call()
            self.assertIn("read-only", str(caught.exception))
        self.assertEqual(self.stub.files[self.rid], self.before)

    def test_a_folder_with_no_payload_is_not_a_vault(self):
        self.stub.files[self.rid]["/plain/a.txt"] = b"x"
        with self.assertRaises(store.StoreError) as caught:
            self.view("/plain").stat(refs.full("sha256:" + "00" * 32))
        self.assertIn("no payload folder", str(caught.exception))

    def test_the_client_is_never_told_it_is_a_stores_layout(self):
        view = self.view()
        self.assertFalse(getattr(view.client, "store_layout", False))
        self.assertIsNone(getattr(view.client, "layout_root", None))


class PayloadVaultCommandTests(PayloadFixture):
    def test_the_read_commands_take_payload_vault(self):
        ref = f"sha256:{obj(BIG)}"
        out = self.work / "got.bin"
        self.assertEqual(self.run_cli("get", "-o", str(out), ref, "--payload-vault")[0], 0)
        self.assertEqual(out.read_bytes(), BIG)
        self.assertEqual(self.run_cli("exists", ref, "--payload-vault")[0], 0)
        self.assertEqual(self.run_cli("verify", "--deep", "--payload-vault")[0], 0)
        code, text, _ = self.run_cli("ls", "--payload-vault")
        self.assertEqual(code, 0)
        self.assertIn(ref, text)
        self.assertEqual(self.stub.files[self.rid], self.before)

    def test_the_commands_that_write_are_refused_with_a_usage_error(self):
        for command in ("put", "pin", "unpin", "gc", "init", "status"):
            args = {"put": [command, str(self.source)], "pin": [command, "n", "sha256:" + "00" * 32],
                    "unpin": [command, "n"]}.get(command, [command])
            code, _, err = self.run_cli(*args, "--payload-vault")
            self.assertEqual(code, 2, command)
            self.assertIn("blob store", err)
        self.assertEqual(self.stub.files[self.rid], self.before)

    def test_the_refusal_of_a_write_command_is_a_json_failure_too(self):
        # Round 9 (Gemini P8a): these two early exits printed nothing on standard output with --json
        code, text, _ = self.run_cli("put", str(self.source), "--payload-vault", "--json")
        self.assertEqual(code, 2)
        document = json.loads(text)
        self.assertEqual((document["ok"], document["error"]["code"], document["error"]["exit"]), (False, "usage", 2))

    def test_without_the_flag_a_vault_is_never_adopted_as_a_store(self):
        code, _, err = self.run_cli("put", str(self.source))
        self.assertEqual(code, 1)
        self.assertIn("vault", err)


class PayloadRecordTests(PayloadFixture):
    manifest = {"files": [{"path": "data/big.bin", "size": len(BIG), "mode": "100644", "sha256": obj(BIG), "mtime_ns": 1},
                          {"path": "small.txt", "size": len(SMALL), "mode": "100644", "sha256": obj(SMALL), "mtime_ns": 2}]}

    def test_a_manifest_becomes_record_entries(self):
        entries = payload_view.records_from_manifest(self.manifest)
        self.assertEqual([(e.path, e.size, e.ref.hex) for e in entries],
                         [("data/big.bin", len(BIG), obj(BIG)), ("small.txt", len(SMALL), obj(SMALL))])

    def test_payload_record_writes_the_record_says_what_it_leaves_out_and_get_from_restores_it(self):
        recfile = self.work / "vault-record.txt"
        with patch.object(cli, "_manifest_of_vault", return_value=(self.manifest, 5)):
            code, text, err = self.run_cli("payload-record", "-o", str(recfile), "--payload-vault")
        self.assertEqual(code, 0)
        self.assertIn("2 payload file(s)", text)
        self.assertIn("5 other file(s)", err)          # the vault's small git files, modes and times are not in a record
        self.assertEqual(len(record.parse(recfile.read_text(encoding="utf-8"))), 2)
        out = self.work / "restored"
        code, _, err = self.run_cli("get", "--from", str(recfile), "--to", str(out), "--payload-vault")
        self.assertEqual(code, 0, err)
        self.assertEqual((out / "data" / "big.bin").read_bytes(), BIG)
        self.assertEqual((out / "small.txt").read_bytes(), SMALL)
        with patch.object(cli, "_manifest_of_vault", return_value=(self.manifest, 0)):
            self.assertEqual(self.run_cli("payload-record", "-o", str(recfile), "--payload-vault")[0], 2)          # never overwrites

    def test_the_manifest_is_read_from_a_real_git_vault_and_the_other_files_are_counted(self):
        import subprocess
        repo = self.work / "vault-src"
        (repo / ".seafile-payload").mkdir(parents=True)
        (repo / ".seafile-payload" / "manifest.json").write_text(json.dumps({**self.manifest, "format": payload_upload.PAYLOAD_FORMAT}), encoding="utf-8")
        (repo / "notes.txt").write_text("a small file git keeps", encoding="utf-8")
        for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "snapshot"]):
            subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
        manifest, other = cli._manifest_of_vault(str(repo), None)
        self.assertEqual([f["path"] for f in manifest["files"]], ["data/big.bin", "small.txt"])
        self.assertEqual(other, 1)          # notes.txt is in git, not in the payload

    def test_a_snapshot_with_no_manifest_says_so(self):
        import subprocess
        repo = self.work / "no-manifest"
        repo.mkdir()
        (repo / "a.txt").write_text("x", encoding="utf-8")
        for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "s"]):
            subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
        with self.assertRaises(store.StoreError) as caught:
            cli._manifest_of_vault(str(repo), None)
        self.assertIn("no payload manifest", str(caught.exception))

    def test_payload_record_needs_the_vault_flag(self):
        self.assertEqual(self.run_cli("payload-record", "-o", str(self.work / "r.txt"))[0], 2)

    def test_a_file_name_a_record_cannot_hold_is_said_not_a_traceback(self):
        # Round 9 (DeepSeek P3): a tab is legal in a file name and a record line is tab-separated; this raised RecordError out of main
        weird = {"files": [{"path": "a" + chr(9) + "b.txt", "size": len(SMALL), "mode": "100644", "sha256": obj(SMALL), "mtime_ns": 1}]}
        recfile = self.work / "weird.txt"
        with patch.object(cli, "_manifest_of_vault", return_value=(weird, 0)):
            code, _, err = self.run_cli("payload-record", "-o", str(recfile), "--payload-vault")
        self.assertEqual(code, 1)
        self.assertIn("cannot name", err)
        self.assertFalse(recfile.exists())

    def test_a_store_address_that_is_not_seafile_never_reaches_git(self):
        # Round 9 (Gemini P5, DeepSeek P12): ``--store=--upload-pack=...`` was handed to ``git clone`` as an option
        with patch("subprocess.run", side_effect=AssertionError("git must not be started")):
            code, _, err = self.run_cli("payload-record", "-o", str(self.work / "r.txt"), "--payload-vault",
                                        "--store=--upload-pack=evil")
        self.assertEqual(code, 2)
        self.assertIn("not a seafile:// address", err)

    def test_git_is_given_the_address_after_a_double_dash_and_not_the_callers_git_dir(self):
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"], seen["env"] = command, kwargs.get("env", {})
            raise store.StoreError("stop here")

        with patch.dict(os.environ, {"GIT_DIR": "elsewhere"}), patch("subprocess.run", fake_run):
            with self.assertRaises(store.StoreError):
                cli._manifest_of_vault("seafile://lib/vault", None)
        self.assertEqual(seen["command"][:5], ["git", "clone", "--bare", "--quiet", "--"])
        self.assertNotIn("GIT_DIR", seen["env"])

    def test_the_json_answer_names_the_files_and_the_other_files(self):
        recfile = self.work / "r.txt"
        with patch.object(cli, "_manifest_of_vault", return_value=(self.manifest, 7)):
            _, text, _ = self.run_cli("payload-record", "-o", str(recfile), "--payload-vault", "--json")
        data = json.loads(text)
        self.assertEqual((data["files"], data["other_files_in_git"], data["bytes"]), (2, 7, len(BIG) + len(SMALL)))


if __name__ == "__main__":
    unittest.main()
