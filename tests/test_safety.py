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
from git_remote_seafile.client import SeafileAPIError, SeafileClient
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

    def test_an_unreadable_ignore_file_says_so(self):
        """Returning [] silently makes Trap 2 blame the wrong thing.

        Trap 2 tells the user to add a rule to seafile-ignore.txt.  If the file
        could not be *read*, that advice points at a file whose rule may already
        be in it, so the warning is what separates "you forgot a rule" from "I
        could not look".
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "seafile-ignore.txt").write_text("code/\n", encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=OSError("sharing violation")), \
                 patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                rules = read_seafile_ignore_rules(tmppath)

        self.assertEqual(rules, [])
        self.assertIn("could not read", mock_err.getvalue())
        self.assertIn("seafile-ignore.txt", mock_err.getvalue())
        self.assertIn("sharing violation", mock_err.getvalue())

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
                {"repo_id": "repo1", "name": "Documents", "worktree": doc_dir, "server_url": "https://seafile.example.com"}
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

            # Working tree is directly at the library root -> Trap 1 must block any subfolder
            with self.assertRaises(SafetyError) as ctx:
                check_preflight_safety(
                    client,
                    "Documents",
                    "/code/myproject",
                    local_worktree=doc_dir,
                    push_mode=True,
                    synced_libs=synced_libs,
                )
            self.assertIn("DANGEROUS PATH COLLISION DETECTED (Trap 1)", str(ctx.exception))

    def test_trap_1_matched_by_repo_id_when_folder_renamed(self):
        client = MagicMock()
        client.get_repo_id.return_value = "repo-id-123"

        with tempfile.TemporaryDirectory() as tmpdir:
            # Synced folder is named "CustomFolder" locally, but maps to repo-id-123 ("Documents" on server)
            custom_dir = Path(tmpdir) / "CustomFolder"
            custom_dir.mkdir()
            proj_dir = custom_dir / "myproject"
            proj_dir.mkdir()

            synced_libs = [
                {"repo_id": "repo-id-123", "name": "CustomFolder", "worktree": custom_dir, "server_url": "https://seafile.example.com"}
            ]

            with self.assertRaises(SafetyError) as ctx:
                check_preflight_safety(
                    client,
                    "Documents",
                    "/myproject",
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
                {"repo_id": "repo1", "name": "Documents", "worktree": doc_dir, "server_url": "https://seafile.example.com"}
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

    def test_safety_does_not_fallback_to_name_when_target_repo_id_resolved(self):
        # N-14: Server library is 'Documents' with repo_id 'server-id'.
        # Local synced library is also named 'Documents' but has repo_id 'local-id' (different library).
        # Should NOT falsely flag collision because repo_id was resolved and does not match.
        client = MagicMock()
        client.get_repo_id.return_value = "server-id"

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            doc_dir.mkdir()
            proj_dir = doc_dir / "code" / "myproject"
            proj_dir.mkdir(parents=True)

            synced_libs = [
                {"repo_id": "local-id", "name": "Documents", "worktree": doc_dir, "server_url": "https://seafile.example.com"}
            ]

            # With local_worktree outside synced library, check should return [] without SafetyError
            warnings = check_preflight_safety(
                client,
                "Documents",
                "/code/myproject",
                local_worktree=Path(tmpdir) / "outside_worktree",
                push_mode=True,
                synced_libs=synced_libs,
            )
            self.assertEqual(warnings, [])

    def test_skip_safety_checks_override(self):
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with patch.dict(os.environ, {"SEAFILE_SKIP_SAFETY_CHECKS": "1"}):
            # Even with root pollution and collision, should return empty list
            res = check_preflight_safety(client, "Documents", "/", push_mode=True, synced_libs=[])
            self.assertEqual(res, [])

    def test_library_typo_suggestion_is_reachable(self):
        """The suggestion must survive the path a user actually takes.

        It is generated in ``SeafileClient.get_repo_id``, which
        ``RemoteHelper.__init__`` calls *before* ``check_preflight_safety`` ever
        runs.  The previous test called ``check_preflight_safety`` with a stub
        client, certifying a path the user could never reach; this drives a real
        ``RemoteHelper`` over a real (session-mocked) client instead.
        """
        client = SeafileClient(server_url="https://seafile.example.com", token="tok")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [{"id": "id-1", "name": "Documents"}]
        client.session.get = MagicMock(return_value=mock_resp)

        with self.assertRaises(SeafileAPIError) as ctx:
            RemoteHelper("test", "seafile://docment/myproject", client=client)

        msg = str(ctx.exception)
        self.assertIn("Seafile library not found: 'docment'", msg)
        self.assertIn("Did you mean: Documents?", msg)

    def test_check_library_existence_ambiguous_libraries_raises_safety_error(self):
        client = MagicMock()
        client.server_url = "https://seafile.example.com"
        client.get_repo_id.side_effect = Exception("Multiple libraries named 'code' found")

        with self.assertRaises(SafetyError) as ctx:
            check_preflight_safety(client, "code", "/myproject", push_mode=False, synced_libs=[])

        self.assertIn("Ambiguous library 'code'", str(ctx.exception))

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
                {"repo_id": "repo1", "name": "Documents", "worktree": doc_dir,
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

    def test_git_dir_fallback_blocks_clone_from_outside_the_synced_library(self):
        """The cwd fallback cannot see a clone destination outside cwd; GIT_DIR can.

        Measured against real git: during ``git clone`` the helper inherits
        GIT_DIR pointing at ``<destination>/.git`` (absolute) while cwd stays
        wherever the user was.  The fallback must therefore resolve the
        destination from GIT_DIR and Trap 1 must fire on it -- this is the
        clone-from-outside-the-synced-library case that previously went
        undetected (REVIEW.glm.md P3 #4 spike).
        """
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            dest_git = doc_dir / "code" / "myproject" / ".git"
            dest_git.mkdir(parents=True)

            synced_libs = [
                {"repo_id": "repo1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            original_cwd = os.getcwd()
            os.chdir(Path(tmpdir))  # caller stands well outside the synced library
            try:
                with patch(
                    "git_remote_seafile.safety.get_local_work_tree", return_value=None
                ), patch.dict(os.environ, {"GIT_DIR": str(dest_git)}, clear=False):
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

    def test_git_dir_outside_any_synced_library_does_not_block(self):
        """A destination GIT_DIR that is not inside a synced library must pass."""
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            doc_dir.mkdir()
            elsewhere_git = Path(tmpdir) / "elsewhere" / "myproject" / ".git"
            elsewhere_git.mkdir(parents=True)

            synced_libs = [
                {"repo_id": "repo1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            original_cwd = os.getcwd()
            os.chdir(Path(tmpdir))
            try:
                with patch(
                    "git_remote_seafile.safety.get_local_work_tree", return_value=None
                ), patch.dict(os.environ, {"GIT_DIR": str(elsewhere_git)}, clear=False):
                    warnings = check_preflight_safety(
                        client,
                        "Documents",
                        "/code/myproject",
                        local_worktree=None,
                        push_mode=False,
                        synced_libs=synced_libs,
                    )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(warnings, [])

    def test_git_work_tree_wins_over_git_dir(self):
        """GIT_WORK_TREE is the more specific hint and must be preferred."""
        client = MagicMock()
        client.get_repo_id.return_value = "repo1"

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            doc_dir.mkdir()
            inside = doc_dir / "code" / "myproject"
            inside.mkdir(parents=True)
            outside_git = Path(tmpdir) / "elsewhere" / ".git"
            outside_git.mkdir(parents=True)

            synced_libs = [
                {"repo_id": "repo1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            original_cwd = os.getcwd()
            os.chdir(Path(tmpdir))
            try:
                with patch(
                    "git_remote_seafile.safety.get_local_work_tree", return_value=None
                ), patch.dict(
                    os.environ,
                    {"GIT_DIR": str(outside_git), "GIT_WORK_TREE": str(inside)},
                    clear=False,
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
