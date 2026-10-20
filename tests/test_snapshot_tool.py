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
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
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


def _symlinks_round_trip() -> bool:
    """Can this machine create a symlink *and* have git carry it as one?

    Probes the property the symlink tests actually need, rather than reading
    ``core.symlinks``: that key is usually unset (git auto-detects it), and
    ``os.symlink`` can succeed while git still stores the target path as an
    ordinary file.  Both halves matter -- a snapshot records a link only if
    ``git add`` writes a ``120000`` entry, and a restore reproduces one only
    if ``git checkout`` creates a link rather than a text file.

    Windows needs Developer Mode (or an elevated process) for either half, so
    the CI Windows runner answers False here and the tests skip instead of
    failing on something the platform cannot do.  Any failure at all reads as
    "no": a capability probe that raises is a probe that breaks collection.
    """
    probe = pathlib.Path(tempfile.mkdtemp(prefix="grs-symlink-probe-"))
    try:
        repo = probe / "repo"
        repo.mkdir()
        (repo / "target").write_bytes(b"x\n")
        os.symlink("target", repo / "link")
        _git(repo, "init", "-q", "-b", "main", ".")
        _git(repo, "config", "user.email", "probe@example.com")
        _git(repo, "config", "user.name", "Probe")
        _git(repo, "add", "-A")
        # Half one: is the on-disk link recorded as a 120000 entry?
        entry = _git(repo, "ls-files", "-s", "--", "link").stdout.split()
        if not entry or entry[0] != "120000":
            return False
        # Half two: does a checkout hand the link back?
        _git(repo, "commit", "-qm", "probe")
        (repo / "link").unlink()
        _git(repo, "checkout", "--", "link")
        return (repo / "link").is_symlink()
    except Exception:  # any failure means "this platform cannot do it"
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


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

    def _second_machine(self) -> tuple[pathlib.Path, pathlib.Path]:
        """A second source tree and its own (never cloned) vault.

        Lives on the fixture because two separate multi-machine classes need
        it; a shared *helper* is safe, where a shared *test* would be run twice.
        """
        code_b = self.tmp / "code-b"
        shutil.copytree(self.code, code_b)
        return code_b, self.tmp / "vault-b"

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
        # A *first* snapshot is not "no changes": a dry run never commits, so
        # the report has to compare trees rather than trust the missing commit.
        self.assertNotIn("no changes since the last snapshot", out)
        self.assertIn("a real run would record this tree", out)

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

    def test_multi_segment_exclude_matches_a_directory_prefix(self):
        """--exclude packages/web/build must exclude MORE than a bare name, not nothing.

        The old component-match test could never match a pattern containing a
        slash, so the more-precise spelling silently excluded nothing and the
        whole directory went to the remote (SNAP-03).
        """
        build = self.code / "packages" / "web" / "build"
        build.mkdir(parents=True)
        (build / "bundle.js").write_bytes(b"console.log(1)\n")
        (self.code / "packages" / "keep.txt").write_bytes(b"keep\n")
        rc, _, err = self.snapshot("--exclude", "packages/web/build")
        self.assertEqual(rc, 0, err)
        tree = self.tree()
        self.assertNotIn("packages/web/build/bundle.js", tree)
        self.assertIn("packages/keep.txt", tree)

    def test_exclude_is_case_insensitive_and_takes_backslashes(self):
        """One command line must exclude the same set on every platform.

        Windows tab-completion hands the tool backslash separators, and the
        bare-name match was case-sensitive while fnmatch was not -- so
        `--exclude Build` behaved differently from `--exclude build` (SNAP-06,
        T-11).
        """
        (self.code / "Build").mkdir()
        (self.code / "Build" / "out.o").write_bytes(b"x\n")
        rc, _, err = self.snapshot("--exclude", "build")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Build/out.o", self.tree())

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

    def _nested_repo(self, name: str = "nested") -> pathlib.Path:
        nested = self.code / name
        nested.mkdir()
        (nested / "inner.txt").write_bytes(b"inner\n")
        (nested / "sub").mkdir()
        (nested / "sub" / "deep.txt").write_bytes(b"deep\n")
        _git(nested, "init", "-q", "-b", "main", ".")
        _git(nested, "config", "user.email", "inner@example.com")
        _git(nested, "config", "user.name", "Inner")
        _git(nested, "add", "-A")
        _git(nested, "commit", "-qm", "inner")
        (nested / "ignored.log").write_bytes(b"ignored\n")
        (nested / ".gitignore").write_bytes(b"*.log\n")
        return nested

    def test_embedded_repositories_are_captured_as_their_files(self):
        # git records a directory containing its own .git as a gitlink: the
        # commit id is captured, the contents are not, and a restore of that was
        # an empty directory.  The files go in (ignored ones too -- this is a
        # whole-tree backup) and the .git does not.
        self._nested_repo()
        rc, out, _ = self.snapshot()
        self.assertEqual(rc, 0)
        self.assertIn("embedded git repositories", out)
        self.assertIn("nested (4 file(s))", out)
        self.assertNotIn("WARNING: these paths are embedded", out)
        tree = self.tree()
        for name in ("nested/inner.txt", "nested/sub/deep.txt", "nested/ignored.log"):
            self.assertIn(name, tree)
        self.assertFalse([p for p in tree if "/.git/" in p or p.endswith("/.git")])
        self.assertNotIn("160000", _git(self.vault, "ls-tree", "-r", BRANCH).stdout)

    def test_a_captured_repository_follows_changes_and_excludes(self):
        nested = self._nested_repo()
        self.assertEqual(self.snapshot()[0], 0)
        (nested / "added.txt").write_bytes(b"added\n")
        (nested / "inner.txt").unlink()
        rc, out, _ = self.snapshot("--exclude", "*.log")
        self.assertEqual(rc, 0)
        tree = self.tree()
        self.assertIn("nested/added.txt", tree)
        self.assertNotIn("nested/inner.txt", tree)
        self.assertNotIn("nested/ignored.log", tree)
        self.assertNotIn("160000", _git(self.vault, "ls-tree", "-r", BRANCH).stdout)

    def test_no_embedded_repos_keeps_the_gitlink_and_the_warning(self):
        self._nested_repo()
        rc, out, _ = self.snapshot("--no-embedded-repos")
        self.assertEqual(rc, 0)
        self.assertIn("WARNING: these paths are embedded", out)
        self.assertNotIn("nested/inner.txt", self.tree())

    def test_a_repository_with_no_commit_is_captured_not_a_crash(self):
        bare = self.code / "fresh"
        bare.mkdir()
        _git(bare, "init", "-q", "-b", "main", ".")
        (bare / "notes.txt").write_bytes(b"uncommitted\n")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("fresh/notes.txt", self.tree())

    def test_no_embedded_repos_with_a_commitless_repository_names_the_fix(self):
        bare = self.code / "fresh"
        bare.mkdir()
        _git(bare, "init", "-q", "-b", "main", ".")
        (bare / "notes.txt").write_bytes(b"uncommitted\n")
        rc, out, err = self.snapshot("--no-embedded-repos")
        self.assertEqual(rc, 1)
        self.assertIn("--exclude", err)
        self.assertIn("no commit", err)

    def test_the_flag_means_the_same_on_the_second_run_as_on_the_first(self):
        self._nested_repo()
        self.assertEqual(self.snapshot()[0], 0)
        self.assertIn("nested/inner.txt", self.tree())
        rc, out, err = self.snapshot("--no-embedded-repos")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("nested/inner.txt", self.tree())
        self.assertIn("160000", _git(self.vault, "ls-tree", "-r", BRANCH).stdout)
        self.assertIn("WARNING: these paths are embedded", out)

    def test_the_history_note_is_printed_on_every_capturing_run(self):
        nested = self._nested_repo()
        rc, first, err = self.snapshot()
        self.assertEqual(rc, 0, first + err)
        (nested / "more.txt").write_bytes(b"more\n")
        rc, second, err = self.snapshot()
        self.assertEqual(rc, 0, second + err)
        self.assertIn("embedded git repositories", first)
        self.assertIn("embedded git repositories", second)
        self.assertIn("nested (", second)

    @unittest.skipUnless(os.name == "nt", "a directory junction is a Windows reparse point")
    def test_paths_behind_a_junction_are_left_out_and_reported(self):
        target = self.tmp / "outside"
        target.mkdir()
        (target / "elsewhere.txt").write_bytes(b"not part of the source\n")
        link = self.code / "linked"
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True
        )
        if made.returncode != 0:
            self.skipTest(f"cannot create a junction here: {made.stderr or made.stdout}")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertFalse([p for p in self.tree() if p.startswith("linked/")], self.tree())
        self.assertIn("behind a Windows junction", out)

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "needs POSIX permissions that bind the current user",
    )
    def test_a_directory_that_cannot_be_read_stops_the_run_and_is_named(self):
        # git add prints a warning and exits 0 for a directory it cannot open;
        # everything under it was silently missing from the snapshot.
        locked = self.code / "locked"
        locked.mkdir()
        (locked / "secret.txt").write_bytes(b"x\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 1, out + err)
        self.assertIn("could not be read", err)
        self.assertIn("locked", err)
        self.assertIn("--exclude", err)
        # leaving it out on purpose is allowed
        rc, out, err = self.snapshot("--exclude", "locked")
        self.assertEqual(rc, 0, out + err)

    def test_an_unreadable_directory_warning_from_git_stops_the_run_on_every_platform(self):
        """The test above needs POSIX permissions and is skipped on Windows,
        leaving the guard unexercised there.  This feeds git's own warning."""
        real_git = snap.git

        def git_with_a_warning(git_dir, work_tree, args, **kw):
            proc = real_git(git_dir, work_tree, args, **kw)
            staging = args[:1] == ["add"] or (
                args[:1] == ["ls-files"] and "-o" in args and "-z" in args
            )
            if staging:
                return subprocess.CompletedProcess(
                    proc.args, proc.returncode, proc.stdout,
                    "warning: could not open directory 'locked/': Permission denied\n",
                )
            return proc

        with unittest.mock.patch.object(snap, "git", git_with_a_warning):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 1, out + err)
        self.assertIn("could not be read", err)
        self.assertIn("locked", err)

    def test_git_is_asked_for_english_messages(self):
        seen: list[dict[str, str]] = []
        real_run = snap._run

        def spy(cmd, *a, **kw):
            seen.append(dict(kw.get("env") or {}))
            return real_run(cmd, *a, **kw)

        with unittest.mock.patch.dict(os.environ, {"LC_ALL": "de_DE.UTF-8"}):
            with unittest.mock.patch.object(snap, "_run", spy):
                self.assertEqual(self.snapshot("--dry-run")[0], 0)
        gits = [e for e in seen if "GIT_LITERAL_PATHSPECS" in e]
        self.assertTrue(gits)
        for env in gits:
            self.assertEqual(env.get("LC_MESSAGES"), "C")
            self.assertEqual(env.get("LANGUAGE"), "C")
            self.assertEqual(env.get("LC_ALL"), "C")

    def test_the_opt_out_names_the_nested_repositorys_own_head(self):
        nested = self._nested_repo()
        want = _git(nested, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.snapshot("--no-embedded-repos")
        self.assertEqual(rc, 0, out + err)
        listing = _git(self.vault, "ls-tree", BRANCH, "nested").stdout
        self.assertIn(f"160000 commit {want}", listing)
        vault_head = _git(self.vault, "rev-parse", BRANCH).stdout.strip()
        self.assertNotIn(vault_head, listing, "the gitlink names the vault's own commit")

    def test_the_opt_out_after_capturing_a_commitless_repository_names_the_fix(self):
        bare = self.code / "fresh"
        bare.mkdir()
        _git(bare, "init", "-q", "-b", "main", ".")
        (bare / "notes.txt").write_bytes(b"uncommitted\n")
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.snapshot("--no-embedded-repos")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--exclude", err)

    def test_a_repository_inside_a_repository_is_one_gitlink_under_the_opt_out(self):
        nested = self._nested_repo()
        deep = nested / "deep"
        deep.mkdir()
        (deep / "d.txt").write_bytes(b"deep repo\n")
        _git(deep, "init", "-q", "-b", "main", ".")
        _git(deep, "config", "user.email", "d@example.com")
        _git(deep, "config", "user.name", "D")
        _git(deep, "add", "-A")
        _git(deep, "commit", "-qm", "deep")
        self.assertEqual(self.snapshot()[0], 0)
        self.assertIn("nested/deep/d.txt", self.tree())
        rc, out, err = self.snapshot("--no-embedded-repos")
        self.assertEqual(rc, 0, out + err)
        links = [l for l in _git(self.vault, "ls-tree", "-r", BRANCH).stdout.splitlines() if l.startswith("160000")]
        self.assertEqual(len(links), 1, links)
        self.assertTrue(links[0].endswith("\tnested"), links)

    @unittest.skipUnless(os.name == "nt", "a directory junction is a Windows reparse point")
    def test_a_junction_to_a_repository_outside_the_source_is_not_followed(self):
        outside = self.tmp / "elsewhere-repo"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"not mine\n")
        _git(outside, "init", "-q", "-b", "main", ".")
        _git(outside, "config", "user.email", "o@example.com")
        _git(outside, "config", "user.name", "O")
        _git(outside, "add", "-A")
        _git(outside, "commit", "-qm", "x")
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.code / "lib"), str(outside)],
            capture_output=True, text=True,
        )
        if made.returncode != 0:
            self.skipTest(f"cannot create a junction here: {made.stderr or made.stdout}")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertFalse([p for p in self.tree() if p.startswith("lib/")], self.tree())
        self.assertIn("behind a Windows junction", out)

    def test_a_vault_that_holds_a_gitlink_converts_on_the_next_run(self):
        self._nested_repo()
        self.assertEqual(self.snapshot("--no-embedded-repos")[0], 0)
        self.assertNotIn("nested/inner.txt", self.tree())
        self.assertEqual(self.snapshot()[0], 0)
        self.assertIn("nested/inner.txt", self.tree())

