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
import socket
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


class _SnapshotFixture(unittest.TestCase):
    """A source repository with ignored files, plus an empty vault path.

    Fixture only -- no test methods, on purpose.  Both the end-to-end class
    (:class:`SourceSafetyTests`) and the defaults class
    (:class:`DefaultsTests`) need this machinery, but neither may inherit the
    other's *tests*: the parallel runner schedules work by class, so a
    test-bearing base class would have every one of its cases run twice, once
    under the base's own name and once under the subclass's.
    """

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

class SourceSafetyTests(_SnapshotFixture):
    """Taking a snapshot: the source is untouched, and ignored files are in."""

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

class BasicSnapshotTests(_SnapshotFixture):
    """Excludes, no-op runs, and how additions and deletions propagate."""

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

class SnapshotGuardTests(_SnapshotFixture):
    """Layout refusals and the vault-local lock, before anything is committed."""

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
        # No host, so the holder cannot be identified and nothing is reclaimed.
        lock.write_text("1234", encoding="utf-8")
        rc, _, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("another snapshot is running", err)

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

class ByteExactnessTests(_SnapshotFixture):
    """The round trip: bytes on disk come back unchanged, filters or not."""

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

class MultiMachineTests(_SnapshotFixture):
    """One shared vault, several machines: linear chain, or a loud refusal."""

    def _second_machine(self) -> tuple[pathlib.Path, pathlib.Path]:
        """A second source tree and its own (never cloned) vault."""
        code_b = self.tmp / "code-b"
        shutil.copytree(self.code, code_b)
        return code_b, self.tmp / "vault-b"

    def test_a_second_machine_appends_to_a_shared_remote(self):
        # The bug this guards: a second machine built on its own unrelated
        # genesis, the helper rejected the push as non-fast-forward, and the
        # rejection never moved its branch back -- so it was rejected forever.
        self.snapshot("--remote", str(self.remote))
        a_tip = _git(self.remote, "rev-parse", BRANCH).stdout.strip()

        code_b, vault_b = self._second_machine()
        (code_b / "b-only.txt").write_bytes(b"from b\n")
        rc, out, err = self.run_tool(
            "--source", code_b, "--vault", vault_b, "--remote", str(self.remote)
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("push complete", out)

        # one linear chain holding both machines' snapshots
        self.assertEqual(
            _git(self.remote, "rev-list", "--count", BRANCH).stdout.strip(), "2"
        )
        tip = _git(self.remote, "rev-parse", BRANCH).stdout.strip()
        self.assertEqual(_git(self.remote, "rev-parse", f"{tip}^").stdout.strip(), a_tip)
        self.assertIn(
            "b-only.txt",
            _git(self.remote, "ls-tree", "-r", "--name-only", BRANCH).stdout,
        )

    def test_a_machine_that_is_behind_fast_forwards_first(self):
        self.snapshot("--remote", str(self.remote))
        # machine B starts from the shared chain rather than its own genesis
        _git(self.tmp, "clone", "-q", "-b", BRANCH, str(self.remote), str(self.vault))

        # machine A moves the chain on
        (self.code / "a2.txt").write_bytes(b"a2\n")
        self.snapshot("--remote", str(self.remote))
        a_tip = _git(self.remote, "rev-parse", BRANCH).stdout.strip()

        # machine B snapshots: it must extend A's newest commit, not its stale one
        (self.code / "b.txt").write_bytes(b"b\n")
        rc, out, _ = self.snapshot("--remote", str(self.remote))
        self.assertEqual(rc, 0)
        self.assertIn(a_tip[:12], out)
        self.assertEqual(
            _git(self.remote, "rev-parse", f"{BRANCH}^").stdout.strip(), a_tip
        )

    def test_diverged_vaults_are_refused_without_changing_anything(self):
        self.snapshot("--remote", str(self.remote))

        code_b, vault_b = self._second_machine()
        # B snapshots while offline, so it never sees the remote's commit
        self.run_tool("--source", code_b, "--vault", vault_b)
        b_before = _git(vault_b, "rev-parse", BRANCH).stdout.strip()

        rc, _, err = self.run_tool(
            "--source", code_b, "--vault", vault_b, "--remote", str(self.remote)
        )
        self.assertEqual(rc, 1)
        self.assertIn("diverged", err)
        # refusing is the whole point: nothing moved on either side
        self.assertEqual(_git(vault_b, "rev-parse", BRANCH).stdout.strip(), b_before)
        self.assertEqual(
            _git(self.remote, "rev-list", "--count", BRANCH).stdout.strip(), "1"
        )

    def test_reset_to_remote_adopts_the_remote_chain(self):
        self.snapshot("--remote", str(self.remote))
        code_b, vault_b = self._second_machine()
        self.run_tool("--source", code_b, "--vault", vault_b)
        (code_b / "b-only.txt").write_bytes(b"from b\n")

        rc, _, err = self.run_tool(
            "--source", code_b,
            "--vault", vault_b,
            "--remote", str(self.remote),
            "--reset-to-remote",
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            _git(self.remote, "rev-list", "--count", BRANCH).stdout.strip(), "2"
        )

    def test_a_rejected_push_rolls_the_local_branch_back(self):
        # A push the remote refuses must not leave the vault claiming a snapshot
        # the remote never took -- that divergence is otherwise permanent,
        # because the next run builds on the advanced branch and is refused
        # again.
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_bytes(b"#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        rc, _, err = self.snapshot("--remote", self.remote.as_uri())
        self.assertEqual(rc, 1)
        self.assertIn("rolled back", err)

        proc = _git(self.vault, "rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}")
        self.assertNotEqual(proc.returncode, 0)

    def test_a_push_that_loses_a_race_is_reparented_and_retried(self):
        # Two machines can land between this run's fetch and its push.  The
        # loser's commit is rejected as non-fast-forward; because a snapshot is
        # just a tree it is re-committed on the new tip and pushed again, so the
        # chain stays linear and nothing is lost.
        self.snapshot("--remote", str(self.remote))
        r = _git(self.remote, "rev-parse", BRANCH).stdout.strip()

        _git(self.tmp, "clone", "-q", "-b", BRANCH, str(self.remote), str(self.vault))
        (self.code / "b.txt").write_bytes(b"b\n")

        _git(self.remote, "config", "user.name", "Other")
        _git(self.remote, "config", "user.email", "other@example.com")
        real_git = snap.git
        state = {"raced": False}

        def racing_git(git_dir, work_tree, args, **kwargs):
            # just before the push, another machine lands a commit on the remote
            if args and args[0] == "push" and not state["raced"]:
                state["raced"] = True
                tree = _git(self.remote, "rev-parse", f"{r}^{{tree}}").stdout.strip()
                rival = _git(
                    self.remote, "commit-tree", tree, "-p", r, "-m", "rival"
                ).stdout.strip()
                _git(self.remote, "update-ref", f"refs/heads/{BRANCH}", rival)
            return real_git(git_dir, work_tree, args, **kwargs)

        with unittest.mock.patch.object(snap, "git", racing_git):
            rc, out, err = self.snapshot("--remote", str(self.remote))
        self.assertEqual(rc, 0, err)
        self.assertIn("re-parenting the snapshot and retrying", out)

        self.assertEqual(
            _git(self.remote, "rev-list", "--count", BRANCH).stdout.strip(), "3"
        )
        # the retried snapshot sits on top of the rival's commit, not beside it
        rival = _git(self.remote, "rev-parse", f"{BRANCH}^").stdout.strip()
        self.assertEqual(_git(self.remote, "rev-parse", f"{rival}^").stdout.strip(), r)

class CommitterIdentityTests(_SnapshotFixture):
    """Who commits in the vault: the source's identity, never an inherited lie."""

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

    def _global_config(self, name: str, email: str) -> pathlib.Path:
        path = self.tmp / "global-config"
        path.write_text(
            f"[user]\n\tname = {name}\n\temail = {email}\n", encoding="utf-8"
        )
        return path

    def test_the_source_identity_wins_over_a_global_one(self):
        # The bug this guards: `git config --get` reads through to the global
        # file, so the old check "does the vault have an identity?" was answered
        # *yes* by the global one, the source repo was never consulted, and the
        # vault committed under the global identity instead.  The inheritance
        # test above only passed because it cleared the global config -- a green
        # check for the wrong reason.
        global_cfg = self._global_config("Global Person", "global@example.com")
        with unittest.mock.patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": str(global_cfg)}
        ):
            rc, _, _ = self.snapshot()
        self.assertEqual(rc, 0)
        self.assertEqual(
            _git(self.vault, "config", "--local", "--get", "user.email").stdout.strip(),
            "dev@example.com",
        )
        self.assertEqual(
            _git(self.vault, "config", "--local", "--get", "user.name").stdout.strip(),
            "Dev",
        )

    def test_a_global_identity_is_accepted_when_the_source_has_none(self):
        _git(self.code, "config", "--local", "--unset", "user.email")
        _git(self.code, "config", "--local", "--unset", "user.name")
        global_cfg = self._global_config("Global Person", "global@example.com")
        with unittest.mock.patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": str(global_cfg)}
        ):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 0, err)
        # Nothing is copied into the vault: git resolves the global one itself.
        proc = _git(self.vault, "config", "--local", "--get", "user.email")
        self.assertNotEqual(proc.returncode, 0)

    def test_the_default_message_names_the_machine(self):
        # A shared vault is one chain of snapshots from several machines, and the
        # commit author is the same person on all of them -- so the message is
        # the only thing that makes `git log` readable across machines.
        self.snapshot()
        subject = _git(self.vault, "log", "-1", "--format=%s", BRANCH).stdout.strip()
        self.assertTrue(subject.startswith("snapshot "), subject)
        self.assertIn(socket.gethostname(), subject)

