"""test_git_util.py - Unit tests for git_util module using real and mocked Git environments."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_remote_seafile.git_util import (
    HEX_SHA_RE,
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

    def test_filter_existing_objects_ignores_input_that_is_not_a_sha(self):
        """A malformed SHA must not become an exclusion.

        The result feeds ``git rev-list ... ^<sha>``, so returning a bad value
        here would *exclude* the wrong objects from a push -- the direction that
        loses data rather than merely wasting bandwidth.  Garbage in has to mean
        "exclude nothing", and it should not reach a subprocess at all.
        """
        self.assertEqual(filter_existing_objects(["not-a-sha", "", "zzzz"]), [])

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

    def test_installing_a_pack_over_an_existing_name_succeeds(self):
        """A re-install must not die on git's own read-only pack files.

        git writes pack and index files read-only (0444) to protect them, and
        on Windows ``os.replace`` onto a read-only destination fails with
        ``PermissionError: [WinError 5]``.  Re-installing a name that was
        already there -- a retry, or a forced re-fetch -- therefore raised a
        permission error naming neither git nor the cause.  Latent today
        (``cmd_fetch`` checks for the name before downloading) and fatal for
        the first caller that retries.

        The read-only bit is set explicitly so the test reproduces git's state
        on every platform rather than only where the failure happens.
        """
        head = rev_parse("HEAD")
        pack_sha, pack_bytes, idx_bytes = create_packfile(get_objects_to_push(head))

        with tempfile.TemporaryDirectory() as td:
            bare_dir = Path(td)
            subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True)
            orig_cwd = os.getcwd()
            try:
                os.chdir(str(bare_dir))
                pack_name = f"pack-{pack_sha}.pack"
                pack_dir = bare_dir / "objects" / "pack"
                install_packfile(pack_name, pack_bytes, idx_bytes)
                (pack_dir / pack_name).chmod(0o444)
                (pack_dir / f"pack-{pack_sha}.idx").chmod(0o444)

                install_packfile(pack_name, pack_bytes, idx_bytes)

                self.assertTrue((pack_dir / pack_name).is_file())
                _, _, code = run_git(["verify-pack", "-v", str(pack_dir / f"pack-{pack_sha}.idx")])
                self.assertEqual(code, 0)
            finally:
                os.chdir(orig_cwd)

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


class TestInstallPackfileStagedInputs(unittest.TestCase):
    """install_packfile also accepts paths, and must not silently copy them.

    The streaming fetch path stages a multi-gigabyte pack on disk precisely to
    avoid buffering it in memory.  Re-copying it at install time would undo the
    reason the staging exists, so ``move=True`` has to *consume* the staged
    files rather than leave duplicates behind -- a branch the in-memory tests
    never reach, because they pass ``bytes``.
    """

    def _bare_repo(self, td: str) -> Path:
        bare_dir = Path(td)
        subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True)
        return bare_dir

    def _stage(self, stage_td: str, objs: list[str]) -> tuple[str, Path, Path]:
        """Write a pack/idx pair into *stage_td*; return (name, pack, idx).

        Built from ``create_packfile``'s in-memory output rather than its
        ``staged_dir=`` argument on purpose.  That argument makes git stage the
        pack inside the *repository's* ``objects/pack`` and then rename it to
        the requested directory, which fails with "Improper link" (EXDEV)
        whenever that directory is on another drive -- the constraint
        ``git_util.py:217-219`` documents, and which the production caller
        avoids by staging inside the git dir (``helper.py:320``).  The subject
        here is ``install_packfile``'s handling of *paths*, so the files are
        written wherever the test wants them.
        """
        pack_sha, pack_bytes, idx_bytes = create_packfile(objs)
        pack_name = f"pack-{pack_sha}.pack"
        staged_pack = Path(stage_td) / pack_name
        staged_idx = Path(stage_td) / f"pack-{pack_sha}.idx"
        staged_pack.write_bytes(pack_bytes)
        staged_idx.write_bytes(idx_bytes)
        return pack_name, staged_pack, staged_idx

    def test_install_from_staged_paths_moves_them(self):
        objs = get_objects_to_push(rev_parse("HEAD"))

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as stage_td:
            bare_dir = self._bare_repo(td)
            pack_name, staged_pack, staged_idx = self._stage(stage_td, objs)

            with patch("git_remote_seafile.git_util.get_git_dir", return_value=bare_dir):
                install_packfile(pack_name, staged_pack, staged_idx, move=True)

            pack_dir = bare_dir / "objects" / "pack"
            self.assertTrue((pack_dir / pack_name).is_file())
            self.assertTrue((pack_dir / staged_idx.name).is_file())
            self.assertFalse(staged_pack.exists(), "move=True left the staged pack behind")
            self.assertFalse(staged_idx.exists(), "move=True left the staged index behind")

    def test_install_from_staged_paths_copies_them_when_not_moving(self):
        """move=False must leave the caller's staged files alone.

        The fetch path may want to retry an install, so the default has to be
        non-destructive: the staged copy belongs to the caller, not to us.
        """
        objs = get_objects_to_push(rev_parse("HEAD"))

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as stage_td:
            bare_dir = self._bare_repo(td)
            pack_name, staged_pack, staged_idx = self._stage(stage_td, objs)

            with patch("git_remote_seafile.git_util.get_git_dir", return_value=bare_dir):
                install_packfile(pack_name, staged_pack, staged_idx)

            pack_dir = bare_dir / "objects" / "pack"
            self.assertTrue((pack_dir / pack_name).is_file())
            self.assertTrue(staged_pack.exists(), "the default must not consume the staged pack")
            self.assertTrue(staged_idx.exists(), "the default must not consume the staged index")

    def test_a_pack_whose_index_cannot_be_rebuilt_is_rejected(self):
        """Discarding a bad index is best-effort; rebuilding it is not.

        When the supplied index fails verification it is thrown away and git is
        asked to regenerate one.  If that fails too there is nothing safe to
        publish, so the install must raise rather than leave an unindexed pack
        at the final path.  The discard is allowed to fail as well -- Windows
        can hold the file open -- and must not mask the real error.
        """
        with tempfile.TemporaryDirectory() as td:
            bare_dir = self._bare_repo(td)

            with patch("git_remote_seafile.git_util.get_git_dir", return_value=bare_dir), patch(
                "pathlib.Path.unlink", side_effect=OSError("in use")
            ):
                with self.assertRaises(GitError) as ctx:
                    install_packfile("pack-badcafe.pack", b"not a pack", b"not an index")

            self.assertIn("index-pack failed", str(ctx.exception))


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


class TestHexShaValidation(unittest.TestCase):
    def test_valid_sha1_and_sha256(self):
        self.assertTrue(bool(HEX_SHA_RE.fullmatch("a" * 40)))
        self.assertTrue(bool(HEX_SHA_RE.fullmatch("F" * 40)))
        self.assertTrue(bool(HEX_SHA_RE.fullmatch("0123456789abcdef" * 4)))
        self.assertTrue(bool(HEX_SHA_RE.fullmatch("0123456789ABCDEF" * 4)))

    def test_invalid_sha(self):
        self.assertIsNone(HEX_SHA_RE.fullmatch("a" * 39))
        self.assertIsNone(HEX_SHA_RE.fullmatch("a" * 41))
        self.assertIsNone(HEX_SHA_RE.fullmatch("g" * 40))
        self.assertIsNone(HEX_SHA_RE.fullmatch("a" * 40 + "\n"))
        self.assertIsNone(HEX_SHA_RE.fullmatch(""))


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

        with patch("git_remote_seafile.git_util.get_git_config", return_value="abc"):
            # A non-numeric value must not silently read as the default --
            # a typo'd setting looks exactly like an honoured one otherwise.
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(get_git_config_int("seafile.locklease", default=60), 60)
            self.assertIn("not an integer", stderr.getvalue())
            self.assertIn("seafile.locklease", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
