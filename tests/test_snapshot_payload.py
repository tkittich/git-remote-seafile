"""Payload mode of the snapshot tool, against the in-process Seafile stub.

Large files bypass git: they are stored on the server by SHA-256 under
``<path>/payload/objects/<xx>/<sha256>`` and the vault commit carries a
manifest.  These tests drive the real tool (``snap.main``) with the real helper
behind a PATH shim, so the vault push and the payload upload both hit the stub.

Every requirement in PAYLOAD.md names a test here, and each was checked the way
a requirement should be: by breaking the code in one line and seeing a test fail
(the review that prompted the rewrite found five requirements whose tests did
not).
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
import time
import unicodedata
import unittest
import unittest.mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT / "tests"))

import seafile_snapshot as snap  # noqa: E402
from e2e_harness import REPO_ID, SeafileStub, make_helper_env  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402

DAY = 86400
OLD_NS = 1_600_000_000_123_456_789  # September 2020, not a whole second


def _git(cwd: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _good_entry(**over):
    entry = {"path": "a/b.bin", "size": 3, "mode": "100644", "mtime_ns": 1, "sha256": "0" * 64}
    entry.update(over)
    return entry


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
        self.base = "/pvault/payload"
        self.objects = self.base + "/objects"

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

    def restore(self, name: str, *extra: object) -> tuple[int, str, str, pathlib.Path]:
        into = self.case / name
        rc, out, err = self.run_tool("--restore", "--remote", self.remote, "--into", into, *extra)
        return rc, out, err, into

    def gc(self, *extra: object) -> tuple[int, str, str]:
        return self.run_tool("--payload-gc", "--remote", self.remote, *extra)

    def server_files(self) -> dict[str, bytes]:
        return dict(self.stub.files.get(REPO_ID, {}))

    def object_key(self, data: bytes) -> str:
        sha = _sha(data)
        return f"{self.objects}/{sha[:2]}/{sha}"

    def object_count(self) -> int:
        return sum(1 for k in self.server_files() if k.startswith(self.objects + "/"))

    def stored_names(self) -> list[str]:
        return [k.rsplit("/", 1)[-1] for k in self.server_files() if k.startswith(self.objects + "/")]

    def backdate_pending(self, days: float) -> None:
        key = f"{self.base}/gc-pending.json"
        state = json.loads(self.stub.files[REPO_ID][key])
        old = time.time() - days * DAY
        self.stub.files[REPO_ID][key] = json.dumps({sha: old for sha in state}).encode()

    def orphan(self, content: bytes = b"orphaned object") -> str:
        sha = _sha(content)
        key = f"{self.objects}/{sha[:2]}/{sha}"
        self.stub.files.setdefault(REPO_ID, {})[key] = content
        return key

    # -- the basics (R1, R4, R5, R8, R10) ---------------------------------------
    def test_payload_bypasses_git_and_lands_by_hash(self):
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload: 2 file(s)", out)
        files = self.server_files()
        self.assertEqual(files[self.object_key(self.big1)], self.big1)
        self.assertEqual(files[self.object_key(self.big2)], self.big2)
        self.assertEqual(self.object_count(), 2)
        self.assertFalse(any(".part-" in k for k in files), "a temporary name was left behind")
        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertIn("small.txt", tree)
        self.assertIn(".seafile-payload/manifest.json", tree)
        self.assertNotIn("big1.bin", tree)
        manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
        by_path = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(by_path["big1.bin"]["sha256"], _sha(self.big1))
        self.assertEqual(manifest["format"], 2)

    def test_restore_downloads_and_verifies_the_payload(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err, into = self.restore("restored")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload verified: 2 file(s)", out)
        self.assertEqual((into / "big1.bin").read_bytes(), self.big1)
        self.assertEqual((into / "sub" / "big2.bin").read_bytes(), self.big2)
        self.assertEqual((into / "small.txt").read_bytes(), b"small\n")
        self.assertEqual(list(into.rglob("*.part")), [])

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
        edited = bytes(b ^ 0xFF for b in self.big1)
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
        rc, out, err, into = self.restore("old", "--ref", "snapshot~1")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "big1.bin").read_bytes(), self.big1)

    def test_a_rename_uploads_nothing(self):
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "big1.bin").rename(self.src / "renamed.bin")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("uploaded 0 object(s)", out)
        self.assertEqual(self.object_count(), 2)
        rc, out, err, into = self.restore("renamed")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "renamed.bin").read_bytes(), self.big1)
        self.assertFalse((into / "big1.bin").exists())

    def test_duplicate_content_is_one_object(self):
        (self.src / "copy.bin").write_bytes(self.big1)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload: 3 file(s)", out)
        self.assertIn("uploaded 2 object(s)", out)
        rc, out, err, into = self.restore("dup")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "copy.bin").read_bytes(), self.big1)

    def test_a_file_removed_from_the_source_keeps_its_object(self):
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "sub" / "big2.bin").unlink()
        self.assertEqual(self.snapshot()[0], 0)
        self.assertIn(self.object_key(self.big2), self.server_files())

    def test_corrupt_payload_fails_the_restore_and_leaves_nothing_behind(self):
        self.assertEqual(self.snapshot()[0], 0)
        self.stub.files[REPO_ID][self.object_key(self.big1)] = bytes(len(self.big1))
        rc, out, err, into = self.restore("bad")
        self.assertNotEqual(rc, 0)
        self.assertIn("SHA-256 does not match the manifest", out + err)
        self.assertFalse((into / "big1.bin").exists())
        self.assertEqual(list(into.rglob("*.part")), [])
        # The other file was still restored and verified.
        self.assertEqual((into / "sub" / "big2.bin").read_bytes(), self.big2)

    def test_a_missing_object_names_the_file_and_the_rest_restores(self):
        self.assertEqual(self.snapshot()[0], 0)
        del self.stub.files[REPO_ID][self.object_key(self.big2)]
        rc, out, err, into = self.restore("gone")
        self.assertNotEqual(rc, 0)
        self.assertIn("sub/big2.bin", out + err)
        self.assertIn("not found", out + err)
        self.assertEqual((into / "big1.bin").read_bytes(), self.big1)

    def test_a_vault_that_used_payload_refuses_a_run_without_the_flag(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--remote", self.remote)
        self.assertNotEqual(rc, 0)
        self.assertIn("payload mode", out + err)

    def test_dry_run_reads_the_server_and_changes_nothing(self):
        rc, out, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("to upload 2 object(s)", out)
        self.assertEqual(self.server_files(), {})
        self.assertFalse((self.vault / ".git" / "payload-cache.json").exists())

    # -- the object is what its name says (R2, R6) ------------------------------
    def test_an_object_is_never_stored_under_a_name_that_is_not_its_hash(self):
        original = SeafileClient.upload_file
        target = self.src / "big1.bin"
        stat = target.stat()
        altered = bytes(b ^ 0x55 for b in self.big1)
        state = {"done": False}

        def edit_in_place_then_upload(client, rid, parent, name, content, *a, **kw):
            # A same-size write that leaves the mtime exactly as it was: the case a
            # size-and-mtime comparison cannot see.
            if not state["done"] and name.startswith(_sha(self.big1)):
                state["done"] = True
                target.write_bytes(altered)
                os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return original(client, rid, parent, name, content, *a, **kw)

        with unittest.mock.patch.object(SeafileClient, "upload_file", edit_in_place_then_upload):
            rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertNotEqual(rc, 0)
        self.assertIn("changed while it was being uploaded", out + err)
        files = self.server_files()
        self.assertNotIn(self.object_key(self.big1), files, "poisoned object left under the hash")
        self.assertFalse(any(".part-" in k for k in files), "temporary object left behind")

        # The file goes back to its original bytes; the next run must succeed and
        # a later snapshot of the genuine bytes must restore.
        target.write_bytes(self.big1)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        rc, out, err, into = self.restore("healed")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "big1.bin").read_bytes(), self.big1)

    def test_a_file_that_changes_between_hash_and_upload_is_refused(self):
        with unittest.mock.patch.object(snap, "_unchanged", return_value=False):
            rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("changed since it was hashed", out + err)
        self.assertEqual(self.object_count(), 0)

    def test_every_object_named_is_rechecked_just_before_the_push(self):
        # An object that was on the server when the run listed it, and gone by the
        # time of the push (a retention run on another machine), must be put back.
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "big1.bin").write_bytes(os.urandom(6000))
        real = snap.auto_incremental_repack
        gone = {}

        def delete_an_object_first(*a, **kw):
            # Called right after the commit and right before the push.
            victim = next(k for k in self.server_files() if k.startswith(self.objects)
                          and k != self.object_key(self.big1))
            gone["key"] = victim
            del self.stub.files[REPO_ID][victim]
            return real(*a, **kw)

        with unittest.mock.patch.object(snap, "auto_incremental_repack", delete_an_object_first):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("re-uploading", out)
        self.assertIn(gone["key"], self.server_files())

    def test_an_unpushed_snapshot_whose_object_vanished_blocks_the_next_push(self):
        self.assertEqual(self.snapshot()[0], 0)
        s2 = os.urandom(6000)
        (self.src / "big1.bin").write_bytes(s2)
        with unittest.mock.patch.object(snap, "push_snapshot", side_effect=snap.SnapshotError("net down")):
            rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        # S2's object is lost, and the file moves on again before the retry.
        del self.stub.files[REPO_ID][self.object_key(s2)]
        (self.src / "big1.bin").write_bytes(os.urandom(6100))
        rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("missing from the server", out + err)
        self.assertIn("Not pushing", out + err)

    # -- change detection (R4, R6, D6) --------------------------------------------
    def _tiles(self):
        a, b, c = (os.urandom(4000) for _ in range(3))
        for name, data in (("tile_1.bin", a), ("tile_2.bin", b), ("tile_3.bin", c)):
            path = self.src / name
            path.write_bytes(data)
            os.utime(path, ns=(OLD_NS, OLD_NS))
        return a, b, c

    def test_renumbered_same_size_same_mtime_files_are_not_given_their_predecessors_hash(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        a, b, c = self._tiles()
        self.assertEqual(self.snapshot()[0], 0)
        # delete tile_1, tile_2 -> tile_1, tile_3 -> tile_2: a rename keeps size and mtime
        (self.src / "tile_1.bin").unlink()
        (self.src / "tile_2.bin").rename(self.src / "tile_1.bin")
        (self.src / "tile_3.bin").rename(self.src / "tile_2.bin")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        rc, out, err, into = self.restore("tiles")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "tile_1.bin").read_bytes(), b)
        self.assertEqual((into / "tile_2.bin").read_bytes(), c)

    def test_an_old_unchanged_file_is_not_rehashed(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        self._tiles()
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("hashing ["), 3)
        # A cache entry is trusted only when the file is older than the moment it
        # was hashed by the racy window; age the entry, as the passage of time does.
        cache_path = self.vault / ".git" / "payload-cache.json"
        cache = json.loads(cache_path.read_text())
        for entry in cache.values():
            entry["at"] -= 10 * 1_000_000_000
        cache_path.write_text(json.dumps(cache))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("hashing ["), 0, out)

    def test_a_file_edited_within_the_racy_window_is_rehashed(self):
        # An mtime in the future can never be "older than the hash time by the racy
        # window", whatever the machine's load: deterministic, unlike "just now".
        target = self.src / "big1.bin"
        future = time.time_ns() + 60 * 1_000_000_000 + 123_456_789
        os.utime(target, ns=(future, future))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        # Same size, same mtime, edited right after it was hashed.
        stat = target.stat()
        target.write_bytes(bytes(b ^ 0x0F for b in self.big1))
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        rc, out, err, into = self.restore("racy")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "big1.bin").read_bytes(), bytes(b ^ 0x0F for b in self.big1))

    def test_a_hash_the_remote_wrote_is_never_used(self):
        # The cache is this machine's: a manifest on the remote does not feed it.
        self.assertEqual(self.snapshot()[0], 0)
        cache_path = self.vault / ".git" / "payload-cache.json"
        cache_path.unlink()
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("hashing ["), 2)

    # -- hostile and unusual manifests (R7, D4, D8, D12) ----------------------------
    def test_manifest_paths_that_could_escape_or_run_code_are_refused(self):
        dest = self.case / "dest"
        dest.mkdir()
        snap.validate_manifest({"format": 2, "files": [_good_entry()]}, dest)  # control
        bad_paths = (
            "../evil", "a/../../evil", "/etc/passwd", "a//b", "./a", "", "a/.",
            ".git/config", ".GIT/hooks/post-checkout", "a/.git/x", ".git ", "git~1/config",
            ".seafile-payload/manifest.json", ".SEAFILE-PAYLOAD/x", "a\0b",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(snap.SnapshotError):
                snap.validate_manifest({"format": 2, "files": [_good_entry(path=path)]}, dest)

    def test_manifest_fields_are_checked(self):
        dest = self.case / "dest"
        dest.mkdir()
        for key, value in (("sha256", "../../x"), ("sha256", "A" * 64), ("size", -1),
                           ("size", True), ("mode", "120000"), ("mtime_ns", "now"),
                           ("mtime_ns", 10 ** 30)):
            with self.subTest(key=key, value=value), self.assertRaises(snap.SnapshotError):
                snap.validate_manifest({"format": 2, "files": [_good_entry(**{key: value})]}, dest)
        for broken in ({"files": "nope", "format": 2}, {"format": 2}, [], {"format": True, "files": []}):
            with self.subTest(broken=broken), self.assertRaises(snap.SnapshotError):
                snap.validate_manifest(broken, dest)

    def test_a_newer_manifest_format_is_refused_everywhere(self):
        with self.assertRaises(snap.SnapshotError) as ctx:
            snap.check_manifest_shape({"format": 3, "files": []})
        self.assertIn("format 3", str(ctx.exception))
        with self.assertRaises(snap.SnapshotError):
            snap.check_manifest_shape({"files": []})

    def test_a_symlink_in_the_tree_cannot_carry_a_payload_write_outside(self):
        if os.name == "nt":
            self.skipTest("creating symlinks needs a privilege on Windows")
        into = self.case / "linked"
        into.mkdir()
        outside = self.case / "outside"
        outside.mkdir()
        os.symlink(outside, into / "link")
        with self.assertRaises(snap.SnapshotError):
            snap.validate_manifest({"format": 2, "files": [_good_entry(path="link/evil.bin")]}, into)

    def test_a_restore_never_overwrites_something_already_there(self):
        # A manifest entry whose path is a file git already restored (or a name this
        # filesystem treats as another entry's) is skipped and reported, never
        # written over; the other entries are still restored.
        self.assertEqual(self.snapshot()[0], 0)
        into = self.case / "occupied"
        (into / ".seafile-payload").mkdir(parents=True)
        (into / "small.txt").write_bytes(b"keep me")
        entries = [
            {"path": "small.txt", "size": len(self.big1), "mode": "100644", "mtime_ns": 1,
             "sha256": _sha(self.big1)},
            {"path": "fresh.bin", "size": len(self.big2), "mode": "100644", "mtime_ns": 1,
             "sha256": _sha(self.big2)},
        ]
        (into / ".seafile-payload" / "manifest.json").write_bytes(
            json.dumps({"format": 2, "files": entries}).encode()
        )
        with unittest.mock.patch.dict(os.environ, self.env),                 self.assertRaises(snap.SnapshotError) as ctx:
            snap.restore_payload(self.remote, into)
        self.assertIn("something else is already there", str(ctx.exception))
        self.assertEqual((into / "small.txt").read_bytes(), b"keep me")
        self.assertEqual((into / "fresh.bin").read_bytes(), self.big2)

    def test_windows_name_rules(self):
        for bad in ("a:b.tif", "x?.bin", "trail.", "trail ", "CON", "aux.txt", "dir/NUL.x", 'q"q'):
            with self.subTest(name=bad):
                self.assertIsNotNone(snap.windows_name_problem(bad))
        for good in ("plain.bin", "sub/dir/file.tif", "café.bin", "กข.bin", "auxiliary.txt"):
            with self.subTest(name=good):
                self.assertIsNone(snap.windows_name_problem(good))

    def test_names_that_windows_cannot_hold_are_warned_about_at_snapshot_time(self):
        if os.name == "nt":
            self.skipTest("cannot create a name with a colon on Windows")
        (self.src / "2024-01-01T10:00.bin").write_bytes(os.urandom(3000))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("cannot be restored on Windows", out)
        self.assertIn("2024-01-01T10:00.bin", out)

    def test_names_need_no_special_handling(self):
        nfd = unicodedata.normalize("NFD", "café") + ".bin"
        long_name = "ก" * 86 + ".bin"  # 262 bytes of UTF-8: refused as a server name
        content = {nfd: os.urandom(3000), long_name: os.urandom(3100)}
        if os.name != "nt":
            content['say "hi".bin'] = os.urandom(3200)
        for name, data in content.items():
            (self.src / name).write_bytes(data)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        rc, out, err, into = self.restore("names")
        self.assertEqual(rc, 0, out + err)
        for name, data in content.items():
            self.assertEqual((into / name).read_bytes(), data)

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

    def test_a_server_error_in_the_payload_step_is_a_message_not_a_traceback(self):
        from git_remote_seafile.client import SeafileAPIError

        real = SeafileClient.list_dir

        def listing_fails_for_objects(client, rid, path="/"):
            if "/payload/objects" in path:
                raise SeafileAPIError(f"Failed to list dir {path}: HTTP 500 injected")
            return real(client, rid, path)

        with unittest.mock.patch.object(SeafileClient, "list_dir", listing_fails_for_objects):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 1, out + err)
        self.assertIn("listing", err)
        self.assertIn("injected", err)
        self.assertNotIn("Traceback", err)

    def test_payload_paths_that_look_like_credentials_are_reported(self):
        (self.src / "server.pem").write_bytes(os.urandom(4000))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("server.pem", out)
        self.assertIn("look like credentials", out)

    def test_the_payload_directory_in_a_source_is_never_staged(self):
        # A restored payload tree used as a source must not carry its old manifest
        # into a new vault, nor lock a plain vault into needing the flag.
        (self.src / ".seafile-payload").mkdir()
        (self.src / ".seafile-payload" / "manifest.json").write_bytes(
            json.dumps({"format": 2, "files": []}).encode()
        )
        for _ in range(2):
            rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--remote", self.remote)
            self.assertEqual(rc, 0, out + err)
        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertNotIn(".seafile-payload", tree)
        rc, out, err, into = self.restore("plain")
        self.assertEqual(rc, 0, out + err)

    # -- selection (D1) -----------------------------------------------------------
    def test_the_auto_rule_sends_big_and_incompressible_files_to_payload(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        data = {
            "huge.bin": os.urandom(9000),
            "mid_random.bin": os.urandom(3000),
            "mid_text.bin": b"abcdefgh" * 500,
            "small_random.bin": os.urandom(1500),
        }
        for name, content in data.items():
            (self.src / name).write_bytes(content)
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err = self.run_tool(
                self.src, "--vault", self.vault, "--remote", self.remote, "--payload-min-size", "auto",
            )
        self.assertEqual(rc, 0, out + err)
        manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
        self.assertEqual(manifest["rule"], "auto")
        self.assertEqual({f["path"] for f in manifest["files"]}, {"huge.bin", "mid_random.bin"})
        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertIn("mid_text.bin", tree)
        self.assertNotIn("mid_random.bin", tree)
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--remote", self.remote)
        self.assertNotEqual(rc, 0)
        self.assertIn("--payload-min-size auto", out + err)

    def test_a_bare_flag_means_auto(self):
        rc, out, err = self.run_tool(
            self.src, "--vault", self.vault, "--remote", self.remote, "--payload-min-size"
        )
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload:", out)

    def test_parse_size_is_strict(self):
        self.assertEqual(snap.parse_size("64M"), 64 * 1024 ** 2)
        self.assertEqual(snap.parse_size("1g"), 1024 ** 3)
        self.assertEqual(snap.parse_size("500k"), 500 * 1024)
        self.assertEqual(snap.parse_size("1048576"), 1048576)
        self.assertEqual(snap.parse_size("0"), 0)
        for bad in ("lots", "-1", "0.5", "1e400", "", "1.5M"):
            with self.subTest(text=bad), self.assertRaises(snap.SnapshotError):
                snap.parse_size(bad)

    def test_a_size_that_is_also_a_directory_is_questioned(self):
        (self.src / "2024").mkdir()
        cwd = os.getcwd()
        os.chdir(self.src)  # the default SOURCE is the working directory
        try:
            rc, out, err = self.run_tool("--vault", self.vault, "--remote", self.remote,
                                         "--payload-min-size", "2024")
        finally:
            os.chdir(cwd)
        self.assertNotEqual(rc, 0)
        self.assertIn("also a directory", out + err)

    def test_flag_combinations_that_would_be_ignored_are_refused(self):
        cases = (
            (("--vault", self.vault, "--payload-min-size", "1000", "--no-push"), "--no-push"),
            (("--vault", self.vault, "--payload-jobs", "4"), "--payload-jobs only applies"),
            (("--vault", self.vault, "--payload-delete"), "--payload-delete only applies"),
            (("--vault", self.vault, "--payload-grace-days", "3"), "--payload-grace-days only applies"),
            (("--payload-gc",), "requires --remote"),
            (("--payload-gc", "--remote", self.remote, "--payload-grace-days", "0"), "at least"),
            (("--restore", "--remote", self.remote, "--into", self.case / "x",
              "--payload-min-size", "1000"), "does not apply"),
        )
        for argv, needle in cases:
            with self.subTest(argv=argv):
                rc, out, err = self.run_tool(self.src, *argv) if "--restore" not in argv \
                    else self.run_tool(*argv)
                self.assertEqual(rc, 1, out + err)
                self.assertIn(needle, err)

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

    # -- retention (D9) ----------------------------------------------------------
    def test_gc_reports_and_changes_nothing_by_default(self):
        self.assertEqual(self.snapshot()[0], 0)
        orphan = self.orphan()
        rc, out, err = self.gc("--payload-grace-days", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("2 referenced object(s)", out)
        self.assertIn("unused : 1 object(s)", out)
        self.assertIn("report only", out)
        self.assertIn(orphan, self.server_files())
        self.assertNotIn(f"{self.base}/gc-pending.json", self.server_files())

    def test_gc_marks_first_and_deletes_only_after_the_grace_period(self):
        self.assertEqual(self.snapshot()[0], 0)
        orphan = self.orphan()
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("marked 1 newly unreferenced", out)
        self.assertIn(orphan, self.server_files(), "first run must only mark")
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertIn(orphan, self.server_files(), "not yet unreferenced for the grace period")
        self.backdate_pending(2)
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(orphan, self.server_files())
        self.assertEqual(self.object_count(), 2)

    def test_an_object_referenced_again_before_the_second_phase_is_kept(self):
        # The object a run reused, between the mark and the delete.
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "big1.bin").write_bytes(os.urandom(6000))
        self.assertEqual(self.snapshot()[0], 0)
        # Drop the first snapshot's reference by moving the branch back is not
        # possible here; instead mark an object, then name it in a new manifest.
        reuse = os.urandom(2500)
        key = self.orphan(reuse)
        self.assertEqual(self.gc("--payload-delete", "--payload-grace-days", "1")[0], 0)
        self.backdate_pending(2)
        (self.src / "reuse.bin").write_bytes(reuse)
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(key, self.server_files())

    def test_gc_protects_the_objects_of_an_unpushed_local_snapshot(self):
        self.assertEqual(self.snapshot()[0], 0)
        s2 = os.urandom(6000)
        (self.src / "big1.bin").write_bytes(s2)
        with unittest.mock.patch.object(snap, "push_snapshot", side_effect=snap.SnapshotError("net down")):
            self.assertNotEqual(self.snapshot()[0], 0)
        key = self.object_key(s2)
        self.assertIn(key, self.server_files())
        for _ in range(2):
            self.gc("--payload-delete", "--payload-grace-days", "1", "--vault", self.vault)
            self.backdate_pending(2) if f"{self.base}/gc-pending.json" in self.server_files() else None
        self.assertIn(key, self.server_files(), "the local vault's snapshot must protect its object")
        # Without --vault the same object is unreferenced, which is the point.
        self.gc("--payload-delete", "--payload-grace-days", "1")
        self.backdate_pending(2)
        self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertNotIn(key, self.server_files())

    def test_gc_will_not_run_with_a_vault_that_is_not_one(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.gc("--vault", self.case / "no-such-vault", "--payload-grace-days", "1")
        self.assertNotEqual(rc, 0)
        self.assertIn("is not a vault", out + err)

    def test_gc_leaves_names_that_are_not_objects_alone(self):
        self.assertEqual(self.snapshot()[0], 0)
        stranger = f"{self.objects}/ab/not-a-hash.txt"
        part = f"{self.objects}/ab/{'c' * 64}.part-1234"
        self.stub.files[REPO_ID][stranger] = b"x"
        self.stub.files[REPO_ID][part] = b"y"
        for _ in range(2):
            self.gc("--payload-delete", "--payload-grace-days", "1")
            if f"{self.base}/gc-pending.json" in self.server_files():
                self.backdate_pending(2)
        self.assertIn(stranger, self.server_files())
        self.assertIn(part, self.server_files())
        rc, out, err = self.gc("--payload-grace-days", "1")
        self.assertIn("other  : 2 entry(ies) that are not objects", out)
        self.assertIn("unused : 0 object(s)", out)

    def test_gc_refuses_when_a_ref_is_a_conflict_copy(self):
        self.assertEqual(self.snapshot()[0], 0)
        self.stub.files[REPO_ID]["/pvault/refs/heads/snapshot (SFConflict a@b 2026-10-13)"] = b"0" * 40
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertNotEqual(rc, 0)
        self.assertIn("conflict copies", out + err)

    def test_gc_refuses_a_history_it_cannot_read(self):
        self.assertEqual(self.snapshot()[0], 0)
        # A manifest of a format this tool does not know, committed to the local
        # vault (as a newer tool on another machine would push it).
        bad = json.dumps({"format": 3, "entries": []}).encode()
        gd = str(self.vault / ".git")
        blob = subprocess.run(["git", f"--git-dir={gd}", "hash-object", "-w", "--stdin"],
                              input=bad, capture_output=True, check=True).stdout.decode().strip()
        env = dict(os.environ, GIT_INDEX_FILE=str(self.vault / ".git" / "tmp-index"),
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        def plumbing(*args, **kw):
            return subprocess.run(["git", f"--git-dir={gd}", *args], env=env, capture_output=True,
                                  check=True, **kw).stdout.decode().strip()
        plumbing("read-tree", "snapshot")
        plumbing("update-index", "--add", "--cacheinfo", f"100644,{blob},.seafile-payload/manifest.json")
        tree = plumbing("write-tree")
        commit = plumbing("commit-tree", tree, "-p", "snapshot", "-m", "from the future")
        plumbing("update-ref", "refs/heads/snapshot", commit)
        orphan = self.orphan()
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1", "--vault", self.vault)
        self.assertNotEqual(rc, 0)
        self.assertIn("format 3", out + err)
        self.assertIn(orphan, self.server_files())

    def test_gc_refuses_to_act_when_it_finds_no_manifest(self):
        self.assertEqual(self.snapshot()[0], 0)
        other = "seafile://testlib/otherrepo"
        for key, data in list(self.server_files().items()):
            if key.startswith(self.objects):
                self.stub.files[REPO_ID][key.replace("/pvault/", "/otherrepo/")] = data
        self.assertEqual(
            self.run_tool(self.src, "--vault", self.case / "v2", "--remote", other)[0], 0
        )
        rc, out, err = self.run_tool(
            "--payload-gc", "--remote", other, "--payload-grace-days", "1", "--payload-delete"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("found no payload manifest", out + err)
        self.assertEqual(
            len([k for k in self.server_files() if k.startswith("/otherrepo/payload/objects")]), 2
        )


if __name__ == "__main__":
    unittest.main()
