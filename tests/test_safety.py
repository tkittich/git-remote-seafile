"""test_safety.py - Unit tests for git-remote-seafile safety guardrails."""

import io
import os
import sqlite3
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


class TestWorkingTreeFallback(unittest.TestCase):
    """The collision check needs a directory even when git cannot supply one.

    During `git clone` the helper runs in the directory being created, which is
    not a repository yet, so `git rev-parse --show-toplevel` fails.  That
    directory is exactly the one that must be checked for a collision with the
    remote path -- and it is where the process's cwd points.
    """

    def test_falls_back_to_cwd_when_git_cannot_name_a_work_tree(self):
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            proj_dir = doc_dir / "code" / "myproject"
            proj_dir.mkdir(parents=True)

            synced_libs = [
                {"repo_id": "r1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            original_cwd = os.getcwd()
            os.chdir(proj_dir)
            try:
                with patch(
                    "git_remote_seafile.safety.get_local_work_tree", return_value=None
                ):
                    with self.assertRaises(SafetyError) as ctx:
                        check_preflight_safety(
                            client,
                            "Documents",
                            "/code/myproject",
                            local_worktree=None,
                            push_mode=False,
                            synced_libs=synced_libs,
                        )
            finally:
                os.chdir(original_cwd)

            self.assertIn("DANGEROUS PATH COLLISION DETECTED (Trap 1)", str(ctx.exception))

    def test_an_explicit_work_tree_still_wins_over_cwd(self):
        # The fallback must not override a work tree that was determined
        # properly.  Here cwd *is* inside the synced library and would collide,
        # while the explicit (correct) work tree is outside it -- so if the code
        # preferred cwd, this would raise.
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            inside = doc_dir / "code" / "myproject"
            inside.mkdir(parents=True)
            elsewhere = Path(tmpdir) / "elsewhere"
            elsewhere.mkdir()

            synced_libs = [
                {"repo_id": "r1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            original_cwd = os.getcwd()
            os.chdir(inside)
            try:
                warnings = check_preflight_safety(
                    client,
                    "Documents",
                    "/code/myproject",
                    local_worktree=elsewhere,
                    push_mode=False,
                    synced_libs=synced_libs,
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(warnings, [])


class TestDiscoverLocalSyncedLibraries(unittest.TestCase):
    """The client runs in WAL mode, so the rows sit in repo.db-wal.

    A read that copied only repo.db would find no libraries at all, and the
    pre-flight check would then silently pass on a worktree that is in fact
    inside a synced library -- which is the situation the check exists to catch.
    """

    def _seed(self, home: Path, worktree: Path, journal: str) -> sqlite3.Connection:
        ccnet = home / "ccnet"
        ccnet.mkdir(parents=True, exist_ok=True)
        worktree.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(ccnet / "repo.db"))
        con.execute(f"PRAGMA journal_mode={journal}")
        con.execute("CREATE TABLE RepoProperty (repo_id TEXT, key TEXT, value TEXT)")
        con.executemany(
            "INSERT INTO RepoProperty VALUES (?,?,?)",
            [
                ("repo-1", "worktree", str(worktree)),
                ("repo-1", "server-url", "https://seafile.example"),
                ("repo-1", "username", "alice"),
            ],
        )
        con.commit()
        return con  # left open: stands in for the running desktop client

    def test_finds_library_while_client_holds_a_wal_database_open(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            worktree = Path(td) / "MyLibrary"
            con = self._seed(home, worktree, "wal")
            try:
                self.assertTrue(
                    (home / "ccnet" / "repo.db-wal").is_file(),
                    "precondition: the rows must still be sitting in the WAL",
                )
                with patch("pathlib.Path.home", return_value=home):
                    libs = discover_local_synced_libraries()
                self.assertEqual(len(libs), 1, f"expected the synced library, got {libs}")
                self.assertEqual(libs[0]["repo_id"], "repo-1")
                self.assertEqual(libs[0]["worktree"], worktree.resolve())
                self.assertEqual(libs[0]["username"], "alice")
            finally:
                con.close()

    def test_finds_library_in_rollback_journal_mode(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            worktree = Path(td) / "OtherLibrary"
            con = self._seed(home, worktree, "delete")
            try:
                with patch("pathlib.Path.home", return_value=home):
                    libs = discover_local_synced_libraries()
                self.assertEqual([lib["repo_id"] for lib in libs], ["repo-1"])
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
