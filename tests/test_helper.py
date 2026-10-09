"""test_helper.py - Remote-helper protocol tests: list, push, fetch, staging."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.client import SeafileAPIError
from git_remote_seafile.config import RemoteConfig
from git_remote_seafile.safety import SafetyError, check_preflight_safety
from git_remote_seafile.git_util import GitError
from git_remote_seafile.helper import RemoteHelper
from git_remote_seafile.lock import RepositoryLockedError

class TestRemoteHelper(unittest.TestCase):
    def test_capabilities(self):
        h = RemoteHelper.__new__(RemoteHelper)
        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_capabilities()
        lines = out.getvalue().splitlines()
        self.assertIn("push", lines)
        self.assertIn("fetch", lines)
        self.assertIn("option", lines)
        self.assertIn("object-format", lines)

    def test_object_format_negotiation(self):
        h = RemoteHelper.__new__(RemoteHelper)
        # Git sends 'option object-format true'
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option object-format true\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "ok\n")

        # Unsupported option replies 'unsupported\n'
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option unknown-opt value\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "unsupported\n")

    def test_unknown_command_replies_error(self):
        """An unrecognized top-level command must fail loudly, not reply with an empty no-op line."""
        h = RemoteHelper.__new__(RemoteHelper)
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("frobnicate\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "error unsupported\n")

    def test_cmd_list_detects_sha256_format(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        sha256_hash = "f" * 64
        h.client.list_dir.side_effect = lambda repo_id, path: (
            [{"type": "file", "name": "main"}] if "heads" in path else []
        )
        h.client.get_file_text.return_value = sha256_hash

        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list()

        output = out.getvalue()
        self.assertIn(":object-format sha256\n", output)
        self.assertIn(f"{sha256_hash} refs/heads/main\n", output)

    def test_detect_object_format_uses_refs_only(self):
        """Format comes from the ref SHAs, with no remote/local fallback.

        The pack-name and ``rev-parse --show-object-format`` fallbacks were
        unreachable from the only call site (refs are always 40 or 64 hex), so
        they were removed rather than left to read as a live safety net.
        """
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"

        self.assertEqual(h._detect_remote_object_format([("refs/heads/main", "a" * 40)]), "sha1")
        self.assertEqual(h._detect_remote_object_format([("refs/heads/main", "b" * 64)]), "sha256")
        h.client.list_dir.assert_not_called()

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
        mock_pack.assert_called_once()
        self.assertEqual(mock_pack.call_args[0][0], ["objA", "objB"])
        self.assertIn("staged_dir", mock_pack.call_args[1])
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

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha_main")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_aborts_ref_updates_if_lock_ownership_lost(self, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        mock_lock = MagicMock()
        mock_lock.__enter__.return_value = mock_lock
        # Ownership verification fails right before ref writes
        mock_lock.verify_ownership.return_value = False

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), patch("git_remote_seafile.helper.RemoteLock", return_value=mock_lock):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("error refs/heads/main lost lock ownership before updating refs", out.getvalue())
        self.assertIn("Push error: lost lock ownership before updating refs", err.getvalue())
        # Ref was never uploaded to remote
        h.client.upload_file.assert_not_called()

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_auto_gc_notification_when_disabled(self, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://repo/path"
        h._refs_cache = {}

        # 22 remote packfiles -> threshold is 20, auto-gc disabled
        h.client.list_dir.return_value = [{"name": f"pack-{i}.pack"} for i in range(22)]
        cfg = RemoteConfig(lock_timeout=15, lock_lease=60, auto_gc=False, gc_threshold=20)

        err = io.StringIO()
        with patch("sys.stderr", err), \
             patch("git_remote_seafile.helper.RemoteLock"), \
             patch("git_remote_seafile.helper.RemoteConfig.load", return_value=cfg):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("Notice: Remote repository has 22 packfiles", err.getvalue())
        self.assertIn("Tip: Run 'git-remote-seafile gc", err.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    @patch("git_remote_seafile.gc.compact_repository")
    def test_auto_gc_triggered_when_enabled(self, mock_compact, mock_objs, mock_ancestor, mock_rev):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://repo/path"
        h._refs_cache = {}

        # 22 remote packfiles -> threshold is 20, auto-gc enabled
        h.client.list_dir.return_value = [{"name": f"pack-{i}.pack"} for i in range(22)]
        cfg = RemoteConfig(lock_timeout=15, lock_lease=60, auto_gc=True, gc_threshold=20)

        with patch("git_remote_seafile.helper.RemoteLock"), \
             patch("git_remote_seafile.helper.RemoteConfig.load", return_value=cfg):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        mock_compact.assert_called_once_with(h.client, "repo1", "/git-repo", config=cfg, min_packs=20, verbose=True)

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
        # The directory cache must forget the deleted path (public API), or a
        # future D/F ref conflict can never be recreated.
        h.client.evict_known_dir.assert_called_once_with("repo1", "/git-repo/refs/heads/feature")

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
    def _helper(library_name="Documents", repo_path="/code/myproject", repo_id="r1"):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.client.get_repo_id.return_value = repo_id
        h.repo_id = repo_id
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

    def test_invalid_pack_name_ignored_in_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [
                {"name": "../evil.pack"},
                {"name": "malformed.pack"},
            ]
            h = self._helper(client)

            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO) as err:
                    h.cmd_fetch(["refs/heads/main"])

            self.assertEqual(out.getvalue(), "\n")
            self.assertIn("ignoring invalid remote packfile name", err.getvalue())
            client.get_file_bytes.assert_not_called()

    def test_malformed_ref_name_ignored_in_cmd_list(self):
        client = MagicMock()
        client.list_dir.side_effect = lambda repo_id, path: [
            {"name": "main", "type": "file"},
            {"name": "bad..ref", "type": "file"},
            {"name": "bad space", "type": "file"},
        ] if "refs/heads" in path else []
        client.get_file_text.return_value = "0123456789012345678901234567890123456789"
        h = self._helper(client)

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            h.cmd_list()

        output = out.getvalue()
        self.assertIn("refs/heads/main", output)
        self.assertNotIn("bad..ref", output)
        self.assertNotIn("bad space", output)
        self.assertIn("ignoring malformed ref name", err.getvalue())

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
            def fake_download_file_to(repo_id, path, dest, progress_callback=None):
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


class TestSmartPackFetchFiltering(unittest.TestCase):
    """Smart Pack Fetch Filtering (M-9):

    If all requested commit objects in fetch_specs are already present in
    the local object store, cmd_fetch terminates cleanly without downloading
    redundant remote packfiles.
    """

    @staticmethod
    def _helper(client):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = client
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        return h

    def test_fetch_skips_pack_download_when_all_objects_exist_locally(self):
        client = MagicMock()
        h = self._helper(client)
        sha1 = "1111111111111111111111111111111111111111"
        sha2 = "2222222222222222222222222222222222222222"
        out = io.StringIO()

        with patch("git_remote_seafile.helper.filter_existing_objects", return_value=[sha1, sha2]):
            with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                h.cmd_fetch([f"{sha1} refs/heads/main", f"{sha2} refs/heads/feature"])

        self.assertEqual(out.getvalue(), "\n")
        # client.list_dir should NOT even be called because packs are bypassed
        client.list_dir.assert_not_called()

    def test_fetch_downloads_packs_when_objects_missing_locally(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack"}]
            client.get_file_bytes.return_value = b"PACKBYTES"
            h = self._helper(client)
            sha1 = "1111111111111111111111111111111111111111"
            sha2 = "2222222222222222222222222222222222222222"
            out = io.StringIO()

            # Only sha1 exists locally, sha2 is missing
            with patch("git_remote_seafile.helper.filter_existing_objects", return_value=[sha1]):
                with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                    with patch("git_remote_seafile.helper.install_packfile") as install:
                        with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                            h.cmd_fetch([f"{sha1} refs/heads/main", f"{sha2} refs/heads/feature"])

            self.assertEqual(out.getvalue(), "\n")
            client.list_dir.assert_called_once()
            install.assert_called_once()


class TestPushStagingCleanup(unittest.TestCase):
    """Push Staging Cleanup (H-1):

    Ensure temporary push staging directory is cleaned up after upload.
    """

    def test_cmd_push_cleans_up_staging_directory(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        created_staging_dir = []
        original_mkdtemp = tempfile.mkdtemp

        def track_mkdtemp(*args, **kwargs):
            path = original_mkdtemp(*args, **kwargs)
            created_staging_dir.append(Path(path))
            return path

        with patch("git_remote_seafile.helper.rev_parse", return_value="sha_main"):
            with patch("git_remote_seafile.helper.is_ancestor", return_value=True):
                with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["obj1"]):
                    with patch("tempfile.mkdtemp", side_effect=track_mkdtemp):
                        with patch("git_remote_seafile.helper.create_packfile", return_value=("pack1", b"PACK", b"IDX")):
                            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                                with patch("git_remote_seafile.helper.RemoteLock"):
                                    h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertEqual(len(created_staging_dir), 1)
        self.assertFalse(created_staging_dir[0].exists(), "Push staging directory must be cleaned up")


if __name__ == "__main__":
    unittest.main()