class ExcludeDepthTests(_SnapshotFixture):
    """The exclude matcher is not root-anchored -- monorepos depend on it."""

    def test_a_bare_exclude_name_matches_at_any_depth(self):
        # A `git rm` pathspec is anchored at the tree root, so this used to drop
        # the top-level `node_modules/` and silently keep the nested ones --
        # which is exactly the layout a monorepo has.
        (self.code / "packages" / "api" / "node_modules").mkdir(parents=True)
        (self.code / "packages" / "api" / "node_modules" / "b.js").write_bytes(b"y\n")
        (self.code / "packages" / "web" / "dist").mkdir(parents=True)
        (self.code / "packages" / "web" / "dist" / "c.js").write_bytes(b"z\n")
        (self.code / "packages" / "web" / "keep.js").write_bytes(b"k\n")

        rc, _, err = self.snapshot(
            "--exclude", "node_modules", "--exclude", "dist"
        )
        self.assertEqual(rc, 0, err)
        tree = self.tree()
        for gone in (
            "node_modules/pkg/a.js",
            "packages/api/node_modules/b.js",
            "dist/bundle.js",
            "packages/web/dist/c.js",
        ):
            with self.subTest(path=gone):
                self.assertNotIn(gone, tree)
        self.assertIn("packages/web/keep.js", tree)

    def test_a_wildcard_exclude_matches_at_any_depth(self):
        (self.code / "src" / "deep").mkdir(parents=True)
        (self.code / "src" / "deep" / "cached.pyc").write_bytes(b"p\n")
        rc, _, err = self.snapshot("--exclude", "*.pyc")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("src/deep/cached.pyc", self.tree())

    def test_the_excluded_count_is_reported(self):
        rc, out, err = self.snapshot("--exclude", "node_modules")
        self.assertEqual(rc, 0, err)
        self.assertIn("excluded: 1 path(s)", out)

