"""test_git_util.py - Unit tests for git_util module using real and mocked Git environments."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from git_remote_seafile.git_util import (
    GitError,
    create_packfile,
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


if __name__ == "__main__":
    unittest.main()
