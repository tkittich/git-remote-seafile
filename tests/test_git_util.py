"""test_git_util.py - Unit tests for git_util module using real and mocked Git environments."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_remote_seafile.git_util import (
    GitError,
    clean_git_env,
    create_packfile,
    filter_existing_objects,
    get_git_config,
    get_git_config_bool,
    get_git_config_int,
    get_git_dir,
    get_objects_to_push,
    install_packfile,
    is_ancestor,
    rev_parse,
    run_git,
)


class TestGitUtilWithRealGit(unittest.TestCase):
    """Integration-style tests against a real temporary Git repository."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.td.name)
        # Initialize Git repo
        subprocess.run(["git", "init", str(self.repo_dir)], check=True, capture_output=True)
        # Configure test author
        subprocess.run(["git", "-C", str(self.repo_dir), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(self.repo_dir), "config", "user.email", "test@example.com"], check=True)

        # Initial commit
        self.file1 = self.repo_dir / "file1.txt"
        self.file1.write_text("Hello Git", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo_dir), "add", "file1.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo_dir), "commit", "-m", "Initial commit"], check=True)

        self.orig_cwd = os.getcwd()
        os.chdir(str(self.repo_dir))

    def tearDown(self):
        os.chdir(self.orig_cwd)
        self.td.cleanup()

    def test_run_git(self):
        out, err, code = run_git(["status"])
        self.assertEqual(code, 0)
        self.assertIn(b"On branch", out)

    def test_get_git_dir(self):
        git_dir = get_git_dir()
        self.assertTrue(git_dir.is_dir())
        self.assertEqual(git_dir.name, ".git")

    def test_rev_parse(self):
        sha = rev_parse("HEAD")
        self.assertIsNotNone(sha)
        self.assertEqual(len(sha), 40)
        self.assertIsNone(rev_parse("nonexistent_ref_12345"))

    def test_is_ancestor(self):
        c1 = rev_parse("HEAD")
        self.file1.write_text("Hello Git 2", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "Second commit"], check=True)
        c2 = rev_parse("HEAD")

        self.assertTrue(is_ancestor(c1, c2))
        self.assertFalse(is_ancestor(c2, c1))
        # An unknown object triggers exit >= 128 from git merge-base, raising GitError
        with self.assertRaises(GitError):
            is_ancestor("0" * 40, c2)

    def test_get_objects_to_push(self):
        c1 = rev_parse("HEAD")
        self.file1.write_text("Hello Git 2", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "Second commit"], check=True)
        c2 = rev_parse("HEAD")

        all_objs = get_objects_to_push(c2)
        self.assertGreater(len(all_objs), 0)

        # Excluding c1 should return only objects unique to c2
        new_objs = get_objects_to_push(c2, exclude_shas=[c1])
        self.assertGreater(len(new_objs), 0)
        self.assertLess(len(new_objs), len(all_objs))

        # Single string exclude
        new_objs_single = get_objects_to_push(c2, exclude_shas=c1)
        self.assertEqual(new_objs, new_objs_single)

        # Delta compression hint preservation: blob objects should have path hints ("<sha> <path>")
        self.assertTrue(any(" " in obj for obj in all_objs), "Path hints should be preserved for pack-objects")

        # Multi-spec / list of SHAs support
        multi_objs = get_objects_to_push([c1, c2])
        self.assertEqual(set(all_objs), set(multi_objs))

    def test_get_objects_to_push_multiple_exclusions(self):
        # Base commit
        c1 = rev_parse("HEAD")

        # Branch B
        subprocess.run(["git", "checkout", "-b", "branch-b"], check=True)
        (self.repo_dir / "b.txt").write_text("branch b content", encoding="utf-8")
        subprocess.run(["git", "add", "b.txt"], check=True)
        subprocess.run(["git", "commit", "-m", "Commit on B"], check=True)
        b_sha = rev_parse("HEAD")

        # Branch C branching off c1
        subprocess.run(["git", "checkout", c1, "-b", "branch-c"], check=True)
        (self.repo_dir / "c.txt").write_text("branch c content", encoding="utf-8")
        subprocess.run(["git", "add", "c.txt"], check=True)
        subprocess.run(["git", "commit", "-m", "Commit on C"], check=True)
        c_sha = rev_parse("HEAD")

        # Merge B and C
        subprocess.run(["git", "merge", "--no-ff", "-m", "Merge B and C", b_sha], check=True)
        merge_sha = rev_parse("HEAD")

        # With both b_sha and c_sha excluded, only the merge commit objects should be returned.
        # With git's --not toggle bug, repeating --not would have toggled c_sha back on.
        objs_both_excluded = get_objects_to_push(merge_sha, exclude_shas=[b_sha, c_sha])
        objs_b_only_excluded = get_objects_to_push(merge_sha, exclude_shas=[b_sha])
        self.assertLess(len(objs_both_excluded), len(objs_b_only_excluded))

    def test_get_objects_to_push_ignores_exclusions_it_does_not_have(self):
        """An exclusion absent locally must be ignored, not fatal.

        Over diverged history the remote tip is a commit this clone has never
        fetched, and handing it to "rev-list --not" fails with "bad object" --
        which made a legitimate force-push impossible.
        """
        head = rev_parse("HEAD")
        missing = "0" * 40
        self.assertEqual(
            get_objects_to_push(head, exclude_shas=[missing]),
            get_objects_to_push(head),
        )

    def test_filter_existing_objects(self):
        head = rev_parse("HEAD")
        missing = "0" * 40
        self.assertEqual(filter_existing_objects([head, missing]), [head])
        self.assertEqual(filter_existing_objects([head, head]), [head])  # de-duplicated
        self.assertEqual(filter_existing_objects(head), [head])          # bare string
        self.assertEqual(filter_existing_objects([missing]), [])
        self.assertEqual(filter_existing_objects([]), [])
        self.assertEqual(filter_existing_objects(None), [])

    def test_create_and_install_packfile(self):
        c1 = rev_parse("HEAD")
        objs = get_objects_to_push(c1)
        self.assertGreater(len(objs), 0)

        # Empty objects returns empty
        self.assertEqual(create_packfile([]), ("", b"", b""))

        pack_sha, pack_bytes, idx_bytes = create_packfile(objs)
        self.assertTrue(pack_sha)
        self.assertGreater(len(pack_bytes), 0)
        self.assertGreater(len(idx_bytes), 0)

        # Test staged_dir creates and returns Path objects
        with tempfile.TemporaryDirectory() as stage_td:
            s_sha, s_pack, s_idx = create_packfile(objs, staged_dir=stage_td)
            self.assertEqual(s_sha, pack_sha)
            self.assertTrue(isinstance(s_pack, Path))
            self.assertTrue(isinstance(s_idx, Path))
            self.assertTrue(s_pack.is_file())
            self.assertTrue(s_idx.is_file())
            self.assertEqual(s_pack.read_bytes(), pack_bytes)

        # Create another bare repo and install packfile into it
        with tempfile.TemporaryDirectory() as td2:
            bare_dir = Path(td2)
            subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True)
            orig_cwd2 = os.getcwd()
            try:
                os.chdir(str(bare_dir))
                install_packfile(f"pack-{pack_sha}.pack", pack_bytes, idx_bytes)
                installed_pack = bare_dir / "objects" / "pack" / f"pack-{pack_sha}.pack"
                self.assertTrue(installed_pack.is_file())

                # Test install with automatic index generation (idx_bytes=None)
                install_packfile(f"pack-auto-{pack_sha}.pack", pack_bytes, idx_bytes=None)
                auto_idx = bare_dir / "objects" / "pack" / f"pack-auto-{pack_sha}.idx"
                self.assertTrue(auto_idx.is_file())

                # Test install with corrupted idx: must verify, discard bad idx, and regenerate locally (N-5)
                corrupt_idx_bytes = b"<html>502 Bad Gateway</html>"
                install_packfile(f"pack-corrupt-{pack_sha}.pack", pack_bytes, idx_bytes=corrupt_idx_bytes)
                corrupt_fixed_idx = bare_dir / "objects" / "pack" / f"pack-corrupt-{pack_sha}.idx"
                self.assertTrue(corrupt_fixed_idx.is_file())
                _, _, code = run_git(["verify-pack", "-v", str(corrupt_fixed_idx)])
                self.assertEqual(code, 0)
            finally:
                os.chdir(orig_cwd2)

    def test_git_config_helpers(self):
        # Set config
        subprocess.run(["git", "config", "test.key", "some_value"], check=True)
        subprocess.run(["git", "config", "test.booltrue", "true"], check=True)
        subprocess.run(["git", "config", "test.boolfalse", "false"], check=True)
        subprocess.run(["git", "config", "test.intval", "42"], check=True)
        subprocess.run(["git", "config", "test.badint", "not_a_number"], check=True)

        self.assertEqual(get_git_config("test.key"), "some_value")
        self.assertEqual(get_git_config("test.missing", default="def"), "def")

        self.assertTrue(get_git_config_bool("test.booltrue"))
        self.assertFalse(get_git_config_bool("test.boolfalse"))
        self.assertTrue(get_git_config_bool("test.missing", default=True))

        self.assertEqual(get_git_config_int("test.intval", default=0), 42)
        self.assertEqual(get_git_config_int("test.badint", default=10), 10)
        self.assertEqual(get_git_config_int("test.missing", default=99), 99)