class CodeRemoteRefusalTests(_SnapshotFixture):
    """A whole-tree backup must never land where the source repository pushes."""

    def test_pushing_the_vault_to_a_code_remote_is_refused(self):
        # A separate *local* repository keeps the snapshot out of the code
        # repository's object database -- but not off the code remote's server.
        _git(self.code, "remote", "add", "origin", str(self.remote))
        rc, _, err = self.snapshot("--remote", self.remote)
        self.assertEqual(rc, 1)
        self.assertIn("the same place", err)
        self.assertIn("--allow-shared-remote", err)

    def test_a_pushurl_is_checked_too(self):
        _git(self.code, "remote", "add", "origin", str(self.tmp / "other.git"))
        _git(self.code, "remote", "set-url", "--push", "origin", str(self.remote))
        rc, _, err = self.snapshot("--remote", self.remote)
        self.assertEqual(rc, 1)
        self.assertIn("the same place", err)

    def test_a_different_vault_remote_is_allowed(self):
        _git(self.code, "remote", "add", "origin", str(self.tmp / "code.git"))
        rc, _, err = self.snapshot("--remote", self.remote)
        self.assertEqual(rc, 0, err)

    def test_allow_shared_remote_overrides_the_refusal(self):
        _git(self.code, "remote", "add", "origin", str(self.remote))
        rc, _, err = self.snapshot(
            "--remote", self.remote, "--allow-shared-remote"
        )
        self.assertEqual(rc, 0, err)

    def test_a_second_run_against_the_same_remote_pushes_nothing(self):
        self.snapshot("--remote", self.remote)
        rc, out, err = self.snapshot("--remote", self.remote)
        self.assertEqual(rc, 0, err)
        self.assertIn("the remote already has this snapshot", out)