class ByteExactnessTests(_SnapshotFixture):
    """The round trip: bytes on disk come back unchanged, filters or not."""

    def test_vault_is_configured_to_be_byte_exact(self):
        self.snapshot()
        proc = _git(self.vault, "config", "--get", "core.autocrlf")
        self.assertEqual(proc.stdout.strip(), "false")
        proc_qp = _git(self.vault, "config", "--get", "core.quotepath")
        self.assertEqual(proc_qp.stdout.strip(), "false")
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
    """One shared vault, several machines: a linear chain, or a loud refusal.

    Split from :class:`PushRaceTests` on purpose.  The parallel runner schedules
    one unit of work per class, so six end-to-end snapshots in one class is
    ~110 s that cannot overlap with anything -- and it was setting the module's
    wall time on its own.  Two classes of comparable weight overlap instead.
    """

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
        # machine B starts from the shared chain rather than its own genesis: a
        # fresh clone of the remote is exactly what a second machine's vault
        # looks like.  It must NOT be self.vault -- that already holds machine
        # A's repository, so the clone would fail (and _git does not check
        # return codes): "machine B" was secretly machine A, and the reconcile
        # path this test exists for never ran.
        vault_b = self.tmp / "vault-b"
        proc = _git(self.tmp, "clone", "-q", "-b", BRANCH, str(self.remote), str(vault_b))
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # machine A moves the chain on
        (self.code / "a2.txt").write_bytes(b"a2\n")
        self.snapshot("--remote", str(self.remote))
        a_tip = _git(self.remote, "rev-parse", BRANCH).stdout.strip()

        # machine B snapshots: it must extend A's newest commit, not its stale one
        (self.code / "b.txt").write_bytes(b"b\n")
        rc, out, _ = self.run_tool(
            "--source", self.code, "--vault", vault_b, "--remote", str(self.remote)
        )
        self.assertEqual(rc, 0)
        self.assertIn(a_tip[:12], out)
        # B's stale vault fast-forwarded to A's tip before building on it --
        # both locally and on the remote, B's commit's parent is A's tip.
        self.assertEqual(
            _git(vault_b, "rev-parse", f"{BRANCH}^").stdout.strip(), a_tip
        )
        self.assertEqual(
            _git(self.remote, "rev-parse", f"{BRANCH}^").stdout.strip(), a_tip
        )

    def test_diverged_vaults_are_refused_without_changing_anything(self):
        self.snapshot("--remote", str(self.remote))

        code_b, vault_b = self._second_machine()
        # B's tree must differ from A's: two commits of the *same* tree, made
        # with the same identity inside the same wall-clock second (the
        # default message's timestamp has one-second granularity), are the
        # same commit object -- and then the vaults are legitimately not
        # divergent, so the refusal this test exists for cannot happen.  The
        # sibling reset-to-remote test below writes its marker for the same
        # reason.
        (code_b / "b-only.txt").write_bytes(b"from b\n")
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


