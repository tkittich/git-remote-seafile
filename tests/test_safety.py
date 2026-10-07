"""test_safety.py - Unit tests for git-remote-seafile safety guardrails."""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.safety import (
    SafetyError,
    check_preflight_safety,
    discover_local_synced_libraries,
    is_path_ignored,
    is_safety_checks_enabled,
    read_seafile_ignore_rules,
    _safe_relative_to,
)
from git_remote_seafile.helper import RemoteHelper


class TestSafetyGuardrails(unittest.TestCase):
    def test_is_safety_checks_enabled(self):
        # Default should be enabled
        with patch.dict(os.environ, {}, clear=True):
            with patch("git_remote_seafile.safety.get_git_config_bool", return_value=False):
                self.assertTrue(is_safety_checks_enabled())

        # Environment variable overrides
        for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
            with patch.dict(os.environ, {"SEAFILE_SKIP_SAFETY_CHECKS": val}):
                self.assertFalse(is_safety_checks_enabled())

        # Git config override
        with patch.dict(os.environ, {}, clear=True):
            with patch("git_remote_seafile.safety.get_git_config_bool", return_value=True):
                self.assertFalse(is_safety_checks_enabled())

    def test_safe_relative_to(self):
        base = Path("/home/user/Documents")
        target = Path("/home/user/Documents/code/myproject")
        rel = _safe_relative_to(target, base)
        self.assertIsNotNone(rel)
        self.assertEqual(rel.as_posix(), "code/myproject")

        # Outside base
        outside = Path("/home/user/Downloads/file.txt")
        self.assertIsNone(_safe_relative_to(outside, base))

    def test_read_seafile_ignore_rules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            ignore_file = tmppath / "seafile-ignore.txt"
            ignore_file.write_text(
                "# Comment line\n\ncode/\nseafile-git/\n  spaces/  \n# Another comment\n*.bin\n",
                encoding="utf-8",
            )
            rules = read_seafile_ignore_rules(tmppath)
            self.assertEqual(rules, ["code/", "seafile-git/", "spaces/", "*.bin"])

            # Non-existent ignore file returns empty
            empty_path = tmppath / "sub"
            self.assertEqual(read_seafile_ignore_rules(empty_path), [])

    def test_is_path_ignored(self):
        rules = ["code/", "seafile-git/", "/build/", "docs/*", "*.log"]

        # Directory prefix matching
        self.assertTrue(is_path_ignored("code/myproject", rules))
        self.assertTrue(is_path_ignored("code/deep/nested", rules))
        self.assertTrue(is_path_ignored("seafile-git/repo", rules))
        self.assertTrue(is_path_ignored("build/artifact.tar", rules))

        # Wildcards
        self.assertTrue(is_path_ignored("docs/api/index.html", rules))
        self.assertTrue(is_path_ignored("debug.log", rules))

        # Case-insensitivity
        self.assertTrue(is_path_ignored("CODE/Project", rules))
        self.assertTrue(is_path_ignored("Seafile-Git/Repo", rules))

        # Non-matching paths
        self.assertFalse(is_path_ignored("src/main.py", rules))
        self.assertFalse(is_path_ignored("unignored/code", rules))

    def test_library_root_pollution_blocked(self):
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        for bad_path in ("/", "", "///"):
            with self.assertRaises(SafetyError) as ctx:
                check_preflight_safety(client, "Documents", bad_path, push_mode=True, synced_libs=[])
            self.assertIn("library root '/'", str(ctx.exception))

    def test_trap_1_collision_blocked(self):
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            doc_dir.mkdir()
            proj_dir = doc_dir / "code" / "myproject"
            proj_dir.mkdir(parents=True)

            synced_libs = [
                {"repo_id": "r1", "name": "Documents", "worktree": doc_dir, "server_url": "https://seafile.example.com"}
            ]

            # Remote path is exact same as local worktree -> Trap 1!
            with self.assertRaises(SafetyError) as ctx:
                check_preflight_safety(
                    client,
                    "Documents",
                    "/code/myproject",
                    local_worktree=proj_dir,
                    push_mode=True,
                    synced_libs=synced_libs,
                )
            self.assertIn("DANGEROUS PATH COLLISION DETECTED (Trap 1)", str(ctx.exception))
            self.assertIn("seafile-git/", str(ctx.exception))

            # Nested inside worktree also blocked
            with self.assertRaises(SafetyError) as ctx:
                check_preflight_safety(
                    client,
                    "Documents",
                    "/code/myproject/nested",
                    local_worktree=proj_dir,
                    push_mode=True,
                    synced_libs=synced_libs,
                )
            self.assertIn("DANGEROUS PATH COLLISION DETECTED (Trap 1)", str(ctx.exception))

    def test_trap_2_unignored_in_synced_library_blocked(self):
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            doc_dir.mkdir()
            (doc_dir / "seafile-ignore.txt").write_text("code/\n", encoding="utf-8")

            # Local worktree is outside or inside, but remote points to unignored "seafile-git/myproject"
            synced_libs = [
                {"repo_id": "r1", "name": "Documents", "worktree": doc_dir, "server_url": "https://seafile.example.com"}
            ]

            # "seafile-git" is NOT in seafile-ignore.txt -> Trap 2 blocks!
            with self.assertRaises(SafetyError) as ctx:
                check_preflight_safety(
                    client,
                    "Documents",
                    "/seafile-git/myproject",
                    local_worktree=Path(tmpdir) / "other_worktree",
                    push_mode=True,
                    synced_libs=synced_libs,
                )
            self.assertIn("UNIGNORED REMOTE PATH IN SYNCED LIBRARY (Trap 2 - Download Reflection)", str(ctx.exception))
            self.assertIn("seafile-git/", str(ctx.exception))

            # Now add "seafile-git/" to seafile-ignore.txt -> Trap 2 passes!
            (doc_dir / "seafile-ignore.txt").write_text("code/\nseafile-git/\n", encoding="utf-8")
            warnings = check_preflight_safety(
                client,
                "Documents",
                "/seafile-git/myproject",
                local_worktree=Path(tmpdir) / "other_worktree",
                push_mode=True,
                synced_libs=synced_libs,
            )
            self.assertEqual(warnings, [])

    def test_unsynced_library_passes_completely(self):
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        # Synced library is Documents, but user pushes to dedicated "code" library
        synced_libs = [
            {"repo_id": "r1", "name": "Documents", "worktree": Path("/home/user/Documents"), "server_url": "https://example.com"}
        ]

        warnings = check_preflight_safety(
            client,
            "code",
            "/myproject",
            local_worktree=Path("/home/user/Documents/code/myproject"),
            push_mode=True,
            synced_libs=synced_libs,
        )
        # Should not raise any SafetyError
        self.assertIsInstance(warnings, list)

    def test_skip_safety_checks_override(self):
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with patch.dict(os.environ, {"SEAFILE_SKIP_SAFETY_CHECKS": "1"}):
            # Even with root pollution and collision, should return empty list
            res = check_preflight_safety(client, "Documents", "/", push_mode=True, synced_libs=[])
            self.assertEqual(res, [])

    def test_library_typo_suggestion(self):
        client = MagicMock()
        client.server_url = "https://seafile.example.com"
        client.get_repo_id.side_effect = Exception("Not found")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"name": "Documents"}, {"name": "Pictures"}]
        client.session.get.return_value = mock_resp

        with self.assertRaises(SafetyError) as ctx:
            check_preflight_safety(client, "docment", "/myproject", push_mode=False, synced_libs=[])

        self.assertIn("Did you mean: Documents?", str(ctx.exception))
        self.assertIn("Available libraries: Documents, Pictures", str(ctx.exception))

    def test_cmd_push_blocked_when_safety_error(self):
        h = RemoteHelper.__new__(RemoteHelper)
        h.client = MagicMock()
        h.library_name = "Documents"
        h.repo_id = "repo1"
        h.repo_path = "/"  # Triggers root pollution SafetyError

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        err_output = err.getvalue()

        self.assertIn("error refs/heads/main safety check failed", output)
        self.assertIn("[git-remote-seafile PRE-FLIGHT SAFETY BLOCK]", err_output)
        self.assertIn("Cannot use the library root '/'", err_output)
        # Verify RemoteLock was never instantiated
        h.client.upload_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
