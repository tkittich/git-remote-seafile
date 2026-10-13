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
    GIT_TIMEOUT,
    HEX_SHA_RE,
    GitError,
    clean_git_env,
    create_packfile,
    filter_existing_objects,
    get_git_config,
    get_git_config_bool,
    get_git_config_int,
    get_git_dir,
    get_git_timeout,
    get_objects_to_push,
    has_promisor_remote,
    install_packfile,
    is_ancestor,
    is_shallow_repository,
    objects_may_be_incomplete,
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

        # the documented truthy and falsy spellings, both ways
        with patch("git_remote_seafile.git_util.get_git_config", return_value="on"):
            self.assertTrue(get_git_config_bool("seafile.autogc"))
        with patch("git_remote_seafile.git_util.get_git_config", return_value="no"):
            self.assertFalse(get_git_config_bool("seafile.autogc"))
        with patch("git_remote_seafile.git_util.get_git_config", return_value="0"):
            self.assertFalse(get_git_config_bool("seafile.autogc"))
        with patch("git_remote_seafile.git_util.get_git_config", return_value="yes"):
            self.assertTrue(get_git_config_bool("seafile.autogc"))

        with patch("git_remote_seafile.git_util.get_git_config", return_value="abc"):
            # A non-numeric value must not silently read as the default --
            # a typo'd setting looks exactly like an honoured one otherwise.
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(get_git_config_int("seafile.locklease", default=60), 60)
            self.assertIn("not an integer", stderr.getvalue())
            self.assertIn("seafile.locklease", stderr.getvalue())