class DryRunTests(_SnapshotFixture):
    """--dry-run promises no push, and that promise covers the code repo too."""

    def test_dry_run_does_not_push_the_code_remote(self):
        # Pointed at a repository that does not exist, so a real push would
        # fail.  --dry-run promising "do not push" has to cover this too.
        _git(self.code, "remote", "add", "origin", str(self.tmp / "absent.git"))
        rc, out, err = self.snapshot("--dry-run", "--code-remote", "origin")
        self.assertEqual(rc, 0, err)
        self.assertIn("would push the source repo", out)
        self.assertNotIn("pushing source repo", out)

class CredentialWarningTests(_SnapshotFixture):
    """A backup should capture .env -- but the user has to be told it did."""

    def test_captured_credentials_are_reported(self):
        rc, out, _ = self.snapshot("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("look like credentials", out)
        self.assertIn(".env", out)

    def test_no_secret_warning_when_nothing_looks_like_one(self):
        rc, out, _ = self.snapshot("--dry-run", "--exclude", ".env")
        self.assertEqual(rc, 0)
        self.assertNotIn("look like credentials", out)

class StaleLockTests(_SnapshotFixture):
    """A lock left by a dead run is reclaimed; a live or foreign one is not."""

    def test_a_stale_lock_from_a_dead_process_is_reclaimed(self):
        self.snapshot()
        lock = self.vault / ".git" / "seafile-snapshot.lock"
        lock.write_text(f"{socket.gethostname()} 4294967295", encoding="utf-8")
        rc, out, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("reclaiming a stale lock", out)

    def test_a_lock_from_another_host_is_not_reclaimed(self):
        self.snapshot()
        lock = self.vault / ".git" / "seafile-snapshot.lock"
        lock.write_text("some-other-machine 4294967295", encoding="utf-8")
        rc, _, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("another snapshot is running", err)
        self.assertTrue(lock.exists())

    def test_a_lock_held_by_a_live_process_is_respected(self):
        self.snapshot()
        lock = self.vault / ".git" / "seafile-snapshot.lock"
        lock.write_text(f"{socket.gethostname()} {os.getpid()}", encoding="utf-8")
        rc, _, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("another snapshot is running", err)

class RestoreVerificationTests(_SnapshotFixture):
    """--restore proves the bytes, by the only check that cannot be fooled."""

    def test_restore_verifies_byte_exactness(self):
        self.snapshot("--remote", str(self.remote))
        rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, err)
        self.assertIn("verified byte-exact", out)

    def test_verify_also_runs_fsck(self):
        self.snapshot("--remote", str(self.remote))
        rc, out, err = self.restore_to(self.restored, "--verify")
        self.assertEqual(rc, 0, err)
        self.assertIn("git fsck", out)

    def test_verification_catches_a_mangled_checkout(self):
        # The bug the check exists for: bytes on disk that disagree with the
        # snapshot's blobs.  Only --no-filters hashes the file as it lies on
        # disk; the two filter-aware checks can be fooled on a checkout made
        # by something other than our own `restore` (see verify_restore).
        self.snapshot("--remote", str(self.remote))
        rc, _, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, err)
        (self.restored / "README.md").write_bytes(b"hello\r\n")

        blob = _git(self.vault, "rev-parse", f"{BRANCH}:README.md").stdout.strip()
        self.assertEqual(snap.verify_restore(self.restored, BRANCH), ["README.md"])
        # The invariant the check rests on, and the only one of the three that
        # is true of *any* checkout: the bytes on disk do not hash to the blob.
        self.assertNotEqual(
            _git(self.restored, "hash-object", "--no-filters", "--", "README.md")
            .stdout.strip(),
            blob,
        )
        # For the record: our restore pins core.autocrlf=false and
        # `* -text -filter -ident`, so on this tree the filter-aware checks
        # happen to agree as well.  That is a property of our restore, not
        # something verify_restore may lean on.
        status = _git(self.restored, "status", "--porcelain")
        self.assertIn("README.md", status.stdout)
        self.assertNotEqual(
            _git(self.restored, "hash-object", "--", "README.md").stdout.strip(),
            blob,
        )


