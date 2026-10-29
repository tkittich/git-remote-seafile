"""test_lfs_setup.py - ``git-remote-seafile lfs-setup`` writes the same four settings every time."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_remote_seafile.lfs_setup import run_lfs_setup, stale_lfs_destination


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


class _Repo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        empty = root / "gitconfig"
        empty.write_text("", encoding="utf-8")
        env = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def setup_lfs(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = run_lfs_setup(list(args))
        return rc, out.getvalue(), err.getvalue()

    def cfg(self, key):
        return git(self.repo, "config", "--local", "--get", key).stdout.strip()


class TestLfsSetup(_Repo):
    def test_the_library_comes_from_the_repositorys_seafile_remote(self):
        git(self.repo, "remote", "add", "origin", "seafile://code/proj")
        rc, out, err = self.setup_lfs()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cfg("lfs.customtransfer.seafile.path"), "git-remote-seafile")
        self.assertEqual(self.cfg("lfs.customtransfer.seafile.args"), "lfs-transfer seafile://code/proj")
        self.assertEqual(self.cfg("lfs.customtransfer.seafile.direction"), "both")
        self.assertEqual(self.cfg("lfs.standalonetransferagent"), "seafile")
        self.assertIn("now stores large files in seafile://code/proj", out)

    def test_running_it_again_changes_nothing_and_says_so(self):
        git(self.repo, "remote", "add", "origin", "seafile://code/proj")
        self.setup_lfs()
        rc, out, err = self.setup_lfs()
        self.assertEqual(rc, 0, err)
        self.assertIn("already stores large files", out)

    def test_a_stale_library_is_replaced_and_the_old_one_is_named(self):
        git(self.repo, "remote", "add", "origin", "seafile://old/proj")
        self.setup_lfs()
        git(self.repo, "remote", "set-url", "origin", "seafile://code/proj")
        rc, out, err = self.setup_lfs()
        self.assertEqual(rc, 0, err)
        self.assertIn("Note: the LFS destination was seafile://old/proj; it is now seafile://code/proj", out)
        self.assertEqual(self.cfg("lfs.customtransfer.seafile.args"), "lfs-transfer seafile://code/proj")

    def test_an_explicit_url_wins_over_the_remote(self):
        git(self.repo, "remote", "add", "origin", "seafile://code/proj")
        rc, out, err = self.setup_lfs("seafile://big/assets")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cfg("lfs.customtransfer.seafile.args"), "lfs-transfer seafile://big/assets")

    def test_another_remote_stops_the_all_remotes_selector_unless_asked(self):
        git(self.repo, "remote", "add", "origin", "seafile://code/proj")
        git(self.repo, "remote", "add", "github", "https://github.com/x/y.git")
        rc, out, err = self.setup_lfs()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cfg("lfs.standalonetransferagent"), "")
        self.assertIn("lfs.standalonetransferagent was not set, because this repository also has", out)
        self.assertEqual(self.cfg("lfs.customtransfer.seafile.args"), "lfs-transfer seafile://code/proj")
        rc, out, err = self.setup_lfs("--all-remotes")
        self.assertEqual(self.cfg("lfs.standalonetransferagent"), "seafile")
        self.assertNotIn("was not set", out)

    def test_a_selector_set_earlier_beside_another_remote_is_called_out(self):
        git(self.repo, "remote", "add", "origin", "seafile://code/proj")
        git(self.repo, "remote", "add", "github", "https://github.com/x/y.git")
        git(self.repo, "config", "--local", "lfs.standalonetransferagent", "seafile")
        rc, out, err = self.setup_lfs()
        self.assertIn("is already 'seafile' here", out)

    def test_no_seafile_remote_asks_for_the_url(self):
        rc, out, err = self.setup_lfs()
        self.assertEqual(rc, 1)
        self.assertIn("has no seafile:// remote to take the library from", err)

    def test_two_seafile_remotes_must_be_told_apart(self):
        git(self.repo, "remote", "add", "a", "seafile://code/one")
        git(self.repo, "remote", "add", "b", "seafile://code/two")
        rc, out, err = self.setup_lfs()
        self.assertEqual(rc, 1)
        self.assertIn("has several seafile:// remotes", err)

    def test_outside_a_repository_and_bad_arguments_are_refused(self):
        with tempfile.TemporaryDirectory() as bare:
            cwd = os.getcwd()
            os.chdir(bare)
            try:
                with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(Path(bare).parent)}):
                    rc, out, err = self.setup_lfs()
            finally:
                os.chdir(cwd)
        self.assertEqual(rc, 1)
        self.assertIn("this is not a git repository", err)
        rc, out, err = self.setup_lfs("--bogus")
        self.assertEqual(rc, 2)
        self.assertIn("Usage: git-remote-seafile lfs-setup", err)
        rc, out, err = self.setup_lfs("https://github.com/x/y.git")
        self.assertEqual(rc, 2)
        self.assertIn("is not a seafile:// URL", err)

    def test_a_missing_helper_on_the_path_is_warned_about(self):
        git(self.repo, "remote", "add", "origin", "seafile://code/proj")
        with patch("git_remote_seafile.lfs_setup.shutil.which", return_value=None):
            rc, out, err = self.setup_lfs()
        self.assertIn("is not on your PATH, so Git LFS cannot start", out)

    def test_git_lfs_itself_missing_is_warned_about(self):
        git(self.repo, "remote", "add", "origin", "seafile://code/proj")
        real = __import__("git_remote_seafile.lfs_setup", fromlist=["_git"])._git

        def fake(*args):
            return ("", 1) if args[:1] == ("lfs",) else real(*args)

        with patch("git_remote_seafile.lfs_setup._git", side_effect=fake):
            rc, out, err = self.setup_lfs()
        self.assertIn("Git LFS itself is not installed", out)


class TestStaleLfsDestination(_Repo):
    def test_a_remote_that_moved_leaves_lfs_pointing_at_the_old_library(self):
        git(self.repo, "remote", "add", "origin", "seafile://old/proj")
        self.setup_lfs()
        git(self.repo, "remote", "set-url", "origin", "seafile://code/proj")
        self.assertEqual(stale_lfs_destination(), ("seafile://old/proj", ["seafile://code/proj"]))

    def test_it_is_quiet_when_they_agree_or_there_is_nothing_to_compare(self):
        self.assertIsNone(stale_lfs_destination())                     # no agent
        git(self.repo, "remote", "add", "origin", "seafile://code/proj")
        self.assertIsNone(stale_lfs_destination())                     # still no agent
        self.setup_lfs()
        self.assertIsNone(stale_lfs_destination())                     # agree
        git(self.repo, "remote", "remove", "origin")
        self.assertIsNone(stale_lfs_destination())                     # no seafile remote left
        git(self.repo, "remote", "add", "gh", "https://github.com/x/y.git")
        self.assertIsNone(stale_lfs_destination())                     # only another forge

    def test_a_trailing_slash_is_the_same_place(self):
        git(self.repo, "remote", "add", "origin", "seafile://code/proj/")
        self.setup_lfs()
        git(self.repo, "remote", "set-url", "origin", "seafile://code/proj")
        self.assertIsNone(stale_lfs_destination())

    def test_outside_a_repository_it_says_nothing(self):
        with tempfile.TemporaryDirectory() as bare:
            cwd = os.getcwd()
            os.chdir(bare)
            try:
                with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(Path(bare).parent)}):
                    self.assertIsNone(stale_lfs_destination())
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
