"""test_remote.py - Comprehensive unit tests for git_remote_seafile."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.client import SeafileAPIError, SeafileClient
from git_remote_seafile.gc import compact_repository, describe_size_delta
from git_remote_seafile.git_util import (
    GitError,
    get_git_config_bool,
    get_git_config_int,
)
from git_remote_seafile.helper import RemoteHelper
from git_remote_seafile.lfs import LFSTransferAgent
from git_remote_seafile.lock import RemoteLock, RepositoryLockedError
from git_remote_seafile.safety import SafetyError, check_preflight_safety


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
        sha_main = "a" * 40
        sha_feat = "b" * 40
        sha_tag = "c" * 40
        h.client.get_file_text.side_effect = lambda repo_id, path: (
            sha_main if "heads/main" in path
            else sha_feat if "heads/feature" in path
            else sha_tag if "tags/v1.0" in path
            else "ref: refs/heads/main" if path.endswith("HEAD")
            else None
        )

        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list()

        output = out.getvalue()
        self.assertIn(f"{sha_main} refs/heads/main", output)
        self.assertIn(f"{sha_feat} refs/heads/feature", output)
        self.assertIn(f"{sha_tag} refs/tags/v1.0", output)
        self.assertIn("@refs/heads/main HEAD", output)

    def test_cmd_list_skips_invalid_sha(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.library_name = "lib"
        h._refs_cache = {}
        h._repository_has_objects = MagicMock(return_value=False)

        h.client.list_dir.side_effect = lambda repo_id, path: (
            [{"type": "file", "name": "main"}, {"type": "file", "name": "corrupt"}]
            if "heads" in path
            else []
        )
        valid_sha = "1" * 40
        h.client.get_file_text.side_effect = lambda repo_id, path: (
            valid_sha if "heads/main" in path
            else "<html>error</html>" if "heads/corrupt" in path
            else None
        )

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            h.cmd_list()

        self.assertIn(f"{valid_sha} refs/heads/main", out.getvalue())
        self.assertNotIn("corrupt", out.getvalue())
        self.assertIn("Warning: ignoring invalid SHA", err.getvalue())

    def test_cmd_list_for_push_allows_empty_refs_with_packs(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.library_name = "lib"
        h._refs_cache = {}
        h._repository_has_objects = MagicMock(return_value=True)
        h.client.list_dir.return_value = []
        h.client.get_file_text.return_value = None

        # Without for_push, raises SeafileAPIError
        with self.assertRaises(SeafileAPIError):
            h.cmd_list(for_push=False)

        # With for_push=True, succeeds and reports empty list
        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list(for_push=True)
        self.assertEqual(out.getvalue(), "\n")

    def test_cmd_push_rejects_disallowed_namespaces(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["HEAD:refs/notes/commits", ":refs/stash"])

        output = out.getvalue()
        self.assertIn("error refs/notes/commits refusing to push outside refs/heads and refs/tags", output)
        self.assertIn("error refs/stash refusing to push outside refs/heads and refs/tags", output)

    def test_cmd_push_does_not_duplicate_error_for_successful_ref(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h._full_path = lambda p: f"/git-repo/{p.lstrip('/')}"
        h.client.get_file_text.return_value = None

        # Ref 1 succeeds, Ref 2 fails
        call_count = [0]
        def mock_rev_parse(ref):
            call_count[0] += 1
            if call_count[0] == 1:
                return "1" * 40
            raise RuntimeError("Failure on ref 2")

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"), \
             patch("git_remote_seafile.helper.rev_parse", side_effect=mock_rev_parse), \
             patch("git_remote_seafile.helper.get_objects_to_push", return_value=[]):
            h.cmd_push(["HEAD:refs/heads/ok-branch", "HEAD:refs/heads/fail-branch"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/ok-branch\n", output)
        self.assertIn("error refs/heads/fail-branch", output)
        self.assertNotIn("error refs/heads/ok-branch", output)

    def test_cmd_push_delete_branch(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        # A SHA learned from the ref advertisement.  It is only usable as a push
        # exclusion while the ref exists on the remote, so a delete has to drop
        # it -- leaving it behind would have a later push exclude objects on the
        # authority of a ref that is gone.
        h._refs_cache = {"refs/heads/old-branch": "stale-sha"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/old-branch"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/old-branch", output)
        h.client.delete_entry.assert_called_with("repo1", "/git-repo/refs/heads/old-branch")
        self.assertNotIn("refs/heads/old-branch", h._refs_cache)

    def test_a_failed_delete_keeps_the_cached_sha(self):
        """Nothing was deleted, so the cache is still an accurate picture."""
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.client.delete_entry.return_value = False
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/old-branch": "still-there"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/old-branch"])

        self.assertIn("failed to delete", out.getvalue())
        self.assertEqual(h._refs_cache["refs/heads/old-branch"], "still-there")

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

    @patch("git_remote_seafile.helper.rev_parse", side_effect=lambda ref: f"sha_{ref.split('/')[-1]}")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.create_packfile", return_value=("batch1234", b"BATCH_PACK", b"BATCH_IDX"))
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=["objA", "objB"])
    def test_cmd_push_multiple_branches_batches_single_pack(self, mock_objs, mock_pack, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([
                "refs/heads/b1:refs/heads/b1",
                "refs/heads/b2:refs/heads/b2",
            ])

        output = out.getvalue()
        self.assertIn("ok refs/heads/b1", output)
        self.assertIn("ok refs/heads/b2", output)
        mock_objs.assert_called_once_with(["sha_b1", "sha_b2"], [])
        mock_pack.assert_called_once_with(["objA", "objB"])
        uploaded_files = [call.args[2] for call in h.client.upload_file.call_args_list]
        self.assertEqual(uploaded_files.count("pack-batch1234.pack"), 1)
        self.assertEqual(uploaded_files.count("pack-batch1234.idx"), 1)
        self.assertIn("b1", uploaded_files)
        self.assertIn("b2", uploaded_files)

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha_feature")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_excludes_all_known_remote_refs(self, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {
            "refs/heads/main": "remote_sha_main",
            "refs/heads/dev": "remote_sha_dev",
        }

        with patch("sys.stdout", io.StringIO()), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/feature:refs/heads/feature"])

        mock_objs.assert_called_once()
        _, exclude = mock_objs.call_args[0]
        self.assertIn("remote_sha_main", exclude)
        self.assertIn("remote_sha_dev", exclude)

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

    def test_cmd_push_delete_prunes_empty_nested_directories(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/feature/auth": "sha123"}
        h.client._known_dirs = {("repo1", "/git-repo/refs/heads/feature")}
        h.client.delete_entry.return_value = True

        def fake_list(repo_id, path):
            if path.endswith("/feature"):
                return []
            return [{"name": "main"}]
        h.client.list_dir.side_effect = fake_list

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/feature/auth"])

        self.assertIn("ok refs/heads/feature/auth", out.getvalue())
        h.client.delete_entry.assert_any_call("repo1", "/git-repo/refs/heads/feature")
        self.assertNotIn(("repo1", "/git-repo/refs/heads/feature"), h.client._known_dirs)

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

    def test_helper_run_protocol_loop(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.cmd_capabilities = MagicMock()
        h.cmd_list = MagicMock()
        h.cmd_push = MagicMock()
        h.cmd_fetch = MagicMock()

        # Simulate git sending capabilities, list, push, fetch, unknown command, and EOF
        commands = (
            "capabilities\n"
            "list\n"
            "list for-push\n"
            "push refs/heads/main:refs/heads/main\n"
            "push refs/heads/dev:refs/heads/dev\n"
            "\n"
            "fetch sha1\n"
            "fetch sha2\n"
            "\n"
            "unknown-cmd\n"
            "\n"
        )
        with patch("sys.stdin", io.StringIO(commands)), patch("sys.stdout", io.StringIO()):
            h.run()

        h.cmd_capabilities.assert_called_once()
        self.assertEqual(h.cmd_list.call_count, 2)
        h.cmd_list.assert_any_call(for_push=False)
        h.cmd_list.assert_any_call(for_push=True)
        h.cmd_push.assert_called_once_with([
            "refs/heads/main:refs/heads/main",
            "refs/heads/dev:refs/heads/dev"
        ])
        h.cmd_fetch.assert_called_once_with(["sha1", "sha2"])

    @patch("git_remote_seafile.helper.rev_parse", return_value=None)
    def test_cmd_push_local_ref_not_found(self, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/missing:refs/heads/missing"])

        self.assertIn("error refs/heads/missing local ref does not exist", out.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=False)
    def test_cmd_push_non_fast_forward_rejected(self, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "remote_sha"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("error refs/heads/main non-fast-forward", out.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha")
    @patch("git_remote_seafile.helper.is_ancestor", side_effect=GitError("fatal: Not a valid object name remote_sha"))
    def test_cmd_push_unknown_remote_tip_reports_fetch_first(self, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "remote_sha"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("error refs/heads/main fetch first", out.getvalue())

    def test_cmd_push_branch_deletion(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        # Success case
        h.client.delete_entry.return_value = True
        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/feature"])
        self.assertIn("ok refs/heads/feature", out.getvalue())

        # Exception case
        h.client.delete_entry.side_effect = RuntimeError("Delete failed")
        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/feature"])
        self.assertIn("error refs/heads/feature Delete failed", out.getvalue())


class TestConcurrencyAndLocking(unittest.TestCase):
    def test_lock_acquire_and_release(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        with lock:
            self.assertTrue(lock.acquired)
            self.assertEqual(mock_client.upload_file.call_count, 2)
        self.assertFalse(lock.acquired)
        mock_client.delete_entry.assert_any_call("repo1", "/path/.git-lock.json")

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
                # Collision simulation if already locked by another client
                info = json.loads(shared_remote_storage["lock"])
                new_info = json.loads(content)
                if time.time() < info["timestamp"] + info["lease"]:
                    if info.get("nonce") != new_info.get("nonce"):
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

    def test_release_does_not_delete_a_lock_another_machine_took_over(self):
        """A lease can expire mid-operation, and someone else then holds it.

        Releasing unconditionally would delete *their* lock, letting two writers
        into the repository at once.
        """
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()
        self.assertTrue(lock.acquired)

        # Our lease expired while we worked; another machine took the lock.
        mock_client.get_file_text.return_value = json.dumps({
            "owner": "someone-else",
            "machine": "otherhost",
            "timestamp": time.time(),
            "lease": 60,
        })
        err = io.StringIO()
        with patch("sys.stderr", err):
            lock.release()

        mock_client.delete_entry.assert_not_called()
        self.assertFalse(lock.acquired)
        self.assertIn("otherhost", err.getvalue())

    def test_release_removes_its_own_lock(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()
        # The payload on the remote is our own.
        mock_client.get_file_text.return_value = mock_client.upload_file.call_args[0][3].decode("utf-8")
        lock.release()

        mock_client.delete_entry.assert_any_call("repo1", "/path/.git-lock.json")

    def test_release_does_not_delete_same_machine_lock_if_overridden(self):
        """When lease expires and same machine re-acquires with new nonce, release must not delete it."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock1 = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=1)
        lock1.acquire()
        self.assertTrue(lock1.acquired)

        # Process 2 on the same machine/account overrides after lease expiry
        payload1 = json.loads(mock_client.upload_file.call_args[0][3].decode("utf-8"))
        overriding_payload = dict(payload1)
        overriding_payload["nonce"] = "different-new-nonce-uuid"
        overriding_payload["timestamp"] = time.time() + 10
        mock_client.get_file_text.return_value = json.dumps(overriding_payload)

        err = io.StringIO()
        with patch("sys.stderr", err):
            lock1.release()

        # Must not delete the newly acquired lock
        mock_client.delete_entry.assert_not_called()
        self.assertFalse(lock1.acquired)

    def test_lock_payload_does_not_leak_the_api_token(self):
        """The payload is stored in the repository, so it must not carry the token.

        The previous implementation wrote the token's first eight characters
        into it.
        """
        secret = "s3cr3t-token-value"
        mock_client = MagicMock()
        mock_client.token = secret
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()

        payload = mock_client.upload_file.call_args[0][3].decode("utf-8")
        self.assertNotIn("s3cr3t", payload)
        self.assertEqual(
            json.loads(payload)["owner"],
            hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8],
        )

    @patch("git_remote_seafile.lock.get_git_config_int")
    def test_lock_reads_git_config_defaults(self, mock_get_cfg):
        mock_get_cfg.side_effect = lambda key, default: 30 if "timeout" in key else 90
        mock_client = MagicMock()
        lock = RemoteLock(mock_client, "repo1", "/path")
        self.assertEqual(lock.timeout, 30)
        self.assertEqual(lock.lease, 90)

    def test_lock_handles_malformed_timestamp_without_crashing(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Timestamp is a string instead of float/int, lease is invalid
        mock_client.get_file_text.return_value = json.dumps({
            "owner": "alice",
            "machine": "nodeA",
            "timestamp": "INVALID_TIMESTAMP",
            "lease": "NOT_A_NUMBER",
        })

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        # Should override safely as expired instead of raising TypeError
        lock.acquire()
        self.assertTrue(lock.acquired)

    def test_lock_detects_post_write_collision(self):
        mock_client = MagicMock()
        mock_client.token = "token123"
        # Initial read says no lock, but verification read after upload sees competitor nonce
        call_count = [0]
        def fake_get_file_text(repo_id, path):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            return json.dumps({
                "owner": "competitor",
                "machine": "other-node",
                "nonce": "competitor-nonce-xyz",
                "timestamp": time.time(),
                "lease": 60,
            })

        mock_client.get_file_text.side_effect = fake_get_file_text
        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
        with self.assertRaises(RepositoryLockedError):
            lock.acquire()

    def test_dead_pid_fast_reclaim_on_local_machine(self):
        """A lock held by the same machine with a dead PID should be fast-reclaimed."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        dead_pid = 999999
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=False):
            stale_lock = json.dumps({
                "owner": "crashed-local-user",
                "machine": socket.gethostname(),
                "nonce": "crashed-nonce",
                "timestamp": time.time(),
                "lease": 60,
                "pid": dead_pid,
            })
            call_count = [0]
            def fake_read(repo_id, path):
                call_count[0] += 1
                if call_count[0] == 1:
                    return stale_lock
                return None
            mock_client.get_file_text.side_effect = fake_read
            lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
            err = io.StringIO()
            with patch("sys.stderr", err):
                lock.acquire()
            self.assertTrue(lock.acquired)
            self.assertIn("Reclaiming stale lock from dead local process", err.getvalue())

    def test_alive_pid_on_local_machine_blocks(self):
        """A lock held by the same machine with an alive PID must not be stolen."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        alive_pid = os.getpid()
        with patch("git_remote_seafile.lock._is_pid_alive", return_value=True):
            mock_client.get_file_text.return_value = json.dumps({
                "owner": "running-local-user",
                "machine": socket.gethostname(),
                "nonce": "alive-nonce",
                "timestamp": time.time(),
                "lease": 60,
                "pid": alive_pid,
            })
            lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
            with self.assertRaises(RepositoryLockedError):
                lock.acquire()

    def test_ticket_based_ordering_earliest_wins(self):
        """When multiple tickets exist, the earliest ticket wins."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        now = time.time()

        ticket_a = json.dumps({
            "owner": "alice",
            "machine": "nodeA",
            "nonce": "ticketA",
            "timestamp": now - 10,
            "lease": 60,
            "pid": 1234,
        })
        mock_client.get_file_text.side_effect = lambda repo_id, path: ticket_a if "ticketA" in path else None
        mock_client.list_dir.return_value = [
            {"name": "ticketA.json", "mtime": int(now - 10)},
        ]

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=0, lease=60)
        with self.assertRaises(RepositoryLockedError) as ctx:
            lock.acquire()
        self.assertIn("locked by 'alice'", str(ctx.exception))

    def test_lock_lease_renew(self):
        """Lease renewal updates timestamps on both ticket and legacy lock."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        lock.acquire()
        upload_count_before = mock_client.upload_file.call_count

        res = lock.renew()
        self.assertTrue(res)
        self.assertEqual(mock_client.upload_file.call_count, upload_count_before + 2)

    def test_lock_status_and_unlock(self):
        """get_status and unlock methods inspect and clear locks."""
        mock_client = MagicMock()
        mock_client.token = "token123"
        mock_client.get_file_text.return_value = None

        lock = RemoteLock(mock_client, "repo1", "/path", timeout=5, lease=60)
        status_unlocked = lock.get_status()
        self.assertFalse(status_unlocked["locked"])

        lock.acquire()
        mock_client.get_file_text.return_value = mock_client.upload_file.call_args[0][3].decode("utf-8")
        status_locked = lock.get_status()
        self.assertTrue(status_locked["locked"])
        self.assertEqual(status_locked["owner"], lock._owner_id())

        self.assertTrue(lock.unlock())


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
        mock_client.get_file_text.return_value = "sha-main"

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
            # Verify obsolete packs were deleted plus the lock release (4 obsolete files + ticket + legacy lock = 6)
            self.assertEqual(mock_client.delete_entry.call_count, 6)
            mock_client.delete_entry.assert_any_call("repo1", "/path/.git-lock.json")

    @patch("subprocess.run")
    def test_gc_compaction_warns_on_failed_pack_download(self, mock_subprocess):
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

        with patch("pathlib.Path.glob") as mock_glob, patch("sys.stderr", new_callable=io.StringIO) as mock_err:
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
            self.assertIn("Warning: failed to download pack-1.pack during compaction", mock_err.getvalue())

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

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], "1234567890abcdef")
            progress_events = [m for m in msgs if m.get("event") == "progress"]
            self.assertEqual(len(progress_events), 1)
            self.assertEqual(progress_events[0]["oid"], "1234567890abcdef")
            self.assertEqual(progress_events[0]["bytesSoFar"], len(b"SAMPLE-LFS-CONTENT"))
            mock_client.upload_file.assert_called_once()
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_upload_streams_from_disk(self):
        """An LFS object can be far larger than RAM, so it must not be read in."""
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"SAMPLE-LFS-CONTENT")
                temp_path = tf.name

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "abcdef01", "path": temp_path})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")

            args, _ = mock_client.upload_file.call_args
            self.assertNotIsInstance(
                args[3], (bytes, bytearray),
                "the LFS payload must be a path or file object, not the file's bytes",
            )
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

        def fake_download(repo_id, file_path, dest):
            Path(dest).write_bytes(b"BINARY-OBJECT-BYTES")
            return True

        mock_client.download_file_to = MagicMock(side_effect=fake_download)
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_download({"event": "download", "oid": "abcdef0123456789"})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], "abcdef0123456789")
            self.assertTrue(Path(resp["path"]).is_file())
            self.assertEqual(Path(resp["path"]).read_bytes(), b"BINARY-OBJECT-BYTES")
            progress_events = [m for m in msgs if m.get("event") == "progress"]
            self.assertEqual(len(progress_events), 1)
            mock_client.get_file_bytes.assert_not_called()
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_upload_skips_duplicate_existing_file(self):
        mock_client = MagicMock()
        mock_client.list_dir.return_value = [
            {"name": "1234567890abcdef", "size": len(b"SAMPLE-LFS-CONTENT"), "type": "file"}
        ]
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"SAMPLE-LFS-CONTENT")
                temp_path = tf.name

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "1234567890abcdef", "path": temp_path})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], "1234567890abcdef")
            # Upload should be skipped
            mock_client.upload_file.assert_not_called()
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_rejects_invalid_oid_path_traversal(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            # Traversal in upload
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "../../etc/passwd", "path": "/some/path"})
            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["error"]["code"], 400)
            self.assertIn("Invalid OID", resp["error"]["message"])

            # Traversal in download
            stdout_buf_dl = io.StringIO()
            with patch("sys.stdout", stdout_buf_dl):
                agent.handle_download({"event": "download", "oid": "../../etc/shadow"})
            resp_dl = json.loads(stdout_buf_dl.getvalue().strip())
            self.assertEqual(resp_dl["event"], "complete")
            self.assertEqual(resp_dl["error"]["code"], 400)
            self.assertIn("Invalid OID", resp_dl["error"]["message"])
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_download_not_found_404(self):
        mock_client = MagicMock()
        mock_client.download_file_to.return_value = False
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

        # Mock delete response: 200 on /file/
        client.session.delete.return_value = MagicMock(status_code=200)
        self.assertTrue(client.delete_entry("repo1", "/refs/heads/feature"))

        # Mock fallback: 404 on /file/, 200 on /dir/
        client.session.delete.side_effect = [MagicMock(status_code=404), MagicMock(status_code=200)]
        self.assertTrue(client.delete_entry("repo1", "/somedir"))

        # Mock failure on both
        client.session.delete.side_effect = None
        client.session.delete.return_value = MagicMock(status_code=500)
        self.assertFalse(client.delete_entry("repo1", "/refs/heads/feature"))

    def test_dir_exists_and_duplicate_prevention(self):
        client = SeafileClient.__new__(SeafileClient)
        client.server_url = "https://seafile.example.com"
        client.timeout = 10
        client.session = MagicMock()
        client._known_dirs = set()

        # Root always exists without HTTP call
        self.assertTrue(client.dir_exists("repo1", "/"))
        self.assertEqual(client.session.get.call_count, 0)

        # Non-existing dir returns False
        client.session.get.return_value = MagicMock(status_code=404)
        self.assertFalse(client.dir_exists("repo1", "/new-folder"))

        # Existing dir returns True and caches
        client.session.get.return_value = MagicMock(status_code=200)
        self.assertTrue(client.dir_exists("repo1", "/existing-folder"))
        self.assertIn(("repo1", "/existing-folder"), client._known_dirs)

        # Second check hits cache (no GET call)
        get_calls_before = client.session.get.call_count
        self.assertTrue(client.dir_exists("repo1", "/existing-folder"))
        self.assertEqual(client.session.get.call_count, get_calls_before)

        # mkdir_p on already existing directory does NOT call POST mkdir
        client.session.post.reset_mock()
        self.assertTrue(client.mkdir_p("repo1", "/existing-folder"))
        self.assertEqual(client.session.post.call_count, 0)

    def test_mkdir_p_fails_loudly_when_the_directory_was_not_created(self):
        """A rejected mkdir must not be reported as success.

        list_dir answers [] (never None) for a missing directory, so an
        emptiness test always passes: a failed mkdir used to be cached as
        created, after which every upload into it failed for no visible reason.
        """
        client = SeafileClient.__new__(SeafileClient)
        client.server_url = "https://seafile.example.com"
        client.timeout = 10
        client.session = MagicMock()
        client._known_dirs = set()

        # The directory does not exist and the server refuses to create it.
        client.session.get.return_value = MagicMock(status_code=404)
        client.session.post.return_value = MagicMock(status_code=500, text="boom")

        with self.assertRaises(SeafileAPIError):
            client.mkdir_p("repo1", "/a/b")

        self.assertNotIn(("repo1", "/a/b"), client._known_dirs)

    def test_mkdir_p_succeeds_when_the_directory_appears_concurrently(self):
        """A rejected mkdir is still fine if the directory turns up anyway."""
        client = SeafileClient.__new__(SeafileClient)
        client.server_url = "https://seafile.example.com"
        client.timeout = 10
        client.session = MagicMock()
        client._known_dirs = set()

        # First existence check says "absent"; the mkdir is rejected; the
        # follow-up check finds it (someone else created it in between).
        client.session.get.side_effect = [
            MagicMock(status_code=404),
            MagicMock(status_code=200),
        ]
        client.session.post.return_value = MagicMock(status_code=500, text="boom")

        self.assertTrue(client.mkdir_p("repo1", "/a"))
        self.assertIn(("repo1", "/a"), client._known_dirs)