class TestGitUtilErrorHandling(unittest.TestCase):
    """Test error handling in git_util."""

    @patch("git_remote_seafile.git_util.run_git")
    def test_get_git_dir_failure(self, mock_run):
        mock_run.return_value = (b"", b"fatal: not a git repository", 128)
        with self.assertRaises(GitError):
            get_git_dir()

    @patch("git_remote_seafile.git_util.run_git")
    def test_get_objects_to_push_failure(self, mock_run):
        mock_run.return_value = (b"", b"fatal: bad object", 128)
        with self.assertRaises(GitError):
            get_objects_to_push("deadbeef")

    @patch("git_remote_seafile.git_util.run_git")
    def test_create_packfile_pack_objects_error(self, mock_run):
        mock_run.return_value = (b"", b"fatal: pack-objects failed", 1)
        with self.assertRaises(GitError):
            create_packfile(["abcd1234abcd1234abcd1234abcd1234abcd1234"])


class TestInstallPackfileAtomicity(unittest.TestCase):
    """A failed install must not leave a partial packfile at the final path.

    cmd_fetch decides whether to download a pack by checking whether a file of
    that *name* already exists.  A truncated .pack left behind by a failed
    install is therefore never retried and poisons the object store for good,
    so the final path must only ever be populated by a complete, verified pack.
    """

    def test_failed_install_leaves_no_packfile_behind(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("git_remote_seafile.git_util.get_git_dir", return_value=Path(td)):
                with self.assertRaises(GitError):
                    install_packfile("pack-deadbeef.pack", b"this is not a packfile", None)

            pack_dir = Path(td) / "objects" / "pack"
            leftovers = sorted(p.name for p in pack_dir.glob("pack-deadbeef*")) if pack_dir.is_dir() else []
            self.assertEqual(leftovers, [], f"partial packfile left at the final path: {leftovers}")

    def test_failed_install_leaves_no_stray_temp_files(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("git_remote_seafile.git_util.get_git_dir", return_value=Path(td)):
                with self.assertRaises(GitError):
                    install_packfile("pack-deadbeef.pack", b"still not a packfile", None)

            pack_dir = Path(td) / "objects" / "pack"
            all_files = sorted(p.name for p in pack_dir.iterdir()) if pack_dir.is_dir() else []
            self.assertEqual(all_files, [], f"stray files left in the pack directory: {all_files}")


class TestCleanGitEnv(unittest.TestCase):
    """git commands aimed at a scratch repository must not inherit GIT_DIR.

    A remote helper is launched by git, which exports GIT_DIR pointing at the
    caller's repository.  That variable overrides -C, so a compaction running
    inside a helper would otherwise try to repack the caller's repo instead of
    its own scratch copy and fail with "not a git repository".
    """

    def test_removes_repo_locating_vars(self):
        with patch.dict(
            os.environ,
            {"GIT_DIR": ".git", "GIT_WORK_TREE": "/w", "GIT_INDEX_FILE": "/i", "PATH": "/bin"},
            clear=True,
        ):
            env = clean_git_env()
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_WORK_TREE", env)
        self.assertNotIn("GIT_INDEX_FILE", env)
        self.assertEqual(env.get("PATH"), "/bin")

    def test_keeps_unrelated_variables(self):
        with patch.dict(
            os.environ,
            {"GIT_AUTHOR_NAME": "someone", "SEAFILE_TOKEN": "tok"},
            clear=True,
        ):
            env = clean_git_env()
        self.assertEqual(env.get("GIT_AUTHOR_NAME"), "someone")
        self.assertEqual(env.get("SEAFILE_TOKEN"), "tok")


if __name__ == "__main__":
    unittest.main()
