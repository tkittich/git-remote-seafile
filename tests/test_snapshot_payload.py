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

import argparse
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


class _PayloadFixture(unittest.TestCase):
    """Stub server, a small source tree and the helpers; holds no tests."""

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
        if "--payload-delete" in extra and not {"--vault", "--payload-no-vault"} & set(extra):
            extra = (*extra, "--payload-no-vault")
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

    def age_cache_entries(self, seconds: float) -> None:
        """Age every hash-cache entry past the racy window, as time does.

        Handles both on-disk shapes: the machine-stamped wrapper (0.9.13+)
        and the flat map older tools wrote.
        """
        cache_path = self.vault / ".git" / "payload-cache.json"
        cache = json.loads(cache_path.read_text())
        entries = cache["entries"] if "entries" in cache else cache
        for entry in entries.values():
            entry["at"] -= seconds * 1_000_000_000
        cache_path.write_text(json.dumps(cache))

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

    # -- change detection (R4, R6, D6) --------------------------------------------
    def _tiles(self):
        a, b, c = (os.urandom(4000) for _ in range(3))
        for name, data in (("tile_1.bin", a), ("tile_2.bin", b), ("tile_3.bin", c)):
            path = self.src / name
            path.write_bytes(data)
            os.utime(path, ns=(OLD_NS, OLD_NS))
        return a, b, c

    # -- payload mode is on by default ------------------------------------------
    def bare(self, *extra: object, vault: pathlib.Path | None = None,
             remote: str | None = None) -> tuple[int, str, str]:
        """A run with no payload flag at all, the auto thresholds scaled to the fixture."""
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            return self.run_tool(self.src, "--vault", vault or self.vault,
                                 "--remote", remote or self.remote, *extra)

    def _existing_vault_without_payload(self) -> None:
        """A vault with an earlier snapshot taken before payload mode was the default."""
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--no-push")
        self.assertEqual(rc, 0, out + err)
        (self.src / "small.txt").write_bytes(b"changed\n")      # a new snapshot is due

    def _existing_vault_without_payload_other(self) -> None:
        other = self.case / "other-vault"
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            self.assertEqual(
                self.run_tool(self.src, "--vault", other, "--no-push")[0], 0)
            (self.src / "small.txt").write_bytes(b"other\n")
            rc, out, err = self.run_tool(
                self.src, "--vault", other, "--remote", "seafile://testlib/pvault2",
                "--payload-min-size", "auto")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Notice: payload mode", out)    # the user asked for it

    # -- views (D10) ------------------------------------------------------------
    def view(self, *extra: object) -> tuple[int, str, str]:
        return self.run_tool("--payload-view", "--remote", self.remote, *extra)

    # -- clock domain of the racy rule, long paths, the periodic report ----------
    def _cache_case(self, *, behind_ns: int):
        """One 1 MiB candidate, hashed once, with an mtime ``behind_ns`` before 'now'."""
        path = self.case / "racy-unit.bin"
        path.write_bytes(b"x" * 1024)
        now = time.time_ns()
        mtime = now - behind_ns
        stale = {"size": 1024, "mtime_ns": mtime, "ino": 7, "sha256": "a" * 64, "at": now}
        return path, {"racy-unit.bin": (1024, mtime, "100644", 7)}, stale


class PayloadBasicsTests(_PayloadFixture):
    """Landing by hash, restore, naming, change detection."""


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

    def test_a_fresh_vault_on_another_machine_follows_the_default(self):
        self.assertEqual(self.bare()[0], 0)
        # Payload mode is on by default, so a vault that remembered nothing -- a
        # fresh one on another machine -- carries on in payload mode rather than
        # staging the large files into git.
        rc, out, err = self.bare(vault=self.case / "fresh-vault")
        self.assertEqual(rc, 0, out + err)
        tree = _git(self.case / "fresh-vault", "ls-tree", "-r", "--name-only", "snapshot")
        self.assertNotIn("big1.bin", tree)
        self.assertIn(".seafile-payload/manifest.json", tree)

    def test_a_vault_that_used_payload_still_refuses_when_it_cannot_stay_in_it(self):
        self.assertEqual(self.snapshot()[0], 0)
        _git(self.vault, "config", "--unset", "snapshot.payload")
        # --no-push cannot use payload mode, so the default quietly stays off -- and
        # that must not stage the large files of a payload vault into git.
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--no-push")
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

    def test_a_pre_push_check_that_cannot_list_its_commits_refuses_the_push(self):
        """``rev-list`` failing (a remote tip the vault does not hold, a damaged
        object) used to read as "no commits to check", and the push went ahead
        unverified: the guard against naming a lost object failed open."""
        self.assertEqual(self.snapshot()[0], 0)
        tip = _git(self.vault, "rev-parse", "snapshot").strip()
        with self.assertRaises(snap.SnapshotError) as ctx:
            snap.verify_payload_before_push(
                self.vault / ".git", self.src, self.remote, "0123456789" * 4, tip, None
            )
        self.assertIn("could not list the commits", str(ctx.exception))
        self.assertIn("refusing to push", str(ctx.exception))

    def test_bytes_that_differ_from_the_hash_are_never_named_after_it(self):
        """The digest of what is sent is the guard that cannot be fooled: a file
        rewritten in place with its size and mtime restored (a memory-mapped
        writer on Windows) passes every stat check.  Here the stat check is
        blinded and the bytes swapped between hashing and upload; only the
        hashing reader can notice, and the object must not land under the old
        content's name (the mutation ``clean = True`` fails this test)."""
        target = self.src / "big1.bin"
        original = target.stat()
        swapped = []

        def blind_stat_check(source, rel, size, mtime_ns):
            if rel == "big1.bin" and not swapped:
                swapped.append(True)
                target.write_bytes(os.urandom(len(self.big1)))
                os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
            return True

        with unittest.mock.patch.object(snap, "_unchanged", blind_stat_check):
            rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("changed while it was being uploaded", out + err)
        self.assertNotIn(self.object_key(self.big1), self.server_files())
        self.assertEqual(
            [n for n in self.stored_names() if ".part-" in n], [], "the temporary copy is removed"
        )

    def test_every_object_named_is_rechecked_just_before_the_push(self):
        # An object that was on the server when the run listed it, and gone by
        # the time of the push (a retention run on another machine), must be
        # put back.  The assertion that pins the *pre*-push half of R2: at the
        # moment push_snapshot runs, the object is already present again -- a
        # run that only repaired *after* the ref landed (the post-push check
        # prints the same "re-uploading" words) would fail here.
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "big1.bin").write_bytes(os.urandom(6000))
        real = snap.auto_incremental_repack
        real_push = snap.push_snapshot
        gone = {}
        seen_at_push = {}

        def delete_an_object_first(*a, **kw):
            # Called right after the commit and right before the push.
            victim = next(k for k in self.server_files() if k.startswith(self.objects)
                          and k != self.object_key(self.big1))
            gone["key"] = victim
            del self.stub.files[REPO_ID][victim]
            return real(*a, **kw)

        def push_only_after_repair(*a, **kw):
            seen_at_push["present"] = gone["key"] in self.server_files()
            return real_push(*a, **kw)

        with unittest.mock.patch.object(snap, "auto_incremental_repack", delete_an_object_first), \
                unittest.mock.patch.object(snap, "push_snapshot", push_only_after_repair):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(seen_at_push.get("present"),
                        "the object was still missing when the ref was pushed")
        self.assertIn("re-uploading", out)
        self.assertIn(gone["key"], self.server_files())

    def test_a_post_push_verify_failure_still_remembers_the_settings(self):
        """The push landed; a check *behind* it must not un-remember the run.

        verify_payload_after_push used to raise past the remember block, so
        the vault forgot the payload setting it had just used and the next
        bare scheduled run was refused by R10's guard -- wedging the job until
        a human re-typed the flag (REVIEW.deepseek D5).  Now: the settings are
        remembered, a Warning says what could not be re-verified, exit 1.
        """
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "big1.bin").write_bytes(os.urandom(6000))
        with unittest.mock.patch.object(
            snap, "verify_payload_after_push",
            side_effect=snap.SnapshotError(
                "the snapshot was pushed, but 1 payload object(s) it names are missing"
            ),
        ):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 1, out + err)
        self.assertIn("Warning:", out + err)
        self.assertIn("re-verified", out + err)
        # The vault remembers what this run used, so the next bare run works.
        remembered = subprocess.run(
            ["git", "-C", str(self.vault), "config", "--local", "--get", "snapshot.payload"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertTrue(remembered, "snapshot.payload was not remembered")
        # And the next bare run goes through instead of being refused.
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)

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
        self.age_cache_entries(10)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("hashing ["), 0, out)

    def test_a_failed_upload_keeps_the_hashes_so_the_next_run_does_not_start_over(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        self._tiles()
        self.stub.faults["upload"] = 500
        rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out.count("hashing ["), 3)
        cache_path = self.vault / ".git" / "payload-cache.json"
        self.assertTrue(cache_path.exists(), "the hashes were thrown away with the failed run")
        cache = json.loads(cache_path.read_text())
        self.assertEqual(len(cache["entries"]), 3)
        self.assertIn("machine", cache, "the cache records which machine wrote it")
        self.age_cache_entries(10)  # age them past the racy window, as time does
        del self.stub.faults["upload"]
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("hashing ["), 0, out)

    def test_a_dry_run_still_writes_no_cache_even_when_it_fails(self):
        with unittest.mock.patch.object(snap, "publish_payload", side_effect=snap.SnapshotError("x")):
            rc, out, err = self.snapshot("--dry-run")
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.vault / ".git" / "payload-cache.json").exists())

    def test_a_long_upload_prints_how_much_is_left(self):
        for name in ("p1.bin", "p2.bin", "p3.bin"):
            (self.src / name).write_bytes(os.urandom(3000))
        with unittest.mock.patch.object(snap, "_PULSE_SECONDS", 0.0):
            rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"progress: [\d.]+ \w+ of [\d.]+ \w+ uploaded \(\d+%\), \d+ MiB/s")
        self.assertIn("left", out)

    def test_the_progress_line_survives_a_zero_elapsed_time(self):
        self.assertIn("0 MiB/s", snap._progress_line(0, 100, 0.0))
        line = snap._progress_line(50 * 1048576, 100 * 1048576, 10.0)
        self.assertIn("(50%)", line)
        self.assertIn("5 MiB/s", line)
        self.assertIn("about 10.0s left", line)

    def test_one_changed_file_does_not_stop_the_other_uploads(self):
        for name in ("live.bin", "other1.bin", "other2.bin"):
            (self.src / name).write_bytes(os.urandom(3000))
        real = snap._unchanged

        def live_file_changed(source, rel, size, mtime_ns):
            return False if rel == "live.bin" else real(source, rel, size, mtime_ns)

        with unittest.mock.patch.object(snap, "_unchanged", side_effect=live_file_changed):
            rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertNotEqual(rc, 0)
        text = out + err
        self.assertIn("1 payload file(s) changed while the run was in progress", text)
        self.assertIn("live.bin: changed since it was hashed", text)
        self.assertIn("--exclude", text)
        # everything else landed, so the next run has only the live file left to send
        self.assertEqual(self.object_count(), 4)
        self.assertTrue((self.vault / ".git" / "payload-cache.json").exists())

    def test_a_file_that_vanishes_after_hashing_is_that_files_problem_not_the_servers(self):
        (self.src / "gone.bin").write_bytes(os.urandom(3000))
        real_open = open

        def open_failing_for_gone(file, *a, **kw):
            if str(file).endswith("gone.bin") and (a[:1] == ("rb",) or kw.get("mode") == "rb"):
                if state["hashed"]:
                    raise FileNotFoundError(2, "No such file", str(file))
            return real_open(file, *a, **kw)

        state = {"hashed": False}
        real_publish = snap.publish_payload

        def publish_after_hashing(*a, **kw):
            state["hashed"] = True  # build_manifest has read every file by now
            return real_publish(*a, **kw)

        with unittest.mock.patch.object(snap, "publish_payload", side_effect=publish_after_hashing), \
                unittest.mock.patch("builtins.open", side_effect=open_failing_for_gone):
            rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertNotEqual(rc, 0)
        self.assertIn("gone.bin: cannot be read any more", out + err)
        self.assertEqual(self.object_count(), 2)  # the other two went up

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

    def test_a_placeholder_is_refused_below_the_payload_floor_too(self):
        """The attribute check runs before the size floor (ROADMAP S4).

        A OneDrive placeholder of, say, 1 KiB used to fall through to
        `git add`, which hydrates it (or fails) -- on a tree whose operator
        was told placeholders are refused.  PAYLOAD A13 now holds at any size.
        """
        import types
        import unittest.mock

        real_lstat = os.lstat
        small = self.src / "tiny-placeholder.bin"
        small.write_bytes(b"placeholder bytes")

        def fake_lstat(path, *a, **kw):
            st = real_lstat(path, *a, **kw)
            if str(path).endswith("tiny-placeholder.bin"):
                fake = types.SimpleNamespace(**{k: getattr(st, k) for k in dir(st) if not k.startswith("_")})
                fake.st_file_attributes = 0x1000 | 0x400000   # OFFLINE | RECALL_ON_DATA_ACCESS
                return fake
            return st

        with unittest.mock.patch.object(snap.os, "lstat", fake_lstat):
            with self.assertRaises(snap.SnapshotError) as caught:
                snap.find_payload_files(self.src, [], 1024 * 1024)
        self.assertIn("tiny-placeholder.bin", str(caught.exception))

        # And through a run: the refusal names the file and the remedy.
        with unittest.mock.patch.object(snap.os, "lstat", fake_lstat):
            rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("placeholder", out + err)
        self.assertIn("tiny-placeholder.bin", out + err)

    def test_a_volume_with_no_change_time_gets_the_hour_margin(self):
        """The FAT family keeps one timestamp; ctime answers the mtime back.

        Measured on vfat (REVIEW.iq3_s D2): a same-size in-place rewrite with
        the mtime restored left every field of the cache key unchanged and the
        second run vouched for the old hash.  Such a file is now trusted only
        after an hour, like an unprobed share, and the run says so.

        The mock is active in BOTH snapshots so the cached entry itself
        carries ctime == mtime -- the condition the coarse rule detects -- and
        the mtime is an hour-band value (60 s before the first hash): older
        than the 2 s racy window, so under the ordinary rule the entry is
        trusted and a same-size edit with the mtime restored vouches for the
        OLD bytes; under the coarse 1 h window it is not, and only the coarse
        rule makes the restore return the new bytes.

        (Two earlier drafts failed on all POSIX CI legs.  The first assumed
        NTFS in its control: on ext4/APFS a file that was merely created and
        written has ctime == mtime exactly -- both stamped by the same inode
        operation -- so big2, which the fixture never touches, trips the
        detection in any control run and the note's absence cannot be
        asserted.  The second moved the utime before the control but left the
        cache entry written unmocked, so the mocked run's ctime mismatch, not
        the coarse rule, did the work.  The mock is therefore active from the
        first hash, and the control assertion is gone.)
        """
        import unittest.mock

        target = self.src / "big1.bin"
        second = self.src / "sub" / "big2.bin"
        # An hour-band mtime for every payload file: older than the 2 s racy
        # window by the time the first hash lands, well inside the coarse hour.
        band = time.time_ns() - 60 * 1_000_000_000
        for path in (target, second):
            os.utime(path, ns=(band, band))

        def fat_like(path, st):
            return st.st_mtime_ns   # the FAT family's only timestamp

        with unittest.mock.patch.object(snap, "change_time_ns", fat_like):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("no change time of its own", out + err)

        # Same size, same mtime, edited after the first hash: under the 2 s
        # racy window the entry is trusted (the mtime is a minute old) and
        # the OLD hash would stand; the coarse 1 h window is what forces the
        # re-hash the restore below asserts.
        stat = target.stat()
        edited = bytes(b ^ 0x11 for b in self.big1)
        target.write_bytes(edited)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with unittest.mock.patch.object(snap, "change_time_ns", fat_like):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("no change time of its own", out + err)
        rc, out, err, into = self.restore("coarse")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "big1.bin").read_bytes(), edited)

    def test_a_hash_the_remote_wrote_is_never_used(self):
        # The cache is this machine's: a manifest on the remote does not feed it.
        self.assertEqual(self.snapshot()[0], 0)
        cache_path = self.vault / ".git" / "payload-cache.json"
        cache_path.unlink()
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("hashing ["), 2)


