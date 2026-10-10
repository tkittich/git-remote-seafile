"""Guards for ``tools/seafile_snapshot.py``.

``git push`` transfers commits, so anything matched by ``.gitignore`` never
reaches any remote -- including this project's helper.  The snapshot tool covers
that gap by recording the whole working tree, ignored files included, as commits
in a *separate* vault repository whose ``--work-tree`` points at the source.

The property that matters is not "it uploads the files" -- it is that **the
source repository is never modified**.  The obvious alternative, renaming
``.gitignore`` aside for the duration of the push, fails exactly there: it
mutates user-visible state, so a crash or a concurrent editor leaves a
repository with no ``.gitignore`` and a fully-staged index.  Most of what
follows pins the safer design: the source's ``HEAD``, index, ``.gitignore`` and
status are byte-identical before and after a snapshot.

Every case builds a throwaway source repository plus a vault directory and
drives ``main()`` against them.  Real ``git`` is used rather than a stub,
because the whole point is the interaction between git's index, work-tree and
ref-update semantics -- a stub would test the stub.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import seafile_snapshot as snap  # noqa: E402

BRANCH = "snapshot"


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class SnapshotFixture(unittest.TestCase):
    """A source repository with ignored files, plus an empty vault path."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-snapshot-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.code = self.tmp / "code"
        self.vault = self.tmp / "vault"
        self.code.mkdir()

        # write_bytes, not write_text: on Windows write_text rewrites every
        # "\n" to "\r\n", so a fixture meant to be LF would silently be CRLF --
        # and every byte-exactness assertion below would then be testing the
        # wrong thing.
        (self.code / ".gitignore").write_bytes(b"node_modules/\n*.log\n.env\n")
        (self.code / ".env").write_bytes(b"SECRET=abc\n")
        (self.code / "app.log").write_bytes(b"log\n")
        (self.code / "README.md").write_bytes(b"hello\n")
        (self.code / "dist").mkdir()
        (self.code / "dist" / "bundle.js").write_bytes(b"built\n")
        (self.code / "node_modules" / "pkg").mkdir(parents=True)
        (self.code / "node_modules" / "pkg" / "a.js").write_bytes(b"x\n")

        _git(self.code, "init", "-q", "-b", "main", ".")
        _git(self.code, "config", "user.email", "dev@example.com")
        _git(self.code, "config", "user.name", "Dev")
        _git(self.code, "add", ".gitignore", "README.md")
        _git(self.code, "commit", "-qm", "init")

        # A stand-in for the Seafile remote, and somewhere to restore into.
        self.remote = self.tmp / "remote.git"
        _git(self.tmp, "init", "-q", "--bare", str(self.remote))
        self.restored = self.tmp / "restored"

    # -- helpers ---------------------------------------------------------

    def run_tool(self, *argv: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    def snapshot(self, *extra: object) -> tuple[int, str, str]:
        return self.run_tool(
            "--source", self.code, "--vault", self.vault, *extra
        )

    def restore_to(self, into: pathlib.Path, *extra: object):
        return self.run_tool(
            "--restore", "--remote", self.remote, "--into", into, *extra
        )

    def tree(self) -> set[str]:
        proc = _git(self.vault, "ls-tree", "-r", "--name-only", BRANCH)
        return set(proc.stdout.split())

    def commits(self) -> int:
        proc = _git(self.vault, "rev-list", "--count", BRANCH)
        return int(proc.stdout.strip() or 0)

    def source_fingerprint(self) -> tuple[str, str, bytes]:
        return (
            _git(self.code, "status", "--porcelain").stdout,
            _git(self.code, "rev-parse", "HEAD").stdout,
            (self.code / ".gitignore").read_bytes(),
        )

    # -- the safety property --------------------------------------------

    def test_snapshot_does_not_modify_the_source_repository(self):
        before = self.source_fingerprint()
        rc, _, _ = self.snapshot()
        self.assertEqual(rc, 0)
        self.assertEqual(self.source_fingerprint(), before)

    def test_dry_run_creates_no_commit(self):
        # The vault is initialised (so its location and config can be
        # inspected), but no branch and no commit is written.
        rc, out, _ = self.snapshot("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("dry run", out)
        proc = _git(self.vault, "rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}")
        self.assertNotEqual(proc.returncode, 0)

    def test_captures_gitignored_files(self):
        rc, _, _ = self.snapshot()
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.tree(),
            {
                ".env",
                ".gitignore",
                "README.md",
                "app.log",
                "dist/bundle.js",
                "node_modules/pkg/a.js",
            },
        )

    def test_never_captures_the_source_git_directory(self):
        self.snapshot()
        self.assertFalse([n for n in self.tree() if n.startswith(".git/")])

    def test_force_overrides_a_global_excludes_file(self):
        # A fresh vault with its own core.excludesFile must still be overridden
        # by `git add -f`, or a machine-level ignore rule would silently drop
        # files from every backup.
        self.snapshot()
        excludes = self.tmp / "global-excludes"
        excludes.write_text("*.log\n", encoding="utf-8")
        _git(self.vault, "config", "core.excludesFile", str(excludes))
        (self.code / "fresh.log").write_bytes(b"new\n")
        self.snapshot()
        self.assertIn("fresh.log", self.tree())

    # -- behaviour --------------------------------------------------------

    def test_excludes_are_honoured(self):
        rc, _, _ = self.snapshot(
            "--exclude", "node_modules", "--exclude", "dist"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.tree(), {".env", ".gitignore", "README.md", "app.log"}
        )

    def test_unchanged_run_creates_no_commit(self):
        self.snapshot()
        before = self.commits()
        rc, out, _ = self.snapshot()
        self.assertEqual(rc, 0)
        self.assertIn("no changes since the last snapshot", out)
        self.assertEqual(self.commits(), before)

    def test_deletions_and_additions_propagate(self):
        self.snapshot()
        (self.code / "app.log").unlink()
        (self.code / "added.txt").write_bytes(b"new\n")
        self.snapshot()
        tree = self.tree()
        self.assertNotIn("app.log", tree)
        self.assertIn("added.txt", tree)

    def test_dry_run_reports_no_changes_on_a_second_pass(self):
        self.snapshot()
        rc, out, _ = self.snapshot("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to commit", out)

    # -- guards -----------------------------------------------------------

    def test_vault_inside_the_source_is_refused(self):
        rc, _, err = self.run_tool(
            "--source", self.code, "--vault", self.code / "inner", "--dry-run"
        )
        self.assertEqual(rc, 1)
        self.assertIn("outside the source directory", err)

    def test_source_inside_the_vault_is_refused(self):
        rc, _, err = self.run_tool(
            "--source", self.code,
            "--vault", self.code.parent,  # contains the source
            "--dry-run",
        )
        self.assertEqual(rc, 1)
        self.assertIn("must not live inside the vault", err)

    def test_concurrent_run_is_blocked_by_the_lock(self):
        self.snapshot()
        lock = self.vault / ".git" / "seafile-snapshot.lock"
        lock.write_text("1234", encoding="utf-8")
        rc, _, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("another snapshot appears to be running", err)

    def test_embedded_repositories_are_reported(self):
        # git records a directory containing its own .git as a gitlink: the
        # commit id is captured, the contents are not.  Silently producing a
        # broken restore is the worst thing a backup tool can do, so this must
        # be loud.
        nested = self.code / "nested"
        nested.mkdir()
        (nested / "inner.txt").write_bytes(b"inner\n")
        _git(nested, "init", "-q", "-b", "main", ".")
        _git(nested, "config", "user.email", "inner@example.com")
        _git(nested, "config", "user.name", "Inner")
        _git(nested, "add", "-A")
        _git(nested, "commit", "-qm", "inner")

        rc, out, _ = self.snapshot()
        self.assertEqual(rc, 0)
        self.assertIn("embedded git repositories", out)
        self.assertIn("nested", out)
        # and the warning is telling the truth: the content is not there
        self.assertNotIn("nested/inner.txt", self.tree())

    # -- byte-exactness and restore ----------------------------------------

    def test_vault_is_configured_to_be_byte_exact(self):
        self.snapshot()
        proc = _git(self.vault, "config", "--get", "core.autocrlf")
        self.assertEqual(proc.stdout.strip(), "false")
        attrs = (self.vault / ".git" / "info" / "attributes").read_text(
            encoding="utf-8"
        )
        self.assertIn("* -text -filter -ident", attrs)

    def test_restore_is_byte_identical(self):
        # The blob was always correct; it was the *checkout* that converted.
        # Git for Windows ships core.autocrlf=true in its system config, so a
        # naive clone rewrites LF to CRLF and the restore stops matching the
        # original.  These bytes are the whole point of the tool.
        (self.code / "lf.txt").write_bytes(b"a\nb\nc\n")
        (self.code / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")
        (self.code / "bin.dat").write_bytes(bytes(range(256)))
        (self.code / "nonl.txt").write_bytes(b"no trailing newline")

        self.snapshot("--remote", str(self.remote))
        rc, _, _ = self.restore_to(self.restored)
        self.assertEqual(rc, 0)

        for name in (
            "lf.txt",
            "crlf.txt",
            "bin.dat",
            "nonl.txt",
            ".gitignore",
            ".env",
            "README.md",
        ):
            self.assertEqual(
                (self.restored / name).read_bytes(),
                (self.code / name).read_bytes(),
                f"{name} did not restore byte-for-byte",
            )
        self.assertEqual(
            (self.restored / "node_modules" / "pkg" / "a.js").read_bytes(),
            (self.code / "node_modules" / "pkg" / "a.js").read_bytes(),
            "the ignored file did not restore byte-for-byte",
        )

    def test_a_source_gitattributes_does_not_defeat_byte_exactness(self):
        # info/attributes outranks a .gitattributes in the source tree, and the
        # source's own .gitattributes still has to survive the round trip.
        (self.code / ".gitattributes").write_bytes(b"* text=auto\n")
        (self.code / "lf.txt").write_bytes(b"a\nb\n")

        self.snapshot("--remote", str(self.remote))
        self.restore_to(self.restored)

        self.assertEqual((self.restored / "lf.txt").read_bytes(), b"a\nb\n")
        self.assertEqual(
            (self.restored / ".gitattributes").read_bytes(), b"* text=auto\n"
        )

    def test_a_source_filter_cannot_corrupt_the_backup(self):
        # Git LFS is the common case.  A source `filter=lfs` attribute makes
        # `git add` store a ~130-byte pointer instead of the file, and the real
        # bytes are uploaded nowhere -- the vault has no LFS remote.  The backup
        # would look fine and be worthless, so `filter` has to be neutralised
        # alongside `text`.
        (self.code / ".gitattributes").write_bytes(b"*.dat filter=upper\n")
        (self.code / "data.dat").write_bytes(b"payload\n")

        self.snapshot("--dry-run")  # creates the vault
        # A filter that would visibly mangle the content if it ever ran.
        _git(self.vault, "config", "filter.upper.clean", "tr a-z A-Z")

        self.snapshot()

        def vault_git(*args: str) -> subprocess.CompletedProcess[str]:
            return _git(
                self.code,
                "--git-dir",
                str(self.vault / ".git"),
                "--work-tree",
                str(self.code),
                *args,
            )

        self.assertEqual(
            vault_git("cat-file", "-p", "snapshot:data.dat").stdout, "payload\n"
        )
        self.assertIn(
            "filter: unset", vault_git("check-attr", "filter", "--", "data.dat").stdout
        )

    def test_a_vault_written_by_an_older_version_is_upgraded(self):
        # Vaults created before the -filter fix carry only "* -text".  The
        # marker check must notice and append, and appending is safe because a
        # later line overrides an earlier one within an attributes file.
        self.snapshot("--dry-run")
        attributes = self.vault / ".git" / "info" / "attributes"
        attributes.write_bytes(b"* -text\n")

        _git(self.vault, "config", "filter.upper.clean", "tr a-z A-Z")
        (self.code / ".gitattributes").write_bytes(b"*.dat filter=upper\n")
        (self.code / "data.dat").write_bytes(b"payload\n")

        self.snapshot()

        self.assertIn(
            "* -text -filter -ident", attributes.read_text(encoding="utf-8")
        )
        stored = _git(
            self.code,
            "--git-dir",
            str(self.vault / ".git"),
            "--work-tree",
            str(self.code),
            "cat-file",
            "-p",
            "snapshot:data.dat",
        ).stdout
        self.assertEqual(stored, "payload\n")

    def test_restore_can_roll_back_to_an_older_snapshot(self):
        (self.code / "lf.txt").write_bytes(b"original\n")
        self.snapshot("--remote", str(self.remote))
        (self.code / "lf.txt").write_bytes(b"changed\n")
        self.snapshot("--remote", str(self.remote))

        rc, _, _ = self.restore_to(self.restored, "--ref", "snapshot~1")
        self.assertEqual(rc, 0)
        self.assertEqual((self.restored / "lf.txt").read_bytes(), b"original\n")

    def test_restore_requires_a_remote_and_a_destination(self):
        rc, _, err = self.run_tool("--restore", "--into", self.restored)
        self.assertEqual(rc, 1)
        self.assertIn("--restore requires --remote", err)

        rc, _, err = self.run_tool("--restore", "--remote", self.remote)
        self.assertEqual(rc, 1)
        self.assertIn("--restore requires --into", err)

    def test_restore_refuses_a_non_empty_destination(self):
        self.snapshot("--remote", str(self.remote))
        self.restored.mkdir(parents=True)
        (self.restored / "keep.txt").write_bytes(b"mine\n")

        rc, _, err = self.restore_to(self.restored)
        self.assertEqual(rc, 1)
        self.assertIn("not empty", err)
        # and it must not have touched what was already there
        self.assertEqual((self.restored / "keep.txt").read_bytes(), b"mine\n")

    # -- committer identity ------------------------------------------------

    def test_identity_is_inherited_from_the_source_repo(self):
        empty = self.tmp / "no-global-config"
        empty.write_text("", encoding="utf-8")
        with unittest.mock.patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": str(empty)}
        ):
            rc, _, _ = self.snapshot()
        self.assertEqual(rc, 0)
        proc = _git(self.vault, "config", "--local", "--get", "user.email")
        self.assertEqual(proc.stdout.strip(), "dev@example.com")

    def test_missing_identity_everywhere_is_a_clear_error(self):
        empty = self.tmp / "no-global-config"
        empty.write_text("", encoding="utf-8")
        _git(self.code, "config", "--local", "--unset", "user.email")
        _git(self.code, "config", "--local", "--unset", "user.name")
        with unittest.mock.patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": str(empty)}
        ):
            rc, _, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("no git identity is configured", err)


if __name__ == "__main__":
    unittest.main()