class PushRaceTests(_SnapshotFixture):
    """A push that fails -- refused outright, or losing a race -- recovers."""

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

    def test_a_fetch_failure_during_the_retry_still_rolls_back(self):
        # The rollback is the invariant that makes a re-run the correct remedy,
        # so it must survive a failure in the retry itself: the push is
        # rejected, the tool re-fetches the tip to look for a race, and that
        # fetch dies (a network blip is exactly when this happens).  The
        # branch used to stay advanced at the rejected commit, which read as
        # permanent divergence on the next run.
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_bytes(b"#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        real_fetch = snap.fetch_remote_tip
        calls = {"n": 0}

        def fetch_that_dies_on_retry(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_fetch(*args, **kwargs)  # main()'s reconcile fetch
            raise snap.SnapshotError("network down")

        with unittest.mock.patch.object(
            snap, "fetch_remote_tip", side_effect=fetch_that_dies_on_retry
        ):
            rc, _, err = self.snapshot("--remote", self.remote.as_uri())

        self.assertEqual(calls["n"], 2)
        self.assertEqual(rc, 1)
        self.assertIn("could not re-check the remote tip", err)
        self.assertIn("rolled back", err)
        proc = _git(self.vault, "rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}")
        self.assertNotEqual(proc.returncode, 0)

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

    def test_a_fresh_dry_run_needs_no_identity_at_all(self):
        """A preview must work on a machine with no git identity anywhere.

        CI runners have none: the dry run writes nothing, so it cannot
        inherit-and-pin identity either, and building the
        preview commit used to die in commit-tree with "empty ident name".
        The preview stops at the tree, which answers everything it prints.
        """
        empty = self.tmp / "no-global"
        empty.write_text("", encoding="utf-8")
        with unittest.mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_SYSTEM": str(empty)},
        ):
            rc, out, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("dry run", out)

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

class ExcludeBeforeStagingTests(_SnapshotFixture):
    """B2 — an excluded path must never be read, hashed or written.

    ``git add -A -f .`` stages the whole tree and only then drops the excluded
    entries from the index, so the excluded bytes were read, compressed and left
    in the vault's object store as an unreferenced loose object -- on every run,
    because the excluded paths never reach the index and so the stat cache never
    helps.  Staging is now filtered before git sees the paths.
    """

    def _blob_of(self, path: pathlib.Path) -> str:
        return _git(
            self.vault, "hash-object", "--no-filters", str(path)
        ).stdout.strip()

    def test_an_excluded_file_never_reaches_the_object_store(self):
        rc, _, err = self.snapshot("--exclude", ".env")
        self.assertEqual(rc, 0, err)
        self.assertNotIn(".env", self.tree())

        blob = self._blob_of(self.code / ".env")
        self.assertNotEqual(blob, "", "could not hash the fixture's .env")
        self.assertNotEqual(
            _git(self.vault, "cat-file", "-e", blob).returncode,
            0,
            "the excluded file's content was written into the vault's object "
            "store, so the exclusion happened after staging",
        )

    def test_a_glob_shaped_name_does_not_stage_its_neighbours(self):
        """N4 -- a kept name must be staged as itself, never as a pattern.

        ``a[b].txt`` is a valid pathspec *glob*: it matches ``ab.txt`` as well as
        itself.  Handing the surviving paths to git as patterns therefore stages
        files the exclude pre-filter had already decided against -- and the
        safety net only removes them from the *index* afterwards, once their
        bytes have been read, hashed and written into the object store, which is
        the whole of B2.  ``GIT_LITERAL_PATHSPECS=1`` in the tool's git
        environment is what stops the expansion.
        """
        (self.code / "a[b].txt").write_bytes(b"kept\n")
        (self.code / "ab.txt").write_bytes(b"excluded\n")

        rc, _, err = self.snapshot("--exclude", "ab.txt")
        self.assertEqual(rc, 0, err)
        self.assertIn("a[b].txt", self.tree())
        self.assertNotIn("ab.txt", self.tree())

        blob = self._blob_of(self.code / "ab.txt")
        self.assertNotEqual(blob, "", "could not hash the fixture's ab.txt")
        self.assertNotEqual(
            _git(self.vault, "cat-file", "-e", blob).returncode,
            0,
            "the pathspec `a[b].txt` also matched `ab.txt`, so the excluded "
            "file was staged and hashed before the net dropped it",
        )

    def test_a_deleted_file_leaves_the_snapshot(self):
        """A pathspec add stages no deletion, so the stale entry must be dropped.

        Regression: switching staging to ``--pathspec-from-file`` would leave a
        file deleted from the source sitting in the snapshot for ever, because
        nothing in the pathspec names it and the index persists between runs.
        """
        self.assertEqual(self.snapshot("--exclude", "node_modules")[0], 0)
        self.assertIn("dist/bundle.js", self.tree())

        (self.code / "dist" / "bundle.js").unlink()
        rc, _, err = self.snapshot("--exclude", "node_modules")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("dist/bundle.js", self.tree())

    def test_a_file_that_becomes_excluded_leaves_the_snapshot(self):
        """An exclude added later must also drop what a previous run recorded."""
        self.assertEqual(self.snapshot("--exclude", "node_modules")[0], 0)
        self.assertIn("dist/bundle.js", self.tree())

        rc, _, err = self.snapshot("--exclude", "node_modules", "--exclude", "dist")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("dist/bundle.js", self.tree())

    def test_ignored_files_are_still_captured_without_excludes(self):
        """The enumeration must not quietly acquire `.gitignore` semantics.

        ``ls-files -o`` *with* ``--exclude-standard`` would drop exactly the
        ignored files ``-f`` exists to force in -- a silent, one-way data loss.
        """
        rc, _, err = self.snapshot()
        self.assertEqual(rc, 0, err)
        tree = self.tree()
        self.assertIn(".env", tree)
        self.assertIn("app.log", tree)
        self.assertIn("node_modules/pkg/a.js", tree)


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

    def test_location_patterns_match_at_any_depth(self):
        # .ssh/* and .aws/credentials name a *location*, so they must match
        # below the tree root too -- fnmatch anchors at the start, which used
        # to make them root-only and blind to deploy/.ssh/known_hosts.
        self.assertEqual(
            snap.find_secrets(
                ["deploy/.ssh/known_hosts", "infra/.aws/credentials"]
            ),
            ["deploy/.ssh/known_hosts", "infra/.aws/credentials"],
        )
        (self.code / "deploy" / ".ssh").mkdir(parents=True)
        (self.code / "deploy" / ".ssh" / "known_hosts").write_bytes(b"host key\n")
        rc, out, _ = self.snapshot("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("deploy/.ssh/known_hosts", out)

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

    def test_an_unparseable_lock_is_reclaimed_once_it_is_old(self):
        # A run that died between creating the lock and writing it leaves an
        # empty file that can never name its owner.  A fresh one may be a run
        # mid-write, so it is respected; an old one is reclaimed.
        self.snapshot()
        lock = self.vault / ".git" / "seafile-snapshot.lock"
        lock.write_bytes(b"")
        rc, _, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("another snapshot is running", err)

        old = time.time() - (snap._UNPARSEABLE_LOCK_GRACE_SECONDS + 5)
        os.utime(lock, (old, old))
        rc, out, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("unparseable lock", out)

    @unittest.skipUnless(os.name == "nt", "exercises the Win32 API directly")
    def test_an_access_denied_process_is_not_declared_dead(self):
        # OpenProcess fails with ERROR_ACCESS_DENIED for a live process owned
        # by another (elevated) user; reading that as "dead" would let a
        # concurrent run steal its lock.  Only "no such process" proves death.
        #
        # pid_is_alive builds its own WinDLL("kernel32", use_last_error=True)
        # and reads the failure through ctypes.get_last_error() -- the
        # documented pattern: reading GetLastError()
        # through ctypes.windll can return a stale value.  The real module's
        # get_last_error is patched so both outcomes below exercise the
        # dispatch logic against a controlled error code.
        import ctypes

        # OpenProcess is stubbed to fail: with a real call, a hosted runner that
        # happens to be running a process with this pid opens it, never reaches
        # the error dispatch, and the second assertion fails (v0.9.8 release job,
        # windows-latest 3.12).
        failing_kernel32 = unittest.mock.Mock()
        failing_kernel32.OpenProcess.return_value = None
        real_get_last_error = ctypes.get_last_error
        with unittest.mock.patch.object(
            snap, "_win_kernel32", return_value=failing_kernel32
        ):
            with unittest.mock.patch.object(
                ctypes, "get_last_error", return_value=5
            ):  # ERROR_ACCESS_DENIED
                self.assertTrue(snap.pid_is_alive(1234))
            with unittest.mock.patch.object(
                ctypes, "get_last_error", return_value=87
            ):  # ERROR_INVALID_PARAMETER: no such process
                self.assertFalse(snap.pid_is_alive(1234))
        # The patch restored the real reader (leak-check discipline).
        self.assertEqual(ctypes.get_last_error, real_get_last_error)

class RestoreVerificationTests(_SnapshotFixture):
    """--restore proves the bytes, by the only check that cannot be fooled."""

    def test_restore_verifies_byte_exactness(self):
        self.snapshot("--remote", str(self.remote))
        rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, err)
        self.assertIn("verified byte-exact", out)

    def test_a_gitlink_in_the_snapshot_warns_at_restore_time(self):
        """The snapshot-time warning must be repeated where the hole opens.

        A snapshot holding an embedded repository (a gitlink) restores as an
        empty directory -- the commit id was recorded, not the contents.  The
        only warning used to live at snapshot time, possibly years and
        machines away, while the restore printed "verified byte-exact".
        """
        inner = self.code / "vendor-lib"
        inner.mkdir()
        _git(inner, "init", "-q", "-b", "main")
        (inner / "lib.c").write_text("int f(void) { return 0; }\n", encoding="utf-8")
        _git(inner, "add", "-A")
        _git(inner, "-c", "user.email=e@x", "-c", "user.name=v",
             "commit", "-q", "-m", "inner")

        self.snapshot("--remote", str(self.remote), "--no-embedded-repos")
        rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, err)
        self.assertIn("embedded git repositories", out)
        self.assertIn("vendor-lib", out)

    def test_a_restore_that_fails_after_the_clone_says_where_it_left_the_tree(self):
        """The retry used to stop with "destination is not empty" and no reason."""
        self.snapshot("--remote", str(self.remote))
        with unittest.mock.patch.object(snap, "verify_restore", return_value=["README.md"]):
            rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("left in", err)
        self.assertIn("delete it before running the restore again", err)
        self.assertTrue(any(self.restored.iterdir()))

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