class TestCLISubcommands(unittest.TestCase):
    @patch("git_remote_seafile.cli.RemoteHelper")
    def test_cli_set_head(self, mock_helper_cls):
        mock_helper = MagicMock()
        mock_helper_cls.return_value = mock_helper
        mock_helper.repo_id = "repo1"
        mock_helper.repo_path = "/git-repo"
        mock_helper.client.list_dir.return_value = [{"name": "main", "type": "file"}]
        mock_helper.client.get_file_text.return_value = "1" * 40

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


class TestUrlParsing(unittest.TestCase):
    """The bare URL forms are ambiguous, so the parser must fail safely.

    `seafile://a.b/c` could be "library a.b, path /c" or "host a.b, library c".
    The old rule -- "a dot in the first segment means it is a host" -- resolved
    that in favour of the host, so a library whose name merely contained a dot
    was silently split: the library became "c" and the path "/git-repo".
    """

    @staticmethod
    def _parse(url: str):
        return RemoteHelper.__new__(RemoteHelper)._parse_url(url)

    def test_a_dotted_library_name_is_not_mistaken_for_a_host(self):
        server, lib, path = self._parse("seafile://my.library/repo")
        self.assertIsNone(server, "a two-segment URL is always <library>/<path>")
        self.assertEqual(lib, "my.library")
        self.assertEqual(path, "/repo")

    def test_a_host_form_still_needs_a_library_and_a_path(self):
        # The documented host form keeps working: three segments, first is a host.
        server, lib, path = self._parse("seafile://seafile.example.com/code/myproject")
        self.assertEqual(server, "https://seafile.example.com")
        self.assertEqual(lib, "code")
        self.assertEqual(path, "/myproject")

    def test_a_nested_library_path_is_not_read_as_a_host(self):
        server, lib, path = self._parse("seafile://Documents/seafile-git/myproject")
        self.assertIsNone(server)
        self.assertEqual(lib, "Documents")
        self.assertEqual(path, "/seafile-git/myproject")

    def test_a_bare_host_with_no_library_is_rejected_with_a_fix(self):
        with self.assertRaises(ValueError) as ctx:
            self._parse("seafile://seafile.example.com")
        message = str(ctx.exception)
        self.assertIn("no library", message)
        # The error must say how to write it instead.
        self.assertIn("seafile://https://seafile.example.com/", message)

    def test_a_single_segment_that_is_not_host_like_is_still_a_library(self):
        server, lib, path = self._parse("seafile://Documents/")
        self.assertIsNone(server)
        self.assertEqual(lib, "Documents")
        self.assertEqual(path, "/git-repo")

    def test_an_empty_url_is_rejected(self):
        for url in ("seafile://", "seafile:///"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    self._parse(url)

    def test_explicit_scheme_form_is_unambiguous(self):
        # The escape hatch for genuinely ambiguous names.
        server, lib, path = self._parse("seafile://https://my.library/repo")
        self.assertEqual(server, "https://my.library")
        self.assertEqual(lib, "repo")
        self.assertEqual(path, "/git-repo")

    def test_percent_encoded_segments_are_unquoted(self):
        server, lib, path = self._parse("seafile://My%20Library/my%20repo")
        self.assertIsNone(server)
        self.assertEqual(lib, "My Library")
        self.assertEqual(path, "/my repo")

        server, lib, path = self._parse("seafile://https://seafile.example.com/Team%20Docs/nested%20repo/sub")
        self.assertEqual(server, "https://seafile.example.com")
        self.assertEqual(lib, "Team Docs")
        self.assertEqual(path, "/nested repo/sub")


class TestFetchSafety(unittest.TestCase):
    """The guardrails must run on fetch/clone, not only on push (#13).

    ``git clone seafile://Documents/code/myproject`` run from inside the synced
    ``Documents/`` library used to sail through: the clone downloaded packfiles
    straight into the synced tree and started the churn/reflection cycle the
    guardrails exist to prevent.  Fetch is read-only against the *server*, but
    it writes a whole repository into the local directory, and that directory is
    what the collision check is about.

    These tests drive the real ``check_preflight_safety`` -- only the desktop
    client's repo.db discovery is stubbed -- so the path logic under test is the
    shipping code.
    """

    @staticmethod
    def _helper(library_name="Documents", repo_path="/code/myproject"):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = repo_path
        h.library_name = library_name
        h._refs_cache = {}
        return h

    def test_fetch_runs_the_guardrails_in_fetch_mode(self):
        """cmd_fetch must consult the guardrails, and with push_mode=False."""
        seen = {}

        def fake_check(client, library, path, push_mode=True, **kwargs):
            seen["push_mode"] = push_mode
            seen["library"] = library
            seen["path"] = path
            return []

        h = self._helper()
        with patch(
            "git_remote_seafile.helper.check_preflight_safety", side_effect=fake_check
        ), patch(
            "git_remote_seafile.helper.get_git_dir",
            side_effect=RuntimeError("reached the fetch body"),
        ):
            with patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(RuntimeError):
                    h.cmd_fetch(["refs/heads/main"])

        self.assertIs(
            seen["push_mode"], False,
            "a fetch must be checked in fetch mode -- the push-only reflection "
            "gate (Trap 2) would otherwise block legitimate fetches",
        )
        self.assertEqual(seen["library"], "Documents")
        self.assertEqual(seen["path"], "/code/myproject")

    def test_clone_inside_the_synced_library_is_blocked(self):
        """The reviewer's exact scenario: clone into the tree it warns about.

        ``git clone`` runs the helper with cwd = the parent of the directory
        being created, which is not a repository yet -- so git cannot name a
        work tree and the cwd fallback is what catches this.
        """
        h = self._helper()

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            inside = doc_dir / "code" / "myproject"
            inside.mkdir(parents=True)

            libs = [
                {"repo_id": "r1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            original_cwd = os.getcwd()
            os.chdir(inside)
            try:
                with patch(
                    "git_remote_seafile.safety.discover_local_synced_libraries",
                    return_value=libs,
                ), patch(
                    "git_remote_seafile.safety.get_local_work_tree",
                    return_value=None,
                ), patch(
                    "git_remote_seafile.safety.get_git_config_bool",
                    return_value=False,
                ), patch(
                    "git_remote_seafile.helper.get_git_dir",
                    side_effect=AssertionError(
                        "the fetch body ran despite the Trap 1 collision"
                    ),
                ):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SafetyError) as ctx:
                            h.cmd_fetch(["refs/heads/main"])
            finally:
                os.chdir(original_cwd)

        self.assertIn("Trap 1", str(ctx.exception))

    def test_the_reflection_gate_still_blocks_a_push(self):
        """Fetch skips Trap 2; the identical push is still blocked by it.

        Without the paired push assertion this test would also pass if Trap 2
        had simply been broken for everyone.
        """
        h = self._helper(repo_path="/elsewhere/repo")

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            doc_dir.mkdir()
            outside = Path(tmpdir) / "outside"
            outside.mkdir()

            libs = [
                {"repo_id": "r1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            with patch(
                "git_remote_seafile.safety.get_git_config_bool", return_value=False
            ), patch.dict(os.environ, {"SEAFILE_SKIP_SAFETY_CHECKS": ""}):
                # push: the un-ignored path in a synced library is blocked
                with self.assertRaises(SafetyError) as ctx:
                    check_preflight_safety(
                        h.client, "Documents", "/elsewhere/repo",
                        local_worktree=outside, push_mode=True, synced_libs=libs,
                    )
                self.assertIn("Trap 2", str(ctx.exception))

                # fetch: the same inputs must pass, and reach the fetch body
                # -- and the spy proves the guardrails actually ran, in fetch
                # mode, rather than never being consulted at all.
                modes_seen = []
                real_check = check_preflight_safety

                def spy(client, library, path, **kwargs):
                    modes_seen.append(kwargs.get("push_mode"))
                    return real_check(client, library, path, **kwargs)

                with patch(
                    "git_remote_seafile.helper.check_preflight_safety", side_effect=spy
                ), patch(
                    "git_remote_seafile.safety.discover_local_synced_libraries",
                    return_value=libs,
                ), patch(
                    "git_remote_seafile.helper.get_git_dir",
                    side_effect=RuntimeError("reached the fetch body"),
                ):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(RuntimeError):
                            h.cmd_fetch(["refs/heads/main"])

                self.assertEqual(
                    modes_seen, [False],
                    "the fetch must be checked exactly once, in fetch mode",
                )


class TestFetchFailureIsLoud(unittest.TestCase):
    """A failed fetch must never look like a successful one.

    cmd_fetch caught every exception, wrote the protocol terminator and
    returned normally, so the helper exited 0.  Git then reported a successful
    fetch of nothing: `git clone` produced an *empty* repository and exited 0,
    and the user only found out later.  This is the fetch half of the same
    silent-success class as the refs-listing bug.
    """

    @staticmethod
    def _helper(client):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = client
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        return h

    def test_a_listing_error_propagates_and_emits_no_terminator(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.side_effect = SeafileAPIError("HTTP 500 server error")
            h = self._helper(client)

            out = io.StringIO()
            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SeafileAPIError):
                        h.cmd_fetch(["refs/heads/main"])

            self.assertEqual(
                out.getvalue(), "",
                "a failed fetch must not write the success terminator -- that is "
                "what made git report an empty repository as a successful clone",
            )

    def test_an_advertised_pack_that_cannot_be_downloaded_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [
                {"name": "pack-ffffffffffffffffffffffffffffffffffffffff.pack"},
            ]
            client.get_file_bytes.return_value = None  # 404 on the download link
            h = self._helper(client)

            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("sys.stdout", new_callable=io.StringIO):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SeafileAPIError) as ctx:
                            h.cmd_fetch(["refs/heads/main"])

            self.assertIn("pack-ffffffffffffffffffffffffffffffffffffffff.pack", str(ctx.exception))

    def test_a_successful_fetch_still_emits_the_terminator(self):
        # The guard against false failure: a healthy fetch must stay quiet and
        # terminate the protocol normally.
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack"}]
            client.get_file_bytes.return_value = b"PACKBYTES"
            h = self._helper(client)

            out = io.StringIO()
            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("git_remote_seafile.helper.install_packfile") as install:
                    with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                        h.cmd_fetch(["refs/heads/main"])

            install.assert_called_once()
            self.assertEqual(out.getvalue(), "\n")

    def test_truncated_pack_download_fails_with_size_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack", "size": 1024}]
            client.get_file_bytes.return_value = b"SHORT_BYTES"
            del client.download_file_to  # ensure fallback to get_file_bytes
            h = self._helper(client)

            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SeafileAPIError) as ctx:
                        h.cmd_fetch(["refs/heads/main"])

            self.assertIn("truncated", str(ctx.exception))

    def test_streaming_pack_download_via_download_file_to(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack", "size": 10}]
            def fake_download_file_to(repo_id, path, dest):
                Path(dest).write_bytes(b"0123456789")
                return True
            client.download_file_to.side_effect = fake_download_file_to
            h = self._helper(client)

            out = io.StringIO()
            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("git_remote_seafile.helper.install_packfile") as install:
                    with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                        h.cmd_fetch(["refs/heads/main"])

            client.download_file_to.assert_called()
            install.assert_called_once()
            self.assertEqual(out.getvalue(), "\n")


if __name__ == "__main__":
    unittest.main()
