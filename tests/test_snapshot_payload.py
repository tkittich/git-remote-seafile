"""Payload mode of the snapshot tool, against the in-process Seafile stub.

Large files bypass git: they are stored on the server by SHA-256 under
``<path>/payload/objects/<xx>/<sha256>`` and the vault commit carries a
manifest.  These tests drive the real tool (``snap.main``) with the real helper
behind a PATH shim, so the vault push and the payload upload both hit the stub.
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
from e2e_harness import REPO_ID, SeafileStub, make_helper_env  # noqa: E402


def _git(cwd: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
        self.objects = "/pvault/payload/objects"

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
        return dict(self.stub.files.get(REPO_ID, {}))

    def object_key(self, data: bytes) -> str:
        sha = _sha(data)
        return f"{self.objects}/{sha[:2]}/{sha}"

    def object_count(self) -> int:
        return sum(1 for k in self.server_files() if k.startswith(self.objects + "/"))

    # -- tests ----------------------------------------------------------------
    def test_payload_bypasses_git_and_lands_by_hash(self):
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload: 2 file(s)", out)

        files = self.server_files()
        self.assertEqual(files[self.object_key(self.big1)], self.big1)
        self.assertEqual(files[self.object_key(self.big2)], self.big2)
        self.assertEqual(self.object_count(), 2)

        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertIn("small.txt", tree)
        self.assertIn(".seafile-payload/manifest.json", tree)
        self.assertNotIn("big1.bin", tree)
        self.assertNotIn("big2.bin", tree)

        manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
        by_path = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(by_path["big1.bin"]["sha256"], _sha(self.big1))
        self.assertEqual(by_path["sub/big2.bin"]["size"], 7000)

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
        self.assertIn("uploaded 0 object(s)", out)
        self.assertEqual(self.server_files(), before)

    def test_a_same_size_edit_is_a_new_object_and_the_old_one_stays(self):
        self.assertEqual(self.snapshot()[0], 0)
        edited = bytes(b ^ 0xFF for b in self.big1)  # same length, different bytes
        (self.src / "big1.bin").write_bytes(edited)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        files = self.server_files()
        self.assertEqual(files[self.object_key(edited)], edited)
        self.assertEqual(files[self.object_key(self.big1)], self.big1)

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

    def test_a_rename_uploads_nothing(self):
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "big1.bin").rename(self.src / "renamed.bin")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("uploaded 0 object(s)", out)
        self.assertEqual(self.object_count(), 2)
        into = self.case / "renamed"
        self.assertEqual(self.run_tool("--restore", "--remote", self.remote, "--into", into)[0], 0)
        self.assertEqual((into / "renamed.bin").read_bytes(), self.big1)
        self.assertFalse((into / "big1.bin").exists())

    def test_duplicate_content_is_one_object(self):
        (self.src / "copy.bin").write_bytes(self.big1)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload: 3 file(s)", out)
        self.assertIn("uploaded 2 object(s)", out)
        self.assertEqual(self.object_count(), 2)
        into = self.case / "dup"
        self.assertEqual(self.run_tool("--restore", "--remote", self.remote, "--into", into)[0], 0)
        self.assertEqual((into / "copy.bin").read_bytes(), self.big1)

    def test_a_file_removed_from_the_source_keeps_its_object(self):
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "sub" / "big2.bin").unlink()
        self.assertEqual(self.snapshot()[0], 0)
        # Nothing is ever deleted by a snapshot run: the older snapshot needs it.
        self.assertIn(self.object_key(self.big2), self.server_files())

    def test_corrupt_payload_fails_the_restore(self):
        self.assertEqual(self.snapshot()[0], 0)
        self.stub.files[REPO_ID][self.object_key(self.big1)] = bytes(len(self.big1))
        rc, out, err = self.run_tool(
            "--restore", "--remote", self.remote, "--into", self.case / "bad"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("SHA-256 does not match the manifest", out + err)

    def test_a_missing_object_fails_the_restore_naming_the_file(self):
        self.assertEqual(self.snapshot()[0], 0)
        del self.stub.files[REPO_ID][self.object_key(self.big2)]
        rc, out, err = self.run_tool(
            "--restore", "--remote", self.remote, "--into", self.case / "gone"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("sub/big2.bin", out + err)
        self.assertIn("not found", out + err)

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
        self.assertIn("to upload 2 object(s)", out)
        self.assertEqual(self.server_files(), {})

    def test_names_need_no_special_handling(self):
        # Objects are named by hash, so decomposed Unicode, a long name and (off
        # Windows) a double quote never meet the server's name rules.
        nfd = unicodedata.normalize("NFD", "café") + ".bin"
        long_name = "ก" * 86 + ".bin"  # 262 bytes of UTF-8: refused as a server name
        content = {nfd: os.urandom(3000), long_name: os.urandom(3100)}
        if os.name != "nt":
            content['say "hi".bin'] = os.urandom(3200)
        for name, data in content.items():
            (self.src / name).write_bytes(data)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        into = self.case / "names"
        rc, out, err = self.run_tool("--restore", "--remote", self.remote, "--into", into)
        self.assertEqual(rc, 0, out + err)
        for name, data in content.items():
            self.assertEqual((into / name).read_bytes(), data)

    def test_a_file_that_changes_between_hash_and_upload_is_refused(self):
        with unittest.mock.patch.object(snap, "_unchanged", return_value=False):
            rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("changed", out + err)
        self.assertEqual(self.object_count(), 0)

    def test_an_unreadable_file_names_itself(self):
        real_open = open

        def refuse(path, *a, **k):
            if str(path).endswith("big1.bin"):
                raise PermissionError(13, "sharing violation")
            return real_open(path, *a, **k)

        with unittest.mock.patch("builtins.open", side_effect=refuse):
            rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("big1.bin", out + err)

    def test_many_files_upload_in_parallel_and_all_land(self):
        contents = {}
        for i in range(12):
            data = os.urandom(1500 + i * 100)
            (self.src / f"par{i:02d}.bin").write_bytes(data)
            contents[f"par{i:02d}.bin"] = data
        rc, out, err = self.snapshot("--payload-jobs", "4")
        self.assertEqual(rc, 0, out + err)
        files = self.server_files()
        for data in contents.values():
            self.assertEqual(files[self.object_key(data)], data)
        self.assertIn("uploaded 14 object(s)", out)

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
        self.assertEqual(self.object_count(), 6)

    def test_parse_size(self):
        self.assertEqual(snap.parse_size("64M"), 64 * 1024 ** 2)
        self.assertEqual(snap.parse_size("1g"), 1024 ** 3)
        self.assertEqual(snap.parse_size("500k"), 500 * 1024)
        self.assertEqual(snap.parse_size("1048576"), 1048576)
        with self.assertRaises(snap.SnapshotError):
            snap.parse_size("lots")


if __name__ == "__main__":
    unittest.main()