class SymlinkVerificationTests(unittest.TestCase):
    """``verify_restore`` must judge a symlink by *its own* blob.

    A symlink is a tree entry of mode ``120000`` whose blob is the link
    target, and :func:`verify_restore` collects those entries into a list
    deliberately kept apart from the regular files'.  Reading that list as if
    it were parallel to the other one compared a link against an unrelated
    file's blob, so a correctly restored link was reported as a mismatch --
    and a mismatch aborts the restore -- while a tree holding nothing but
    symlinks raised IndexError outright.

    Both trees here are built with ``update-index --cacheinfo``, which records
    a ``120000`` entry *without* asking the OS for a symlink, so these cases
    run on every platform -- including the Windows CI runner, where a real
    symlink is not available.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-symlink-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _repo_with(
        self, regular: dict[str, bytes], links: dict[str, str]
    ) -> pathlib.Path:
        """A committed repo holding the given files and ``120000`` entries.

        The links exist only in the index: ``--cacheinfo`` never touches the
        working tree, which is exactly the state a Windows checkout without
        ``core.symlinks`` leaves behind (a path that is not a link on disk).
        """
        repo = self.tmp / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main", ".")
        _git(repo, "config", "user.email", "dev@example.com")
        _git(repo, "config", "user.name", "Dev")
        for name, body in regular.items():
            (repo / name).write_bytes(body)
        _git(repo, "add", *regular)
        scratch = self.tmp / "scratch"
        for name, target in links.items():
            scratch.write_bytes(target.encode())
            sha = _git(repo, "hash-object", "-w", "--", str(scratch)).stdout.strip()
            # The comma form: the space-separated `--cacheinfo <mode> <sha>
            # <path>` spelling is rejected with "option `cacheinfo' takes no
            # value" on the git versions this project supports.
            _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{sha},{name}")
        _git(repo, "commit", "-qm", "tree")
        return repo

    def test_a_tree_of_only_symlinks_does_not_raise(self):
        # The crash: `expected` (regular files) is empty while `symlinks` is
        # not, so indexing `expected` by the link's position raised IndexError
        # and no restore of such a tree could complete.
        #
        # The links have to be *readable* for the old code to reach the lookup
        # at all -- an unreadable one is reported and skipped -- so os.readlink
        # is patched to return the target the blobs hold: the tree as a POSIX
        # restore produces it, where every link is correct and the only thing
        # missing is a regular file to index.
        repo = self._repo_with({}, {"a": "target", "b": "target"})
        with unittest.mock.patch.object(os, "readlink", return_value="target"):
            self.assertEqual(snap.verify_restore(repo, "main"), [])

    def test_a_correctly_restored_link_matches_its_own_blob(self):
        # The regression, and the reason this is a bug rather than a doc fix:
        # with `expected` non-empty the old lookup used the *first regular
        # file's* blob for the link, so a link whose bytes on disk were right
        # was still reported -- and a report aborts the restore.
        #
        # os.readlink is what verify_restore uses to judge a link, so it is
        # patched to return the target the blob holds: the state a POSIX
        # restore produces.  The tree deliberately holds a regular file whose
        # blob differs from that target, because that file is what the old
        # positional lookup would have compared the link against.
        repo = self._repo_with({"data.txt": b"unrelated bytes\n"}, {"a": "target"})
        with unittest.mock.patch.object(os, "readlink", return_value="target"):
            self.assertEqual(snap.verify_restore(repo, "main"), [])

    def test_a_link_absent_from_the_working_tree_is_reported(self):
        # The Windows text-file case, and the one legitimate mismatch: the
        # entry is a link in the tree, but nothing readable as a link is on
        # disk.  verify_restore must still name it rather than pass silently.
        repo = self._repo_with({"data.txt": b"unrelated bytes\n"}, {"a": "target"})
        self.assertEqual(snap.verify_restore(repo, "main"), ["a"])


@unittest.skipUnless(
    _symlinks_round_trip(), "this machine cannot round-trip a symlink"
)
class SymlinkSnapshotTests(_SnapshotFixture):
    """A real symlink survives a snapshot and comes back as a link.

    The unit cases above pin :func:`verify_restore` in isolation; these drive
    the whole tool, so the capture side (``git add`` recording ``120000``) and
    the restore side (``git checkout`` making a link) are covered too.  They
    skip where the platform cannot do either half.
    """

    def test_a_snapshot_records_a_symlink_as_a_120000_entry(self):
        os.symlink("README.md", self.code / "link.txt")
        rc, out, err = self.snapshot("--remote", str(self.remote))
        self.assertEqual(rc, 0, err)
        entry = _git(
            self.vault, "ls-tree", BRANCH, "--", "link.txt"
        ).stdout.split()
        self.assertEqual(entry[0], "120000")
        # The blob is the target *path*, not the target's contents.
        self.assertEqual(
            _git(self.vault, "cat-file", "blob", f"{BRANCH}:link.txt").stdout,
            "README.md",
        )

    def test_a_restored_symlink_is_not_reported_as_a_mismatch(self):
        os.symlink("README.md", self.code / "link.txt")
        self.snapshot("--remote", str(self.remote))
        rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.restored / "link.txt").is_symlink())
        self.assertEqual(os.readlink(self.restored / "link.txt"), "README.md")
        self.assertEqual(snap.verify_restore(self.restored, BRANCH), [])


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
        # The tool resolves paths; CI temp dirs come back canonicalized
        # (/var -> /private/var on macOS, RUNNER~1 -> the long name on
        # Windows), so compare resolved forms rather than raw spellings.
        self.assertIn(f"source : {self.code.resolve()}", out)
        self.assertIn("vault  :", out)

    def test_a_positional_source_works_like_the_flag(self):
        rc, out, err = self.run_tool(str(self.code), "--vault", self.vault, "--dry-run")
        self.assertEqual(rc, 0, err)
        # Same canonicalization as above: resolve() on both sides.
        self.assertIn(f"source : {self.code.resolve()}", out)

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
        # fetch_remote_tip is stubbed because a dry run still fetches the real
        # base: with the derived seafile:// remote that means spawning the real
        # helper with real (or absent) credentials -- on the dev machine this
        # test was quietly talking to the actual Seafile server, and on CI the
        # helper aborted with no credentials at all.  What is under test is the
        # derivation, not the fetch; the fetch is covered by the multi-machine
        # tests against a local remote.
        with unittest.mock.patch.object(snap, "fetch_remote_tip", return_value=None):
            rc, out, err = self.run_tool(
                "--source", self.code, "--vault", self.vault, "--dry-run"
            )
        self.assertEqual(rc, 0, err)
        self.assertIn("seafile://code/myproject.git-vault", out)

    def test_a_derived_destination_is_announced_as_the_code_library(self):
        _git(self.code, "remote", "add", "origin", "seafile://team/app")
        args = snap.parse_args(["--source", str(self.code), "--vault", str(self.vault)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _src, _vault, remote, *_rest = snap.resolve_snapshot_settings(args)
        self.assertEqual(remote, "seafile://team/app-vault")
        self.assertIn("same library as your code", out.getvalue())
        self.assertIn("--remote", out.getvalue())
        # an explicit --remote is not second-guessed
        args = snap.parse_args(["--source", str(self.code), "--vault", str(self.vault),
                                "--remote", "seafile://private/mine"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            snap.resolve_snapshot_settings(args)
        self.assertNotIn("same library as your code", out.getvalue())

    def test_a_second_seafile_remote_makes_derivation_give_up(self):
        # Two is ambiguous; guessing wrong files a whole-tree backup somewhere
        # the user did not choose, so it is better to push nothing.
        _git(self.code, "remote", "add", "a", "seafile://code/one")
        _git(self.code, "remote", "add", "b", "seafile://code/two")
        self.assertIsNone(snap.derive_vault_remote(self.code))

    def test_a_non_seafile_remote_derives_nothing(self):
        _git(self.code, "remote", "add", "origin", "https://github.com/x/y.git")
        self.assertIsNone(snap.derive_vault_remote(self.code))

    def test_a_subfolder_of_the_repository_derives_nothing(self):
        # git finds the enclosing repo's remote from a subfolder; deriving from
        # it would push a snapshot of the folder onto the whole repo's vault.
        _git(self.code, "remote", "add", "origin", "seafile://code/app")
        sub = self.code / "report"
        sub.mkdir()
        self.assertEqual(snap.derive_vault_remote(self.code), "seafile://code/app-vault")
        self.assertIsNone(snap.derive_vault_remote(sub))


class SubfolderSourceTests(_SnapshotFixture):
    """A snapshot is of the repository, wherever in it the command is run."""

    def setUp(self):
        super().setUp()
        self.sub = self.code / "report"
        self.sub.mkdir()
        (self.sub / "r.txt").write_text("r", encoding="utf-8")

    def test_the_toplevel_is_the_repository_root(self):
        self.assertEqual(snap.repo_toplevel(self.sub), self.code.resolve())
        self.assertEqual(snap.repo_toplevel(self.code), self.code.resolve())
        self.assertIsNone(snap.repo_toplevel(self.tmp))

    def test_running_from_a_subfolder_snapshots_the_whole_repository(self):
        previous = os.getcwd()
        os.chdir(self.sub)
        try:
            args = snap.parse_args(["--dry-run"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                source, vault, *_rest = snap.resolve_snapshot_settings(args)
        finally:
            os.chdir(previous)
        self.assertEqual(source, self.code.resolve())
        self.assertEqual(vault, self.code.resolve().with_name(self.code.name + "-vault"))
        self.assertIn("snapshotting the whole repository", out.getvalue())

    def test_an_explicit_subfolder_source_is_refused(self):
        rc, _out, err = self.run_tool("--source", self.sub, "--vault", self.vault)
        self.assertEqual(rc, 1)
        self.assertIn("subfolder of the repository", err)
        self.assertIn(str(self.code.resolve()), err)

    def test_a_vault_that_remembers_a_subfolder_keeps_working(self):
        # Made before roots were resolved: the vault's own memory is a default
        # that worked, so it is honoured rather than turned into an error.
        _git(self.vault.parent, "init", "-q", str(self.vault))
        _git(self.vault, "config", "--local", "snapshot.source", str(self.sub.resolve()))
        args = snap.parse_args(["--source", str(self.sub), "--vault", str(self.vault)])
        source, _vault, *_rest = snap.resolve_snapshot_settings(args)
        self.assertEqual(source, self.sub.resolve())


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
        # --no-push wins over the remembered remote for THIS run...
        rc, out, err = self.run_tool(
            "--source", self.code, "--vault", self.vault, "--no-push"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("kept locally", out)
        # ...but it is a per-run override, not a clearing: the scheduled job's
        # remote survives, and a bare re-run still pushes.  (A regression here
        # erased the memory, and this test still passed -- it only looked at
        # the run that did the damage.)
        self.assertEqual(
            snap._read_config_key(self.vault / ".git", "snapshot.remote"),
            str(self.remote),
        )
        rc, out, err = self.run_tool("--source", self.code, "--vault", self.vault)
        self.assertEqual(rc, 0, err)
        self.assertIn("push", out.lower())

    def test_no_push_suppresses_a_remembered_code_remote_too(self):
        # --no-push promises "pushes nothing"; a remembered --code-remote is
        # also a push, and it used to fire anyway.  The remote must accept the
        # first push, or the run aborts before it can remember anything.
        origin = self.tmp / "code-origin.git"
        _git(self.tmp, "init", "-q", "--bare", str(origin))
        _git(self.code, "remote", "add", "origin", str(origin))
        _git(self.code, "push", "-q", "-u", "origin", "main")
        self.snapshot("--code-remote", "origin")
        self.assertEqual(
            snap._read_config_key(self.vault / ".git", "snapshot.coderemote"),
            "origin",
        )
        rc, out, err = self.run_tool(
            "--source", self.code, "--vault", self.vault, "--no-push"
        )
        self.assertEqual(rc, 0, err)
        self.assertNotIn("pushing source repo", out)
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


class VaultBranchTests(_SnapshotFixture):
    """The vault's initial branch is the tool's choice, and it is named portably.

    ``ensure_vault`` used ``git init -b <branch>``, which is Git 2.28; the tool
    is documented to run on Git 2.20, so the branch is named with
    ``symbolic-ref`` on the unborn HEAD instead.  That is the same operation on
    every version -- the initial branch is only a symref -- so these pin the
    observable contract rather than the command used to reach it.
    """

    def test_the_vault_starts_on_the_requested_branch(self):
        vault = self.tmp / "named-vault"
        snap.ensure_vault(vault, "custom-name")
        proc = _git(vault, "symbolic-ref", "HEAD")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "refs/heads/custom-name")

    def test_a_slash_in_the_branch_name_survives(self):
        # `refs/heads/team/audit` is a legal branch; naming it must not be
        # flattened to a single component by the plumbing.
        vault = self.tmp / "nested-vault"
        snap.ensure_vault(vault, "team/audit")
        proc = _git(vault, "symbolic-ref", "HEAD")
        self.assertEqual(proc.stdout.strip(), "refs/heads/team/audit")

    def test_an_existing_vault_keeps_its_branch(self):
        # A second call must be a no-op: it returns the same git dir and does
        # not re-point HEAD, so a vault already on its branch stays there.
        vault = self.tmp / "existing-vault"
        git_dir = snap.ensure_vault(vault, "snapshot")
        again = snap.ensure_vault(vault, "other-name")
        self.assertEqual(git_dir, again)
        proc = _git(vault, "symbolic-ref", "HEAD")
        self.assertEqual(proc.stdout.strip(), "refs/heads/snapshot")


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

    def test_positional_source_with_restore_is_refused(self):
        # The restore tree comes from the remote; a positional SOURCE here
        # would be silently ignored, which is the failure class this guard
        # exists for.
        rc, _, err = self.run_tool(
            "--restore", "--remote", "seafile://code/x", "--into", "C:/tmp/x",
            "C:/somewhere",
        )
        self.assertEqual(rc, 1)
        self.assertIn("SOURCE (positional) does not apply to --restore", err)

    def test_source_flag_with_restore_is_refused(self):
        rc, _, err = self.run_tool(
            "--restore", "--remote", "seafile://code/x", "--into", "C:/tmp/x",
            "--source", "C:/somewhere",
        )
        self.assertEqual(rc, 1)
        self.assertIn("--source does not apply to --restore", err)

    def test_no_object_compression_with_restore_is_refused(self):
        # A restore writes no new objects, so the flag would be silently
        # ignored there.
        rc, _, err = self.run_tool(
            "--restore", "--remote", "seafile://code/x", "--into", "C:/tmp/x",
            "--no-object-compression",
        )
        self.assertEqual(rc, 1)
        self.assertIn("--no-object-compression does not apply to --restore", err)

    def test_maintain_rejects_snapshot_only_flags(self):
        # Housekeeping works on the object store alone; a snapshot flag here
        # would be silently ignored.
        rc, _, err = self.run_tool("--maintain", "--message", "m")
        self.assertEqual(rc, 1)
        self.assertIn("--message does not apply to --maintain", err)

    def test_maintain_and_restore_are_mutually_exclusive(self):
        rc, _, err = self.run_tool(
            "--maintain", "--restore", "--remote", "seafile://code/x",
        )
        self.assertEqual(rc, 1)
        self.assertIn("--maintain and --restore are different modes", err)

    def test_full_repack_requires_maintain(self):
        # Outside housekeeping there is no repack to select one for.
        rc, _, err = self.run_tool("--full-repack")
        self.assertEqual(rc, 1)
        self.assertIn("--full-repack only applies to --maintain", err)

    def test_no_repack_does_not_apply_to_maintain(self):
        # Housekeeping performs the repack itself; skipping it is a snapshot
        # concern and would be silently ignored here.
        rc, _, err = self.run_tool("--maintain", "--no-repack")
        self.assertEqual(rc, 1)
        self.assertIn("--no-repack does not apply to --maintain", err)


class AutoRepackTests(_SnapshotFixture):
    """The push-time incremental repack packs the loose tier and nothing else."""

    def test_packs_the_loose_tier_once_it_passes_the_gate(self):
        rc, _, _ = self.snapshot()
        self.assertEqual(rc, 0)
        git_dir = self.vault / ".git"
        before = snap._object_store_stats(git_dir, self.code)
        self.assertGreater(before["count"], 0)
        # A tiny test repo's loose tier is under 1 KiB, which `count-objects`
        # reports as 0, so the gate has to be lowered to zero to open it.
        with unittest.mock.patch.object(snap, "_REPACK_MIN_LOOSE_BYTES", 0):
            snap.auto_incremental_repack(git_dir, self.code)
        after = snap._object_store_stats(git_dir, self.code)
        self.assertEqual(after["count"], 0)
        self.assertGreater(after.get("size-pack", 0), 0)

    def test_skips_below_the_gate(self):
        rc, _, _ = self.snapshot()
        self.assertEqual(rc, 0)
        git_dir = self.vault / ".git"
        before = snap._object_store_stats(git_dir, self.code)
        with unittest.mock.patch.object(snap, "_REPACK_MIN_LOOSE_BYTES", 1 << 60):
            snap.auto_incremental_repack(git_dir, self.code)
        after = snap._object_store_stats(git_dir, self.code)
        self.assertEqual(after["count"], before["count"])


class FreeSpaceGuardTests(unittest.TestCase):
    """Staging a tree the volume cannot hold fills the disk; refuse it up front.

    The incident: a 130 GiB tree and a vault on a volume with 60 GiB free.  A
    dry run stages exactly as much as a real run, so the guard covers both.
    """

    MARGIN = 1024 ** 3

    def _check(self, free, held_kib=0, allow=False, vault_config=None, mib=2):
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "src"
            src.mkdir()
            for i in range(3):
                (src / f"raw{i}.bin").write_bytes(os.urandom(mib * 1024 * 1024))
            usage = shutil._ntuple_diskusage(total=free * 2, used=free, free=free)
            with unittest.mock.patch.object(snap.shutil, "disk_usage", return_value=usage),                  unittest.mock.patch.object(snap, "_object_store_stats",
                                            return_value={"size": held_kib, "size-pack": 0}):
                snap.check_free_space(pathlib.Path(td), pathlib.Path(td), src, [],
                                      vault_config or {}, allow_low_disk=allow)

    def test_a_tree_that_does_not_fit_is_refused_with_the_numbers(self):
        with self.assertRaises(snap.SnapshotError) as ctx:
            self._check(free=self.MARGIN + 1024 * 1024)
        msg = str(ctx.exception)
        self.assertIn("refusing to stage", msg)
        self.assertIn("--allow-low-disk", msg)

    def test_enough_room_passes_and_so_does_the_override(self):
        self._check(free=self.MARGIN + 64 * 1024 * 1024)
        self._check(free=1, allow=True)

    def test_what_the_vault_already_holds_is_not_counted_again(self):
        # 6 MiB tree, 6 MiB already stored: nothing new to write.
        self._check(free=self.MARGIN, held_kib=6 * 1024)


class DryRunUsesTheRawProfileTests(unittest.TestCase):
    """A preview must stage at the speed of the run it previews.

    On the flood tree the dry run deflated 8 GiB of incompressible zips on one
    core (~46 MiB/s) while the real run would have used core.compression=0
    (~186 MiB/s) -- four times slower, on a machine that looked idle.
    """

    def test_the_settings_reach_git_children_without_touching_the_vault(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            for k in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
                del os.environ[k]
            snap._use_profile_for_this_process_only()
            self.assertEqual(os.environ["GIT_CONFIG_COUNT"], "2")
            pairs = {
                os.environ[f"GIT_CONFIG_KEY_{i}"]: os.environ[f"GIT_CONFIG_VALUE_{i}"]
                for i in range(2)
            }
            self.assertEqual(pairs["core.compression"], "0")
            self.assertEqual(pairs["core.bigFileThreshold"], "50m")

    def test_existing_config_environment_entries_are_kept(self):
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "x.y", "GIT_CONFIG_VALUE_0": "z"}
        with unittest.mock.patch.dict(os.environ, env):
            snap._use_profile_for_this_process_only()
            self.assertEqual(os.environ["GIT_CONFIG_COUNT"], "3")
            self.assertEqual(os.environ["GIT_CONFIG_KEY_0"], "x.y")
            self.assertEqual(os.environ["GIT_CONFIG_KEY_1"], "core.compression")

    def test_choose_profile_dry_run_applies_it_and_writes_no_config(self):
        gib_tree = {"total": 4.0 * 1024 ** 3, "payload": 4.0 * 1024 ** 3, "sampled": 1e6, "ratio": 0.99}
        with tempfile.TemporaryDirectory() as td,              unittest.mock.patch.dict(os.environ, {}, clear=False),              unittest.mock.patch.object(snap, "sample_payload", return_value=gib_tree),              contextlib.redirect_stdout(io.StringIO()):
            for k in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
                del os.environ[k]
            vault = pathlib.Path(td)
            snap.choose_profile(vault, vault / ".git", vault, [], {}, write=False)
            self.assertEqual(os.environ.get("GIT_CONFIG_COUNT"), "2")
            self.assertFalse((vault / ".git").exists(), "a dry run writes nothing")


class TempPackSweepTests(unittest.TestCase):
    """A killed `git pack-objects` leaves `tmp_pack_*` that git never removes.

    On the flood vault one was 22.5 GiB (a killed run on 2026-10-12) and nothing
    reported it: `count-objects` calls it "garbage" and the tool read that number
    as unreachable objects.
    """

    def _vault(self, td):
        git_dir = pathlib.Path(td) / ".git"
        (git_dir / "objects" / "pack").mkdir(parents=True)
        return git_dir

    def test_only_old_temporary_packs_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            git_dir = self._vault(td)
            pack_dir = git_dir / "objects" / "pack"
            old = pack_dir / "tmp_pack_Old123"
            fresh = pack_dir / "tmp_pack_New456"
            real = pack_dir / "pack-abc.pack"
            for p, n in ((old, 5000), (fresh, 3000), (real, 100)):
                p.write_bytes(b"x" * n)
            stale = time.time() - 7200
            os.utime(old, (stale, stale))
            # git leaves its temporary packs read-only, and Windows will not
            # delete a read-only file: the real 22.5 GiB one survived the first
            # version of this sweep for exactly that reason.
            os.chmod(old, 0o444)
            os.utime(real, (stale, stale))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                freed = snap.sweep_temp_packs(git_dir)
            self.assertEqual(freed, 5000)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists(), "a pack that may still be being written stays")
            self.assertTrue(real.exists(), "a finished pack is never touched")
            self.assertIn("leftover temporary pack", out.getvalue())

    def test_nothing_to_sweep_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(snap.sweep_temp_packs(self._vault(td)), 0)
            self.assertEqual(out.getvalue(), "")


class StaleGitLockHintTests(unittest.TestCase):
    def test_a_leftover_index_lock_is_explained_not_just_quoted(self):
        proc = unittest.mock.MagicMock(
            returncode=128, stdout="",
            stderr="fatal: Unable to create 'V/.git/index.lock': File exists.",
        )
        with unittest.mock.patch.object(snap, "_run", return_value=proc):
            with self.assertRaises(snap.SnapshotError) as ctx:
                snap.git(pathlib.Path("V/.git"), None, ["add", "-A"], cwd=pathlib.Path("."))
        self.assertIn("left its lock behind", str(ctx.exception))
        self.assertIn("delete the lock file", str(ctx.exception))


class LargePayloadProfileTests(unittest.TestCase):
    """A first-run sample decides once whether the vault should skip deflate and
    delta search -- the two costs that made a 20 GB+ raw-data tree time out."""

    def setUp(self):
        # A dry-run verdict puts git settings into this process's environment
        # (so the preview stages at the real run's speed); keep that from
        # leaking into the next test's `git config` reads.
        patcher = unittest.mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tree(self, td, big_files=0, big_size=0, text_files=0, random_data=True):
        src = pathlib.Path(td) / "src"
        src.mkdir()
        for i in range(big_files):
            data = os.urandom(big_size) if random_data else b"\x00" * big_size
            (src / f"raw{i}.bin").write_bytes(data)
        for i in range(text_files):
            (src / f"t{i}.txt").write_text("line\n" * 200)
        return src

    def test_decision_needs_a_big_tree_a_dominant_payload_and_incompressible_data(self):
        gib = 1024 ** 3
        base = {"total": 4.0 * gib, "payload": 4.0 * gib, "sampled": 1e6, "ratio": 0.99}
        self.assertEqual(snap.decide_profile(base), "raw")
        self.assertEqual(snap.decide_profile({**base, "total": 1.0 * gib, "payload": 1.0 * gib}), "default")
        self.assertEqual(snap.decide_profile({**base, "payload": 1.0 * gib}), "default")
        self.assertEqual(snap.decide_profile({**base, "ratio": 0.4}), "default")
        # Mixed data that shrinks only ~15% still goes raw: deflate costs more
        # one-core time than the bytes it saves cost on the wire (measured on
        # the flood tree, sample ratio 0.85: 45 vs ~190 MiB/s staging).
        self.assertEqual(snap.decide_profile({**base, "ratio": 0.85}), "raw")
        self.assertEqual(snap.decide_profile({**base, "sampled": 0.0, "ratio": 0.0}), "default")

    def test_the_sample_sees_incompressible_and_compressible_payloads(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._tree(td, big_files=3, big_size=2 * 1024 * 1024, text_files=5)
            m = snap.sample_payload(src, [])
        self.assertGreater(m["ratio"], 0.95)
        self.assertEqual(m["payload"], 3 * 2 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as td:
            src = self._tree(td, big_files=3, big_size=2 * 1024 * 1024, random_data=False)
            m = snap.sample_payload(src, [])
        self.assertLess(m["ratio"], 0.1)

    def test_excluded_paths_are_not_sampled_and_git_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._tree(td, big_files=2, big_size=2 * 1024 * 1024)
            (src / ".git").mkdir()
            (src / ".git" / "pack.bin").write_bytes(os.urandom(2 * 1024 * 1024))
            m = snap.sample_payload(src, ["raw0.bin"])
        self.assertEqual(m["payload"], 1 * 2 * 1024 * 1024)

    def test_applying_the_profile_sets_both_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            snap.apply_large_payload_profile(vault, git_dir)
            snap.apply_large_payload_profile(vault, git_dir)
            self.assertEqual(snap._config(vault, "core.compression"), "0")
            self.assertEqual(snap._config(vault, "core.bigFileThreshold"), "50m")
            self.assertEqual(snap._config(vault, "snapshot.profile"), "raw")

    def test_choose_profile_is_decided_once_and_respects_a_hand_tuned_vault(self):
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            src = self._tree(td, big_files=1, big_size=2 * 1024 * 1024)
            gib = 1024 ** 3
            forced = {"total": 5.0 * gib, "payload": 5.0 * gib, "sampled": 1e6, "ratio": 0.99}
            with unittest.mock.patch.object(snap, "sample_payload", return_value=forced) as sampler, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                snap.choose_profile(vault, git_dir, src, [], snap._read_local_config_map(vault), write=True)
                self.assertIn("large raw payload detected", out.getvalue())
                self.assertEqual(snap._config(vault, "core.compression"), "0")
                # Second run: decided already, so no new sample.
                snap.choose_profile(vault, git_dir, src, [], snap._read_local_config_map(vault), write=True)
                self.assertEqual(sampler.call_count, 1)

        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            subprocess.run(["git", "-C", str(vault), "config", "core.compression", "6"], check=True)
            src = self._tree(td)
            with unittest.mock.patch.object(snap, "sample_payload") as sampler:
                snap.choose_profile(vault, git_dir, src, [], snap._read_local_config_map(vault), write=True)
            sampler.assert_not_called()
            self.assertEqual(snap._config(vault, "core.compression"), "6")

    def test_a_dry_run_reports_but_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            src = self._tree(td)
            gib = 1024 ** 3
            forced = {"total": 5.0 * gib, "payload": 5.0 * gib, "sampled": 1e6, "ratio": 0.99}
            with unittest.mock.patch.object(snap, "sample_payload", return_value=forced), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                snap.choose_profile(vault, git_dir, src, [], snap._read_local_config_map(vault), write=False)
            self.assertIn("dry run", out.getvalue())
            # The preview's settings travel in the environment; the question
            # here is what was *written*, so look at the vault's file alone.
            self.assertEqual(os.environ.get("GIT_CONFIG_COUNT"), "2")
            for k in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
                del os.environ[k]
            self.assertEqual(snap._config(vault, "core.compression"), "")
            self.assertEqual(snap._config(vault, "snapshot.profile"), "")

    def test_a_vault_that_grew_is_sampled_again_once_per_doubling(self):
        gib = 1024 ** 3
        cfg = {"snapshot.profile": ["default"], "snapshot.profilebytes": [str(1 * gib)]}
        self.assertFalse(snap.profile_recheck_due(cfg, int(1.5 * gib)))   # below the floor
        cfg = {"snapshot.profile": ["default"], "snapshot.profilebytes": [str(3 * gib)]}
        self.assertFalse(snap.profile_recheck_due(cfg, 5 * gib))          # not yet doubled
        self.assertTrue(snap.profile_recheck_due(cfg, 6 * gib))           # doubled
        # raw is final, and an undecided vault is the first-run path's business
        self.assertFalse(snap.profile_recheck_due({"snapshot.profile": ["raw"]}, 50 * gib))
        self.assertFalse(snap.profile_recheck_due({}, 50 * gib))

    def test_recheck_replaces_a_default_verdict_with_raw_when_the_sample_says_so(self):
        gib = 1024 ** 3
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            subprocess.run(["git", "-C", str(vault), "config", "snapshot.profile", "default"], check=True)
            src = self._tree(td)
            forced = {"total": 6.0 * gib, "payload": 6.0 * gib, "sampled": 1e6, "ratio": 0.99}
            cfg = snap._read_local_config_map(vault)
            with unittest.mock.patch.object(snap, "sample_payload", return_value=forced), \
                    contextlib.redirect_stdout(io.StringIO()):
                snap.choose_profile(vault, git_dir, src, [], cfg, write=True)             # final
                self.assertEqual(snap._config(vault, "core.compression"), "")
                snap.choose_profile(vault, git_dir, src, [], cfg, write=True, recheck=True)
            self.assertEqual(snap._config(vault, "core.compression"), "0")
            self.assertEqual(snap._config(vault, "snapshot.profile"), "raw")

    def test_no_object_compression_flag_applies_the_whole_profile(self):
        # The explicit override must give the same result as a detected payload,
        # not just the compression half.
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            snap.apply_large_payload_profile(vault, git_dir)
            self.assertEqual(snap._config(vault, "core.bigFileThreshold"), "50m")


class ObjectCompressionTests(unittest.TestCase):
    """--no-object-compression skips deflation for new objects, losslessly."""

    def test_flag_sets_repo_local_config_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            snap.disable_object_compression(vault, git_dir)
            self.assertEqual(snap._config(vault, "core.compression"), "0")
            # Second call: a no-op that must not error or change the value.
            snap.disable_object_compression(vault, git_dir)
            self.assertEqual(snap._config(vault, "core.compression"), "0")

    def test_new_objects_are_stored_uncompressed(self):
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            blob = pathlib.Path(td) / "zeros.bin"
            data = b"\x00" * (256 * 1024)  # maximally compressible on purpose
            for compressed in (True, False):
                if compressed:
                    subprocess.run(
                        ["git", "-C", str(vault), "config", "core.compression", "-1"],
                        check=True, capture_output=True,
                    )
                    payload = data + b"y"  # unique content -> a fresh SHA each pass
                else:
                    snap.disable_object_compression(vault, git_dir)
                    payload = data + b"x"
                blob.write_bytes(payload)
                proc = subprocess.run(
                    ["git", "hash-object", "-w", str(blob)],
                    cwd=str(vault), capture_output=True, text=True, check=True,
                )
                sha = proc.stdout.strip()
                obj_size = (git_dir / "objects" / sha[:2] / sha[2:]).stat().st_size
                if compressed:
                    # Deflated: 256 KiB of zeros collapse to almost nothing.
                    self.assertLess(obj_size, len(data) // 10)
                else:
                    # Level 0 keeps the zlib wrapper around *stored* blocks: no
                    # shrinking (a real compression would be far smaller), and a
                    # round-trip through git must come back byte-identical.
                    self.assertGreater(obj_size, len(payload))
                    got = subprocess.run(
                        ["git", "cat-file", "blob", sha],
                        cwd=str(vault), capture_output=True, check=True,
                    ).stdout
                    self.assertEqual(got, payload)


class SyncedFolderGuardTests(_SnapshotFixture):
    """The tool's own synced-library guard, for source, vault and restore."""

    def run_tool(self, *argv: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    @staticmethod
    def _lib(auto_sync: bool = True, rule: str | None = None) -> dict:
        """One containing synced library as matched_synced_libraries reports it."""
        return {
            "name": "Documents",
            "worktree": pathlib.Path("C:/synced/Documents"),
            "auto_sync": auto_sync,
            "ignore_rule": rule,
        }

    def test_both_the_source_and_the_vault_are_checked(self):
        checked: list[pathlib.Path] = []
        real = snap.matched_synced_libraries

        def recording(path: pathlib.Path) -> list[dict]:
            checked.append(path)
            return real(path)

        with unittest.mock.patch.object(
            snap, "matched_synced_libraries", side_effect=recording
        ):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 0, err)
        self.assertIn(self.code.resolve(), checked)
        self.assertIn(self.vault.resolve(), checked)

    def test_a_synced_vault_is_refused(self):
        with unittest.mock.patch.object(
            snap,
            "matched_synced_libraries",
            side_effect=lambda p: [self._lib()] if p == self.vault.resolve() else [],
        ):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 1)
        self.assertIn("vault is inside a Seafile-synced library", err)
        self.assertIn("--allow-synced", err)

    def test_a_synced_source_is_refused(self):
        with unittest.mock.patch.object(
            snap,
            "matched_synced_libraries",
            side_effect=lambda p: [self._lib()] if p == self.code.resolve() else [],
        ):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 1)
        self.assertIn("source is inside a Seafile-synced library", err)

    def test_a_source_covered_by_the_library_ignore_file_passes_with_a_note(self):
        # The client never touches a path its own seafile-ignore.txt covers, so
        # the guard lets it through -- but says so on stderr, because removing
        # the rule (or re-enabling auto sync) makes this refuse again.
        with unittest.mock.patch.object(
            snap,
            "matched_synced_libraries",
            side_effect=lambda p: [self._lib(rule="code/")] if p == self.code.resolve() else [],
        ):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 0, err)
        self.assertIn("will not touch it", err)
        self.assertIn("'code/'", err)

    def test_a_source_in_a_library_with_auto_sync_off_passes_with_a_note(self):
        # A per-library auto-sync disable is persisted in repo.db, so the daemon
        # un-watches that worktree and will not rewrite files under it.
        with unittest.mock.patch.object(
            snap,
            "matched_synced_libraries",
            side_effect=lambda p: [self._lib(auto_sync=False)] if p == self.code.resolve() else [],
        ):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 0, err)
        self.assertIn("auto sync is disabled", err)

    def test_allow_synced_overrides_the_refusal(self):
        with unittest.mock.patch.object(
            snap,
            "matched_synced_libraries",
            side_effect=lambda p: [self._lib()] if p == self.code.resolve() else [],
        ):
            rc, _, err = self.snapshot("--allow-synced")
        self.assertEqual(rc, 0, err)
        self.assertIn("--allow-synced overrides the synced-folder guard", err)

    def test_a_missing_seafile_client_leaves_the_guard_off_but_the_run_working(self):
        # A "cannot tell" answer must not read as a verified "safe": an
        # unimportable package or an unreadable repo.db returns no libraries so
        # the run still works, but announces the degradation on stderr -- the
        # same reasoning as safety.py's transient-failure warning.  The sys.path
        # insertion the tool does at startup makes the package importable from a
        # bare checkout, so the import branch below is defense in depth, pinned
        # so it cannot silently regress.
        with unittest.mock.patch.dict(
            sys.modules,
            {"git_remote_seafile": None, "git_remote_seafile.safety": None},
        ):
            err = io.StringIO()
            with unittest.mock.patch.object(sys, "stderr", err):
                self.assertEqual(snap.matched_synced_libraries(self.code), [])
            self.assertIn(
                "could not check for Seafile-synced folders", err.getvalue()
            )
        with unittest.mock.patch.object(
            snap, "matched_synced_libraries", return_value=[]
        ):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 0, err)

    def test_a_synced_restore_destination_is_refused(self):
        self.snapshot("--remote", str(self.remote))
        with unittest.mock.patch.object(
            snap, "matched_synced_libraries", return_value=[self._lib()]
        ):
            rc, _, err = self.restore_to(self.restored)
        self.assertEqual(rc, 1)
        self.assertIn("restore destination is inside a Seafile-synced library", err)
        self.assertFalse(self.restored.exists() and any(self.restored.iterdir()))

    def test_allow_synced_overrides_the_restore_destination_refusal(self):
        self.snapshot("--remote", str(self.remote))
        with unittest.mock.patch.object(
            snap, "matched_synced_libraries", return_value=[self._lib()]
        ):
            rc, _, err = self.restore_to(self.restored, "--allow-synced")
        self.assertEqual(rc, 0, err)
        self.assertIn("--allow-synced overrides the synced-folder guard", err)


