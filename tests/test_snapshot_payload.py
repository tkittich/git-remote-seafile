"""Payload mode of the snapshot tool, against the in-process Seafile stub.

Large files bypass git: they are uploaded to ``<path>.payload/current`` and
snapshotted by one server-side copy, and the vault commit carries a manifest.
These tests drive the real tool (``snap.main``) with the real helper behind a
PATH shim, so the vault push and the payload upload both hit the stub.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import unittest.mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT / "tests"))

import seafile_snapshot as snap  # noqa: E402
from e2e_harness import SeafileStub, make_helper_env  # noqa: E402

REPO_ID = "00000000-0000-0000-0000-0000000000e2"


def _git(cwd: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout


class PayloadModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = SeafileStub().start()
        cls._tmp = tempfile.TemporaryDirectory(prefix="grs-payload-")
        cls.work = pathlib.Path(cls._tmp.name)
        cls.env = make_helper_env(cls.work / "bin", cls.stub.base_url, _ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.stub.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.stub.reset()
        self.case = pathlib.Path(tempfile.mkdtemp(prefix="case-", dir=self.work))
        self.addCleanup(shutil.rmtree, self.case, ignore_errors=True)
        self.src = self.case / "src"
        self.vault = self.case / "vault"
        (self.src / "sub").mkdir(parents=True)
        (self.src / ".gitignore").write_bytes(b"*.bin\n")
        (self.src / "small.txt").write_bytes(b"small\n")
        self.big1 = os.urandom(5000)
        self.big2 = os.urandom(7000)
        (self.src / "big1.bin").write_bytes(self.big1)  # ignored by git, payload by size
        (self.src / "sub" / "big2.bin").write_bytes(self.big2)
        _git(self.src, "init", "-q", "-b", "main", ".")
        _git(self.src, "config", "user.email", "dev@example.com")
        _git(self.src, "config", "user.name", "Dev")
        _git(self.src, "add", ".gitignore", "small.txt")
        _git(self.src, "commit", "-qm", "init")
        self.remote = "seafile://testlib/pvault"
        self.payload_dir = "/pvault.payload"

    # -- helpers --------------------------------------------------------------
    def run_tool(self, *argv: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    def snapshot(self, *extra: object) -> tuple[int, str, str]:
        return self.run_tool(
            self.src, "--vault", self.vault, "--remote", self.remote,
            "--payload-min-size", "1000", *extra,
        )

    def server_files(self) -> dict[str, bytes]:
        return dict(self.stub.files.get(self.stub_repo(), {}))

    def stub_repo(self) -> str:
        # The stub serves one library; its id is whatever the listing returns.
        import e2e_harness
        return e2e_harness.REPO_ID

    def snapshot_dirs(self) -> list[str]:
        prefix = f"{self.payload_dir}/snapshots/"
        return sorted({k[len(prefix):].split("/")[0] for k in self.server_files() if k.startswith(prefix)})

    # -- tests ----------------------------------------------------------------
    def test_payload_bypasses_git_and_lands_on_the_server(self):
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload: 2 file(s)", out)

        files = self.server_files()
        self.assertEqual(files[f"{self.payload_dir}/current/big1.bin"], self.big1)
        self.assertEqual(files[f"{self.payload_dir}/current/sub/big2.bin"], self.big2)
        ids = self.snapshot_dirs()
        self.assertEqual(len(ids), 1)
        self.assertEqual(files[f"{self.payload_dir}/snapshots/{ids[0]}/big1.bin"], self.big1)

        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertIn("small.txt", tree)
        self.assertIn(".seafile-payload/manifest.json", tree)
        self.assertNotIn("big1.bin", tree)
        self.assertNotIn("big2.bin", tree)

        manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
        by_path = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(by_path["big1.bin"]["sha256"], hashlib.sha256(self.big1).hexdigest())
        self.assertEqual(by_path["sub/big2.bin"]["size"], 7000)
        self.assertEqual(manifest["snapshot"], ids[0])

    def test_restore_downloads_and_verifies_the_payload(self):
        self.assertEqual(self.snapshot()[0], 0)
        into = self.case / "restored"
        rc, out, err = self.run_tool("--restore", "--remote", self.remote, "--into", into)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload verified: 2 file(s)", out)
        self.assertEqual((into / "big1.bin").read_bytes(), self.big1)
        self.assertEqual((into / "sub" / "big2.bin").read_bytes(), self.big2)
        self.assertEqual((into / "small.txt").read_bytes(), b"small\n")

    def test_an_unchanged_tree_uploads_nothing_and_commits_nothing(self):
        self.assertEqual(self.snapshot()[0], 0)
        before = self.server_files()
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("no changes since the last snapshot", out)
        self.assertIn("uploaded 0 file(s)", out)
        self.assertEqual(self.server_files(), before)

    def test_a_same_size_edit_is_still_uploaded(self):
        self.assertEqual(self.snapshot()[0], 0)
        edited = bytes(b ^ 0xFF for b in self.big1)  # same length, different bytes
        (self.src / "big1.bin").write_bytes(edited)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.server_files()[f"{self.payload_dir}/current/big1.bin"], edited)
        self.assertEqual(len(self.snapshot_dirs()), 2)

    def test_an_older_snapshot_restores_the_older_bytes(self):
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "big1.bin").write_bytes(os.urandom(6000))
        self.assertEqual(self.snapshot()[0], 0)
        into = self.case / "old"
        rc, out, err = self.run_tool(
            "--restore", "--remote", self.remote, "--into", into, "--ref", "snapshot~1"
        )
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "big1.bin").read_bytes(), self.big1)

    def test_a_file_removed_from_the_source_leaves_the_mirror(self):
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "sub" / "big2.bin").unlink()
        self.assertEqual(self.snapshot()[0], 0)
        files = self.server_files()
        self.assertNotIn(f"{self.payload_dir}/current/sub/big2.bin", files)
        # ...but the earlier snapshot still holds it.
        self.assertTrue(any(k.endswith("/sub/big2.bin") and "/snapshots/" in k for k in files))

    def test_corrupt_payload_fails_the_restore(self):
        self.assertEqual(self.snapshot()[0], 0)
        sid = self.snapshot_dirs()[0]
        key = f"{self.payload_dir}/snapshots/{sid}/big1.bin"
        self.stub.files[self.stub_repo()][key] = bytes(len(self.big1))  # right size, wrong bytes
        rc, out, err = self.run_tool(
            "--restore", "--remote", self.remote, "--into", self.case / "bad"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("SHA-256 does not match the manifest", out + err)

    def test_a_vault_that_used_payload_refuses_a_run_without_the_flag(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--remote", self.remote)
        self.assertNotEqual(rc, 0)
        self.assertIn("payload mode", out + err)

    def test_no_push_cannot_be_combined_with_payload(self):
        rc, out, err = self.run_tool(
            self.src, "--vault", self.vault, "--payload-min-size", "1000", "--no-push"
        )
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--no-push", err)

    def test_dry_run_reads_the_server_and_changes_nothing(self):
        rc, out, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("to upload 2 file(s)", out)
        self.assertEqual(self.server_files(), {})

    def test_names_the_server_rewrites_still_round_trip(self):
        # The real server stores names NFC-normalised and turns '"' into %22;
        # the stub does the same when asked, so the tool must compare and fetch
        # by the name the server holds, not the name it sent.
        self.stub.faults["server_names"] = 1
        nfd = unicodedata.normalize("NFD", "caf\u00e9") + ".bin"
        content = os.urandom(3000)
        (self.src / nfd).write_bytes(content)
        quoted = None
        if os.name != "nt":
            quoted = 'say "hi".bin'
            (self.src / quoted).write_bytes(content[::-1])
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        held = {k.rsplit("/", 1)[-1] for k in self.server_files() if "/current/" in k}
        self.assertIn(unicodedata.normalize("NFC", nfd), held)
        # A second run recognises what is there: nothing to upload.
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("uploaded 0 file(s)", out)
        into = self.case / "names"
        rc, out, err = self.run_tool("--restore", "--remote", self.remote, "--into", into)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / nfd).read_bytes(), content)
        if quoted:
            self.assertEqual((into / quoted).read_bytes(), content[::-1])

    def test_a_name_over_255_bytes_is_refused_before_any_upload(self):
        long_name = "\u0e01" * 86 + ".bin"  # 86 x 3 bytes + 4 = 262 bytes of UTF-8
        (self.src / long_name).write_bytes(os.urandom(3000))
        rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("longer than 255 bytes", out + err)
        self.assertEqual(self.server_files(), {})

    def test_two_paths_that_become_one_server_name_are_refused(self):
        nfc = unicodedata.normalize("NFC", "caf\u00e9") + ".bin"
        nfd = unicodedata.normalize("NFD", "caf\u00e9") + ".bin"
        (self.src / nfc).write_bytes(os.urandom(2000))
        (self.src / nfd).write_bytes(os.urandom(2100))
        if len({p.name for p in self.src.iterdir() if p.suffix == ".bin"}) < 3:
            self.skipTest("this filesystem folds the two spellings into one file")
        rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("become the same name on the server", out + err)

    def test_many_files_upload_in_parallel_and_all_land(self):
        contents = {}
        for i in range(12):
            data = os.urandom(1500 + i * 100)
            (self.src / f"par{i:02d}.bin").write_bytes(data)
            contents[f"par{i:02d}.bin"] = data
        rc, out, err = self.snapshot("--payload-jobs", "4")
        self.assertEqual(rc, 0, out + err)
        files = self.server_files()
        for name, data in contents.items():
            self.assertEqual(files[f"{self.payload_dir}/current/{name}"], data)
        self.assertIn("uploaded 14 file(s)", out)

    def test_a_failed_parallel_upload_names_the_file_and_resumes(self):
        for i in range(4):
            (self.src / f"fail{i}.bin").write_bytes(os.urandom(2000))
        self.stub.faults["upload"] = 500
        rc, out, err = self.snapshot("--payload-jobs", "3")
        self.assertNotEqual(rc, 0)
        self.assertIn("payload upload(s) failed", out + err)
        self.assertIn("the next run resumes", out + err)
        del self.stub.faults["upload"]
        rc, out, err = self.snapshot("--payload-jobs", "3")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"{self.payload_dir}/current/fail0.bin", self.server_files())

    def test_parse_size(self):
        self.assertEqual(snap.parse_size("64M"), 64 * 1024 ** 2)
        self.assertEqual(snap.parse_size("1g"), 1024 ** 3)
        self.assertEqual(snap.parse_size("500k"), 500 * 1024)
        self.assertEqual(snap.parse_size("1048576"), 1048576)
        with self.assertRaises(snap.SnapshotError):
            snap.parse_size("lots")


if __name__ == "__main__":
    unittest.main()