class ExcludeMatchingTests(unittest.TestCase):
    """The matcher behind --exclude, independent of git and of a filesystem."""

    def test_a_bare_name_matches_any_depth(self):
        for path in (
            "node_modules/x.js",
            "pkg/node_modules/y.js",
            "a/b/node_modules/z.js",
        ):
            with self.subTest(path=path):
                self.assertTrue(snap.exclude_matches(path, "node_modules"))

    def test_a_bare_name_does_not_match_a_longer_component(self):
        self.assertFalse(snap.exclude_matches("my_node_modules/x.js", "node_modules"))

    def test_a_wildcard_crosses_separators(self):
        self.assertTrue(snap.exclude_matches("a/b/c.pyc", "*.pyc"))

    def test_a_trailing_slash_is_ignored(self):
        self.assertTrue(snap.exclude_matches("pkg/dist/x.js", "dist/"))

    def test_an_unrelated_name_does_not_match(self):
        self.assertFalse(snap.exclude_matches("src/main.py", "dist"))

    def test_an_empty_pattern_matches_nothing(self):
        self.assertFalse(snap.exclude_matches("anything", "  "))


class RemoteNormalisationTests(unittest.TestCase):
    """``--remote`` comparison has to be loose, and in the safe direction."""

    def test_a_trailing_slash_and_git_are_ignored(self):
        self.assertEqual(
            snap.normalise_remote("seafile://code/lib.git/"),
            snap.normalise_remote("seafile://code/lib"),
        )

    def test_the_scheme_case_is_ignored(self):
        self.assertEqual(
            snap.normalise_remote("Seafile://code/lib"),
            snap.normalise_remote("seafile://code/lib"),
        )

    def test_different_libraries_do_not_collide(self):
        self.assertNotEqual(
            snap.normalise_remote("seafile://code/lib"),
            snap.normalise_remote("seafile://code/lib-vault"),
        )


