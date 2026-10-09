"""test_gc.py - Remote compaction: fail-closed downloads, fencing, ref mirroring."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from git_remote_seafile.gc import compact_repository, describe_size_delta

class TestRemoteGC(unittest.TestCase):
    def test_a_growth_is_not_reported_as_a_saving(self):
        """Compaction can enlarge a repository, and the message must not lie.

        Repacking can produce a slightly larger pack when the existing packs
        were already tight.  The delta is signed because that is the honest
        value, but printing it raw produced ``(saved -12 KB)`` -- which reads
        like a bug and calls a growth a saving.
        """
        self.assertEqual(describe_size_delta(100), "saved ~100 KB")

        grown = describe_size_delta(-12)
        self.assertIn("grew", grown)
        self.assertIn("12", grown)
        self.assertNotIn("-12", grown)

    def test_an_unchanged_size_is_not_called_a_growth(self):
        self.assertEqual(describe_size_delta(0), "saved ~0 KB")

    def test_gc_skipped_below_threshold(self):
        mock_client = MagicMock()
        mock_client.list_dir.return_value = [{"name": "pack-1.pack"}]

        res = compact_repository(mock_client, "repo1", "/path", min_packs=3, verbose=False)
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["count"], 1)

    @patch("subprocess.run")
    def test_gc_compaction_flow(self, mock_subprocess):
        mock_client = MagicMock()
        # list_dir must answer per path: the compactor reads both the pack
        # directory and the ref namespace, and it now refuses to run at all when
        # no refs can be read -- an empty ref listing is exactly what a failed
        # request looks like, and compacting on that guess destroys history.
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        mock_client.get_file_bytes.return_value = b"PACK-DATA"
        # renew()'s takeover fence reads the ticket back before rewriting it,
        # so the client must serve the lock payloads it stored; every other
        # read is a ref file ("sha-main").
        lock_payloads: list[str] = []
        ticket_paths: list[str] = []

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if ".git-lock" in filename or ".git-lock.d" in parent:
                lock_payloads.append(content.decode("utf-8"))
                ticket_paths.append(f"{parent.rstrip('/')}/{filename}")
            return True

        mock_client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            if ".git-lock" in path:
                # Serve the stored ticket payload once one exists; before that
                # (including acquire's orphan-mirror cleanup probe) there is
                # nothing on the remote.
                return lock_payloads[-1] if lock_payloads else None
            return "sha-main"

        mock_client.get_file_text.side_effect = fake_get_text

        # Mock git pack-objects writing a packfile
        def fake_subprocess(cmd, **kwargs):
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"abcdef1234567890abcdef1234567890abcdef12\n")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake_subprocess

        with patch("pathlib.Path.glob") as mock_glob:
            p_pack = MagicMock()
            p_pack.name = "pack-abcdef1234567890abcdef1234567890abcdef12.pack"
            p_pack.read_bytes.return_value = b"NEW-PACK"
            p_pack.stat.return_value.st_size = 8

            p_idx = MagicMock()
            p_idx.name = "pack-abcdef1234567890abcdef1234567890abcdef12.idx"
            p_idx.read_bytes.return_value = b"NEW-IDX"
            p_idx.stat.return_value.st_size = 7

            mock_glob.return_value = [p_pack, p_idx]

            res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["old_packs"], 2)
            # Every pack download renews the lease as it goes: gc holds the
            # lock across multi-gigabyte transfers.
            for call in mock_client.download_file_to.call_args_list:
                self.assertIsNotNone(call.kwargs.get("progress_callback"))
            # Name what was deleted rather than counting it: a bare
            # "call_count == 5" passes just as happily if gc removed the wrong
            # five things.
            deleted = [call.args[1] for call in mock_client.delete_entry.call_args_list]
            self.assertEqual(
                sorted(p for p in deleted if ".git-lock.d" not in p),
                [
                    "/path/objects/pack/pack-1.idx",
                    "/path/objects/pack/pack-1.pack",
                    "/path/objects/pack/pack-2.idx",
                    "/path/objects/pack/pack-2.pack",
                ],
                "exactly the two obsolete packs and their indexes, and nothing else",
            )
            # ...plus the holder's own ticket, which the lock releases last.
            self.assertTrue(ticket_paths, "the holder's ticket was never uploaded")
            self.assertEqual(
                sorted(set(p for p in deleted if ".git-lock.d" in p)),
                sorted(set(ticket_paths)),
            )

    @patch("subprocess.run")
    def test_gc_compaction_aborts_on_failed_pack_download(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}, {"name": "pack-3.pack"}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        def fake_get_file_bytes(repo_id, path):
            if "pack-1.pack" in path:
                return b""
            return b"PACK-DATA"

        mock_client.get_file_bytes.side_effect = fake_get_file_bytes
        del mock_client.download_file_to
        mock_client.get_file_text.return_value = "sha-main"

        def fake_subprocess(cmd, **kwargs):
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"abcdef1234567890abcdef1234567890abcdef12\n")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake_subprocess

        res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("Failed to download pack-1.pack", res["message"])
        # Ensure no remote pack files were deleted when download failed (N-2 fail-closed)
        deleted_paths = [call.args[1] for call in mock_client.delete_entry.call_args_list if len(call.args) > 1]
        self.assertFalse(any(p.endswith(".pack") for p in deleted_paths))

    @patch("subprocess.run")
    def test_gc_compaction_aborts_on_pack_size_mismatch(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack", "size": 1000}, {"name": "pack-2.pack", "size": 500}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        # Server returns fewer bytes than reported in list_dir
        mock_client.get_file_bytes.return_value = b"TRUNCATED"
        del mock_client.download_file_to

        res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("does not match expected size", res["message"])
        deleted_paths = [call.args[1] for call in mock_client.delete_entry.call_args_list if len(call.args) > 1]
        self.assertFalse(any(p.endswith(".pack") for p in deleted_paths))

    @patch("subprocess.run")
    def test_gc_compaction_aborts_when_pack_exceeds_memory_limit(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack", "size": 20 * 1024 * 1024}, {"name": "pack-2.pack", "size": 500}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        del mock_client.download_file_to

        res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("exceeds in-memory buffer limit", res["message"])
        # get_file_bytes must NOT be called for 20MB file
        mock_client.get_file_bytes.assert_not_called()
        deleted_paths = [call.args[1] for call in mock_client.delete_entry.call_args_list if len(call.args) > 1]
        self.assertFalse(any(p.endswith(".pack") for p in deleted_paths))

    @patch("subprocess.run")
    def test_gc_preserves_all_branches_and_tags(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}, {"type": "file", "name": "dev"}, {"type": "file", "name": "feature"}] if "refs/heads" in path
            else [{"type": "file", "name": "v1.0"}, {"type": "file", "name": "v2.0"}] if "refs/tags" in path
            else []
        )
        mock_client.get_file_bytes.return_value = b"PACK-DATA"
        # Same lock-payload plumbing as test_gc_compaction_flow: the takeover
        # fence in renew() reads the ticket back before rewriting it.
        lock_payloads: list[str] = []

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if ".git-lock" in filename or ".git-lock.d" in parent:
                lock_payloads.append(content.decode("utf-8"))
            return True

        mock_client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            if ".git-lock" in path and lock_payloads:
                return lock_payloads[-1]
            return "sha000111"

        mock_client.get_file_text.side_effect = fake_get_text

        def fake_subprocess(cmd, **kwargs):
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"sha123\n")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake_subprocess

        with patch("pathlib.Path.glob") as mock_glob:
            p_pack = MagicMock()
            p_pack.name = "pack-sha123.pack"
            p_pack.read_bytes.return_value = b"NEW-PACK"
            p_pack.stat.return_value.st_size = 10
            mock_glob.return_value = [p_pack]

            res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
            self.assertEqual(res["status"], "ok")

            # Check that repack was called
            repack_called = any("repack" in cmd for cmd, _ in [(c.args[0], c) for c in mock_subprocess.call_args_list if c.args])
            self.assertTrue(repack_called)

    @patch("git_remote_seafile.gc.subprocess.run")
    def test_gc_aborts_and_preserves_packs_if_lock_lost_before_cleanup(self, mock_subprocess):
        """GC must verify lock ownership before deleting old remote packs and abort if lost (N-4)."""
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        mock_client.get_file_bytes.return_value = b"PACK-DATA"
        mock_client.get_file_text.return_value = "sha000111"
        mock_subprocess.return_value = MagicMock(returncode=0)

        with patch("pathlib.Path.glob") as mock_glob, patch("git_remote_seafile.gc.RemoteLock") as mock_lock_cls:
            p_pack = MagicMock()
            p_pack.name = "pack-sha123.pack"
            p_pack.stat.return_value.st_size = 10
            mock_glob.return_value = [p_pack]

            mock_lock = MagicMock()
            mock_lock.__enter__.return_value = mock_lock
            # Verify ownership fails right before step 7
            mock_lock.verify_ownership.return_value = False
            mock_lock_cls.return_value = mock_lock

            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)

            self.assertIn("refusing to delete obsolete packfiles", str(ctx.exception))
            # Remote packs must NOT be deleted!
            mock_client.delete_entry.assert_not_called()

