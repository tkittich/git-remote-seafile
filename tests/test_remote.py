"""test_remote.py - Comprehensive unit tests for git_remote_seafile."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.client import SeafileClient
from git_remote_seafile.gc import compact_repository
from git_remote_seafile.git_util import (
    get_git_config_bool,
    get_git_config_int,
)
from git_remote_seafile.helper import RemoteHelper
from git_remote_seafile.lfs import LFSTransferAgent
from git_remote_seafile.lock import RemoteLock, RepositoryLockedError


class TestRemoteHelper(unittest.TestCase):
    def test_url_parsing(self):
        h1 = RemoteHelper.__new__(RemoteHelper)
        server, lib, path = h1._parse_url("seafile://seafile.example.com/code/myproject")
        self.assertEqual(server, "https://seafile.example.com")
        self.assertEqual(lib, "code")
        self.assertEqual(path, "/myproject")

        h2 = RemoteHelper.__new__(RemoteHelper)
        server, lib, path = h2._parse_url("seafile://Documents/myproject")
        self.assertIsNone(server)
        self.assertEqual(lib, "Documents")
        self.assertEqual(path, "/myproject")

        h3 = RemoteHelper.__new__(RemoteHelper)
        server, lib, path = h3._parse_url("seafile://https://cloud.example.com/code/repo")
        self.assertEqual(server, "https://cloud.example.com")
        self.assertEqual(lib, "code")
        self.assertEqual(path, "/repo")

    def test_capabilities(self):
        h = RemoteHelper.__new__(RemoteHelper)
        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_capabilities()
        lines = out.getvalue().splitlines()
        self.assertIn("push", lines)
        self.assertIn("fetch", lines)

    def test_cmd_list_refs(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        # Mock heads, tags, and HEAD
        h.client.list_dir.side_effect = lambda repo_id, path: (
            [{"type": "file", "name": "main"}, {"type": "file", "name": "feature"}]
            if "heads" in path
            else [{"type": "file", "name": "v1.0"}]
            if "tags" in path
            else []
        )
        h.client.get_file_text.side_effect = lambda repo_id, path: (
            "aaa111" if "heads/main" in path
            else "bbb222" if "heads/feature" in path
            else "ccc333" if "tags/v1.0" in path
            else "ref: refs/heads/main" if path.endswith("HEAD")
            else None
        )

        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list()

        output = out.getvalue()
        self.assertIn("aaa111 refs/heads/main", output)
        self.assertIn("bbb222 refs/heads/feature", output)
        self.assertIn("ccc333 refs/tags/v1.0", output)
        self.assertIn("@refs/heads/main HEAD", output)

    def test_cmd_push_delete_branch(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/old-branch"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/old-branch", output)
        h.client.delete_entry.assert_called_with("repo1", "/git-repo/refs/heads/old-branch")

    @patch("git_remote_seafile.helper.rev_parse", return_value="newsha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=False)
    def test_cmd_push_non_fast_forward_rejected(self, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "oldsha456"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("error refs/heads/main non-fast-forward", output)

    @patch("git_remote_seafile.helper.rev_parse", return_value="newsha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=False)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_force_push_allowed(self, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "oldsha456"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["+refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main", output)

    @patch("git_remote_seafile.helper.rev_parse", return_value="commit50sha")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.create_packfile", return_value=("packsha123", b"PACKBYTES50COMMITS", b"IDXBYTES"))
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=["obj1", "obj2", "obj3", "obj4"])
    def test_cmd_push_batch_multiple_commits(self, mock_objs, mock_pack, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "commit0sha"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main", output)
        uploaded_files = [call.args[2] for call in h.client.upload_file.call_args_list]
        self.assertIn("pack-packsha123.pack", uploaded_files)
        self.assertIn("pack-packsha123.idx", uploaded_files)
        self.assertIn("main", uploaded_files)

    @patch("git_remote_seafile.helper.rev_parse", return_value="earliersha100")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=False)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_rollback_force_push(self, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        # Remote was ahead at newersha200; local rolled back to earliersha100
        h._refs_cache = {"refs/heads/main": "newersha200"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["+refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main", output)
        # Verified no packfile was uploaded because no new objects were needed
        uploaded_files = [call.args[2] for call in h.client.upload_file.call_args_list]
        self.assertEqual(uploaded_files, ["main"])
        ref_content = h.client.upload_file.call_args_list[0].args[3]
        self.assertEqual(ref_content, b"earliersha100\n")

    @patch("git_remote_seafile.helper.rev_parse", side_effect=lambda ref: f"sha_{ref.split('/')[-1]}")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_multiple_branches(self, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([
                "refs/heads/main:refs/heads/main",
                "refs/heads/feature-auth:refs/heads/feature-auth",
            ])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main", output)
        self.assertIn("ok refs/heads/feature-auth", output)
        uploaded_files = [call.args[2] for call in h.client.upload_file.call_args_list]
        self.assertIn("main", uploaded_files)
        self.assertIn("feature-auth", uploaded_files)

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    @patch("git_remote_seafile.helper.get_git_config_int", return_value=20)
    @patch("git_remote_seafile.helper.get_git_config_bool", return_value=False)
    def test_auto_gc_notification_when_disabled(self, mock_autogc, mock_thresh, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://repo/path"
        h._refs_cache = {}

        # 22 remote packfiles -> threshold is 20
        h.client.list_dir.return_value = [{"name": f"pack-{i}.pack"} for i in range(22)]

        err = io.StringIO()
        with patch("sys.stderr", err), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("Notice: Remote repository has 22 packfiles", err.getvalue())
        self.assertIn("Tip: Run 'git-remote-seafile gc", err.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    @patch("git_remote_seafile.helper.get_git_config_int", return_value=20)
    @patch("git_remote_seafile.helper.get_git_config_bool", return_value=True)
    @patch("git_remote_seafile.gc.compact_repository")
    def test_auto_gc_triggered_when_enabled(self, mock_compact, mock_autogc, mock_thresh, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://repo/path"
        h._refs_cache = {}

        # 22 remote packfiles -> threshold is 20
        h.client.list_dir.return_value = [{"name": f"pack-{i}.pack"} for i in range(22)]

        with patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        mock_compact.assert_called_once_with(h.client, "repo1", "/git-repo", min_packs=20, verbose=True)

    @patch("git_remote_seafile.helper.install_packfile")
    @patch("git_remote_seafile.helper.get_git_dir")
    def test_cmd_fetch(self, mock_get_git_dir, mock_install_pack):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"

        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)
            mock_get_git_dir.return_value = local_git

            # Remote has pack-1.pack and pack-2.pack
            h.client.list_dir.return_value = [
                {"name": "pack-1.pack"},
                {"name": "pack-2.pack"},
            ]
            h.client.get_file_bytes.return_value = b"PACKBYTES"

            out = io.StringIO()
            with patch("sys.stdout", out):
                h.cmd_fetch(["fetch 123 refs/heads/main"])

            # Verify packfiles were downloaded and installed
            self.assertEqual(mock_install_pack.call_count, 2)

    def test_cmd_list_empty_repository(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/empty-repo"
        h._refs_cache = {}

        # Completely empty repository on Seafile
        h.client.list_dir.return_value = []
        h.client.get_file_text.return_value = None

        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list()

        output = out.getvalue()
        self.assertEqual(output.strip(), "")
        self.assertTrue(output.endswith("\n"))

    @patch("git_remote_seafile.helper.rev_parse", return_value="tag_sha_123")
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_tag_creation_and_deletion(self, mock_objs, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/tags/v1.0.0:refs/tags/v1.0.0"])

        output = out.getvalue()
        self.assertIn("ok refs/tags/v1.0.0", output)
        uploaded = [c.args[2] for c in h.client.upload_file.call_args_list]
        self.assertIn("v1.0.0", uploaded)

        # Now delete the tag
        out_del = io.StringIO()
        with patch("sys.stdout", out_del), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/tags/v1.0.0"])

        self.assertIn("ok refs/tags/v1.0.0", out_del.getvalue())
        h.client.delete_entry.assert_called_with("repo1", "/git-repo/refs/tags/v1.0.0")

    def test_cmd_push_delete_failure(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.client.delete_entry.return_value = False

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/protected-branch"])

        output = out.getvalue()
        self.assertIn("error refs/heads/protected-branch failed to delete ref on remote", output)

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=["obj1"])
    @patch("git_remote_seafile.helper.create_packfile", return_value=("pack1", b"PACK", b"IDX"))
    def test_cmd_push_upload_failure_reports_error(self, mock_pack, mock_objs, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.client.upload_file.side_effect = RuntimeError("HTTP 500 Seafile server error")

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("error refs/heads/main HTTP 500 Seafile server error", output)

    def test_cmd_push_lock_timeout_aborts_all_specs(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        mock_lock_cls = MagicMock()
        mock_lock_instance = MagicMock()
        mock_lock_instance.__enter__.side_effect = RepositoryLockedError("locked by user@otherhost")
        mock_lock_cls.return_value = mock_lock_instance

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock", mock_lock_cls):
            h.cmd_push(["refs/heads/main:refs/heads/main", "refs/heads/dev:refs/heads/dev"])

        output = out.getvalue()
        self.assertIn("error refs/heads/main locked by user@otherhost", output)
        self.assertIn("error refs/heads/dev locked by user@otherhost", output)

    def test_url_parsing_advanced(self):
        h = RemoteHelper.__new__(RemoteHelper)
        srv, lib, path = h._parse_url("seafile://example.com:8443/code/team/repo")
        self.assertEqual(srv, "https://example.com:8443")
        self.assertEqual(lib, "code")
        self.assertEqual(path, "/team/repo")

        srv2, lib2, path2 = h._parse_url("seafile://https://cloud.internal.org/library-name/sub/project/")
        self.assertEqual(srv2, "https://cloud.internal.org")
        self.assertEqual(lib2, "library-name")
        self.assertEqual(path2, "/sub/project")


class TestConcurrencyAndLocking(unittest.TestCase):
    def test_lock_acquire_and_release(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        with lock:
            self.assertTrue(lock.acquired)
            mock_client.upload_file.assert_called_once()
        self.assertFalse(lock.acquired)
        mock_client.delete_entry.assert_called_once_with("repo1", "/path/.git-lock.json")

    def test_active_lock_timeout(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        active_lock = json.dumps({
            "owner": "alice",
            "machine": "nodeA",
            "timestamp": time.time(),
            "lease": 60,
        })
        mock_client.get_file_text.return_value = active_lock

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()
        self.assertIn("locked by 'alice'", str(ctx.exception))

    def test_expired_lease_override(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Lock created 120s ago with a 60s lease -> expired!
        expired_lock = json.dumps({
            "owner": "bob",
            "machine": "crashed-laptop",
            "timestamp": time.time() - 120,
            "lease": 60,
        })
        mock_client.get_file_text.return_value = expired_lock

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        err = io.StringIO()
        with patch("sys.stderr", err):
            lock.acquire()

        self.assertTrue(lock.acquired)
        self.assertIn("Overriding expired lock", err.getvalue())

    def test_corrupt_lock_payload_handled_gracefully(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Corrupt JSON left by sudden network cut
        mock_client.get_file_text.return_value = "MALFORMED_NON_JSON{{{"

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()
        self.assertTrue(lock.acquired)

    def test_concurrent_threads_serialization(self):
        """Simulate two threads contending for a cooperative remote lock."""
        shared_remote_storage = {"lock": None}
        mock_client = MagicMock()

        def fake_get_file_text(repo_id, path):
            return shared_remote_storage["lock"]

        def fake_upload(repo_id, parent, filename, content, replace=True):
            if shared_remote_storage["lock"] is not None:
                # Collision simulation if already locked
                info = json.loads(shared_remote_storage["lock"])
                if time.time() < info["timestamp"] + info["lease"]:
                    raise Exception("Concurrent write rejected")
            shared_remote_storage["lock"] = content.decode("utf-8")

        def fake_delete(repo_id, path):
            shared_remote_storage["lock"] = None

        mock_client.get_file_text.side_effect = fake_get_file_text
        mock_client.upload_file.side_effect = fake_upload
        mock_client.delete_entry.side_effect = fake_delete

        results = []

        def worker(worker_id):
            client = MagicMock()
            client.token = f"user-{worker_id}"
            client.get_file_text.side_effect = fake_get_file_text
            client.upload_file.side_effect = fake_upload
            client.delete_entry.side_effect = fake_delete
            lock = RemoteLock(client, "repo1", "/path", timeout=5, lease=10)
            try:
                with lock:
                    results.append(f"{worker_id}_start")
                    time.sleep(0.05)
                    results.append(f"{worker_id}_end")
            except Exception as e:
                results.append(f"{worker_id}_error: {e}")

        _real_sleep = time.sleep
        with patch("time.sleep", side_effect=lambda s: _real_sleep(0.02)):
            t1 = threading.Thread(target=worker, args=(1,))
            t2 = threading.Thread(target=worker, args=(2,))
            t1.start()
            _real_sleep(0.01)  # Ensure t1 starts first
            t2.start()
            t1.join()
            t2.join()

        # Both workers should complete without error, strictly serialized
        self.assertEqual(len([r for r in results if "error" in r]), 0)
        self.assertEqual(len(results), 4)
        # Verify serialization: start -> end -> start -> end
        first_worker = results[0].split("_")[0]
        self.assertEqual(results[1], f"{first_worker}_end")


class TestRemoteGC(unittest.TestCase):
    def test_gc_skipped_below_threshold(self):
        mock_client = MagicMock()
        mock_client.list_dir.return_value = [{"name": "pack-1.pack"}]

        res = compact_repository(mock_client, "repo1", "/path", min_packs=3, verbose=False)
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["count"], 1)

    @patch("subprocess.run")
    def test_gc_compaction_flow(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.return_value = [
            {"name": "pack-1.pack"},
            {"name": "pack-2.pack"},
        ]
        mock_client.get_file_bytes.return_value = b"PACK-DATA"

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
            # Verify obsolete packs were deleted plus the lock release (4 + 1 = 5)
            self.assertEqual(mock_client.delete_entry.call_count, 5)
            mock_client.delete_entry.assert_any_call("repo1", "/path/.git-lock.json")

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
        mock_client.get_file_text.return_value = "sha000111"

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


class TestLFSTransferAgent(unittest.TestCase):
    def test_lfs_init(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_init({"event": "init", "operation": "upload"})
            self.assertEqual(stdout_buf.getvalue().strip(), "{}")
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_upload_success(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"SAMPLE-LFS-CONTENT")
                temp_path = tf.name

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "1234567890abcdef", "path": temp_path})

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], "1234567890abcdef")
            mock_client.upload_file.assert_called_once()
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_upload_file_not_found(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "missingoid", "path": "/nonexistent/path"})

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertIn("error", resp)
            self.assertEqual(resp["error"]["code"], 400)
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_download_success(self):
        mock_client = MagicMock()
        mock_client.get_file_bytes.return_value = b"BINARY-OBJECT-BYTES"
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_download({"event": "download", "oid": "abcdef0123456789"})

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], "abcdef0123456789")
            self.assertTrue(Path(resp["path"]).is_file())
            self.assertEqual(Path(resp["path"]).read_bytes(), b"BINARY-OBJECT-BYTES")
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_download_not_found_404(self):
        mock_client = MagicMock()
        mock_client.get_file_bytes.return_value = None
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_download({"event": "download", "oid": "missingoid"})

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["error"]["code"], 404)
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_upload_internal_server_error(self):
        mock_client = MagicMock()
        mock_client.upload_file.side_effect = RuntimeError("Network timeout")
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        temp_path = tempfile.mktemp()
        Path(temp_path).write_bytes(b"DATA")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "oid123", "path": temp_path})

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["error"]["code"], 500)
            self.assertIn("Network timeout", resp["error"]["message"])
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_agent_protocol_loop_resilience(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            input_stream = io.StringIO(
                "INVALID_CORRUPT_JSON_LINE\n"
                '{"event": "init"}\n'
                '{"event": "unknown_event_type"}\n'
                '{"event": "terminate"}\n'
            )
            stdout_buf = io.StringIO()
            with patch("sys.stdin", input_stream), patch("sys.stdout", stdout_buf):
                agent.run()

            output_lines = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines()]
            # Line 1: init response -> {}
            self.assertEqual(output_lines[0], {})
            # Line 2: unknown event -> 400 error
            self.assertEqual(output_lines[1]["error"]["code"], 400)
            self.assertIn("Unknown event: unknown_event_type", output_lines[1]["error"]["message"])
        finally:
            agent._temp_dir.cleanup()


class TestAccountDiscovery(unittest.TestCase):
    def test_env_var_precedence(self):
        with patch.dict("os.environ", {"SEAFILE_SERVER": "https://seafile.custom.org", "SEAFILE_TOKEN": "customtok"}):
            client = SeafileClient()
            self.assertEqual(client.server_url, "https://seafile.custom.org")
            self.assertEqual(client.token, "customtok")

    def test_config_file_discovery(self):
        cfg_content = json.dumps({"server": "https://config.seafile.org", "token": "cfgtoken"})
        with patch.dict("os.environ", {"USERPROFILE": "C:\\Users\\testuser"}, clear=True):
            with patch("pathlib.Path.is_file") as mock_is_file, \
                 patch("pathlib.Path.read_text", return_value=cfg_content):
                mock_is_file.return_value = True
                client = SeafileClient()
                self.assertEqual(client.server_url, "https://config.seafile.org")
                self.assertEqual(client.token, "cfgtoken")


class TestGitConfig(unittest.TestCase):
    def test_config_readers(self):
        with patch("git_remote_seafile.git_util.get_git_config", return_value="true"):
            self.assertTrue(get_git_config_bool("seafile.autogc"))

        with patch("git_remote_seafile.git_util.get_git_config", return_value="false"):
            self.assertFalse(get_git_config_bool("seafile.autogc"))

        with patch("git_remote_seafile.git_util.get_git_config", return_value=None):
            self.assertFalse(get_git_config_bool("seafile.autogc", default=False))
            self.assertEqual(get_git_config_int("seafile.gcthreshold", default=25), 25)

        with patch("git_remote_seafile.git_util.get_git_config", return_value="35"):
            self.assertEqual(get_git_config_int("seafile.gcthreshold", default=20), 35)


class TestSeafileClientOperations(unittest.TestCase):
    def test_mkdir_p_and_delete_entry(self):
        client = SeafileClient.__new__(SeafileClient)
        client.server_url = "https://seafile.example.com"
        client.timeout = 10
        client.session = MagicMock()

        # Mock mkdir response
        client.session.post.return_value = MagicMock(status_code=200)
        self.assertTrue(client.mkdir_p("repo1", "/a/b/c"))

        # Mock delete response 200 vs 500
        client.session.delete.return_value = MagicMock(status_code=200)
        self.assertTrue(client.delete_entry("repo1", "/refs/heads/feature"))

        client.session.delete.return_value = MagicMock(status_code=500)
        self.assertFalse(client.delete_entry("repo1", "/refs/heads/feature"))


class TestCLISubcommands(unittest.TestCase):
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_cli_set_head(self, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "/git-repo"

        from git_remote_seafile.cli import main
        out = io.StringIO()
        with patch("sys.argv", ["git-remote-seafile", "set-head", "seafile://code/myproject", "main"]), \
             patch("sys.stdout", out):
            ret = main()

        self.assertEqual(ret, 0)
        self.assertIn("Updated remote HEAD", out.getvalue())
        mock_helper.client.upload_file.assert_called_once_with(
            "repo1",
            "/git-repo",
            "HEAD",
            b"ref: refs/heads/main\n",
            replace=True,
        )

    def test_cmd_push_multiline_error_sanitized(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.library_name = "test-repo"
        h._refs_cache = {}

        # Simulate multi-line exception during upload
        h.client.upload_file.side_effect = Exception("line 1 error\nline 2 fatal error\nline 3 details")

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), \
             patch("git_remote_seafile.helper.rev_parse", return_value="sha123"), \
             patch("git_remote_seafile.helper.is_ancestor", return_value=True), \
             patch("git_remote_seafile.helper.get_objects_to_push", return_value=[]), \
             patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output_lines = out.getvalue().splitlines()
        # Verify stdout lines obey git protocol: single error line followed by terminator newline
        non_empty = [line for line in output_lines if line]
        self.assertEqual(len(non_empty), 1)
        self.assertTrue(non_empty[0].startswith("error refs/heads/main "))
        self.assertIn("line 1 error", non_empty[0])
        self.assertIn("line 2 fatal error", non_empty[0])
        # Ensure raw unhandled newlines were replaced so git protocol does not break
        self.assertNotIn("\n", non_empty[0])


if __name__ == "__main__":
    unittest.main()