class TestCloneCompletenessProbes(unittest.TestCase):
    """is_shallow_repository / has_promisor_remote / objects_may_be_incomplete.

    These three gate the fetch-skip heuristic, which reasons from tip-commit
    presence and is therefore sound only in a full clone.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.orig_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.orig_cwd)
        self.td.cleanup()

    def _init(self, path: Path) -> Path:
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
        return path

    def _commit(self, repo: Path, name: str, body: str) -> None:
        (repo / name).write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", f"add {name}"],
            check=True, capture_output=True,
        )

    def _two_commit_source(self) -> Path:
        src = self._init(self.root / "src")
        self._commit(src, "a.txt", "a")
        self._commit(src, "b.txt", "b")
        return src

    def test_a_full_clone_is_not_reported_incomplete(self):
        src = self._two_commit_source()
        dst = self.root / "full"
        subprocess.run(["git", "clone", str(src), str(dst)], check=True, capture_output=True)

        os.chdir(str(dst))
        self.assertFalse(is_shallow_repository())
        self.assertFalse(has_promisor_remote())
        self.assertFalse(objects_may_be_incomplete())

    def test_a_shallow_clone_is_reported_incomplete(self):
        """`--depth 1` has the tip but a truncated history."""
        src = self._two_commit_source()
        dst = self.root / "shallow"
        subprocess.run(
            ["git", "clone", "--depth", "1", src.as_uri(), str(dst)],
            check=True, capture_output=True,
        )

        os.chdir(str(dst))
        self.assertTrue(is_shallow_repository())
        self.assertTrue(objects_may_be_incomplete())

    def test_a_promisor_remote_is_reported_incomplete(self):
        """The key `git clone --filter=blob:none` records on its remote."""
        src = self._two_commit_source()
        dst = self.root / "partial"
        subprocess.run(["git", "clone", str(src), str(dst)], check=True, capture_output=True)
        # Set the key directly: the probe is being tested for what it *reads*,
        # not for git's own clone plumbing.
        subprocess.run(
            ["git", "-C", str(dst), "config", "remote.origin.promisor", "true"],
            check=True, capture_output=True,
        )

        os.chdir(str(dst))
        self.assertTrue(has_promisor_remote())
        self.assertTrue(objects_may_be_incomplete())

    def test_a_promisor_key_set_to_false_is_not_incomplete(self):
        src = self._two_commit_source()
        dst = self.root / "notpartial"
        subprocess.run(["git", "clone", str(src), str(dst)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(dst), "config", "remote.origin.promisor", "false"],
            check=True, capture_output=True,
        )

        os.chdir(str(dst))
        self.assertFalse(has_promisor_remote())
        self.assertFalse(objects_may_be_incomplete())

    def test_an_unanswerable_probe_fails_safe(self):
        """No git, no answer -- and "no answer" must mean "fetch", not "skip"."""
        os.chdir(str(self.root))  # not a repository
        with patch("git_remote_seafile.git_util.run_git", side_effect=OSError("no git")):
            self.assertTrue(objects_may_be_incomplete())


class TestSubprocessTimeouts(unittest.TestCase):
    """H-24 -- a git child that never returns is an error, not a wait.

    Covers both spawn sites: ``run_git`` (which every helper-side git command
    goes through) and ``filter_existing_objects`` (which dodges it, because it
    feeds the child on stdin).  The second must also stay *safe* when its wait is
    cut short -- it answers "nothing is present", the direction that costs a
    redundant download rather than a wrong skip -- but it says so on stderr
    instead of silently looking like an empty object store.
    """

    def test_run_git_kills_the_child_and_raises_on_timeout(self):
        with patch("subprocess.Popen") as popen:
            popen.return_value.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd="git rev-parse", timeout=1),
                (b"", b""),
            ]
            with self.assertRaises(GitError) as ctx:
                run_git(["rev-parse", "HEAD"], timeout=1)
        popen.return_value.kill.assert_called_once()
        self.assertIn("did not finish within 1s", str(ctx.exception))

    def test_filter_existing_objects_warns_and_returns_empty_on_timeout(self):
        stderr = io.StringIO()
        with (
            patch("git_remote_seafile.git_util.get_git_dir", return_value=None),
            # The ceiling is resolved before the child is spawned, and resolving
            # it is itself a git call.  Pin it, or the Popen mock below is spent
            # on the config read instead of the cat-file this test is about.
            patch("git_remote_seafile.git_util.get_git_timeout", return_value=300),
            patch("subprocess.Popen") as popen,
            patch("sys.stderr", stderr),
        ):
            popen.return_value.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd="git cat-file", timeout=1),
                (b"", b""),
            ]
            self.assertEqual(filter_existing_objects(["a" * 40]), [])
        popen.return_value.kill.assert_called_once()
        self.assertIn("cat-file", stderr.getvalue())


class TestGitTimeoutCeiling(unittest.TestCase):
    """N2 -- the ceiling is raisable, but only by the machine owner.

    A hard cap protects the user from a wedged git child; a repository that could
    raise it could remove that protection, so it answers only to the environment
    and to the system and global git scopes.  That trust boundary is the point of
    this class, and the same reason ``get_trusted_git_config_bool`` exists.
    """

    @staticmethod
    def _empty_env():
        """Pin ``SEAFILE_GIT_TIMEOUT`` empty, so the config scope is the one asked."""
        return patch.dict(os.environ, {"SEAFILE_GIT_TIMEOUT": ""})

    def test_the_trusted_scope_sets_the_ceiling(self):
        with (
            patch("git_remote_seafile.git_util._GIT_TIMEOUT", None),
            self._empty_env(),
            patch(
                "git_remote_seafile.git_util.get_trusted_git_config_int",
                return_value=900,
            ),
        ):
            self.assertEqual(get_git_timeout(), 900)

    def test_the_environment_sets_the_ceiling(self):
        with (
            patch("git_remote_seafile.git_util._GIT_TIMEOUT", None),
            patch.dict(os.environ, {"SEAFILE_GIT_TIMEOUT": "900"}),
            patch(
                "git_remote_seafile.git_util.get_trusted_git_config_int",
                return_value=600,
            ) as reader,
        ):
            self.assertEqual(get_git_timeout(), 900)
        reader.assert_not_called()

    def test_a_non_integer_environment_value_is_ignored(self):
        stderr = io.StringIO()
        with (
            patch("git_remote_seafile.git_util._GIT_TIMEOUT", None),
            patch.dict(os.environ, {"SEAFILE_GIT_TIMEOUT": "soon"}),
            patch(
                "git_remote_seafile.git_util.get_trusted_git_config_int",
                return_value=GIT_TIMEOUT,
            ),
            patch("sys.stderr", stderr),
        ):
            self.assertEqual(get_git_timeout(), GIT_TIMEOUT)
        self.assertIn("not an integer", stderr.getvalue())

    def test_the_default_stands_when_nothing_is_configured(self):
        with (
            patch("git_remote_seafile.git_util._GIT_TIMEOUT", None),
            self._empty_env(),
            patch(
                "git_remote_seafile.git_util.get_trusted_git_config_int",
                return_value=GIT_TIMEOUT,
            ),
        ):
            self.assertEqual(get_git_timeout(), GIT_TIMEOUT)

    def test_a_non_positive_value_is_refused_out_loud(self):
        stderr = io.StringIO()
        with (
            patch("git_remote_seafile.git_util._GIT_TIMEOUT", None),
            self._empty_env(),
            patch(
                "git_remote_seafile.git_util.get_trusted_git_config_int",
                return_value=0,
            ),
            patch("sys.stderr", stderr),
        ):
            self.assertEqual(get_git_timeout(), GIT_TIMEOUT)
        self.assertIn("is not positive", stderr.getvalue())

    def test_a_repository_cannot_raise_its_own_ceiling(self):
        # The real reader against a real repository-local setting: if the
        # ceiling ever grew a `--local` fallback, this is what would catch it.
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(
                ["git", "-C", tmp, "config", "seafile.gittimeout", "99999"],
                check=True,
            )
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                with (
                    patch("git_remote_seafile.git_util._GIT_TIMEOUT", None),
                    self._empty_env(),
                ):
                    self.assertNotEqual(get_git_timeout(), 99999)
            finally:
                os.chdir(previous)

    def test_run_git_resolves_the_ceiling_when_none_is_given(self):
        with (
            patch(
                "git_remote_seafile.git_util.get_git_timeout", return_value=42
            ) as ceiling,
            patch("subprocess.Popen") as popen,
        ):
            popen.return_value.communicate.return_value = (b"", b"")
            run_git(["--version"])
        ceiling.assert_called_once()

    def test_the_config_is_read_once_per_process(self):
        with (
            patch("git_remote_seafile.git_util._GIT_TIMEOUT", None),
            self._empty_env(),
            patch(
                "git_remote_seafile.git_util.get_trusted_git_config_int",
                return_value=600,
            ) as reader,
        ):
            get_git_timeout()
            get_git_timeout()
        self.assertEqual(reader.call_count, 1)


class SnapshotEnvScrubListTests(unittest.TestCase):
    def test_the_snapshot_tool_scrubs_the_same_variables_as_the_library(self):
        import importlib.util
        from pathlib import Path

        from git_remote_seafile.git_util import _GIT_REPO_ENV_VARS

        path = Path(__file__).resolve().parent.parent / "tools" / "seafile_snapshot.py"
        spec = importlib.util.spec_from_file_location("_snapshot_for_env_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(tuple(module._REPO_ENV_VARS), tuple(_GIT_REPO_ENV_VARS))


class SweepStaleScratchTests(unittest.TestCase):
    def test_only_old_scratch_dirs_with_our_prefixes_are_removed(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from git_remote_seafile import git_util

        with tempfile.TemporaryDirectory() as td:
            git_dir = Path(td)
            old = git_dir / "grs-push-old"
            new = git_dir / "grs-fetch-new"
            other = git_dir / "objects"
            foreign = git_dir / "grs-other-old"
            for d in (old, new, other, foreign):
                d.mkdir()
            (old / "pack").write_bytes(b"x")
            stale = 1_000_000.0
            for d in (old, other, foreign):
                os.utime(d, (stale, stale))
            with patch.object(git_util, "get_git_dir", return_value=git_dir):
                removed = git_util.sweep_stale_scratch(max_age_seconds=3600)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(other.exists())
            self.assertTrue(foreign.exists())

    def test_outside_a_repository_it_is_a_silent_no_op(self):
        from unittest.mock import patch

        from git_remote_seafile import git_util

        with patch.object(git_util, "get_git_dir", side_effect=git_util.GitError("not a repo")):
            self.assertEqual(git_util.sweep_stale_scratch(), 0)


class TrustedConfigPrecedenceTests(unittest.TestCase):
    def _run(self, system, global_):
        from unittest.mock import patch

        from git_remote_seafile import git_util

        values = {"--system": system, "--global": global_}
        calls = []

        def fake(args, *a, **k):
            scope = args[1]
            calls.append(scope)
            v = values[scope]
            return (v.encode() if v is not None else b""), b"", (0 if v is not None else 1)

        with patch.object(git_util, "run_git", side_effect=fake):
            return git_util.get_trusted_git_config_int("seafile.gittimeout", 300), calls

    def test_global_overrides_system_as_git_itself_does(self):
        value, calls = self._run(system="111", global_="222")
        self.assertEqual(value, 222)
        self.assertEqual(calls, ["--global"])

    def test_system_is_the_fallback(self):
        value, calls = self._run(system="111", global_=None)
        self.assertEqual(value, 111)
        self.assertEqual(calls, ["--global", "--system"])

    def test_unset_everywhere_is_the_default(self):
        self.assertEqual(self._run(None, None)[0], 300)


if __name__ == "__main__":
    unittest.main()


class TestPushPackSkipsDeltaSearchForBigBlobs(unittest.TestCase):
    """Push-time ``pack-objects`` must not delta-search raw-data blobs.

    Measured: 1 GiB of 256 MiB random files plus 300 similar text files took
    50 s with the default window and 5.1 s with ``core.bigFileThreshold=50m``,
    at an identical pack size.  The default threshold (512 MiB) sends every
    50-500 MB raw file through a window of neighbours it will never deltify
    against, which is what made a 20 GB push exceed its timeout at any
    compression level.
    """

    def test_the_threshold_is_on_the_pack_objects_command_line(self):
        from git_remote_seafile import git_util

        seen: list[list[str]] = []
        real = git_util.run_git

        def spy(args, *a, **kw):
            seen.append(list(args))
            return real(args, *a, **kw)

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.txt").write_text("hello\n")
            subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
            sha = subprocess.run(
                ["git", "-C", str(repo), "ls-files", "-s"], capture_output=True, text=True, check=True
            ).stdout.split()[1]
            old = os.getcwd()
            os.chdir(repo)
            try:
                with patch.object(git_util, "run_git", spy):
                    pack_sha, pack, idx = git_util.create_packfile(
                        [sha], staged_dir=repo / "stage"
                    )
            finally:
                os.chdir(old)
            self.assertTrue(Path(pack).is_file())
            self.assertTrue(Path(idx).is_file())

        packs = [a for a in seen if "pack-objects" in a]
        self.assertEqual(len(packs), 1, seen)
        argv = packs[0]
        self.assertLess(argv.index("-c"), argv.index("pack-objects"))
        self.assertIn(f"core.bigFileThreshold={git_util.PACK_BIG_FILE_THRESHOLD}", argv)

