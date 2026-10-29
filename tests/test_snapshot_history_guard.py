"""test_snapshot_history_guard.py - retention refuses a history with holes (ROADMAP V7).

``--payload-gc --payload-delete`` deletes whatever no manifest in the history names.  A vault
read in place that is shallow, partial or missing an object would make live payload objects look
unreferenced, so it is refused before anything is decided.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402

SHA = "a" * 64


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


class TestIncompleteHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-history-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        git(self.vault, "init", "-q", "-b", "snapshot")
        git(self.vault, "config", "user.email", "a@b")
        git(self.vault, "config", "user.name", "n")
        (self.vault / "a.txt").write_text("before payload mode\n")
        git(self.vault, "add", "a.txt")
        git(self.vault, "commit", "-qm", "old snapshot, no manifest")
        (self.vault / ".seafile-payload").mkdir()
        manifest = {"format": 2, "files": [{"path": "big.bin", "size": 5, "sha256": SHA,
                                          "mode": "100644", "mtime_ns": 1}]}
        (self.vault / ".seafile-payload" / "manifest.json").write_text(json.dumps(manifest))
        git(self.vault, "add", ".")
        git(self.vault, "commit", "-qm", "snapshot with a manifest")

    def read(self, vault=None):
        return snap._hashes_in_history(None, vault or self.vault)

    def test_a_complete_vault_is_read_and_a_commit_without_a_manifest_is_not_a_hole(self):
        history = self.read()
        self.assertEqual(list(history.hashes), [SHA])

    def test_a_vault_that_lost_a_manifest_blob_is_refused(self):
        blob = git(self.vault, "rev-parse", "HEAD:.seafile-payload/manifest.json").stdout.strip()
        loose = self.vault / ".git" / "objects" / blob[:2] / blob[2:]
        os.chmod(loose, 0o666)
        loose.unlink()
        with self.assertRaises(snap.SnapshotError) as ctx:
            self.read()
        self.assertIn("the history to read is incomplete", str(ctx.exception))
        self.assertIn("not in its object store", str(ctx.exception))

    def test_a_shallow_vault_is_refused(self):
        shallow = self.tmp / "shallow"
        git(self.tmp, "clone", "-q", "--depth", "1", self.vault.as_uri(), str(shallow))
        with self.assertRaises(snap.SnapshotError) as ctx:
            self.read(shallow)
        self.assertIn("it is a shallow repository", str(ctx.exception))

    def test_a_partial_clone_is_refused(self):
        git(self.vault, "config", "remote.origin.promisor", "true")
        with self.assertRaises(snap.SnapshotError) as ctx:
            self.read()
        self.assertIn("it is a partial clone", str(ctx.exception))
        self.assertIn("Use a full clone of the vault", str(ctx.exception))

    def test_the_guard_is_quiet_for_a_scratch_repository_fetched_from_a_remote(self):
        # walk=False is the remote path: git's own connectivity check covers it.
        snap.require_complete_history(self.vault / ".git", walk=False)


if __name__ == "__main__":
    unittest.main()