class ConfigPlumbingTests(_SnapshotFixture):
    """The vault's remembered settings: read once, written only when changed."""

    def _config_writes(self):
        """A spy around subprocess.run recording local config *writes*."""
        calls: list[list[str]] = []
        real_run = snap.subprocess.run

        def spy(cmd, *args, **kwargs):
            if (
                isinstance(cmd, list)
                and "config" in cmd
                and "--local" in cmd
                and not any(
                    str(a).startswith("--get") or str(a) in ("--list", "--null")
                    for a in cmd
                )
            ):
                calls.append([str(c) for c in cmd])
            return real_run(cmd, *args, **kwargs)

        return calls, spy

    def test_a_settled_run_writes_no_config(self):
        self.snapshot("--remote", str(self.remote))
        (self.code / "README.md").write_bytes(b"changed\n")
        calls, spy = self._config_writes()
        with unittest.mock.patch.object(snap.subprocess, "run", side_effect=spy):
            rc, _, err = self.snapshot("--remote", str(self.remote))
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls, [])

    def test_changing_one_setting_writes_only_that_key(self):
        self.snapshot("--remote", str(self.remote), "--branch", BRANCH)
        calls, spy = self._config_writes()
        with unittest.mock.patch.object(snap.subprocess, "run", side_effect=spy):
            rc, _, err = self.snapshot(
                "--remote", str(self.remote), "--branch", "other"
            )
        self.assertEqual(rc, 0, err)
        # Same canonicalization as the defaults tests: CI temp dirs alias
        # (/private/var on macOS, RUNNER~1 on Windows), so compare resolved.
        self.assertEqual(
            [[c[0], c[1], *(x.resolve() if isinstance(x, pathlib.PurePath) else x
                            for x in c[2:])] for c in calls],
            [["git", "-C", str(self.vault.resolve()), "config", "--local",
              "snapshot.branch", "other"]],
        )

    def test_shrinking_a_multi_valued_key_drops_the_old_values(self):
        self.snapshot("--exclude", "dist", "--exclude", "node_modules")
        self.assertEqual(
            _git(self.vault, "config", "--local", "--get-all", "snapshot.exclude")
            .stdout.split(),
            ["dist", "node_modules"],
        )
        rc, _, err = self.snapshot("--exclude", "dist")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            _git(self.vault, "config", "--local", "--get-all", "snapshot.exclude")
            .stdout.split(),
            ["dist"],
        )

    def test_an_exclude_backslash_is_data_not_a_separator(self):
        # _same_config_value normalises separators for snapshot.source only;
        # for every other key a backslash is data, so a\b and a/b are
        # different settings and the rewrite actually happens.
        self.snapshot("--exclude", "a\\b")
        self.assertEqual(
            _git(self.vault, "config", "--local", "--get-all", "snapshot.exclude")
            .stdout.split(),
            ["a\\b"],
        )
        rc, _, err = self.snapshot("--exclude", "a/b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            _git(self.vault, "config", "--local", "--get-all", "snapshot.exclude")
            .stdout.split(),
            ["a/b"],
        )

    def test_a_failing_config_write_is_not_silent(self):
        self.snapshot("--remote", str(self.remote))
        real_run = snap.subprocess.run

        def failing_write(cmd, *args, **kwargs):
            if "config" in cmd and "--local" in cmd and "--list" not in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "cannot lock")
            return real_run(cmd, *args, **kwargs)

        # A settled run writes nothing, so change a setting to force a write:
        # a silent failure here would leave the vault remembering "snapshot"
        # while this run believed it had recorded "other".
        with unittest.mock.patch.object(
            snap.subprocess, "run", side_effect=failing_write
        ):
            rc, _, err = self.snapshot("--remote", str(self.remote), "--branch", "other")
        self.assertEqual(rc, 1)
        self.assertIn("could not update the vault's memory", err)