class DefaultsTests(_SnapshotFixture):
    """Every option resolves on its own, most-specific-first."""

    def test_source_defaults_to_the_working_directory(self):
        previous = os.getcwd()
        os.chdir(self.code)
        try:
            rc, out, err = self.run_tool("--vault", self.vault, "--dry-run")
        finally:
            os.chdir(previous)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"source : {self.code}", out)
        self.assertIn("vault  :", out)

    def test_a_positional_source_works_like_the_flag(self):
        rc, out, err = self.run_tool(str(self.code), "--vault", self.vault, "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"source : {self.code}", out)

    def test_two_different_sources_are_refused(self):
        other = self.tmp / "other"
        other.mkdir()
        rc, _, err = self.run_tool(
            str(other), "--source", self.code, "--vault", self.vault
        )
        self.assertEqual(rc, 1)
        self.assertIn("two different sources", err)

    def test_the_vault_defaults_to_a_sibling_of_the_source(self):
        self.assertEqual(
            snap.default_vault_for(pathlib.Path("/code/myproject")),
            pathlib.Path("/code/myproject-vault"),
        )

    def test_the_remote_is_derived_from_the_source_seafile_remote(self):
        _git(self.code, "remote", "add", "origin", "seafile://code/myproject.git")
        rc, out, err = self.run_tool(
            "--source", self.code, "--vault", self.vault, "--dry-run"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("seafile://code/myproject.git-vault", out)

    def test_a_second_seafile_remote_makes_derivation_give_up(self):
        # Two is ambiguous; guessing wrong files a whole-tree backup somewhere
        # the user did not choose, so it is better to push nothing.
        _git(self.code, "remote", "add", "a", "seafile://code/one")
        _git(self.code, "remote", "add", "b", "seafile://code/two")
        self.assertIsNone(snap.derive_vault_remote(self.code))

    def test_a_non_seafile_remote_derives_nothing(self):
        _git(self.code, "remote", "add", "origin", "https://github.com/x/y.git")
        self.assertIsNone(snap.derive_vault_remote(self.code))


class VaultMemoryTests(_SnapshotFixture):
    """The vault is where the second run's defaults come from."""

    def test_settings_are_remembered_and_a_bare_re_run_repeats_them(self):
        self.snapshot("--remote", self.remote, "--exclude", "dist")
        # A second run with no remote and no excludes at all: both come back
        # from the vault's own config.
        rc, out, err = self.run_tool("--source", self.code, "--vault", self.vault)
        self.assertEqual(rc, 0, err)
        self.assertIn("push", out.lower())
        self.assertNotIn("dist/bundle.js", self.tree())

    def test_an_explicit_flag_overrides_what_the_vault_remembers(self):
        self.snapshot("--remote", self.remote, "--exclude", "dist")
        # --no-push wins over the remembered remote.
        rc, out, err = self.run_tool(
            "--source", self.code, "--vault", self.vault, "--no-push"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("kept locally", out)

    def test_a_dry_run_does_not_write_the_remembered_settings(self):
        self.snapshot("--dry-run", "--remote", self.remote)
        self.assertEqual(snap._read_config_key(self.vault / ".git", "snapshot.remote"), "")


class VaultSourceGuardTests(_SnapshotFixture):
    """A vault remembers its source; pointing it elsewhere is refused."""

    def test_a_vault_refuses_a_different_source(self):
        self.snapshot()  # records this source in the vault
        other = self.tmp / "elsewhere"
        shutil.copytree(self.code, other)
        rc, _, err = self.run_tool("--source", other, "--vault", self.vault)
        self.assertEqual(rc, 1)
        self.assertIn("belongs to a different source", err)


class BareRunTests(_SnapshotFixture):
    """The opt-in junk list, and a cron-safe bare run."""

    def test_default_excludes_can_be_opted_into(self):
        rc, out, err = self.run_tool(
            "--source", self.code, "--vault", self.vault, "--default-excludes"
        )
        self.assertEqual(rc, 0, err)
        tree = self.tree()
        self.assertNotIn("node_modules/pkg/a.js", tree)
        self.assertNotIn("dist/bundle.js", tree)
        self.assertIn("README.md", tree)  # ordinary files are untouched

    def test_verbose_output_is_not_a_requirement(self):
        # A bare run must be quiet enough for cron: one summary, and no traceback.
        rc, out, err = self.run_tool(
            "--source", self.code, "--vault", self.vault, "--dry-run"
        )
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", out + err)


class ArgumentValidationTests(unittest.TestCase):
    """Flags that used to be silently ignored now say so."""

    def run_tool(self, *argv: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    def test_ref_without_restore_is_refused(self):
        # The sharp one: without this, a run meant to restore an old snapshot
        # quietly takes a new one instead.
        rc, _, err = self.run_tool("--ref", "snapshot~3")
        self.assertEqual(rc, 1)
        self.assertIn("--ref only applies to --restore", err)

    def test_into_without_restore_is_refused(self):
        rc, _, err = self.run_tool("--into", "C:/tmp/x")
        self.assertEqual(rc, 1)
        self.assertIn("--into only applies to --restore", err)

    def test_no_push_and_remote_together_are_refused(self):
        rc, _, err = self.run_tool("--no-push", "--remote", "seafile://code/x")
        self.assertEqual(rc, 1)
        self.assertIn("contradict each other", err)

    def test_restore_rejects_snapshot_only_flags(self):
        rc, _, err = self.run_tool(
            "--restore", "--remote", "seafile://code/x", "--into", "C:/tmp/x",
            "--dry-run",
        )
        self.assertEqual(rc, 1)
        self.assertIn("--dry-run does not apply to --restore", err)


if __name__ == "__main__":
    unittest.main()