class PayloadManifestAndSelectionTests(_PayloadFixture):
    """Hostile manifests, names, and what qualifies as payload."""


    # -- hostile and unusual manifests (R7, D4, D8, D12) ----------------------------
    def test_manifest_paths_that_could_escape_or_run_code_are_refused(self):
        dest = self.case / "dest"
        dest.mkdir()
        snap.validate_manifest({"format": 2, "files": [_good_entry()]}, dest)  # control
        bad_paths = (
            "../evil", "a/../../evil", "/etc/passwd", "a//b", "./a", "", "a/.",
            ".git/config", ".GIT/hooks/post-checkout", "a/.git/x", ".git ", "git~1/config",
            ".seafile-payload/manifest.json", ".SEAFILE-PAYLOAD/x", "a\0b",
            # NTFS stream spellings that Windows resolves to the real .git
            # (REVIEW.sonnet-5.5 D10): the string test passes, the filesystem
            # does not.
            ".git::$INDEX_ALLOCATION/config", ".git:$I30:$INDEX_ALLOCATION/x",
            ".git::$DATA", "x/git~1:y",
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
        # The link is built as a real filesystem object on every platform: a
        # symlink where creating one is allowed, and a directory junction on
        # Windows (no privilege needed, stdlib _winapi).  The test used to be
        # skipped on Windows -- the platform the project is developed and
        # released from -- which left R7's resolve-based containment guard
        # unguarded there (REVIEW.deepseek E3, mutation-reproduced).
        into = self.case / "linked"
        into.mkdir()
        outside = self.case / "outside"
        outside.mkdir()
        link = into / "link"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
            # stdlib has no removal counterpart; rmdir removes the junction,
            # not its target.
            self.addCleanup(os.rmdir, str(link))
        else:
            os.symlink(outside, link)
            self.addCleanup(link.unlink)
        self.assertTrue((into / "link").exists(), "the junction/link resolves")
        with self.assertRaises(snap.SnapshotError):
            snap.validate_manifest({"format": 2, "files": [_good_entry(path="link/evil.bin")]}, into)

    def test_a_link_to_dot_git_cannot_carry_a_payload_write_into_the_repository(self):
        into = self.case / "linked-git"
        (into / ".git" / "hooks").mkdir(parents=True)
        try:
            os.symlink(into / ".git", into / "tool", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symlink here")
        manifest = {"format": 2, "files": [_good_entry(path="tool/hooks/pre-commit", mode="100755")]}
        with self.assertRaises(snap.SnapshotError) as ctx:
            snap.validate_manifest(manifest, into)
        self.assertIn("through a link", str(ctx.exception))

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
        content = {nfd: os.urandom(3000)}
        if os.name == "nt":
            # 262 bytes of UTF-8: refused as a server name.  A POSIX filesystem
            # cannot hold a name that long at all (255 bytes), so only NTFS can
            # make this file.
            content["ก" * 86 + ".bin"] = os.urandom(3100)
        else:
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
        self.assertNotIn(".seafile-payload/manifest.json", tree)  # no payload mode (the times record may be there)
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

    def test_the_auto_ratio_flag_moves_moderately_compressible_files_and_is_remembered(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        # Half noise, half zeros: deflate keeps about half of it, so the default
        # cut-off (0.9) leaves it in git and a cut-off of 0.3 sends it to payload.
        (self.src / "half.bin").write_bytes(os.urandom(1500) + b"\0" * 1500)
        (self.src / "noise.bin").write_bytes(os.urandom(3000))

        def paid():
            manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
            return {f["path"] for f in manifest["files"]}

        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err = self.run_tool(
                self.src, "--vault", self.vault, "--remote", self.remote, "--payload-min-size", "auto",
            )
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(paid(), {"noise.bin"})  # unchanged default
            self.assertEqual(snap._config(self.vault, "snapshot.payloadratio"), "")

            rc, out, err = self.run_tool(
                self.src, "--vault", self.vault, "--remote", self.remote,
                "--payload-min-size", "auto", "--payload-auto-ratio", "0.3",
            )
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(paid(), {"noise.bin", "half.bin"})
            self.assertEqual(snap._config(self.vault, "snapshot.payloadratio"), "0.3")

            # A bare run keeps the tuned cut-off, as it keeps the payload value.
            (self.src / "extra.txt").write_text("a change, so there is a new snapshot")
            rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--remote", self.remote)
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(paid(), {"noise.bin", "half.bin"})

    # -- the placement report ------------------------------------------------------
    def test_the_survey_records_what_the_auto_rule_saw(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        for name, content in {
            "huge.bin": os.urandom(9000), "noise.bin": os.urandom(3000),
            "half.bin": os.urandom(1500) + b"\0" * 1500, "tiny.bin": b"x" * 100,
        }.items():
            (self.src / name).write_bytes(content)
        survey = snap.new_survey()
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            found = snap.find_payload_files(self.src, [], 8000, True, 0.9, survey)
        self.assertEqual(set(found), {"huge.bin", "noise.bin"})
        self.assertEqual(survey["big"][0], 1)
        self.assertGreaterEqual(survey["small"][0], 1)  # tiny.bin, and whatever the fixture holds
        band = {rel: ratio for _size, ratio, rel in survey["mid"]}
        self.assertEqual(set(band), {"noise.bin", "half.bin"})  # kept and left out alike
        self.assertGreater(band["noise.bin"], 0.9)
        self.assertLess(band["half.bin"], 0.9)

    def test_the_placement_tip_appears_only_when_a_lower_cutoff_clearly_pays(self):
        gib = 1024 ** 3
        band = [(gib, 0.8, f"f{i}") for i in range(4)] + [(gib, 0.2, "c0")]
        survey = {"small": [10, 5 * gib], "big": [1, 9 * gib], "mid": band}
        payload = {"huge": (9 * gib, 0, "100644", 0, 0, 0)}
        with unittest.mock.patch.object(snap, "measure_deflate_mibs", return_value=40.0):
            lines = snap.placement_lines(survey, payload, 0.9, self.src, {})
            self.assertEqual(len(lines), 2, lines)
            self.assertIn("--payload-auto-ratio 0.5", lines[1])
            self.assertIn("assumed", lines[1])  # no measured link yet
            self.assertIn("Not applied", lines[1])
            # measured rates are used, and said to be
            measured = {"snapshot.uplink": ["53"], "snapshot.uplink1": ["32"]}
            self.assertNotIn("assumed", snap.placement_lines(survey, payload, 0.9, self.src, measured)[1])
            # nothing to gain at or below the what-if cut-off, or when the band is small
            self.assertEqual(len(snap.placement_lines(survey, payload, 0.5, self.src, {})), 1)
            small = {**survey, "mid": [(10 * 1024 ** 2, 0.8, "f")]}
            self.assertEqual(len(snap.placement_lines(small, payload, 0.9, self.src, {})), 1)
        # a fast deflate (it beats the link) leaves nothing to suggest
        with unittest.mock.patch.object(snap, "measure_deflate_mibs", return_value=5000.0):
            self.assertEqual(len(snap.placement_lines(survey, payload, 0.9, self.src, {})), 1)

    def test_a_run_reports_its_placement(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        (self.src / "huge.bin").write_bytes(os.urandom(9000))
        (self.src / "half.bin").write_bytes(os.urandom(1500) + b"\0" * 1500)
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err = self.run_tool(
                self.src, "--vault", self.vault, "--remote", self.remote, "--payload-min-size", "auto",
            )
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"placement: 1 file\(s\) \(.*\) go to payload; \d+ \(.*\) stay in git")

    def test_a_remembered_ratio_that_is_not_a_ratio_is_the_default_not_an_error(self):
        vault = self.vault
        snap.ensure_vault(vault, "snapshot")
        for junk in ("", "abc", "0", "-1", "1.5"):
            with self.subTest(junk=junk):
                if junk:
                    _git(vault, "config", "snapshot.payloadratio", junk)
                self.assertEqual(snap.resolve_payload_ratio(None, vault), snap._AUTO_INCOMPRESSIBLE)
        _git(vault, "config", "snapshot.payloadratio", "0.5")
        self.assertEqual(snap.resolve_payload_ratio(None, vault), 0.5)
        self.assertEqual(snap.resolve_payload_ratio(0.7, vault), 0.7)  # typed beats remembered

    def test_a_typed_ratio_with_a_remembered_size_rule_says_so(self):
        with self.assertRaises(snap.SnapshotError) as caught:
            snap.plan_payload("64M", self.remote, self.src, [], ratio=0.5, ratio_typed=True)
        self.assertIn("--payload-auto-ratio", str(caught.exception))
        snap.plan_payload("64M", self.remote, self.src, [], ratio=0.5)  # remembered alone: quiet

    def test_a_bare_flag_means_auto(self):
        rc, out, err = self.run_tool(
            self.src, "--vault", self.vault, "--remote", self.remote, "--payload-min-size"
        )
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload:", out)


class PayloadDefaultAndSafeguardTests(_PayloadFixture):
    """On by default, the conversion warning, safeguards, flags, parallel uploads."""


    def test_payload_mode_is_on_without_any_flag_and_says_so(self):
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload: on by default", out)
        self.assertIn("--no-payload", out)
        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertNotIn("big1.bin", tree)
        self.assertIn(".seafile-payload/manifest.json", tree)
        self.assertEqual(_git(self.vault, "config", "snapshot.payload").strip(), "auto")
        self.assertEqual(self.object_count(), 2)
        # The second run is not "by default" any more: the vault remembered it.
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("on by default", out)

    def test_the_vault_carries_a_note_on_how_to_read_it(self):
        self.assertEqual(self.bare()[0], 0)
        note = _git(self.vault, "show", "snapshot:.seafile-payload/README.txt")
        self.assertIn("--restore", note)
        self.assertIn("manifest.json", note)
        raw = subprocess.run(
            ["git", "-C", str(self.vault), "show", "snapshot:.seafile-payload/README.txt"],
            capture_output=True, check=True,
        ).stdout
        self.assertNotIn(b"\r", raw)       # the same blob on every platform

    def test_no_payload_stages_into_git_and_is_remembered(self):
        rc, out, err = self.bare("--no-payload")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload:", out)
        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertIn("big1.bin", tree)
        self.assertNotIn(".seafile-payload/manifest.json", tree)  # no payload mode (the times record may be there)
        self.assertEqual(self.object_count(), 0)
        self.assertEqual(_git(self.vault, "config", "snapshot.payload").strip(), "0")
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.object_count(), 0)

    def test_a_tree_with_nothing_big_is_snapshotted_as_without_payload_mode(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload:", out)
        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertNotIn(".seafile-payload/manifest.json", tree)  # no payload mode (the times record may be there)
        self.assertFalse(any(k.startswith(self.base) for k in self.server_files()))

    def test_a_remembered_setting_does_not_break_a_local_no_push_run(self):
        # Found by CI: every vault now remembers `auto`, and a --no-push run (no
        # remote to upload to) failed with "payload mode needs a remote".  A
        # remembered value is not a request for this run.
        self.assertEqual(self.bare()[0], 0)
        self.assertEqual(_git(self.vault, "config", "snapshot.payload").strip(), "auto")
        (self.src / "small.txt").write_bytes(b"local only\n")
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--no-push")
        # The payload vault is refused with the actionable message, not "needs a remote".
        self.assertNotEqual(rc, 0)
        self.assertIn("last snapshot used payload mode", out + err)
        self.assertIn("for a local-only snapshot use --no-payload", out + err)
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--no-push", "--no-payload")
        self.assertEqual(rc, 0, out + err)                  # the route the message names
        # A vault that never used payload mode takes the same run without complaint.
        other = self.case / "plain-vault"
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        self.assertEqual(self.bare(vault=other, remote="seafile://testlib/plain")[0], 0)
        self.assertEqual(_git(other, "config", "snapshot.payload").strip(), "auto")
        (self.src / "small.txt").write_bytes(b"local again\n")
        rc, out, err = self.run_tool(self.src, "--vault", other, "--no-push")
        self.assertEqual(rc, 0, out + err)

    def test_the_default_stays_off_where_payload_mode_cannot_work(self):
        # No remote at all: a purely local snapshot.
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--no-push")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("big1.bin", _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot"))
        # --no-push: the explicit flag is an error (see the combinations test), the
        # default just stays off.
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err = self.run_tool(self.src, "--vault", self.case / "v2", "--no-push")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("big1.bin",
                      _git(self.case / "v2", "ls-tree", "-r", "--name-only", "snapshot"))
        # A remote that is not a Seafile library cannot hold a payload.
        bare_remote = self.case / "plain.git"
        _git(self.case, "init", "-q", "--bare", str(bare_remote))
        rc, out, err = self.bare(vault=self.case / "v3", remote=str(bare_remote))
        self.assertEqual(rc, 0, out + err)
        self.assertIn("big1.bin",
                      _git(self.case / "v3", "ls-tree", "-r", "--name-only", "snapshot"))
        # Nothing was remembered from the runs that could not use it.
        for name in ("v2", "v3"):
            probe = subprocess.run(
                ["git", "-C", str(self.case / name), "config", "snapshot.payload"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(probe.returncode, 0, name)

    def test_converting_an_existing_vault_warns_before_anything_is_uploaded(self):
        self._existing_vault_without_payload()
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Notice: payload mode is on by default", out)
        self.assertIn("2 file(s)", out)
        self.assertIn("--no-payload", out)
        self.assertLess(out.index("Notice: payload mode"), out.index("payload: 2 file(s)"))
        # ...and again as the last line, on both streams, where a log ends.
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("Notice: this run moved 2 large"))
        self.assertIn("Notice: this run moved 2 large", err)
        # The warning is said once: the vault now remembers the setting.
        (self.src / "small.txt").write_bytes(b"changed again\n")
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Notice: payload mode", out)

    def test_the_conversion_warning_is_also_in_a_preview_and_changes_nothing(self):
        self._existing_vault_without_payload()
        rc, out, err = self.bare("--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Notice: payload mode", out)
        self.assertIn("would move 2 file(s)", out)
        self.assertEqual(self.server_files(), {})

    def test_a_first_snapshot_and_an_explicit_flag_do_not_get_the_conversion_warning(self):
        rc, out, err = self.bare()                       # nothing to convert yet
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Notice: payload mode", out)
        self.assertIn("payload: on by default", out)
        self._existing_vault_without_payload_other()

    # -- safeguards (PAYLOAD.md D13) ----------------------------------------------
    def test_a_fresh_vault_is_refused_when_the_remote_used_another_rule(self):
        self.assertEqual(self.snapshot()[0], 0)               # explicit size 1000
        rc, out, err = self.bare(vault=self.case / "fresh-vault")
        self.assertNotEqual(rc, 0)
        self.assertIn("--payload-min-size 1000", out + err)
        self.assertIn("auto", out + err)

    def test_every_snapshot_carries_the_tool_version_as_a_trailer(self):
        self.assertEqual(self.snapshot()[0], 0)
        message = _git(self.vault, "log", "-1", "--format=%B", "snapshot")
        self.assertIn(f"{snap.TOOL_TRAILER}: {snap.tool_version()}", message)
        manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
        self.assertEqual(manifest["min_git_remote_seafile"], snap.PAYLOAD_MIN_TOOL)

    def test_a_snapshot_by_a_newer_tool_is_warned_about(self):
        self.assertEqual(self.snapshot()[0], 0)
        with unittest.mock.patch.object(snap, "tool_of_commit", lambda *a: "99.1.0"):
            (self.src / "small.txt").write_bytes(b"newer tool\n")
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("was written by git-remote-seafile 99.1.0", err)

    def test_a_manifest_that_needs_a_newer_tool_is_refused(self):
        with self.assertRaises(snap.SnapshotError) as ctx:
            snap.check_manifest_shape({"format": 2, "min_git_remote_seafile": "99.0.0", "files": []})
        self.assertIn("needs git-remote-seafile 99.0.0", str(ctx.exception))
        snap.check_manifest_shape({"format": 2, "files": []})        # an older manifest is fine

    def test_delete_needs_a_vault_or_an_explicit_no_vault(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.run_tool("--payload-gc", "--remote", self.remote, "--payload-delete")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--payload-delete needs --vault", err)
        rc, out, err = self.run_tool("--payload-gc", "--remote", self.remote,
                                     "--payload-no-vault")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("only applies to --payload-gc --payload-delete", err)

    def test_a_rewritten_history_stops_a_delete_until_it_is_accepted(self):
        self.assertEqual(self.snapshot()[0], 0)
        self.assertEqual(self.gc("--payload-delete", "--payload-grace-days", "1")[0], 0)
        # What the last run saw is a tip the remote no longer contains.
        self.stub.files.setdefault(REPO_ID, {})[f"{self.base}/gc-seen.json"] = json.dumps(
            {"refs/heads/snapshot": "1" * 40}).encode("ascii")
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("history was rewritten", err)
        self.assertIn("--payload-accept-loss", err)
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 1)                        # still refused: nothing was marked
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1",
                               "--payload-accept-loss")
        self.assertEqual(rc, 0, out + err)

    def test_keep_that_expires_snapshots_needs_the_acknowledgement(self):
        for _ in range(2):
            (self.src / "big1.bin").write_bytes(os.urandom(6000))
            self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.gc("--payload-delete", "--payload-keep", "1")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("unrestorable", err)
        rc, out, err = self.gc("--payload-keep", "1")      # a report never needs it
        self.assertEqual(rc, 0, out + err)
        self.assertIn("older snapshot(s) become unrestorable", out)

    def test_keep_does_not_count_snapshots_that_never_used_payload(self):
        # Found on the real server: a snapshot taken before payload mode is whole in
        # git, so expiring it loses nothing and needs no acknowledgement.
        self.assertEqual(self.run_tool(self.src, "--vault", self.vault, "--remote", self.remote,
                                       "--no-payload")[0], 0)
        (self.src / "small.txt").write_bytes(b"second\n")
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.gc("--payload-keep", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("0 older snapshot(s) become unrestorable", out)
        rc, out, err = self.gc("--payload-delete", "--payload-keep", "1")
        self.assertEqual(rc, 0, out + err)

    def test_verify_leaves_a_marker_the_store_line_and_gc_report_show(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.gc()
        self.assertIn("last --payload-verify: never", out)
        rc, out, err = self.run_tool("--payload-verify", "all", "--remote", self.remote)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.gc()
        self.assertIn("last --payload-verify: 0 day(s) ago", out)

    def test_an_upload_that_cannot_fit_the_quota_is_warned_about_first(self):
        with unittest.mock.patch.object(snap, "_account_quota", lambda c: (990, 1000)):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("quota left", err)
        self.assertIn("will probably fail", err)

    def test_a_synced_payload_library_is_warned_about(self):
        fake = [{"repo_id": REPO_ID, "worktree": "C:/sync/lib", "auto_sync": True}]
        with unittest.mock.patch("git_remote_seafile.safety.discover_local_synced_libraries",
                                 lambda: fake):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("library holding the payload is synced", err)

    def test_a_restore_that_cannot_fit_is_refused_before_downloading(self):
        self.assertEqual(self.snapshot()[0], 0)
        free = type("U", (), {"free": 100})()
        with unittest.mock.patch("shutil.disk_usage", lambda p: free):
            rc, out, err, into = self.restore("tight")
        self.assertNotEqual(rc, 0)
        self.assertIn("free space", out + err)
        self.assertIn("--allow-low-disk", out + err)

    def test_the_clone_volume_floor_refuses_before_the_clone(self):
        """The git half of a restore had no space check (the review's E1/D4).

        The payload's need is measured, but the vault's git size is unknowable
        before the transfer, so the destination volume gets a fixed headroom
        floor and the refusal says exactly that.
        """
        self.assertEqual(self.snapshot()[0], 0)
        free = type("U", (), {"free": 100})()

        def no_clone(*a, **kw):
            raise AssertionError("the clone started after the space refusal")

        with unittest.mock.patch("shutil.disk_usage", lambda p: free), \
                unittest.mock.patch.object(snap, "_run", side_effect=no_clone):
            rc, out, err, into = self.restore("tight-clone")
        self.assertNotEqual(rc, 0)
        self.assertIn("headroom", out + err)
        self.assertIn("cannot be known before the transfer", out + err)

    def test_allow_low_disk_tries_the_tight_restore_anyway(self):
        self.assertEqual(self.snapshot()[0], 0)
        free = type("U", (), {"free": 100})()
        with unittest.mock.patch("shutil.disk_usage", lambda p: free):
            rc, out, err, into = self.restore("lowdisk", "--allow-low-disk")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("restored", out)

    def test_an_mtimes_entry_in_a_stream_spelling_is_skipped(self):
        """`.git::$INDEX_ALLOCATION/config` in the times record is remote data.

        It passed the security check as a string and moved `.git/config`'s
        time on a real restore (REVIEW.sonnet-5.5 D10); the Windows-name
        check now skips it like a manifest entry.
        """
        into = self.case / "mt-stream"
        (into / ".git").mkdir(parents=True)
        record = into / ".seafile-payload" / "mtimes.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps(
            {"format": 1, "files": {".git::$INDEX_ALLOCATION/config": 1_600_000_000_000_000_000}}
        ), encoding="utf-8")
        applied, skipped = snap.apply_recorded_mtimes(into)
        self.assertEqual((applied, skipped), (0, 1))

    def test_a_ref_named_like_an_option_is_refused(self):
        """A remote branch called `--detach` reads as git's option (sonnet E3).

        The checkout refuses the name before git sees it, instead of dying
        with "a branch yet to be born" and a partial tree.
        """
        with self.assertRaises(snap.SnapshotError) as caught:
            snap._checkout(self.case, "--detach")
        self.assertIn("reads as a git option", str(caught.exception))

    def test_files_hashed_again_are_explained(self):
        self.assertEqual(self.snapshot()[0], 0)
        target = self.src / "big1.bin"
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        (self.src / "small.txt").write_bytes(b"force a new snapshot\n")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("hashed again because their size, timestamp or inode changed", out)

    def test_jobs_alone_no_longer_needs_the_size_flag(self):
        rc, out, err = self.bare("--payload-jobs", "2")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.object_count(), 2)

    def test_the_store_line_reports_what_the_server_holds(self):
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload store: 2 object(s)", out)
        rc, out, err = self.bare()          # nothing new uploaded: no line
        self.assertNotIn("payload store:", out)

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
            (("--vault", self.vault, "--no-payload", "--payload-jobs", "4"),
             "--payload-jobs only applies when payload mode is on"),
            (("--vault", self.vault, "--no-payload", "--payload-min-size", "1000"),
             "contradict each other"),
            (("--restore", "--remote", self.remote, "--into", self.case / "x",
              "--no-payload"), "does not apply"),
            (("--vault", self.vault, "--payload-delete"), "--payload-delete only applies"),
            (("--vault", self.vault, "--payload-grace-days", "3"), "--payload-grace-days only applies"),
            (("--vault", self.vault, "--payload-auto-ratio", "0"), "above 0 and at most 1"),
            (("--vault", self.vault, "--payload-auto-ratio", "1.5"), "above 0 and at most 1"),
            (("--vault", self.vault, "--payload-min-size", "1000", "--payload-auto-ratio", "0.5"),
             "only applies to the auto rule"),
            (("--vault", self.vault, "--no-payload", "--payload-auto-ratio", "0.5"),
             "only applies to the auto rule"),
            (("--restore", "--remote", self.remote, "--into", self.case / "x",
              "--payload-auto-ratio", "0.5"), "only applies to a snapshot run"),
            (("--payload-verify", "--remote", self.remote, "--payload-auto-ratio", "0.5"),
             "does not apply to --payload-verify"),
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


class PayloadRetentionTests(_PayloadFixture):
    """gc and the second-round findings."""


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

    def test_a_marker_that_does_not_read_back_says_so(self):
        """The upload's 2xx is not evidence the baseline landed (S3 write half)."""
        import contextlib
        import io

        class Fake:
            def mkdir_p(self, rid, base):
                pass

            def upload_file(self, rid, base, name, body):
                return True

            def get_file_text(self, rid, path):
                return "{}"   # anything but what was written

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            snap.write_seen_tips(Fake(), "r", "/base", {"refs/heads/snapshot": "a" * 40})
        self.assertIn("did not read back as what was written", err.getvalue())

    def test_the_manifest_records_the_ratio_that_produced_it(self):
        """The auto cut-off travels beside the rule it belongs to (ROADMAP T6)."""
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
        self.assertEqual(manifest["rule"], "auto")
        self.assertEqual(manifest["ratio"], 0.9)

    def test_a_manifest_ratio_that_is_not_one_is_refused(self):
        """The field feeds the cross-machine guard, so it is validated."""
        dest = self.case / "dest"
        dest.mkdir()
        with self.assertRaises(snap.SnapshotError):
            snap.check_manifest_shape({"format": 2, "files": [], "ratio": 1.5})
        with self.assertRaises(snap.SnapshotError):
            snap.check_manifest_shape({"format": 2, "files": [], "ratio": "0.9"})
        # A genuine value passes.
        snap.check_manifest_shape({"format": 2, "files": [], "ratio": 0.5})

    def test_a_fresh_vault_is_refused_when_the_remote_used_another_ratio(self):
        """The ratio guard beside the rule guard: a default is not a request.

        Two machines whose cut-offs differ move files in the 4-64 MiB band
        between git and the payload on alternate runs (PAYLOAD D13's own row).
        The remote's manifest now carries the ratio, so the guard can see it.
        """
        self.assertEqual(self.bare("--payload-auto-ratio", "0.5")[0], 0)
        manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
        self.assertEqual(manifest["ratio"], 0.5)
        rc, out, err = self.bare(vault=self.case / "fresh-ratio")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("--payload-auto-ratio 0.5", out + err)
        # A vault that remembers its own ratio is not refused: it chose.
        other = self.case / "chose-ratio"
        rc, out, err = self.bare("--payload-auto-ratio", "0.9", vault=other)
        self.assertEqual(rc, 0, out + err)
        # And a fresh vault matching the remote's ratio goes through.
        rc, out, err = self.bare("--payload-auto-ratio", "0.5",
                                 vault=self.case / "matching-ratio")
        self.assertEqual(rc, 0, out + err)

    def test_a_cache_written_by_another_machine_is_not_trusted(self):
        """The cache is per-machine (PAYLOAD D6); the file can be shared.

        An entry's validity depends on the writing machine's clock and its
        probe of the share clock, so a reader on another machine hashes
        everything again and says so (REVIEW.iq3_s D5).
        """
        self.assertEqual(self.snapshot()[0], 0)
        cache_path = self.vault / ".git" / "payload-cache.json"
        cache = json.loads(cache_path.read_text())
        cache["machine"] = "other-machine/0x1a2b"
        cache_path.write_text(json.dumps(cache))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("written by another machine", out + err)
        self.assertEqual(out.count("hashing ["), 2, out)

    def test_a_legacy_flat_cache_is_still_adopted(self):
        """A cache an older tool wrote has no stamp; its entries are kept."""
        for path in (self.src / "big1.bin", self.src / "sub" / "big2.bin"):
            os.utime(path, ns=(OLD_NS, OLD_NS))   # far older than any window
        self.assertEqual(self.snapshot()[0], 0)
        cache_path = self.vault / ".git" / "payload-cache.json"
        cache = json.loads(cache_path.read_text())
        cache_path.write_text(json.dumps(cache["entries"]))   # the pre-0.9.14 shape
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("hashing ["), 0, out)
        self.assertEqual(out.count("written by another machine"), 0)

    def test_the_grace_clock_is_the_server_estimate_not_the_local_wall_clock(self):
        """A local clock jump between the mark and the delete must not delete.

        The wall clock of whichever machine runs the delete decided both ends
        of the grace comparison: a jump of +8 days deleted an object marked
        seconds before (REVIEW.sonnet-5.5 D3, reproduced).  The mark and the
        comparison now run on the server's `Date` estimate, which does not
        jump with either machine.
        """
        self.assertEqual(self.snapshot()[0], 0)
        orphan = self.orphan()
        server_now = {"t": 50_000_000.0}
        with unittest.mock.patch.object(snap, "_grace_now", lambda c: server_now["t"]):
            rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("marked 1 newly unreferenced", out)
        # The mark was written in the server's clock, not the local one.
        pending = json.loads(self.stub.files[REPO_ID][f"{self.base}/gc-pending.json"])
        self.assertEqual(list(pending.values()), [50_000_000.0])
        # Five server-seconds later, while the local wall clock jumps eight
        # days: the object must still be inside its grace period.
        server_now["t"] += 5.0
        local_before = time.time()
        jumped = lambda: local_before + 8 * DAY  # noqa: E731
        with unittest.mock.patch.object(snap, "_grace_now", lambda c: server_now["t"]), \
                unittest.mock.patch("time.time", jumped):
            rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(orphan, self.server_files(),
                      "the local clock jump reached the grace comparison")

    def test_a_report_run_seeds_the_fence_baseline(self):
        """The first delete run had nothing to compare against (ROADMAP S2).

        A force push before anyone had ever deleted was invisible; a report
        run that read the remote's history now records the tips it saw.
        """
        self.assertEqual(self.snapshot()[0], 0)
        seen_key = f"{self.base}/gc-seen.json"
        self.assertNotIn(seen_key, self.server_files())
        rc, out, err = self.gc()   # report only: no --payload-delete, no --vault
        self.assertEqual(rc, 0, out + err)
        self.assertIn("fence baseline", out)
        seeded = json.loads(self.stub.files[REPO_ID][seen_key])
        self.assertIn("refs/heads/snapshot", seeded)
        # The weekly vault-only report must not rewrite the baseline.
        before = self.stub.files[REPO_ID][seen_key]
        _git(self.vault, "config", "snapshot.payloadgcreport", str(time.time() - 8 * 86400))
        (self.src / "small.txt").write_bytes(b"force a snapshot\n")
        self.assertEqual(self.snapshot()[0], 0)
        self.assertEqual(self.stub.files[REPO_ID][seen_key], before,
                         "the vault-only report rewrote the fence baseline")

    def test_an_unreadable_fence_baseline_stops_the_delete(self):
        """`gc-seen.json` that is there but unparseable is an error (S3).

        Reading it as "{}" erased the fence, and the run then wrote a fresh
        baseline from the rewritten tips, hiding the rewrite permanently.
        """
        self.assertEqual(self.snapshot()[0], 0)
        orphan = self.orphan()
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 0, out + err)   # the delete run marks and writes the baseline
        self.stub.files[REPO_ID][f"{self.base}/gc-seen.json"] = b"not json"
        self.backdate_pending(2)
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("gc-seen.json is unreadable", out + err)
        self.assertIn("Remove the file", out + err)
        self.assertIn(orphan, self.server_files(), "nothing may be deleted without the fence")

    def test_a_push_that_lands_while_gc_is_deciding_stops_the_deletion(self):
        self.assertEqual(self.snapshot()[0], 0)
        orphan = self.orphan()
        self.assertEqual(self.gc("--payload-delete", "--payload-grace-days", "1")[0], 0)
        self.backdate_pending(2)
        real = snap._remote_tips

        def moved(remote):
            tips = dict(real(remote))
            tips["refs/heads/snapshot"] = "f" * 40  # somebody pushed since the history was read
            return tips

        with unittest.mock.patch.object(snap, "_remote_tips", moved):
            rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("nothing was deleted", out + err)
        self.assertIn(orphan, self.server_files())

    def test_an_object_deleted_between_the_check_and_the_push_is_put_back(self):
        """A retention run on another machine removes an object after this run
        verified it and before the ref landed; the snapshot just pushed would
        name nothing.  One more look after the push re-uploads it."""
        self.assertEqual(self.snapshot()[0], 0)
        victim = self.object_key(self.big1)
        (self.src / "note.txt").write_bytes(b"a change so there is something to push\n")
        real_push = snap.push_snapshot

        def push_then_lose_an_object(*args, **kwargs):
            result = real_push(*args, **kwargs)
            self.stub.files[REPO_ID].pop(victim, None)
            return result

        with unittest.mock.patch.object(snap, "push_snapshot", push_then_lose_an_object):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("went missing during the push", out)
        self.assertIn(victim, self.server_files())

    def test_a_push_that_lands_during_the_deletion_loop_stops_it(self):
        """The refs are read once before the loop and again every few objects:
        a snapshot pushed mid-loop used to be noticed by nobody."""
        self.assertEqual(self.snapshot()[0], 0)
        keys = [self.orphan(os.urandom(2500 + i)) for i in range(30)]
        self.assertEqual(self.gc("--payload-delete", "--payload-grace-days", "1")[0], 0)
        self.backdate_pending(2)
        real = snap._remote_tips
        calls = {"n": 0}

        def moved_after_the_first_look(remote):
            calls["n"] += 1
            tips = dict(real(remote))
            if calls["n"] > 1:
                tips["refs/heads/snapshot"] = "f" * 40
            return tips

        with unittest.mock.patch.object(snap, "_remote_tips", moved_after_the_first_look):
            rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("stopped after", out + err)
        left = [k for k in keys if k in self.server_files()]
        self.assertTrue(left, "the loop did not stop")
        self.assertLess(len(left), len(keys), "nothing was deleted before the recheck")
        # What was not deleted stays marked, so the next run decides it afresh.
        self.assertIn(f"{self.base}/gc-pending.json", self.server_files())

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
        part = f"{self.objects}/ab/{'c' * 64}.bak"   # looks like an object, is not one
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

    # -- round 2: findings of the second review round ---------------------------
    def test_a_retried_chunk_cannot_hide_an_edit_between_attempts(self):
        # The uploader reads the file again on a retry; the digest of the first
        # attempt must not vouch for what the second attempt sent.
        original = SeafileClient.upload_file
        target = self.src / "big1.bin"
        stat = target.stat()
        altered = bytes(b ^ 0x33 for b in self.big1)
        state = {"done": False}

        def read_edit_then_upload(client, rid, parent, name, content, *a, **kw):
            if not state["done"] and name.startswith(_sha(self.big1)):
                state["done"] = True
                content.read()               # the first attempt sends everything...
                target.write_bytes(altered)  # ...the file moves before the retry...
                os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                content.seek(0)              # ...and the retry re-reads it from the start
            return original(client, rid, parent, name, content, *a, **kw)

        with unittest.mock.patch.object(SeafileClient, "upload_file", read_edit_then_upload):
            rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertNotEqual(rc, 0)
        self.assertIn("changed while it was being uploaded", out + err)
        self.assertNotIn(self.object_key(self.big1), self.server_files())

    def test_a_same_size_edit_with_the_mtime_put_back_cannot_land_under_the_old_name(self):
        """The one case the stat check cannot see, so the guard is the hash of
        the bytes actually sent.  Mutation check: with `clean = True` in
        `_upload_all` the earlier tests all stayed green, because they mock the
        stat check or truncate the file instead."""
        original = SeafileClient.upload_file
        target = self.src / "big1.bin"
        stat = target.stat()
        altered = bytes(b ^ 0x55 for b in self.big1)
        state = {"done": False}

        def edit_then_read(client, rid, parent, name, content, *a, **kw):
            if not state["done"] and name.startswith(_sha(self.big1)):
                state["done"] = True
                # after the stat check, before the first read
                target.write_bytes(altered)
                os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return original(client, rid, parent, name, content, *a, **kw)

        with unittest.mock.patch.object(SeafileClient, "upload_file", edit_then_read):
            rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("changed while it was being uploaded", out + err)
        self.assertNotIn(self.object_key(self.big1), self.server_files())

    def test_a_file_that_shrinks_mid_upload_is_refused(self):
        original = SeafileClient.upload_file
        target = self.src / "big1.bin"
        state = {"done": False}

        def truncate_then_upload(client, rid, parent, name, content, *a, **kw):
            if not state["done"] and name.startswith(_sha(self.big1)):
                state["done"] = True
                os.truncate(target, len(self.big1) // 2)
            return original(client, rid, parent, name, content, *a, **kw)

        with unittest.mock.patch.object(SeafileClient, "upload_file", truncate_then_upload):
            rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertNotEqual(rc, 0)
        self.assertNotIn(self.object_key(self.big1), self.server_files())

    def test_an_upload_the_listing_does_not_confirm_is_an_error(self):
        # D5 step 4: a rename that reports success but loses the object.
        def lossy_rename(client, rid, path, new_name):
            self.stub.files[REPO_ID].pop(path, None)
            return True

        with unittest.mock.patch.object(SeafileClient, "rename_entry", lossy_rename):
            rc, out, err = self.snapshot("--payload-jobs", "1")
        self.assertNotEqual(rc, 0)
        self.assertIn("missing or the wrong size", out + err)

    def test_a_lying_listing_is_a_message_not_a_traceback(self):
        self.assertEqual(self.snapshot()[0], 0)
        real = SeafileClient.list_dir

        def liar(client, rid, path="/"):
            entries = real(client, rid, path)
            if "/payload/objects/" in path:
                for entry in entries:
                    entry["size"] = None
            return entries

        with unittest.mock.patch.object(SeafileClient, "list_dir", liar):
            rc_gc, out_gc, err_gc = self.gc("--payload-grace-days", "1")
        self.assertNotIn("Traceback", err_gc)
        self.assertEqual(rc_gc, 0, out_gc + err_gc)

    def test_gc_protects_a_snapshot_reachable_only_from_a_local_tag(self):
        self.assertEqual(self.snapshot()[0], 0)
        s1_commit = _git(self.vault, "rev-parse", "snapshot").strip()
        s2 = os.urandom(6000)
        (self.src / "big1.bin").write_bytes(s2)
        with unittest.mock.patch.object(snap, "push_snapshot", side_effect=snap.SnapshotError("down")):
            self.assertNotEqual(self.snapshot()[0], 0)
        _git(self.vault, "tag", "keep", "snapshot")
        _git(self.vault, "update-ref", "refs/heads/snapshot", s1_commit)  # the branch moves back
        key = self.object_key(s2)
        for _ in range(2):
            self.gc("--payload-delete", "--payload-grace-days", "1", "--vault", self.vault)
            if f"{self.base}/gc-pending.json" in self.server_files():
                self.backdate_pending(2)
        self.assertIn(key, self.server_files(), "an object named only from a tag was collected")

    def test_gc_refuses_a_manifest_that_is_not_json(self):
        self.assertEqual(self.snapshot()[0], 0)
        gd = str(self.vault / ".git")
        env = dict(os.environ, GIT_INDEX_FILE=str(self.vault / ".git" / "tmp-index"),
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")

        def plumbing(*args):
            return subprocess.run(["git", f"--git-dir={gd}", *args], env=env, capture_output=True,
                                  check=True).stdout.decode().strip()

        blob = subprocess.run(["git", f"--git-dir={gd}", "hash-object", "-w", "--stdin"],
                              input=b"{not json", capture_output=True, check=True).stdout.decode().strip()
        plumbing("read-tree", "snapshot")
        plumbing("update-index", "--add", "--cacheinfo", f"100644,{blob},.seafile-payload/manifest.json")
        commit = plumbing("commit-tree", plumbing("write-tree"), "-p", "snapshot", "-m", "garbled")
        plumbing("update-ref", "refs/heads/snapshot", commit)
        orphan = self.orphan()
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1", "--vault", self.vault)
        self.assertNotEqual(rc, 0)
        self.assertIn("unreadable", out + err)
        self.assertIn(orphan, self.server_files())

    def test_gc_collects_the_temporaries_of_interrupted_uploads_after_the_grace_period(self):
        self.assertEqual(self.snapshot()[0], 0)
        part = f"{self.objects}/cc/{'c' * 64}.part-1234"   # a temporary lives beside its object
        self.stub.files[REPO_ID][part] = b"half an object"
        self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertIn(part, self.server_files(), "marked first, not deleted")
        self.backdate_pending(2)
        rc, out, err = self.gc("--payload-delete", "--payload-grace-days", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(part, self.server_files())
        self.assertEqual(self.object_count(), 2)

    def test_restore_sets_the_recorded_mode_and_mtime(self):
        target = self.src / "big1.bin"
        os.utime(target, ns=(OLD_NS, OLD_NS))
        if os.name != "nt":
            os.chmod(target, 0o755)
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err, into = self.restore("meta")
        self.assertEqual(rc, 0, out + err)
        restored = into / "big1.bin"
        self.assertLess(abs(restored.stat().st_mtime_ns - OLD_NS), 200)  # NTFS: 100 ns ticks
        if os.name != "nt":
            self.assertTrue(restored.stat().st_mode & 0o100, "executable bit was not restored")

    def test_a_flagless_run_is_refused_before_the_code_repository_is_pushed(self):
        self.assertEqual(self.snapshot()[0], 0)
        with self.assertRaises(snap.SnapshotError):
            snap.refuse_flagless_payload_vault(self.vault / ".git", self.src, "snapshot", None)
        snap.refuse_flagless_payload_vault(self.vault / ".git", self.src, "snapshot", 1000)

    def test_the_reserved_directory_in_any_spelling_is_refused(self):
        for spelling in (".seafile-payload./x", ".seafile-payload /x", ".SEAFILE-PAYLOAD/x"):
            with self.subTest(spelling=spelling):
                self.assertIsNotNone(snap.path_security_problem(spelling))

    def test_a_name_another_writer_just_stored_is_not_deleted_by_a_collision(self):
        class Fake:
            def __init__(self):
                self.deleted = []

            def rename_entry(self, rid, path, new):
                return False  # the name is taken

            def list_dir(self, rid, directory):
                return [{"name": "h" * 64, "size": 10}]

            def delete_entry(self, rid, path):
                self.deleted.append(path)
                return True

        fake = Fake()
        self.assertTrue(snap._commit_object(fake, "r", "/d", "h" * 64 + ".part-1", "h" * 64, 10))
        self.assertEqual(fake.deleted, ["/d/" + "h" * 64 + ".part-1"])

    def test_a_rename_onto_a_taken_name_makes_a_copy_and_it_is_judged(self):
        """The server's rename is create-or-duplicate (measured on CE 13.0.28).

        The old code returned True on the rename's 200 and never looked: a
        damaged wrong-size object under <sha> then blocked every later snapshot
        that needed the content, each retry leaving another full-size copy
        (REVIEW.sonnet-5.5 E7, reproduced on the real server; the stub models
        the duplicate since 0.9.13).
        """
        sha = "h" * 64

        class Fake:
            def __init__(self):
                self.deleted = []
                self.files: dict[str, bytes] = {sha: b"stale"}

            def rename_entry(self, rid, path, new):
                # Create-or-duplicate on leaf names, like the server's API.
                src = path.rsplit("/", 1)[-1]
                if src not in self.files:
                    return False
                if new in self.files:
                    self.files[f"{new} (1)"] = self.files.pop(src)
                    return True
                self.files[new] = self.files.pop(src)
                return True

            def list_dir(self, rid, directory):
                return [
                    {"name": name, "size": len(body)} for name, body in self.files.items()
                ]

            def delete_entry(self, rid, path):
                leaf = path.rsplit("/", 1)[-1]
                self.deleted.append(leaf)
                self.files.pop(leaf, None)
                return True

        # Wrong-size object under the hash name: replaced by the good one.
        wrong = Fake()
        wrong.files[f"{sha}.part-1"] = b"good-bytes-here"
        ok = snap._commit_object(wrong, "r", "/d", f"{sha}.part-1", sha, 15)
        self.assertTrue(ok)
        self.assertEqual(wrong.files.get(sha), b"good-bytes-here")
        self.assertNotIn(f"{sha} (1)", wrong.files, "the duplicate copy must be swept")

        # Right-size object with different planted bytes: left alone and the
        # copy dropped -- S17's missing scenario (nothing would have failed if
        # this branch overwrote an object that was already correct).
        right = Fake()
        right.files[sha] = b"x" * 15
        right.files[f"{sha}.part-1"] = b"y" * 15
        ok = snap._commit_object(right, "r", "/d", f"{sha}.part-1", sha, 15)
        self.assertTrue(ok)
        self.assertEqual(right.files.get(sha), b"x" * 15, "a correct object is never replaced")
        self.assertNotIn(f"{sha} (1)", right.files)
        self.assertNotIn(sha, right.deleted, "the right-sized object was not deleted")

    def test_a_damaged_object_no_longer_blocks_snapshots_through_the_stub(self):
        """End to end against the duplicate-modelling stub: the R3 carve-out.

        Before the fix the run failed with "missing or the wrong size" on
        every later run, each retry adding a full-size <sha> (N) copy.
        """
        self.assertEqual(self.snapshot()[0], 0)
        victim = self.object_key(self.big1)
        self.stub.files[REPO_ID][victim] = self.stub.files[REPO_ID][victim][:3]
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        prefix = "/".join(victim.split("/")[:-1])
        dups = [
            k for k in self.server_files()
            if k.startswith(prefix + "/") and "(1)" in k.rsplit("/", 1)[-1]
        ]
        self.assertEqual(dups, [], f"duplicate copies were left: {dups}")
        self.assertEqual(
            len(self.stub.files[REPO_ID][victim]), len(self.big1),
            "the object under the hash name is the right size again",
        )


class PayloadRememberVerifyViewAndClockTests(_PayloadFixture):
    """The remembered setting, verify, views, the clock domain, the report."""

    def test_status_does_not_call_every_git_file_new_when_the_snapshot_has_no_mtimes(self):
        """A payload snapshot taken with --no-record-mtimes has a manifest but no
        baseline for the git-stored files; the report must say so instead of
        counting every one of them as new."""
        self.assertEqual(self.snapshot("--no-record-mtimes")[0], 0)
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--status")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("0 new", out)
        self.assertIn("could not be compared", out)
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--status", "--json")
        status = json.loads(out)["result"]["status"]
        self.assertGreater(status["uncompared"], 0)
        self.assertEqual(status["new"], 0)

    # -- the remembered setting and the force-rehash switch ---------------------
    def test_payload_rehash_ignores_the_cache_for_one_run(self):
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        self._tiles()
        self.assertEqual(self.snapshot()[0], 0)
        self.age_cache_entries(10)      # old enough to be trusted
        rc, out, err = self.snapshot()
        self.assertEqual(out.count("hashing ["), 0, out)
        rc, out, err = self.snapshot("--payload-rehash")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("hashing ["), 3, out)

    def test_a_bare_run_keeps_using_what_the_vault_remembered(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--remote", self.remote)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload: 2 file(s)", out)
        self.assertIn("uploaded 0 object(s)", out)

    def test_leaving_payload_mode_deliberately_is_remembered_too(self):
        self.assertEqual(self.snapshot()[0], 0)
        base = (self.src, "--vault", self.vault, "--remote", self.remote)
        rc, out, err = self.run_tool(*base, "--payload-min-size", "0")
        self.assertEqual(rc, 0, out + err)
        tree = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertIn("big1.bin", tree)            # now an ordinary file in git
        self.assertNotIn(".seafile-payload/manifest.json", tree)  # no payload mode (the times record may be there)
        rc, out, err = self.run_tool(*base)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload:", out)

    def test_a_preview_does_not_set_the_remembered_flag(self):
        self.assertEqual(self.snapshot("--dry-run")[0], 0)
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--remote", self.remote)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload:", out)
        self.assertIn("big1.bin", _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot"))

    def test_payload_keep_lets_older_snapshots_expire(self):
        versions = [self.big1]
        self.assertEqual(self.snapshot()[0], 0)
        for _ in range(2):
            versions.append(os.urandom(6000))
            (self.src / "big1.bin").write_bytes(versions[-1])
            self.assertEqual(self.snapshot()[0], 0)
        # Without --payload-keep every snapshot's objects are referenced.
        rc, out, err = self.gc("--payload-grace-days", "1")
        self.assertIn("unused : 0 object(s)", out)
        args = ("--payload-delete", "--payload-grace-days", "1", "--payload-keep", "1",
                "--payload-accept-loss")
        self.assertEqual(self.gc(*args)[0], 0)
        self.backdate_pending(2)
        rc, out, err = self.gc(*args)
        self.assertEqual(rc, 0, out + err)
        files = self.server_files()
        self.assertNotIn(self.object_key(versions[0]), files)
        self.assertNotIn(self.object_key(versions[1]), files)
        self.assertIn(self.object_key(versions[2]), files)
        self.assertIn(self.object_key(self.big2), files)   # shared with the newest: kept
        rc, out, err, into = self.restore("newest")
        self.assertEqual(rc, 0, out + err)
        rc, out, err, _ = self.restore("expired", "--ref", "snapshot~2")
        self.assertNotEqual(rc, 0)
        self.assertIn("not found", out + err)

    def test_a_tag_is_a_pin_whatever_payload_keep_says(self):
        """D13: a tagged snapshot's objects live, however many snapshots follow it.

        ``--payload-keep N`` counts the newest N commits reachable from each ref,
        and a tag always reaches at least its own commit.  Pushed, so the remote
        history the delete decides from holds it.
        """
        versions = [self.big1]
        self.assertEqual(self.snapshot()[0], 0)
        _git(self.vault, "tag", "keep-this")
        for _ in range(3):
            versions.append(os.urandom(6000))
            (self.src / "big1.bin").write_bytes(versions[-1])
            self.assertEqual(self.snapshot()[0], 0)
        # the tag has to be on the remote for the remote's history to hold it
        proc = subprocess.run(
            ["git", "-C", str(self.vault), "push", "-q", self.remote, "refs/tags/keep-this"],
            env={**os.environ, **self.env}, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = ("--payload-delete", "--payload-grace-days", "1", "--payload-keep", "1",
                "--payload-accept-loss")
        self.assertEqual(self.gc(*args)[0], 0)
        self.backdate_pending(2)
        rc, out, err = self.gc(*args)
        self.assertEqual(rc, 0, out + err)
        files = self.server_files()
        self.assertIn(self.object_key(versions[0]), files, "a tagged snapshot's object was deleted")
        self.assertNotIn(self.object_key(versions[1]), files)
        self.assertNotIn(self.object_key(versions[2]), files)
        self.assertIn(self.object_key(versions[3]), files)

    def test_payload_keep_only_applies_to_gc_and_must_be_positive(self):
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--payload-keep", "2")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--payload-keep only applies", err)
        rc, out, err = self.gc("--payload-keep", "0")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("at least 1", err)

    def test_payload_verify_downloads_and_checks_the_objects(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.run_tool("--payload-verify", "all", "--remote", self.remote)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("verified 2 object(s)", out)
        # Damage one object without changing its size, and a missing one.
        self.stub.files[REPO_ID][self.object_key(self.big1)] = bytes(len(self.big1))
        rc, out, err = self.run_tool("--payload-verify", "all", "--remote", self.remote)
        self.assertNotEqual(rc, 0)
        self.assertIn("1 of 2 sampled object(s) are damaged", out + err)
        self.assertIn(_sha(self.big1), out + err)
        del self.stub.files[REPO_ID][self.object_key(self.big2)]
        rc, out, err = self.run_tool("--payload-verify", "all", "--remote", self.remote)
        self.assertIn("2 of 2", out + err)

    def test_payload_verify_samples_and_validates_its_argument(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.run_tool("--remote", self.remote, "--payload-verify", "1")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("verifying 1 of 2", out)
        rc, out, err = self.run_tool("--remote", self.remote, "--payload-verify", "lots")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("count or 'all'", err)

    def test_a_view_has_real_names_and_moves_no_data(self):
        self.assertEqual(self.snapshot()[0], 0)
        before = {k for k in self.server_files() if k.startswith(self.objects)}
        rc, out, err = self.view("--payload-view-name", "current")
        self.assertEqual(rc, 0, out + err)
        files = self.server_files()
        self.assertEqual(files["/pvault-view/current/big1.bin"], self.big1)
        self.assertEqual(files["/pvault-view/current/sub/big2.bin"], self.big2)
        self.assertEqual({k for k in files if k.startswith(self.objects)}, before)
        self.assertFalse(any(k.startswith("/pvault-view/current/") and len(k.rsplit("/", 1)[-1]) == 64
                             for k in files), "an object name was left in the view")

    def test_a_view_of_an_older_snapshot_shows_the_older_bytes(self):
        self.assertEqual(self.snapshot()[0], 0)
        (self.src / "big1.bin").write_bytes(os.urandom(6000))
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.run_tool("--payload-view=snapshot~1", "--remote", self.remote,
                                     "--payload-view-name", "old")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.server_files()["/pvault-view/old/big1.bin"], self.big1)

    def test_an_existing_view_is_refused_unless_replaced(self):
        self.assertEqual(self.snapshot()[0], 0)
        self.assertEqual(self.view("--payload-view-name", "current")[0], 0)
        rc, out, err = self.view("--payload-view-name", "current")
        self.assertNotEqual(rc, 0)
        self.assertIn("already exists", out + err)
        (self.src / "big1.bin").write_bytes(os.urandom(6000))
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.view("--payload-view-name", "current", "--payload-view-replace")
        self.assertEqual(rc, 0, out + err)
        self.assertNotEqual(self.server_files()["/pvault-view/current/big1.bin"], self.big1)

    def test_identical_files_in_one_directory_are_both_named(self):
        (self.src / "twin.bin").write_bytes(self.big1)
        self.assertEqual(self.snapshot()[0], 0)
        self.assertEqual(self.view("--payload-view-name", "v")[0], 0)
        files = self.server_files()
        self.assertEqual(files["/pvault-view/v/big1.bin"], self.big1)
        self.assertEqual(files["/pvault-view/v/twin.bin"], self.big1)

    @unittest.skipIf(os.name != "nt", "needs a filesystem that allows names over 255 bytes")
    def test_a_view_reports_what_the_server_cannot_name(self):
        long_name = "\u0e01" * 86 + ".bin"        # 262 bytes of UTF-8
        (self.src / long_name).write_bytes(os.urandom(3000))
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.view("--payload-view-name", "v")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("1 file(s) were left out", out)
        self.assertIn("cannot be held", out)

    def test_a_view_is_refused_when_objects_are_missing(self):
        self.assertEqual(self.snapshot()[0], 0)
        del self.stub.files[REPO_ID][self.object_key(self.big2)]
        rc, out, err = self.view("--payload-view-name", "v")
        self.assertNotEqual(rc, 0)
        self.assertIn("missing from the server", out + err)
        self.assertFalse(any(k.startswith("/pvault-view/") for k in self.server_files()))

    def test_view_flags_only_apply_to_a_view(self):
        rc, out, err = self.run_tool(self.src, "--vault", self.vault, "--payload-view-name", "x")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--payload-view-name only applies", err)
        rc, out, err = self.run_tool("--payload-view", "--payload-view-name", "a/b", "--remote", self.remote)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("plain directory name", err)

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
            "--payload-gc", "--remote", other, "--payload-grace-days", "1", "--payload-delete",
            "--payload-no-vault",
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("found no payload manifest", out + err)
        self.assertEqual(
            len([k for k in self.server_files() if k.startswith("/otherrepo/payload/objects")]), 2
        )

    def test_a_share_whose_clock_is_behind_does_not_vouch_for_a_fresh_edit(self):
        # The share's clock is an hour behind, so a file edited this instant carries
        # an mtime an hour "old" by this machine's clock: the unmodified rule trusted
        # it, and a same-size edit within one granule kept its stale hash.
        hour = 3600 * 1_000_000_000
        path, cands, entry = self._cache_case(behind_ns=hour + 500_000_000)
        manifest, _ = snap.build_manifest(
            cands, self.case, {"racy-unit.bin": dict(entry)}, 1024, quiet=True,
            clock_offset_ns=-hour,
        )
        self.assertNotEqual(manifest["files"][0]["sha256"], "a" * 64)  # rehashed
        # Without the correction the same entry is (wrongly) trusted: the guard has
        # a reason to exist.
        manifest, _ = snap.build_manifest(
            cands, self.case, {"racy-unit.bin": dict(entry)}, 1024, quiet=True,
        )
        self.assertEqual(manifest["files"][0]["sha256"], "a" * 64)

    def test_a_hash_made_in_the_shares_clock_is_trusted_once_it_is_old_enough(self):
        hour = 3600 * 1_000_000_000
        _path, cands, entry = self._cache_case(behind_ns=hour + 10 * 1_000_000_000)
        entry["at"] -= hour
        entry["fs"] = True
        manifest, cache = snap.build_manifest(
            cands, self.case, {"racy-unit.bin": dict(entry)}, 1024, quiet=True,
            clock_offset_ns=-hour,
        )
        self.assertEqual(manifest["files"][0]["sha256"], "a" * 64)
        self.assertTrue(cache["racy-unit.bin"]["fs"])

    def test_the_clock_is_probed_only_on_a_network_filesystem(self):
        calls = []

        def probe(_dir):
            calls.append(1)
            return time.time_ns() - 7200 * 1_000_000_000

        self.assertEqual(
            snap.fs_clock_offset_ns(self.case, is_network=lambda _p: False, probe=probe), 0
        )
        self.assertEqual(calls, [])
        off = snap.fs_clock_offset_ns(self.case, is_network=lambda _p: True, probe=probe)
        self.assertLess(abs(off + 7200 * 1_000_000_000), 2_000_000_000)
        near = snap.fs_clock_offset_ns(
            self.case, is_network=lambda _p: True, probe=lambda _d: time.time_ns() + 1_000_000_000
        )
        self.assertEqual(near, 0)  # a second is a round trip, not a skewed clock
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            unknown = snap.fs_clock_offset_ns(
                self.case, is_network=lambda _p: True, probe=lambda _d: None
            )
        self.assertEqual(unknown, 0)
        self.assertIn("could not be measured", buf.getvalue())

    def test_the_probe_file_is_removed_and_leaves_the_directory_as_it_was(self):
        before = sorted(p.name for p in self.src.iterdir())
        stamp = snap._probe_fs_clock(self.src)
        self.assertIsNotNone(stamp)
        self.assertEqual(sorted(p.name for p in self.src.iterdir()), before)

    def test_a_local_source_is_never_written_to_by_the_clock_probe(self):
        with unittest.mock.patch.object(snap, "_probe_fs_clock") as probe:
            rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        probe.assert_not_called()

    def test_macos_mount_output_identifies_a_network_volume(self):
        mount = (
            "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
            "//tk@nas.local/media on /Volumes/media (smbfs, nodev, nosuid, mounted by tk)\n"
            "nas:/export/data on /Volumes/data (nfs, nodev, nosuid)\n"
            "/dev/disk4s2 on /Volumes/USB DRIVE (msdos, local, nodev, nosuid)\n"
        )
        net = snap._darwin_mount_is_network
        self.assertTrue(net("/Volumes/media/projects/x", mount))
        self.assertTrue(net("/Volumes/data", mount))
        self.assertFalse(net("/Users/tk/code", mount))
        self.assertFalse(net("/Volumes/USB DRIVE/x", mount))

    def test_a_push_to_seafile_records_the_times_of_git_stored_files_and_a_restore_applies_them(self):
        want = 1_600_000_000 * 1_000_000_000 + 123_456_700
        os.utime(self.src / "small.txt", ns=(want, want))
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        listing = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertIn(".seafile-payload/mtimes.json", listing)
        record = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/mtimes.json"))
        self.assertEqual(record["files"]["small.txt"], want)
        # payload files carry theirs in the manifest, not here
        self.assertNotIn("big1.bin", record["files"])
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000),                 unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err, into = self.restore("mtimes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "small.txt").stat().st_mtime_ns, want)

    def test_a_git_file_named_like_the_download_temp_file_survives_the_restore(self):
        # big1.bin is payload (ignored by git); big1.bin.part is an ordinary file
        # that git restores beside it.  The download used to stage as
        # "<name>.part" and so overwrote and then renamed away the real file.
        decoy = b"a real file that happens to end in .part\n"
        (self.src / "big1.bin.part").write_bytes(decoy)
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        with unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err, into = self.restore("decoy")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "big1.bin.part").read_bytes(), decoy)
        self.assertEqual((into / "big1.bin").read_bytes(), self.big1)
        self.assertFalse((into / ".git" / "grs-restore").exists())

    def test_a_share_a_few_seconds_behind_is_not_mistaken_for_noise(self):
        def behind(seconds):
            return lambda _d: time.time_ns() - int(seconds * 1_000_000_000)

        off = snap.fs_clock_offset_ns(self.case, is_network=lambda _p: True, probe=behind(4))
        self.assertLess(off, -3_000_000_000, "a 4 s skew was swallowed as noise")
        ahead = snap.fs_clock_offset_ns(
            self.case, is_network=lambda _p: True, probe=lambda _d: time.time_ns() + 3_000_000_000
        )
        self.assertEqual(ahead, 0)  # ahead is the safe side: it only costs a rehash

    def test_names_hfs_ignores_cannot_smuggle_a_dot_git(self):
        ignorable = chr(0x200C)
        for rel in (f".git{ignorable}/hooks/pre-commit", f".git/{ignorable}hooks/x",
                    f".seafile-payload{ignorable}/x"):
            self.assertIsNotNone(snap.path_security_problem(rel), rel)
        self.assertIsNone(snap.path_security_problem("docs/notes.txt"))

    def test_the_routine_report_reads_the_local_vault_not_the_remote(self):
        seen = []
        real = snap._hashes_in_history

        def spy(remote, vault, *a, **kw):
            seen.append(remote)
            return real(remote, vault, *a, **kw)

        self.assertEqual(self.bare()[0], 0)   # starts the clock
        _git(self.vault, "config", "snapshot.payloadgcreport", str(time.time() - 8 * 86400))
        (self.src / "big1.bin").write_bytes(bytes(b ^ 0x01 for b in self.big1))
        with unittest.mock.patch.object(snap, "_hashes_in_history", spy):
            rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload store report", out)
        self.assertEqual(seen, [None], "the report downloaded the remote's history")

    def test_an_interrupt_during_the_report_keeps_what_the_run_earned(self):
        with unittest.mock.patch.object(snap, "maybe_report_payload_gc", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.bare()
        self.assertEqual(_git(self.vault, "config", "snapshot.payload").strip(), "auto")
        # remember_settings (the remote, the branch) runs before the report too
        self.assertEqual(_git(self.vault, "config", "snapshot.remote").strip(), self.remote)

    def test_report_days_is_refused_where_it_cannot_apply(self):
        base = (self.src, "--vault", self.vault, "--remote", self.remote, "--payload-report-days", "3")
        for extra, text in (
            (("--no-payload",), "only applies when payload mode is on"),
            (("--dry-run",), "does not apply to --dry-run"),
        ):
            rc, out, err = self.run_tool(*base, *extra)
            self.assertEqual(rc, 1, out + err)
            self.assertIn(text, err)
        rc, out, err = self.run_tool(
            self.src, "--vault", self.vault, "--remote", str(self.case / "plain.git"),
            "--payload-report-days", "3",
        )
        self.assertEqual(rc, 1, out + err)
        self.assertIn("seafile:// remote", err)
        rc, out, err = self.run_tool("--payload-gc", "--remote", self.remote, "--no-embedded-repos")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--no-embedded-repos only applies to a snapshot run", err)

    def test_a_preview_does_not_write_a_probe_file_into_the_source(self):
        with unittest.mock.patch.object(snap, "fs_clock_offset_ns", return_value=0) as offset:
            rc, out, err = self.bare("--dry-run")
        self.assertEqual(rc, 0, out + err)
        offset.assert_not_called()

    # -- the hash cache key and window (R4 round) --------------------------------
    def _trusted(self, *, cached, now, window=None, root_dev=None, offset=0):
        """Whether build_manifest reuses the cached hash for one 1 KiB file.

        ``cached`` overrides fields of the cache entry; ``now`` is the candidate
        tuple.  A trusted entry keeps the cached sha ("a" * 64); a rehash makes
        the real one.
        """
        path = self.case / "cache-unit.bin"
        path.write_bytes(b"x" * 1024)
        hashed_at = 1_700_000_000 * 1_000_000_000
        entry = {"size": 1024, "mtime_ns": now[1], "ino": now[3], "sha256": "a" * 64,
                 "at": hashed_at, "ctime": now[5], "dev": now[4]}
        entry.update(cached)
        kwargs = {"clock_offset_ns": offset, "root_dev": root_dev}
        if window is not None:
            kwargs["racy_window_ns"] = window
        manifest, _ = snap.build_manifest(
            {"cache-unit.bin": now}, self.case, {"cache-unit.bin": entry}, 1024, quiet=True, **kwargs
        )
        return manifest["files"][0]["sha256"] == "a" * 64

    OLD = 1_600_000_000 * 1_000_000_000 + 5_000_000  # well before the hash time, not a whole second

    def test_an_edit_that_puts_the_old_mtime_back_is_still_a_new_file(self):
        now = (1024, self.OLD, "100644", 7, 3, 111)
        self.assertTrue(self._trusted(cached={}, now=now))
        # same size, same mtime, same inode -- but the inode change time moved
        self.assertFalse(self._trusted(cached={"ctime": 99}, now=now))

    def test_the_change_time_in_the_key_moves_on_a_real_in_place_edit(self):
        """Not a synthetic tuple: a file on this filesystem, rewritten in place
        with the same size, the old mtime put back.  On NTFS ``st_ctime`` is the
        creation time and does not move; the key must use the change time."""
        target = self.case / "inplace.bin"
        target.write_bytes(b"A" * 4096)
        before = snap.find_payload_files(self.case, [], 1024)["inplace.bin"]
        st = target.stat()
        time.sleep(0.05)
        with open(target, "r+b") as fh:
            fh.write(b"B" * 4096)
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        after = snap.find_payload_files(self.case, [], 1024)["inplace.bin"]
        self.assertEqual(before[:5], after[:5], "size, mtime, mode, inode, device must all be unchanged")
        self.assertNotEqual(before[5], after[5], "the change time did not move")

    def test_an_entry_written_before_ctime_and_device_were_keyed_is_adopted_not_rehashed(self):
        now = (1024, self.OLD, "100644", 7, 3, 111)
        path = self.case / "cache-unit.bin"
        path.write_bytes(b"x" * 1024)
        legacy = {"size": 1024, "mtime_ns": self.OLD, "ino": 7, "sha256": "a" * 64,
                  "at": 1_700_000_000 * 1_000_000_000}
        manifest, fresh = snap.build_manifest(
            {"cache-unit.bin": now}, self.case, {"cache-unit.bin": legacy}, 1024, quiet=True
        )
        self.assertEqual(manifest["files"][0]["sha256"], "a" * 64)
        self.assertEqual((fresh["cache-unit.bin"]["ctime"], fresh["cache-unit.bin"]["dev"]), (111, 3))

    def test_a_whole_second_mtime_is_trusted_once_it_is_old_enough(self):
        whole = 1_600_000_000 * 1_000_000_000
        now = (1024, whole, "100644", 7, 3, 111)
        self.assertTrue(self._trusted(cached={}, now=now))
        # ...but only with twice the margin: 3 s before the hash is inside it
        near = 1_700_000_000 * 1_000_000_000 - 3_000_000_000
        self.assertFalse(self._trusted(cached={"mtime_ns": near}, now=(1024, near, "100644", 7, 3, 111)))

    def test_an_unprobed_share_is_trusted_only_for_files_an_hour_older_than_their_hash(self):
        hashed_at = 1_700_000_000 * 1_000_000_000
        recent = hashed_at - 600 * 1_000_000_000 + 5_000_000  # 10 minutes before the hash
        now = (1024, recent, "100644", 7, 3, 111)
        self.assertTrue(self._trusted(cached={"mtime_ns": recent}, now=now))
        self.assertFalse(
            self._trusted(cached={"mtime_ns": recent}, now=now, window=snap._UNPROBED_WINDOW_NS)
        )
        self.assertTrue(self._trusted(cached={}, now=(1024, self.OLD, "100644", 7, 3, 111),
                                      window=snap._UNPROBED_WINDOW_NS))

    def test_a_file_on_another_device_than_the_source_gets_the_wide_window(self):
        hashed_at = 1_700_000_000 * 1_000_000_000
        recent = hashed_at - 600 * 1_000_000_000 + 5_000_000
        on_other = (1024, recent, "100644", 7, 9, 111)
        self.assertFalse(self._trusted(cached={"mtime_ns": recent, "dev": 9}, now=on_other, root_dev=3))
        self.assertTrue(self._trusted(cached={"mtime_ns": recent, "dev": 9}, now=on_other, root_dev=9))

    def test_the_gc_history_fetch_asks_the_helper_for_a_strict_listing(self):
        seen = []

        def fake_git(repo, work_tree, args, **kw):
            if args and args[0] == "fetch":
                seen.append(os.environ.get("SEAFILE_STRICT_REFS"))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        os.environ.pop("SEAFILE_STRICT_REFS", None)
        with unittest.mock.patch.object(snap, "git", fake_git):
            snap._hashes_in_history("seafile://lib/x", None)
        self.assertEqual(seen, ["1"])
        self.assertNotIn("SEAFILE_STRICT_REFS", os.environ)

    def test_a_failed_payload_restore_leaves_no_scratch_directory(self):
        self.assertEqual(self.bare()[0], 0)
        with unittest.mock.patch.object(
            snap, "_download_verified", side_effect=snap.SnapshotError("boom")
        ), unittest.mock.patch.object(snap, "_AUTO_UPPER", 8000), \
                unittest.mock.patch.object(snap, "_AUTO_LOWER", 2000):
            rc, out, err, into = self.restore("scratch-gone")
        self.assertNotEqual(rc, 0)
        self.assertFalse((into / ".git" / "grs-restore").exists())

    def test_a_probe_file_a_killed_run_left_behind_is_not_captured(self):
        (self.src / ".grs-clock-probe-4242-1").write_bytes(b"")
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        listing = _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot")
        self.assertNotIn(".grs-clock-probe", listing)
        self.assertIn("small.txt", listing)

    def test_the_probe_removes_old_debris_but_not_a_fresh_file(self):
        old = self.src / ".grs-clock-probe-1-1"
        fresh = self.src / ".grs-clock-probe-2-2"
        old.write_bytes(b"")
        fresh.write_bytes(b"")
        long_ago = time.time() - 7 * 24 * 3600
        os.utime(old, (long_ago, long_ago))
        self.assertIsNotNone(snap._probe_fs_clock(self.src))
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_an_unmeasurable_clock_is_said_once_per_vault(self):
        git_dir = self.vault / ".git"
        self.vault.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(self.vault)], check=True, capture_output=True)

        def unmeasurable(source, *, note=print, **kw):
            note("note: the source is on a network share and its clock could not be measured")
            return 0, True

        out = io.StringIO()
        with unittest.mock.patch.object(snap, "fs_clock_probe", unmeasurable), \
                contextlib.redirect_stdout(out):
            snap._clock_offset_once_noted(git_dir, self.src)
            snap._clock_offset_once_noted(git_dir, self.src)
        self.assertEqual(out.getvalue().count("could not be measured"), 1)

    def test_restore_paths_are_extended_on_windows_and_untouched_elsewhere(self):
        long_dir = self.case.joinpath(*(["d" * 40] * 8))
        got = snap.extended_path(long_dir / "f.bin")
        if os.name == "nt":
            self.assertTrue(str(got).startswith(chr(92) * 2 + "?" + chr(92)))
            self.assertEqual(snap.extended_path(got), got)  # idempotent
            self.assertEqual(snap.extended_path(pathlib.Path("rel") / "x"), pathlib.Path("rel") / "x")
        else:
            self.assertEqual(got, long_dir / "f.bin")

    def test_the_vault_and_the_restored_clone_allow_long_paths(self):
        self.assertEqual(self.bare()[0], 0)
        self.assertEqual(_git(self.vault, "config", "core.longpaths").strip(), "true")
        self.assertEqual(_git(self.vault, "config", "core.quotepath").strip(), "false")
        rc, out, err, into = self.restore("longpaths")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(_git(into, "config", "core.longpaths").strip(), "true")
        self.assertEqual(_git(into, "config", "core.quotepath").strip(), "false")

    def test_the_payload_store_is_reported_once_a_week_and_never_changed(self):
        # The first run of a vault only starts the clock: what a report would
        # say is what that run just printed.
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload store report", out)
        self.assertTrue(_git(self.vault, "config", "snapshot.payloadgcreport").strip())
        before = dict(self.server_files())
        _git(self.vault, "config", "snapshot.payloadgcreport", str(time.time() - 8 * 86400))
        (self.src / "big1.bin").write_bytes(bytes(b ^ 0x01 for b in self.big1))
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload store report (report only", out)
        self.assertIn("unused :", out)
        # report-only: nothing it wrote to the server is gc state
        added = {k for k in self.server_files() if k not in before}
        self.assertFalse(any("pending" in k or "gc-" in k.lower() for k in added), added)
        # and it is not due again
        (self.src / "big1.bin").write_bytes(bytes(b ^ 0x02 for b in self.big1))
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload store report", out)

    def test_the_report_can_be_turned_off_and_its_flag_is_validated(self):
        rc, out, err = self.bare("--payload-report-days", "0")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("payload store report", out)
        rc, out, err = self.bare("--payload-report-days", "-1")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--payload-report-days must be 0 or more", err)
        rc, out, err = self.run_tool(
            self.src, "--vault", self.vault, "--no-push", "--payload-report-days", "3"
        )
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--payload-report-days only applies", err)

    def test_a_failing_report_never_fails_the_run_that_pushed(self):
        self.assertEqual(self.bare()[0], 0)   # starts the clock
        aged = str(int(time.time() - 8 * 86400))
        _git(self.vault, "config", "snapshot.payloadgcreport", aged)
        (self.src / "big1.bin").write_bytes(bytes(b ^ 0x01 for b in self.big1))
        with unittest.mock.patch.object(snap, "payload_gc", side_effect=snap.SnapshotError("boom")):
            rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("payload report skipped: boom", out)
        # a failed report is retried next time: the stamp is still the aged one
        self.assertEqual(_git(self.vault, "config", "snapshot.payloadgcreport").strip(), aged)


class PayloadSkipChangingTests(_PayloadFixture):
    """One payload file that is always being written must not stop every snapshot."""

    def _live(self, name: str):
        """A stat check that says ``name`` changed while it was read (a live file)."""
        real = snap._unchanged

        def check(source, rel, size, mtime_ns):
            return False if rel == name else real(source, rel, size, mtime_ns)

        return unittest.mock.patch.object(snap, "_unchanged", check)

    def _manifest(self) -> dict:
        return json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))

    def test_a_live_file_stops_the_run_by_default_and_the_error_names_the_way_out(self):
        with self._live("big1.bin"):
            rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("changed while the run was in progress", out + err)
        self.assertIn("--payload-skip-changing", out + err)

    def test_skip_changing_snapshots_everything_else_and_names_the_file(self):
        with self._live("big1.bin"):
            rc, out, err = self.snapshot("--payload-skip-changing")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Warning: 1 payload file(s) changed while they were being read", out)
        self.assertIn("big1.bin", out)
        self.assertIn("are NOT in this snapshot", out)
        manifest = self._manifest()
        self.assertEqual({f["path"] for f in manifest["files"]}, {"sub/big2.bin"})
        self.assertEqual(manifest["snapshot"], snap.manifest_snapshot_id(manifest["files"]))
        files = self.server_files()
        self.assertEqual(files[self.object_key(self.big2)], self.big2)
        self.assertNotIn(self.object_key(self.big1), files, "the live file was never named")
        self.assertIn("small.txt", _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot"))
        self.assertNotIn("big1.bin", _git(self.vault, "ls-tree", "-r", "--name-only", "snapshot"))
        # the snapshot restores, without the file it said it left out
        rc, out, err, into = self.restore("restored")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((into / "sub" / "big2.bin").read_bytes(), self.big2)
        self.assertFalse((into / "big1.bin").exists())

    def test_the_next_quiet_run_picks_the_file_up_again(self):
        with self._live("big1.bin"):
            self.assertEqual(self.snapshot("--payload-skip-changing")[0], 0)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(
            {f["path"] for f in self._manifest()["files"]}, {"big1.bin", "sub/big2.bin"}
        )

    def test_skip_changing_does_not_combine_with_the_other_modes(self):
        rc, out, err = self.run_tool(
            "--restore", "--remote", self.remote, "--into", self.case / "x", "--payload-skip-changing"
        )
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--payload-skip-changing only applies to a snapshot run", err)


class PayloadTuningTests(_PayloadFixture):
    """A new vault measures this machine and its link once and remembers the cut-off."""

    def setUp(self):
        super().setUp()
        (self.src / "big1.bin").unlink()
        (self.src / "sub" / "big2.bin").unlink()
        # ~0.6 under a fast deflate: in git at the default cut-off, payload at 0.5
        (self.src / "half.bin").write_bytes(os.urandom(1800) + b"\0" * 1200)
        self.addCleanup(unittest.mock.patch.stopall)
        for name, value in (("_AUTO_UPPER", 8000), ("_AUTO_LOWER", 2000),
                            ("_WHATIF_MIN_BYTES", 1000), ("_PROBE_BYTES", 65536)):
            unittest.mock.patch.object(snap, name, value).start()
        # Deflate far slower than any link the probe can measure on loopback, so the
        # break-even is below the floor however loaded the machine running the suite is
        # (at 20 MiB/s a busy runner's 5 MiB/s upload put it at 0.52, not 0.50).
        unittest.mock.patch.object(snap, "measure_deflate_mibs", return_value=1.0).start()

    def paid(self) -> set[str]:
        manifest = json.loads(_git(self.vault, "show", "snapshot:.seafile-payload/manifest.json"))
        return {f["path"] for f in manifest["files"]}

    def setting(self, key: str) -> str:
        return snap._config(self.vault, key)

    def no_probe(self):
        return unittest.mock.patch.object(
            snap, "probe_link", side_effect=AssertionError("the link was probed"))

    def test_the_cutoff_is_where_payload_and_git_take_equally_long_within_bounds(self):
        # fast link, slow deflate: payload wins at any ratio, so the floor
        self.assertEqual(snap.tuned_ratio(20.0, 500.0, 300.0), 0.5)
        # a slow link with a fast deflate: compressing in git pays, so the ceiling
        self.assertEqual(snap.tuned_ratio(500.0, 0.5, 0.5), 0.9)
        # in between it is the break-even itself
        middle = snap.tuned_ratio(88.0, 2.0, 1.2)
        self.assertGreater(middle, 0.5)
        self.assertLess(middle, 0.9)
        self.assertAlmostEqual(middle, 1.2 * (1 / 2.0 + 1 / 2000.0 - 1 / 88.0), places=6)

    def test_the_ratio_argument_is_a_ratio_or_measure(self):
        self.assertEqual(snap.parse_ratio_argument("measure"), "measure")
        self.assertEqual(snap.parse_ratio_argument("0.5"), 0.5)
        with self.assertRaises(argparse.ArgumentTypeError):
            snap.parse_ratio_argument("fast")

    def test_the_probe_measures_the_link_and_leaves_nothing_behind(self):
        with unittest.mock.patch.dict(os.environ, self.env):
            result = snap.probe_link(self.remote)
        self.assertIsNotNone(result)
        self.assertGreater(result["single"], 0)
        self.assertGreater(result["upload"], 0)
        self.assertFalse(result["in_use"])
        self.assertEqual(self.object_count(), 0, self.stored_names())

    def test_a_new_vault_measures_once_and_remembers(self):
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("tuned: cut-off 0.50", out)
        self.assertEqual(self.paid(), {"half.bin"})  # the 0.6 file moved to payload
        self.assertEqual(self.setting("snapshot.payloadratio"), "0.5")
        self.assertEqual(self.setting("snapshot.payloadratiosource"), "measured")
        self.assertGreater(float(self.setting("snapshot.uplink")), 0)
        self.assertGreater(float(self.setting("snapshot.uplink1")), 0)
        self.assertNotIn("assumed", out)  # the tip would have used the rates just measured
        # the next run remembers: no probe, same placement
        (self.src / "small.txt").write_bytes(b"changed\n")
        with self.no_probe():
            rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("tuned:", out)
        self.assertEqual(self.paid(), {"half.bin"})

    def test_a_slow_measured_link_is_remembered_as_measured_not_as_zero(self):
        """The rates were stored with one decimal, so a probe slower than
        0.05 MiB/s (a loaded runner, a bad connection) was kept as "0.0", read
        back as unusable and replaced by the assumed 53 MiB/s: the opposite of
        what was measured.  It also made the test above fail on a busy machine."""
        snap.ensure_vault(self.vault, "snapshot")
        snap.remember_tuning(
            self.vault, {"ratio": 0.5, "upload": 0.0312, "single": 0.0187}, dry_run=False
        )
        self.assertGreater(float(self.setting("snapshot.uplink")), 0)
        self.assertGreater(float(self.setting("snapshot.uplink1")), 0)
        upload, single, assumed = snap.link_rates(snap._read_local_config_map(self.vault))
        self.assertAlmostEqual(upload, 0.0312, places=4)
        self.assertAlmostEqual(single, 0.0187, places=4)
        self.assertFalse(assumed, "a measured rate is not an assumed one")
        # ordinary rates keep their short form
        snap.remember_tuning(
            self.vault, {"ratio": 0.5, "upload": 53.21, "single": 32.0}, dry_run=False
        )
        self.assertEqual(self.setting("snapshot.uplink"), "53.21")
        self.assertEqual(self.setting("snapshot.uplink1"), "32")

    def test_an_established_vault_is_never_tuned_on_its_own(self):
        snap.ensure_vault(self.vault, "snapshot")
        _git(self.vault, "config", "snapshot.payload", "auto")
        with self.no_probe():
            rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("tuned:", out)
        self.assertNotIn("half.bin", self.paid_or_empty())

    def paid_or_empty(self) -> set[str]:
        try:
            return self.paid()
        except subprocess.CalledProcessError:
            return set()

    def test_a_remote_that_already_holds_payload_keeps_its_cutoff(self):
        self.orphan()  # another machine's object: the store is in use
        rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("already holds payload objects", out)
        self.assertEqual(self.setting("snapshot.payloadratio"), "")
        self.assertNotIn("half.bin", self.paid_or_empty())
        self.assertEqual([n for n in self.stored_names() if ".part-" in n], [])  # nothing uploaded

    def test_a_probe_that_fails_leaves_the_default_and_the_run_succeeds(self):
        with unittest.mock.patch.object(snap, "probe_link", return_value=None):
            rc, out, err = self.bare()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("could not measure the link", out)
        self.assertEqual(self.setting("snapshot.payloadratio"), "")

    def test_measure_asks_again_even_for_an_established_vault_and_a_shared_remote(self):
        snap.ensure_vault(self.vault, "snapshot")
        _git(self.vault, "config", "snapshot.payload", "auto")
        _git(self.vault, "config", "snapshot.payloadratio", "0.9")
        self.orphan()
        rc, out, err = self.bare("--payload-auto-ratio", "measure")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("tuned: cut-off 0.50", out)
        self.assertEqual(self.setting("snapshot.payloadratio"), "0.5")

    def test_a_typed_ratio_is_not_overridden(self):
        with self.no_probe():
            rc, out, err = self.bare("--payload-auto-ratio", "0.9")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.setting("snapshot.payloadratio"), "0.9")
        self.assertEqual(self.setting("snapshot.payloadratiosource"), "typed")

    def test_a_dry_run_never_touches_the_server_to_measure(self):
        with self.no_probe():
            rc, out, err = self.bare("--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("tuned:", out)


if __name__ == "__main__":
    unittest.main()