class SubprocessTimeoutTests(unittest.TestCase):
    """H-24 -- the snapshot tool holds its vault lock for the whole run, so a git
    child that never returns blocks every other local snapshot as well: a
    credential prompt on a scheduled machine, or a server that stalls mid-push."""

    def test_run_converts_a_timeout_into_a_snapshot_error(self):
        with unittest.mock.patch.object(
            snap.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="git push", timeout=1),
        ):
            with self.assertRaises(snap.SnapshotError) as ctx:
                snap._run(["git", "push", "seafile://code/app"])
        self.assertIn("did not finish within", str(ctx.exception))
        self.assertIn("push", str(ctx.exception))


class StatusLineTests(unittest.TestCase):
    """Long steps must not read as a hang: while a child marked with ``status=``
    is still running, one line per STATUS_INTERVAL goes to stderr.  The watchdog
    kills on *stalled progress* (no bytes written / streamed for STALL_LIMIT),
    not on wall clock -- a slow-but-real run must survive, a dead disk must not."""

    def test_watchdog_prints_a_status_line_for_slow_commands(self):
        with unittest.mock.patch.object(snap, "STATUS_INTERVAL", 0.15):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                proc = snap._run(
                    [sys.executable, "-c", "print('hello'); import time; time.sleep(0.8)"],
                    status="staging the tree",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"hello", proc.stdout)
        text = err.getvalue()
        self.assertIn("still staging the tree ...", text)

    def test_watchdog_reports_growth_of_the_measured_directory(self):
        # The child writes the data itself while running: measure_dir reports
        # growth since the child *started*, so a pre-existing file would read
        # as zero and prove nothing.
        with tempfile.TemporaryDirectory() as td:
            objects = pathlib.Path(td) / "objects"
            objects.mkdir()
            target = str(objects / "late.bin")
            code = (
                "import time, pathlib; "
                f"pathlib.Path(r'{target}').write_bytes(b'x' * 3000000); "
                "time.sleep(0.6)"
            )
            err = io.StringIO()
            with unittest.mock.patch.object(snap, "STATUS_INTERVAL", 0.12), \
                    contextlib.redirect_stderr(err):
                snap._run(
                    [sys.executable, "-c", code],
                    status="staging the tree",
                    measure_dir=objects,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            text = err.getvalue()
        self.assertIn("MiB written to objects", text)

    def test_watchdog_kills_a_child_that_makes_no_progress(self):
        with unittest.mock.patch.object(snap, "STATUS_INTERVAL", 0.1), \
                unittest.mock.patch.object(snap, "STALL_LIMIT", 0.35):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                with self.assertRaises(snap.SnapshotError) as ctx:
                    snap._run(
                        [sys.executable, "-c", "import time; time.sleep(10)"],
                        status="pushing to seafile://code/x-vault",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
        self.assertIn("made no measurable progress for", str(ctx.exception))
        self.assertIn("was killed (stalled?)", str(ctx.exception))
        self.assertIn("still pushing to seafile://code/x-vault ...", err.getvalue())

    def test_watchdog_survives_while_progress_is_measurable(self):
        # The child keeps writing into the measured directory: with a stall
        # limit far below its total runtime, only progress-awareness lets it
        # finish.  A wall-clock timeout of the old shape would have killed it
        # at ~0.4s; here it runs for well over a second.
        with tempfile.TemporaryDirectory() as td:
            objects = pathlib.Path(td) / "objects"
            objects.mkdir()
            target = str(objects / "stream.bin")
            code = (
                "import time, pathlib\n"
                f"p = pathlib.Path(r'{target}')\n"
                "for i in range(6):\n"
                "    p.write_bytes(b'x' * 1000 * (i + 1))\n"
                "    time.sleep(0.5)\n"
            )
            with unittest.mock.patch.object(snap, "STATUS_INTERVAL", 0.1), \
                    unittest.mock.patch.object(snap, "STALL_LIMIT", 2.0):
                proc = snap._run(
                    [sys.executable, "-c", code],
                    status="staging the tree",
                    measure_dir=objects,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        self.assertEqual(proc.returncode, 0)

    def test_heartbeat_only_mode_never_kills(self):
        # stall_limit=None is for steps with no progress signal (the restore
        # verification re-reads every file and writes nothing): the lines keep
        # coming, but however long the step takes it must not be killed.
        err = io.StringIO()
        with unittest.mock.patch.object(snap, "STATUS_INTERVAL", 0.15), \
                contextlib.redirect_stderr(err):
            proc = snap._run(
                [sys.executable, "-c", "import time; time.sleep(0.8)"],
                status="verifying restored files",
                stall_limit=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("still verifying restored files ...", err.getvalue())

    def test_stdin_and_output_survive_the_poll_timeouts(self):
        # The watchdog polls with communicate(timeout=...) from the calling
        # thread.  Input handed over once must still reach the child, and the
        # output of a child that outlives several intervals must come back whole.
        payload = "x" * 2_000_000
        code = (
            "import sys, time\n"
            "data = sys.stdin.read()\n"
            "time.sleep(0.7)\n"
            "sys.stderr.write('diag')\n"
            "print(len(data))\n"
        )
        err = io.StringIO()
        with unittest.mock.patch.object(snap, "STATUS_INTERVAL", 0.15), \
                contextlib.redirect_stderr(err):
            proc = snap._run(
                [sys.executable, "-c", code],
                status="feeding a child",
                stall_limit=None,  # pipes are under test here, not the watchdog
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), str(len(payload)))
        self.assertEqual(proc.stderr, "diag")
        self.assertIn("still feeding a child ...", err.getvalue())

    def test_inherited_stderr_reaches_the_terminal_and_is_not_captured(self):
        # The push mode: stderr is inherited (live_stderr), so there is nothing
        # for the watchdog to capture and CompletedProcess.stderr is None.
        proc = snap._run(
            [sys.executable, "-c", "pass"],
            status="pushing to seafile://code/x-vault",
            stall_limit=None,
            stdout=subprocess.PIPE,
            stderr=None,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(proc.stderr)

    def test_the_push_step_is_never_stall_killed(self):
        # A push's upload is silent by construction; killing it for silence would
        # kill every slow-but-healthy upload.  push_snapshot must therefore ask
        # for heartbeat-only mode.
        seen = {}

        def fake_git(git_dir, work_tree, args, **kwargs):
            if args and args[0] == "push":
                seen.update(kwargs)
            return subprocess.CompletedProcess(args, 0, "", "")

        with unittest.mock.patch.object(snap, "git", fake_git), \
                contextlib.redirect_stdout(io.StringIO()):
            snap.push_snapshot(
                pathlib.Path("v"), pathlib.Path("s"), "seafile://code/x", "snapshot",
                "a" * 40, "b" * 40, None, "msg",
            )
        self.assertIn("stall_limit", seen)
        self.assertIsNone(seen["stall_limit"])
        self.assertTrue(seen.get("live_stderr"))

    def test_stall_limit_can_be_raised_from_the_environment(self):
        with unittest.mock.patch.dict(os.environ, {"SEAFILE_SNAPSHOT_STALL_LIMIT": "7200"}):
            self.assertEqual(snap._stall_limit_from_env(), 7200.0)
        for junk in ("abc", "0", "-5"):
            with unittest.mock.patch.dict(os.environ, {"SEAFILE_SNAPSHOT_STALL_LIMIT": junk}):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(snap._stall_limit_from_env(), snap.STALL_LIMIT)
                self.assertIn("SEAFILE_SNAPSHOT_STALL_LIMIT", err.getvalue())

    def test_children_inherit_a_helper_timeout_sized_to_the_watchdog(self):
        # The helper's own 300 s wall-clock ceiling is meant for an interactive
        # push; under this tool the no-progress watchdog is the guard, so the
        # ceiling is lifted -- unless the user already chose one.
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with unittest.mock.patch.object(snap, "_run", fake_run):
            with unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SEAFILE_GIT_TIMEOUT", None)
                snap.git(pathlib.Path("v"), None, ["status"], cwd=pathlib.Path("."))
            self.assertEqual(captured["env"]["SEAFILE_GIT_TIMEOUT"], str(int(snap.STALL_LIMIT)))
            with unittest.mock.patch.dict(os.environ, {"SEAFILE_GIT_TIMEOUT": "42"}):
                snap.git(pathlib.Path("v"), None, ["status"], cwd=pathlib.Path("."))
            self.assertEqual(captured["env"]["SEAFILE_GIT_TIMEOUT"], "42")


class RecycledPidLockTests(_SnapshotFixture):
    """A crashed run's pid, later held by something else, must not block every later run."""

    def _write_lock(self, text: str) -> pathlib.Path:
        self.assertEqual(self.snapshot("--dry-run")[0], 0)  # makes the vault
        lock = self.vault / ".git" / "seafile-snapshot.lock"
        lock.write_text(text, encoding="utf-8")
        return lock

    def test_a_live_pid_with_a_different_start_time_is_a_dead_holder(self):
        token = snap.process_start_token(os.getpid())
        if token is None:
            self.skipTest("no process start time on this platform")
        self._write_lock(f"{socket.gethostname()} {os.getpid()} {int(token) + 1}")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("reclaiming a stale lock", out)

    def test_a_live_pid_with_the_same_start_time_is_a_live_holder(self):
        token = snap.process_start_token(os.getpid())
        if token is None:
            self.skipTest("no process start time on this platform")
        self._write_lock(f"{socket.gethostname()} {os.getpid()} {token}")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 1)
        self.assertIn("another snapshot is running", err)

    def test_a_lock_written_by_an_older_tool_is_judged_by_the_pid_alone(self):
        self._write_lock(f"{socket.gethostname()} {os.getpid()}")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 1)
        self.assertIn("another snapshot is running", err)


class ModificationTimeTests(_SnapshotFixture):
    """Git stores no timestamps; the snapshot records them and a restore applies them."""

    WHEN = 1_600_000_000  # 2020-09-13, nowhere near "now"

    def _age(self, name: str, seconds: int | None = None) -> int:
        stamp = (seconds or self.WHEN) * 1_000_000_000 + 123_456_700  # 100 ns: NTFS granularity
        os.utime(self.code / name, ns=(stamp, stamp))
        return stamp

    def test_a_restore_puts_the_recorded_times_back(self):
        want = self._age("README.md")
        rc, out, err = self.snapshot("--remote", str(self.remote), "--record-mtimes")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(".seafile-payload/mtimes.json", self.tree())
        rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("restored the modification time of", out)
        self.assertEqual((self.restored / "README.md").stat().st_mtime_ns, want)

    def test_a_local_only_snapshot_records_nothing_unless_asked(self):
        self.assertEqual(self.snapshot()[0], 0)
        self.assertNotIn(".seafile-payload/mtimes.json", self.tree())

    def test_touching_a_file_does_not_make_a_new_snapshot(self):
        self._age("README.md")
        self.assertEqual(self.snapshot("--record-mtimes")[0], 0)
        before = self.commits()
        self._age("README.md", self.WHEN + 86_400)  # touched, same bytes
        rc, out, err = self.snapshot("--record-mtimes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.commits(), before)
        self.assertIn("no changes since the last snapshot", out)

    def test_new_content_records_its_own_time(self):
        self._age("README.md")
        self.assertEqual(self.snapshot("--record-mtimes")[0], 0)
        (self.code / "README.md").write_bytes(b"changed\n")
        later = self._age("README.md", self.WHEN + 5 * 86_400)
        self.assertEqual(self.snapshot("--record-mtimes", "--remote", str(self.remote))[0], 0)
        rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((self.restored / "README.md").stat().st_mtime_ns, later)

    def test_a_reverted_source_does_not_sort_before_an_object_built_from_what_it_replaced(self):
        # s.c: A -> B (s.o rebuilt from B, no snapshot taken) -> A again.  Snapshot 2
        # sees s.c's content equal to snapshot 1's, and the old per-file "first
        # seen" rule gave it its original, older time: before s.o, so a restore
        # made `make` keep an object built from B.
        (self.code / "s.c").write_bytes(b"A\n")
        (self.code / "s.o").write_bytes(b"built from A\n")
        os.utime(self.code / "s.c", ns=(self.WHEN * 10**9,) * 2)
        os.utime(self.code / "s.o", ns=((self.WHEN + 10) * 10**9,) * 2)
        self.assertEqual(self.snapshot("--record-mtimes")[0], 0)
        (self.code / "s.c").write_bytes(b"B\n")
        (self.code / "s.o").write_bytes(b"built from B\n")
        os.utime(self.code / "s.o", ns=((self.WHEN + 30) * 10**9,) * 2)
        (self.code / "s.c").write_bytes(b"A\n")  # reverted: identical to snapshot 1's content
        reverted = (self.WHEN + 40) * 10**9
        os.utime(self.code / "s.c", ns=(reverted, reverted))
        self.assertEqual(self.snapshot("--record-mtimes")[0], 0)
        record = json.loads(_git(self.vault, "show", f"{BRANCH}:.seafile-payload/mtimes.json").stdout)
        self.assertEqual(record["files"]["s.c"], reverted)
        self.assertGreater(record["files"]["s.c"], record["files"]["s.o"])

    def test_a_local_run_keeps_recording_when_the_last_snapshot_did(self):
        self._age("README.md")
        self.assertEqual(self.snapshot("--record-mtimes")[0], 0)
        _git(self.vault, "config", "--unset", "snapshot.mtimes")  # nothing remembered, nothing typed
        (self.code / "more.txt").write_bytes(b"more\n")
        rc, out, err = self.snapshot()  # no remote: the default alone would say "off"
        self.assertEqual(rc, 0, out + err)
        self.assertIn(".seafile-payload/mtimes.json", self.tree())

    def test_the_choice_is_remembered_and_can_be_reversed(self):
        self.assertEqual(self.snapshot("--record-mtimes")[0], 0)
        (self.code / "more.txt").write_bytes(b"x\n")
        self.assertEqual(self.snapshot()[0], 0)  # remembered
        self.assertIn(".seafile-payload/mtimes.json", self.tree())
        (self.code / "more2.txt").write_bytes(b"y\n")
        self.assertEqual(self.snapshot("--no-record-mtimes")[0], 0)
        self.assertNotIn(".seafile-payload/mtimes.json", self.tree())

    def test_the_flags_contradict_and_do_not_apply_to_a_restore(self):
        rc, out, err = self.snapshot("--record-mtimes", "--no-record-mtimes")
        self.assertEqual(rc, 1)
        self.assertIn("contradict", err)
        rc, out, err = self.run_tool("--restore", "--remote", self.remote, "--into",
                                     self.restored, "--record-mtimes")
        self.assertEqual(rc, 1)
        self.assertIn("only apply to a snapshot run", err)

    def test_a_hostile_record_cannot_reach_outside_the_restored_tree(self):
        victim = self.tmp / "victim.txt"
        victim.write_bytes(b"keep\n")
        before = victim.stat().st_mtime_ns
        into = self.tmp / "into"
        (into / ".seafile-payload").mkdir(parents=True)
        (into / "ok.txt").write_bytes(b"ok\n")
        record = {
            "format": 1,
            "files": {
                "ok.txt": 1_500_000_000_000_000_000,
                "../victim.txt": 1_500_000_000_000_000_000,
                "/etc/passwd": 1_500_000_000_000_000_000,
                ".git/config": 1_500_000_000_000_000_000,
                "bad.txt": True,
                "neg.txt": -5,
            },
        }
        (into / ".seafile-payload" / "mtimes.json").write_text(json.dumps(record), encoding="utf-8")
        applied, skipped = snap.apply_recorded_mtimes(into)
        self.assertEqual(applied, 1)
        self.assertGreaterEqual(skipped, 4)
        self.assertEqual(victim.stat().st_mtime_ns, before)
        self.assertEqual((into / "ok.txt").stat().st_mtime_ns, 1_500_000_000_000_000_000)

    def test_a_snapshot_without_a_record_restores_exactly_as_before(self):
        self.assertEqual(self.snapshot("--remote", str(self.remote), "--no-record-mtimes")[0], 0)
        rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("modification time", out)


class OddNameAndLockFailureTests(_SnapshotFixture):
    """ROADMAP T-5..T-8: names git quotes, an unwritable lock, a dead flag."""

    def test_a_lock_that_cannot_be_written_leaves_nothing_behind(self):
        self.snapshot()
        lock = self.vault / ".git" / "seafile-snapshot.lock"
        with unittest.mock.patch("os.fsync", side_effect=OSError(28, "no space left")):
            with self.assertRaises(snap.SnapshotError) as ctx:
                snap.VaultLock(self.vault / ".git").__enter__()
        self.assertIn("could not record the lock holder", str(ctx.exception))
        # Unlinking an open file fails on Windows, so this also proves the
        # descriptor was closed.
        self.assertFalse(lock.exists())
        rc, _, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 0, err)

    def test_code_remote_with_no_push_is_refused(self):
        rc, _, err = self.snapshot("--no-push", "--code-remote", "origin")
        self.assertEqual(rc, 1)
        self.assertIn("--code-remote", err)
        self.assertIn("--no-push", err)

    @unittest.skipIf(os.name == "nt", "NTFS cannot hold these names")
    def test_verify_restore_hashes_names_that_start_with_a_quote_or_hold_a_newline(self):
        repo = self.tmp / "oddnames"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main", ".")
        names = ['"quoted.txt', "two\nlines.txt", "-dash.txt", "plain.txt"]
        for name in names:
            (repo / name).write_bytes(b"data " + name.encode() + b"\n")
        _git(repo, "add", "-A", "--", ".")
        _git(repo, "-c", "user.email=e@x", "-c", "user.name=v", "commit", "-qm", "odd")
        self.assertEqual(snap.verify_restore(repo, "HEAD"), [])
        (repo / '"quoted.txt').write_bytes(b"changed\n")
        (repo / "two\nlines.txt").write_bytes(b"changed\n")
        self.assertEqual(
            sorted(snap.verify_restore(repo, "HEAD")), sorted(['"quoted.txt', "two\nlines.txt"])
        )

    @unittest.skipIf(os.name == "nt", "NTFS cannot hold a non-UTF-8 name")
    def test_a_non_utf8_name_survives_an_exclude_run(self):
        raw = os.fsencode(self.code) + b"/bad_\xff_name.txt"
        try:
            with open(raw, "wb") as fh:
                fh.write(b"latin1\n")
        except OSError:
            self.skipTest("this filesystem refuses non-UTF-8 names")
        rc, out, err = self.snapshot("--no-push", "--exclude", "dist")
        self.assertEqual(rc, 0, out + err)
        names = _git(self.vault, "ls-tree", "-r", "--name-only", "-z", BRANCH).stdout
        self.assertTrue(any(n.startswith("bad_") for n in names.split("\0")))
        self.assertNotIn("dist/bundle.js", names)


if __name__ == "__main__":
    unittest.main()
