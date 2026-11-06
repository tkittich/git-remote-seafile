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
import re
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

try:
    from _helpers import remove_tree  # noqa: E402
except ImportError:  # run as ``python -m unittest tests.<module>``
    from tests._helpers import remove_tree  # noqa: E402

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
        remove_tree(probe)


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
        self.addCleanup(remove_tree, self.tmp)

        self.code = self.tmp / "code"
        self.vault = self.tmp / "vault"
        self.code.mkdir()

        # write_bytes, not write_text: on Windows write_text rewrites every
        # "\n" to "\r\n", so a fixture meant to be LF would silently be CRLF --
        # and every byte-exactness assertion below would then be testing the
        # wrong thing.
        # dist/ is ignored build output here; --default-excludes leaves out only the
        # directories git ignores (an untracked, unignored dist/ may be source).
        (self.code / ".gitignore").write_bytes(b"node_modules/\n*.log\n.env\ndist/\n")
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
        self.assertNotIn("Warning: these paths are embedded", out)
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
        self.assertIn("Warning: these paths are embedded", out)
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
        self.assertIn("Warning: these paths are embedded", out)

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

    def test_rebuild_index_rereads_the_git_side_bytes(self):
        """The git-side audit: an edit invisible to the stat cache is caught.

        Dropping the vault index forces git to re-hash every file, the
        counterpart of --payload-rehash for the files git stores (an edit that
        leaves size and timestamp untouched is otherwise trusted).
        """
        target = self.code / "tracked.txt"
        target.write_bytes(b"original\n")
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        # Same size, same mtime: nothing the index's stat cache compares moves.
        import os as _os
        stat = target.stat()
        target.write_bytes(b"replaced\n")
        _os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        # The audit run re-reads the bytes even though the stats agree.
        rc, out, err = self.snapshot("--rebuild-index")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("rebuilding the vault index", out + err)
        shown = _git(self.vault, "show", "HEAD:tracked.txt").stdout
        self.assertIn("replaced", shown, "the audit caught the invisible edit")
        # And the flag is refused where it cannot apply (a run-failure exit,
        # like every other argument conflict).
        rc, out, err = self.run_tool(
            "--restore", "--remote", self.remote,
            "--into", str(self.tmp / "nope"), "--rebuild-index",
        )
        self.assertEqual(rc, 1, out + err)
        self.assertIn("--rebuild-index does not apply to --restore", out + err)

    def test_an_exclude_typed_composed_matches_a_decomposed_name(self):
        """The comparison is NFC on both sides.

        A decomposed name in the tree against a composed pattern on the command
        line did not match, and the file entered the snapshot behind the
        user's back (REVIEW.iq3_s E3, reproduced on Linux; the same
        normalisation is what S20's embedded-walk fix needs).
        """
        composed = "caf" + chr(233) + ".txt"           # U+00E9 (precomposed)
        decomposed = "cafe" + chr(769) + ".txt"        # e + U+0301 (decomposed)
        self.assertNotEqual(composed, decomposed)
        (self.code / decomposed).write_bytes(b"nfd")
        rc, out, err = self.snapshot("--exclude", composed)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(composed, self.tree())
        self.assertNotIn(decomposed, self.tree())
        self.assertIn("excluded", out)

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "needs POSIX permissions that bind the current user",
    )
    def test_an_unreadable_directory_inside_an_embedded_repository_stops_the_run(self):
        """git cannot warn about a directory inside a repository it refuses to
        enter, so its stderr guard is structurally blind there; the tool's own
        walk must carry the refusal (REVIEW.iq3_s E1, reproduced on Linux)."""
        nested = self.code / "vendor"
        nested.mkdir()
        _git(nested, "init", "-q", "-b", "main", ".")
        (nested / "inner.txt").write_bytes(b"inner\n")
        locked = nested / "locked"
        locked.mkdir()
        (locked / "secret.txt").write_bytes(b"x\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 1, out + err)
        self.assertIn("could not be read", err)
        self.assertIn("vendor", err)

    def test_the_embedded_walks_denied_directory_feeds_the_refusal_everywhere(self):
        """The walk's onerror hook, driven directly, so Windows sees it too."""
        import tempfile as _tf

        with _tf.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "f.txt").write_bytes(b"x\n")
            unreadable: list[str] = []
            real_walk = os.walk

            def walk_that_hits_a_denied_dir(top, **kw):
                onerror = kw.get("onerror")
                if onerror is not None and str(top) == str(root):
                    onerror(PermissionError(13, "Permission denied", str(root / "denied")))
                return real_walk(top, **kw)

            with unittest.mock.patch.object(snap.os, "walk", side_effect=walk_that_hits_a_denied_dir):
                snap._files_of_repository(root, "vend", unreadable)
            self.assertEqual(unreadable, ["vend"])

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

    def test_a_source_working_tree_encoding_cannot_corrupt_the_backup(self):
        # `working-tree-encoding` re-encodes on add and on checkout, so the
        # stored blob was UTF-8 while the file on disk was UTF-16: the snapshot
        # looked fine and the restore failed byte verification.  The pin must
        # unset it too, in a fresh vault and in one pinned by an older version.
        (self.code / ".gitattributes").write_bytes(b"*.txt working-tree-encoding=UTF-16LE\n")
        utf16 = "héllo\n".encode("utf-16-le")
        (self.code / "u.txt").write_bytes(utf16)

        for older_pin in (None, b"* -text -filter -ident\n"):
            if older_pin is not None:
                (self.vault / ".git" / "info" / "attributes").write_bytes(older_pin)
                utf16 += "x".encode("utf-16-le")
                (self.code / "u.txt").write_bytes(utf16)
            rc, out, err = self.snapshot("--remote", str(self.remote))
            self.assertEqual(rc, 0, out + err)
            stored = subprocess.run(
                ["git", "--git-dir", str(self.vault / ".git"), "cat-file", "blob", "snapshot:u.txt"],
                check=True, capture_output=True,
            ).stdout
            self.assertEqual(stored, utf16, f"re-encoded under pin {older_pin!r}")

        self.restore_to(self.restored)
        self.assertEqual((self.restored / "u.txt").read_bytes(), utf16)

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


class ForeignChainTests(_SnapshotFixture):
    """A new vault continues whatever chain the remote has; it must say so, and
    say louder when the source does not look like the tree that wrote it."""

    def _populate(self, root: pathlib.Path, prefix: str, count: int = 8) -> None:
        for i in range(count):
            (root / f"{prefix}-{i}.txt").write_bytes(f"{prefix} {i}\n".encode())

    def test_the_same_tree_on_another_machine_joins_with_a_note(self):
        self._populate(self.code, "proj")
        self.assertEqual(self.snapshot("--remote", str(self.remote))[0], 0)
        code_b, vault_b = self._second_machine()
        rc, out, err = self.run_tool(
            "--source", code_b, "--vault", vault_b, "--remote", str(self.remote)
        )
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Note: this vault is new, so it continues the snapshot chain", out)
        self.assertNotIn("does not look like the same tree", out)

    def test_a_different_project_on_the_same_remote_is_warned_about(self):
        """Reproduced: project B with a fresh vault and project A's --remote was
        chained onto A's history with no word about it, and the chain's newest
        tree was B's."""
        self._populate(self.code, "proj")
        self.assertEqual(self.snapshot("--remote", str(self.remote))[0], 0)
        other = self.tmp / "other-project"
        other.mkdir()
        self._populate(other, "elsewhere")
        _git(other, "init", "-q", "-b", "main", ".")
        # The vault takes its committer identity from the source repository; a
        # CI runner has no global one (the fixture sets it for self.code too).
        _git(other, "config", "user.email", "dev@example.com")
        _git(other, "config", "user.name", "Dev")
        rc, out, err = self.run_tool(
            "--source", other, "--vault", self.tmp / "vault-other", "--remote", str(self.remote)
        )
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Warning: this vault is new, so it will continue the snapshot chain", out)
        self.assertIn("does not look like the same tree", out)
        self.assertIn("another --remote or --branch", out)

    def test_the_first_snapshot_to_an_empty_remote_says_nothing_about_a_chain(self):
        self._populate(self.code, "proj")
        rc, out, err = self.snapshot("--remote", str(self.remote))
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("continue the snapshot chain", out)
        self.assertNotIn("continues the snapshot chain", out)


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

    @unittest.skipIf(os.name == "nt", "* and ? cannot exist in a Windows file name")
    def test_glob_shaped_names_do_not_stage_their_neighbours(self):
        """The literal-pathspec guard exercised with the other glob shapes.

        S19: the guard was proven with one bracketed name; `*` and `?` in a
        file name are the same hazard (git would expand them across the
        directory) and need a POSIX volume to exist at all.
        """
        (self.code / "a*b.txt").write_bytes(b"star kept\n")
        (self.code / "axb.txt").write_bytes(b"star neighbour\n")
        (self.code / "a?b.txt").write_bytes(b"query kept\n")
        (self.code / "aqb.txt").write_bytes(b"query neighbour\n")

        rc, _, err = self.snapshot("--exclude", "axb.txt", "--exclude", "aqb.txt")
        self.assertEqual(rc, 0, err)
        tree = self.tree()
        self.assertIn("a*b.txt", tree)
        self.assertIn("a?b.txt", tree)
        self.assertNotIn("axb.txt", tree)
        self.assertNotIn("aqb.txt", tree)

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

    def test_the_warning_is_said_once_not_on_every_run(self):
        # On every run it made --exit-warnings exit 3 for ever on a tree with a .env.
        # --default-excludes: the fixture's dist/ would otherwise be a warning of its own.
        rc, out, err = self.snapshot("--no-push", "--default-excludes")
        self.assertIn("look like credentials", out)
        (self.code / "notes.txt").write_bytes(b"changed\n")
        rc, out, err = self.snapshot("--no-push", "--default-excludes", "--exit-warnings")
        self.assertNotIn("look like credentials", out)
        self.assertEqual(rc, 0, out + err)

    def test_a_new_credential_file_is_news_again(self):
        self.snapshot("--no-push")
        (self.code / "deploy.pem").write_bytes(b"-----BEGIN-----\n")
        rc, out, _ = self.snapshot("--no-push")
        self.assertIn("2 captured path(s) look like credentials", out)
        self.assertIn("deploy.pem", out)
        rc, out, _ = self.snapshot("--no-push")
        self.assertNotIn("look like credentials", out)

    def test_a_dry_run_does_not_use_up_the_warning(self):
        self.snapshot("--dry-run")
        rc, out, _ = self.snapshot("--dry-run")
        self.assertIn("look like credentials", out)
        rc, out, _ = self.snapshot("--no-push")
        self.assertIn("look like credentials", out)

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

class WarningAndTipTests(_SnapshotFixture):
    """The warnings and tips say something true and name the way out."""

    def test_an_installed_packages_certificate_bundle_is_not_a_credential(self):
        self.assertEqual(
            snap.find_secrets([
                ".venv/Lib/site-packages/certifi/cacert.pem",
                "web/node_modules/tls/ca.pem",
                "certs/server.pem",
                ".env",
            ]),
            ["certs/server.pem", ".env"],
        )

    def test_a_run_with_nowhere_to_push_warns_and_counts_for_exit_warnings(self):
        # It used to print one quiet line and exit 0, so a scheduled job whose
        # remote was never set looked healthy.
        rc, out, err = self.snapshot("--exit-warnings")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("Warning: no remote to push to", out + err)
        self.assertIn("exists only on this machine", out + err)
        self.assertIn("--remote seafile://<library>/", out + err)

    def test_no_push_on_purpose_does_not_warn_about_the_missing_remote(self):
        rc, out, err = self.snapshot("--no-push")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("no remote to push to", out + err)
        self.assertIn("kept locally", out)

    def test_a_virtualenv_is_suggested_for_exclusion_whatever_its_name(self):
        venv = self.code / ".venv-audit"
        (venv / "Lib").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_bytes(b"home = x\n")
        (venv / "Lib" / "mod.py").write_bytes(b"x = 1\n")
        rc, out, err = self.snapshot("--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("--exclude '.venv-audit'", out)
        rc, out, err = self.snapshot("--dry-run", "--exclude", ".venv-audit")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(".venv-audit'", out)  # already left out: nothing to suggest

    def test_default_excludes_cover_a_virtualenv_under_any_name(self):
        venv = self.code / ".venv-audit"
        (venv / "Lib").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_bytes(b"home = x\n")
        (venv / "Lib" / "mod.py").write_bytes(b"x = 1\n")
        nested = self.code / "tools" / "py312"
        nested.mkdir(parents=True)
        (nested / "pyvenv.cfg").write_bytes(b"home = y\n")
        (nested / "x.py").write_bytes(b"y = 2\n")
        self.assertEqual(snap.find_virtualenvs(self.code, []), [".venv-audit", "tools/py312"])
        self.assertEqual(snap.find_virtualenvs(self.code, [".venv-audit"]), ["tools/py312"])
        rc, out, err = self.snapshot("--dry-run", "--default-excludes")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Warning: the snapshot includes reproducible build output", out)
        self.assertIn("path(s) left out", out)
        # without the flag the same tree is still advised about
        rc, out, err = self.snapshot("--dry-run")
        self.assertIn("--exclude '.venv-audit'", out)

    def test_the_tool_labels_its_messages_in_one_style_and_its_bytes_in_binary_units(self):
        self.assertEqual(snap.human_bytes(1536), "1.5 KiB")
        self.assertEqual(snap.human_bytes(5 * 1024 ** 3), "5.0 GiB")
        source = pathlib.Path(snap.__file__).read_text(encoding="utf-8")
        for loud in ("WARNING:", "NOTICE:", "NOTE:"):
            self.assertNotIn(loud, source, f"{loud!r} drifted back; the convention is Warning:/Note:/Notice:")

    def test_a_dry_run_help_says_it_stages_the_tree(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf, self.assertRaises(SystemExit):
            snap.main(["--help"])
        flat = " ".join(buf.getvalue().split())
        self.assertIn("it uploads nothing", flat)

    def test_the_word_help_is_the_flag_unless_a_directory_has_that_name(self):
        # `git seafile-snapshot --help` is read by git as a man-page request.
        self.assertEqual(snap.help_word_to_flag(["help"]), ["--help"])
        self.assertEqual(snap.help_word_to_flag(["help", "--dry-run"]), ["help", "--dry-run"])
        self.assertEqual(snap.help_word_to_flag([]), [])
        with contextlib.redirect_stdout(io.StringIO()) as buf, self.assertRaises(SystemExit):
            snap.main(["help"])
        self.assertIn("ask for", " ".join(buf.getvalue().split()))
        self.assertIn("git seafile-snapshot help", " ".join(buf.getvalue().split()))
        here = pathlib.Path.cwd()
        (self.tmp / "help").mkdir()
        os.chdir(self.tmp)
        try:
            self.assertEqual(snap.help_word_to_flag(["help"]), ["help"])
        finally:
            os.chdir(here)

    def test_the_help_puts_the_options_a_first_run_needs_before_the_rest(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf, self.assertRaises(SystemExit):
            snap.main(["--help"])
        text = buf.getvalue()
        order = [
            text.index(title + ":")
            for title in ("everyday options", "restore and verify", "for scheduled runs",
                          "local vault housekeeping", "advanced options")
        ]
        self.assertEqual(order, sorted(order))
        everyday = text[order[0]:order[1]]
        for flag in ("--remote", "--exclude", "--dry-run", "--status"):
            self.assertIn(flag, everyday)
        # a flag named in no section is still listed, in the last one
        advanced = text[order[-1]:text.index("payload mode")]
        self.assertIn("--allow-shared-remote", advanced)
        self.assertNotIn("--allow-shared-remote", everyday)


class _Tty(io.StringIO):
    def isatty(self):
        return True


class FirstRunExcludePromptTests(_SnapshotFixture):
    """A person's first run is asked about reproducible directories; nothing else is."""

    def offer(self, *flags, answer="", interactive=True, remembered=None):
        args = snap.parse_args(["--source", str(self.code), "--vault", str(self.vault), *flags])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            snap.offer_default_excludes(
                args, self.code, self.vault, remembered or {},
                interactive=interactive, ask=lambda prompt: answer,
            )
        return args, out.getvalue()

    def test_the_candidates_are_listed_with_their_size_and_a_yes_turns_on_default_excludes(self):
        args, out = self.offer(answer="")
        self.assertIn("look reproducible and are usually not worth backing up", out)
        self.assertIn("node_modules  (", out)
        self.assertIn("dist  (", out)
        self.assertTrue(args.default_excludes)
        self.assertIn("Leaving them out; the vault remembers this", out)

    def test_a_no_captures_everything_and_says_how_to_change_its_mind(self):
        args, out = self.offer(answer="n")
        self.assertIsNone(args.default_excludes)
        self.assertIn("Capturing everything. Pass --default-excludes", out)

    def test_it_stays_quiet_when_the_user_already_decided_or_nobody_is_there(self):
        for flags, kw in (
            (("--default-excludes",), {}), (("--no-default-excludes",), {}),
            (("--exclude", "dist"), {}), ((), {"interactive": False}),
            (("--json",), {}), (("--exit-warnings",), {}), (("--status",), {}),
            ((), {"remembered": {"exclude": ["dist"]}}),
        ):
            with self.subTest(flags=flags, kw=kw):
                args, out = self.offer(*flags, answer="y", **kw)
                self.assertEqual(out, "")

    def test_it_asks_only_before_the_vault_exists(self):
        self.snapshot("--no-push")
        args, out = self.offer(answer="y")
        self.assertEqual(out, "")
        self.assertIsNone(args.default_excludes)

    def test_committed_source_is_never_offered(self):
        (self.code / "build").mkdir()
        (self.code / "build" / "make.py").write_bytes(b"print(1)\n")
        _git(self.code, "add", "build/make.py")
        _git(self.code, "commit", "-qm", "build script")
        self.assertEqual([rel for rel, _ in snap.reproducible_candidates(self.code)],
                         ["dist", "node_modules"])

    def test_the_omission_is_named_with_the_files_it_holds(self):
        """Found by a review: ``--default-excludes`` dropped a never-committed ``src/build``
        and the run said only "3 path(s) left out"."""
        self.assertEqual(
            snap.excluded_summary(["src/build/a.py", "src/build/b/c.py", "dist/x", "m/a.pyc", "m/b.pyc"],
                                  ["src/build", "dist", "*.pyc"]),
            "excluded: 5 path(s) left out:" + chr(10) + "  *.pyc  (2 file(s))" + chr(10)
            + "  dist  (1 file(s))" + chr(10) + "  src/build  (2 file(s))",
        )
        many = [f"d{i}/f" for i in range(34)]
        self.assertTrue(snap.excluded_summary(many, []).endswith("  ... and 4 more"))
        rc, out, err = self.run_tool("--source", self.code, "--vault", self.vault, "--no-push",
                                     "--default-excludes")
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"excluded: \d+ path\(s\) left out:")
        self.assertIn("  node_modules  (1 file(s))", out)

    def test_untracked_source_under_a_build_directory_is_kept_and_named(self):
        """The review's case: a ``src/build`` nobody has committed is the work a snapshot is for."""
        (self.code / "src" / "build").mkdir(parents=True)
        (self.code / "src" / "build" / "make.py").write_bytes(b"print(1)" + bytes([10]))
        rc, out, err = self.run_tool("--source", self.code, "--vault", self.vault, "--no-push",
                                     "--default-excludes")
        self.assertEqual(rc, 0, err)
        self.assertIn("src/build/make.py", self.tree())
        self.assertIn("keeps 'src/build' for this run", out)
        self.assertNotIn("node_modules/pkg/a.js", self.tree())    # the rest of the list applies

    def test_an_ignored_build_directory_is_left_out_and_listed(self):
        (self.code / ".gitignore").write_bytes(b"node_modules/" + bytes([10]) + b"dist/" + bytes([10]) + b"build/" + bytes([10]))
        (self.code / "build").mkdir()
        (self.code / "build" / "out.bin").write_bytes(b"x")
        rc, out, err = self.run_tool("--source", self.code, "--vault", self.vault, "--no-push",
                                     "--default-excludes")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("build/out.bin", self.tree())
        self.assertIn("build  (1 file(s))", out)

    def test_outside_a_repository_a_build_directory_needs_a_project_file_beside_it(self):
        plain = self.tmp / "plain"
        for rel in ("a/build/x.o", "b/build/y.o", "b/package.json"):
            (plain / rel).parent.mkdir(parents=True, exist_ok=True)
            (plain / rel).write_bytes(b"o")
        identity = self.tmp / "identity.gitconfig"        # CI has none, and a plain directory brings none
        identity.write_bytes(b"[user]" + bytes([10]) + b"	name = t" + bytes([10]) + b"	email = t@example.com" + bytes([10]))
        with unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(identity)}):
            rc, out, err = self.run_tool("--source", plain, "--vault", self.tmp / "pv", "--no-push",
                                         "--default-excludes")
        self.assertEqual(rc, 0, err)
        tree = {n for n in _git(self.tmp / "pv", "ls-tree", "-r", "--name-only", BRANCH).stdout.split()}
        self.assertIn("a/build/x.o", tree)          # nothing says a build makes it
        self.assertNotIn("b/build/y.o", tree)       # package.json sits beside it
        self.assertIn("keeps 'a/build' for this run", out)

    def test_the_choice_is_remembered_and_what_it_expanded_to_is_not(self):
        args = ("--source", self.code, "--vault", self.vault, "--remote", self.remote)
        self.assertEqual(self.run_tool(*args, "--default-excludes")[0], 0)
        self.assertEqual(_git(self.vault, "config", "snapshot.defaultexcludes").stdout.strip(), "true")
        self.assertNotEqual(_git(self.vault, "config", "--get-all", "snapshot.exclude").returncode, 0)
        # A directory that gains source later is not left out for ever.
        (self.code / "src" / "build").mkdir(parents=True)
        (self.code / "src" / "build" / "late.py").write_bytes(b"x")
        rc, out, err = self.run_tool(*args)                       # no flag: the vault remembers
        self.assertEqual(rc, 0, err)
        self.assertIn("src/build/late.py", self.tree())
        self.assertNotIn("node_modules/pkg/a.js", self.tree())
        rc, out, err = self.run_tool(*args, "--no-default-excludes")
        self.assertEqual(rc, 0, err)
        self.assertIn("node_modules/pkg/a.js", self.tree())
        self.assertNotEqual(_git(self.vault, "config", "snapshot.defaultexcludes").returncode, 0)

    def test_a_virtualenv_is_offered_by_what_makes_it_one(self):
        venv = self.code / "env-audit"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_bytes(b"home = x\n")
        (venv / "lib.py").write_bytes(b"x = 1\n")
        found = dict(snap.reproducible_candidates(self.code))
        self.assertIn("env-audit", found)
        self.assertEqual(found["env-audit"], len(b"home = x\n") + len(b"x = 1\n"))

    def test_an_eof_at_the_prompt_is_a_no(self):
        args = snap.parse_args(["--source", str(self.code), "--vault", str(self.vault)])

        def eof(prompt):
            raise EOFError

        with contextlib.redirect_stdout(io.StringIO()):
            snap.offer_default_excludes(args, self.code, self.vault, {}, interactive=True, ask=eof)
        self.assertIsNone(args.default_excludes)

    def test_a_yes_at_a_terminal_leaves_the_directories_out_of_the_resolved_settings(self):
        args = snap.parse_args(["--source", str(self.code), "--vault", str(self.vault)])
        # The real input(), reading a "y" from a stdin that claims to be a terminal.
        with unittest.mock.patch("sys.stdin", _Tty("y\n")), unittest.mock.patch("sys.stdout", _Tty()):
            settings = snap.resolve_snapshot_settings(args)
        excluded = settings[4]
        self.assertIn("node_modules", excluded)
        self.assertIn("dist", excluded)


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

    def _vault_with_odd_names(self) -> None:
        """A snapshot chain on ``self.remote`` holding names Windows cannot hold.

        ``git add``, ``update-index`` and even ``fast-import`` refuse them on
        Windows, so the objects are written by hand (``hash-object --literally``):
        the shape of a vault captured on Linux and restored on Windows.
        """
        def put(kind: str, data: bytes) -> bytes:
            out = subprocess.run(
                ["git", "-C", str(self.remote), "hash-object", "-w", "--literally",
                 "-t", kind, "--stdin"],
                input=data, check=True, capture_output=True,
            ).stdout.decode().strip()
            return bytes.fromhex(out)

        def tree(entries: list[tuple[str, str, bytes]]) -> bytes:
            body = b"".join(
                f"{mode} {name}".encode() + b"\0" + sha
                for name, mode, sha in sorted(entries, key=lambda e: e[0].encode())
            )
            return put("tree", body)

        blob = {
            name: put("blob", data) for name, data in
            (("ok", b"one\n"), ("colon", b"two\n"), ("aux", b"three\n"), ("fine", b"four\n"))
        }
        sub = tree([("fine.txt", "100644", blob["fine"])])
        root = tree([
            ("ok.txt", "100644", blob["ok"]),
            ("a:b.txt", "100644", blob["colon"]),
            ("aux.js", "100644", blob["aux"]),
            ("sub", "40000", sub),
        ])
        env = {**os.environ, "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
               "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com"}
        commit = subprocess.run(
            ["git", "-C", str(self.remote), "commit-tree", root.hex(), "-m", "odd names"],
            check=True, capture_output=True, text=True, env=env,
        ).stdout.strip()
        _git(self.remote, "update-ref", f"refs/heads/{BRANCH}", commit)
        _git(self.remote, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")

    @unittest.skipUnless(os.name == "nt", "only Windows refuses these names")
    def test_a_restore_on_windows_goes_around_names_it_cannot_hold(self):
        """``git checkout`` refused the whole tree for one such name ("invalid
        path 'a:b.txt'") and wrote nothing: no file of the snapshot came back
        (reproduced Debian -> Windows).  The rest is restored and verified, the
        names are listed, and relaxing core.protectNTFS is not how."""
        self._vault_with_odd_names()
        rc, out, err = self.restore_to(self.restored)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((self.restored / "ok.txt").read_bytes(), b"one\n")
        self.assertEqual((self.restored / "sub" / "fine.txt").read_bytes(), b"four\n")
        self.assertFalse((self.restored / "aux.js").exists())
        self.assertIn("Warning: 2 file(s) in this snapshot have names Windows cannot hold", out)
        self.assertIn("a:b.txt", out)
        self.assertIn("2 unrepresentable name(s) excluded", out)
        self.assertEqual(
            _git(self.restored, "config", "--get", "core.protectNTFS").stdout.strip(), ""
        )

    @unittest.skipUnless(os.name == "nt", "only Windows refuses these names")
    def test_a_restore_of_an_older_snapshot_on_windows_goes_around_them_too(self):
        self._vault_with_odd_names()
        rc, out, err = self.restore_to(self.restored, "--ref", BRANCH)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue((self.restored / "ok.txt").is_file())
        self.assertIn("have names Windows cannot hold", out)

    @unittest.skipIf(os.name == "nt", "Windows is where names are unrepresentable")
    def test_no_name_is_set_aside_off_windows(self):
        self._vault_with_odd_names()
        self.assertEqual(snap._unrepresentable_paths(self.remote, BRANCH), [])

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
        self.addCleanup(remove_tree, self.tmp)

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

    @unittest.skipIf(_symlinks_round_trip(), "this machine can make links, so nothing is written as text")
    def test_a_whole_restore_goes_on_past_a_link_it_can_only_write_as_text(self):
        repo = self._repo_with({"data.txt": b"plain" + bytes([10])}, {"lnk": "data.txt"})
        _git(repo, "branch", "-m", "main", BRANCH)
        out, err = io.StringIO(), io.StringIO()
        into = self.tmp / "restored"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main(["--restore", "--remote", str(repo), "--into", str(into)])
        self.assertEqual(rc, 0, err.getvalue() + out.getvalue())
        self.assertIn("written as text files", out.getvalue())
        self.assertIn("lnk", out.getvalue())
        self.assertEqual((into / "data.txt").read_bytes(), b"plain" + bytes([10]))
        self.assertIn("(1 link(s) written as text)", out.getvalue())

    def test_a_link_git_could_only_write_as_text_is_reported_not_fatal(self):
        """Windows without Developer Mode: the same class as a name it cannot hold.

        The vault is intact and the rest of the restore is verified, so one symlink must not
        fail the whole restore (and take ``--restore-drill`` with it) -- REVIEW.be01e37 deepseek D1.
        """
        repo = self._repo_with({"data.txt": b"unrelated bytes\n"}, {"a": "target"})
        (repo / "a").write_bytes(b"target")                      # what git writes without symlinks
        _git(repo, "config", "core.symlinks", "false")
        self.assertEqual(snap.links_written_as_text(repo, "main", ["a"]), ["a"])
        # Only a link: a regular file that differs is still a defect.
        self.assertEqual(snap.links_written_as_text(repo, "main", ["data.txt"]), [])
        # Only when git itself cannot make links, and only if the file holds the blob.
        (repo / "a").write_bytes(b"something else")
        self.assertEqual(snap.links_written_as_text(repo, "main", ["a"]), [])
        (repo / "a").write_bytes(b"target")
        _git(repo, "config", "core.symlinks", "true")
        self.assertEqual(snap.links_written_as_text(repo, "main", ["a"]), [])


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

    def test_default_excludes_never_drop_source_the_repository_tracks(self):
        """``build`` is a guess about reproducible output; the user's own repository
        knows ``tools/build/make.py`` is committed source.  A restore of a backup
        without it would not reconstitute the working directory."""
        (self.code / "tools" / "build").mkdir(parents=True)
        (self.code / "tools" / "build" / "make.py").write_bytes(b"print('build')\n")
        _git(self.code, "add", "tools/build/make.py")
        _git(self.code, "commit", "-qm", "tracked build dir")
        rc, out, err = self.run_tool(
            "--source", self.code, "--vault", self.vault, "--default-excludes"
        )
        self.assertEqual(rc, 0, err)
        tree = self.tree()
        self.assertIn("tools/build/make.py", tree)
        self.assertIn("keeps 'tools/build' for this run", out)
        self.assertIn("git does not ignore everything in it", out)
        # the rest of the list still applies, and untracked output is still left out
        self.assertNotIn("node_modules/pkg/a.js", tree)
        self.assertNotIn("dist/bundle.js", tree)

    def test_an_explicit_exclude_of_a_tracked_path_is_still_honoured(self):
        (self.code / "tools" / "build").mkdir(parents=True)
        (self.code / "tools" / "build" / "make.py").write_bytes(b"print('build')\n")
        _git(self.code, "add", "tools/build/make.py")
        _git(self.code, "commit", "-qm", "tracked build dir")
        rc, out, err = self.run_tool(
            "--source", self.code, "--vault", self.vault, "--default-excludes",
            "--exclude", "build",
        )
        self.assertEqual(rc, 0, err)
        self.assertNotIn("tools/build/make.py", self.tree())

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

    def test_a_file_rewritten_while_git_reads_it_is_explained_and_nothing_is_snapshotted(self):
        """Git's message names an object id, not the file or an action (reproduced
        six times out of six on Debian by rewriting a large file under ``git add``)."""
        proc = unittest.mock.MagicMock(
            returncode=128, stdout="",
            stderr="fatal: confused by unstable object source data for cda6b24ab12",
        )
        with unittest.mock.patch.object(snap, "_run", return_value=proc):
            with self.assertRaises(snap.SnapshotError) as ctx:
                snap.git(pathlib.Path("V/.git"), None, ["add", "-A"], cwd=pathlib.Path("."))
        message = str(ctx.exception)
        self.assertIn("was rewritten while git was reading it", message)
        self.assertIn("Nothing was snapshotted", message)
        self.assertIn("--exclude it", message)

    def test_a_short_read_while_indexing_names_the_file_and_what_was_lost(self):
        """Seen 27 minutes into staging 222,000 files: git said only which file."""
        proc = unittest.mock.MagicMock(
            returncode=128, stdout="",
            stderr=(
                "error: short read while indexing .scratch/strict_scope_mutation.py\n"
                "error: .scratch/strict_scope_mutation.py: failed to insert into database\n"
                "error: unable to index file '.scratch/strict_scope_mutation.py'\n"
                "fatal: adding files failed\n"
            ),
        )
        with unittest.mock.patch.object(snap, "_run", return_value=proc):
            with self.assertRaises(snap.SnapshotError) as ctx:
                snap.git(pathlib.Path("V/.git"), None, ["add", "-A"], cwd=pathlib.Path("."))
        message = str(ctx.exception)
        self.assertIn(".scratch/strict_scope_mutation.py changed while git", message)
        self.assertIn("Nothing was snapshotted", message)
        self.assertIn("--exclude it", message)


class ParallelStagingTests(_SnapshotFixture):
    """The objects are written by several ``hash-object`` processes before ``add``.

    Measured: 41.6 s for 20,000 files with ``add`` alone, 17 s with 16 processes
    writing first.  ``add`` still does the index update, so the stat cache and
    the tree are exactly what they were; these tests hold it to that.
    """

    def setUp(self):
        super().setUp()
        for i in range(12):
            (self.code / f"f{i:02d}.txt").write_bytes(f"file {i}\n".encode() * 20)
        patcher = unittest.mock.patch.object(snap, "_PREWRITE_MIN_FILES", 3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_in(self, vault: pathlib.Path, workers: str, *extra: object):
        with unittest.mock.patch.dict(os.environ, {"SEAFILE_SNAPSHOT_STAGE_WORKERS": workers}):
            return self.run_tool("--source", self.code, "--vault", vault, *extra)

    def _tree_id(self, vault: pathlib.Path) -> str:
        return _git(vault, "rev-parse", f"{BRANCH}^{{tree}}").stdout.strip()

    def test_the_parallel_step_records_the_tree_the_serial_one_does(self):
        rc, _, err = self._run_in(self.tmp / "v-serial", "1")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("parallel git processes", err)
        rc, _, err = self._run_in(self.tmp / "v-par", "4")
        self.assertEqual(rc, 0, err)
        self.assertIn("parallel git processes", err)
        self.assertEqual(self._tree_id(self.tmp / "v-par"), self._tree_id(self.tmp / "v-serial"))

    def test_the_add_after_a_prewrite_is_not_killed_for_writing_nothing(self):
        """Measured: the object store is byte-identical across that add, so the
        no-growth watchdog saw a dead step on any disk slower than its limit."""
        self.assertEqual(snap.stall_limit_after_prewrite(10.0), snap._stall_limit_from_env())
        self.assertEqual(snap.stall_limit_after_prewrite(2000.0), 6000.0)
        calls = []
        real = snap.git

        expects = []

        def spy(git_dir, work_tree, args, **kw):
            if args[:2] == ["add", "-A"]:
                calls.append(kw.get("stall_limit", snap._UNSET_STALL))
                expects.append(kw.get("expect_bytes"))
            return real(git_dir, work_tree, args, **kw)

        with unittest.mock.patch.object(snap, "git", spy):
            rc, _, err = self._run_in(self.vault, "4")
        self.assertEqual(rc, 0, err)
        self.assertIn("parallel git processes", err)
        self.assertEqual(len(calls), 1)
        self.assertNotIsInstance(calls[0], snap._UnsetStall,
                                 "the add after a pre-write kept the no-growth default")
        # It writes nothing, so it must not be shown as having the whole tree still to write:
        # "at most 11h27m left" was printed on a 226,000-file run that took two minutes.
        self.assertEqual(expects, [0])

    def test_a_raised_stall_limit_says_so_and_the_usual_one_is_quiet(self):
        """DESIGN 3.5 "say what was chosen": a kill on an unmeasured disk must be readable."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(snap.stall_limit_after_prewrite(2000.0), 6000.0)
        self.assertIn("may go 6000 s without writing anything", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as quiet:
            snap.stall_limit_after_prewrite(10.0)
        self.assertEqual(quiet.getvalue(), "")

    def test_without_a_prewrite_the_add_keeps_the_usual_limit(self):
        calls = []
        real = snap.git

        def spy(git_dir, work_tree, args, **kw):
            if args[:2] == ["add", "-A"]:
                calls.append(kw.get("stall_limit", snap._UNSET_STALL))
            return real(git_dir, work_tree, args, **kw)

        with unittest.mock.patch.object(snap, "git", spy),                 unittest.mock.patch.object(snap, "_PREWRITE_MIN_FILES", 10 ** 6):
            rc, _, err = self._run_in(self.vault, "4")
        self.assertEqual(rc, 0, err)
        self.assertIsInstance(calls[0], snap._UnsetStall)

    def test_the_step_itself_writes_the_objects(self):
        """Without this the tests above pass with a step that does nothing."""
        bare = self.tmp / "bare"
        _git(self.tmp, "init", "-q", "--bare", str(bare))
        with unittest.mock.patch.dict(os.environ, {"SEAFILE_SNAPSHOT_STAGE_WORKERS": "3"}):
            with contextlib.redirect_stderr(io.StringIO()):
                count, _spent = snap.prewrite_objects(bare, self.code)
        self.assertGreaterEqual(count, 12)
        blob = _git(bare, "hash-object", "--no-filters", str(self.code / "f01.txt")).stdout.strip()
        self.assertEqual(_git(bare, "cat-file", "-e", blob).returncode, 0)

    def test_a_disk_that_cannot_use_writers_leaves_the_rest_to_add(self):
        """End to end: the ramp gives up, says so, and the tree is still right."""
        (self.code / "more").mkdir()
        for i in range(30):
            (self.code / "more" / f"m{i:02d}.txt").write_bytes(b"more %d\n" % i * 10)
        rc, _, err = self._run_in(self.tmp / "v-serial", "1")
        self.assertEqual(rc, 0, err)
        env = {"SEAFILE_SNAPSHOT_STAGE_WORKERS": ""}
        with unittest.mock.patch.object(snap, "_PREWRITE_BATCH_FILES", 2):
            with unittest.mock.patch.object(snap, "_RAMP_GAIN", 1000.0):
                with unittest.mock.patch.dict(os.environ, env):
                    rc, _, err = self.run_tool("--source", self.code, "--vault", self.tmp / "v-auto")
        self.assertEqual(rc, 0, err)
        self.assertIn("did not help", err)
        self.assertEqual(self._tree_id(self.tmp / "v-auto"), self._tree_id(self.tmp / "v-serial"))

    def test_an_unchanged_tree_writes_nothing_in_parallel_the_second_time(self):
        rc, _, err = self._run_in(self.vault, "4")
        self.assertEqual(rc, 0, err)
        self.assertIn("parallel git processes", err)
        rc, _, err = self._run_in(self.vault, "4")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("parallel git processes", err)

    def test_an_excluded_file_is_not_opened_by_the_parallel_step(self):
        rc, _, err = self._run_in(self.vault, "4", "--exclude", ".env")
        self.assertEqual(rc, 0, err)
        self.assertIn("parallel git processes", err)
        blob = _git(self.vault, "hash-object", "--no-filters", str(self.code / ".env")).stdout.strip()
        self.assertNotEqual(_git(self.vault, "cat-file", "-e", blob).returncode, 0,
                            "the excluded file's bytes reached the object store")

    def test_candidates_are_the_files_add_would_have_to_write(self):
        git_dir = self.code / ".git"
        (self.code / "README.md").write_bytes(b"changed\n")
        found = dict(snap._prewrite_candidates(git_dir, self.code, None))
        self.assertIn("README.md", found)      # tracked and modified
        self.assertIn(".env", found)           # ignored: `add -f` takes it
        self.assertNotIn(".gitignore", found)  # tracked and unchanged
        self.assertEqual(found["README.md"], 8)
        only = dict(snap._prewrite_candidates(git_dir, self.code, {".env"}))
        self.assertEqual(list(only), [".env"])

    @unittest.skipIf(os.name == "nt", "symlinks need a privilege on Windows")
    def test_a_symlink_is_left_to_add(self):
        """``hash-object`` would store the target's bytes where ``add`` stores the link."""
        os.symlink("README.md", self.code / "link.md")
        found = dict(snap._prewrite_candidates(self.code / ".git", self.code, None))
        self.assertNotIn("link.md", found)

    def test_a_bad_worker_count_falls_back_with_a_warning(self):
        with unittest.mock.patch.dict(os.environ, {"SEAFILE_SNAPSHOT_STAGE_WORKERS": "many"}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                workers, forced = snap.prewrite_workers()
        self.assertGreaterEqual(workers, 1)
        self.assertFalse(forced, "a bad value must not count as a typed setting")
        self.assertIn("is not a whole", err.getvalue())

    def test_batches_stop_at_the_file_and_byte_caps(self):
        files = [(f"a{i}", 10) for i in range(1250)]
        sizes = [len(b) for b in snap._prewrite_batches(files)]
        self.assertEqual(sizes, [500, 500, 250])
        big = [("x", 100 * 2**20), ("y", 100 * 2**20), ("z", 1)]
        self.assertEqual([len(b) for b in snap._prewrite_batches(big)], [1, 2])


class StagingRampTests(unittest.TestCase):
    """The number of writers is found on the disk, not assumed from an NVMe drive.

    ``_simulate`` feeds the controller a disk described by one function: the
    files per second ``level`` writers get between them.  Nothing here touches
    a real disk, so the slow cases are as real as the fast ones.
    """

    @staticmethod
    def _simulate(total_rate, ceiling=16, batches=400, batch_files=500):
        import heapq
        ramp = snap._Ramp(ceiling, False, 0.0)
        now, submitted, running = 0.0, 0, []
        while True:
            while submitted < batches and not ramp.gave_up and len(running) < ramp.level:
                per_writer = total_rate(ramp.level) / ramp.level
                heapq.heappush(running, (now + batch_files / per_writer, now, batch_files))
                submitted += 1
            if not running:
                return ramp, submitted
            end, began, files = heapq.heappop(running)
            now = end
            ramp.finished(began, files, now)

    def test_a_disk_that_scales_gets_many_writers(self):
        ramp, _ = self._simulate(lambda n: min(n, 12) * 800.0)
        self.assertFalse(ramp.gave_up)
        self.assertGreaterEqual(ramp.level, 8)

    def test_a_disk_that_saturates_at_four_settles_at_four(self):
        ramp, _ = self._simulate(lambda n: min(n, 4) * 400.0)
        self.assertEqual((ramp.level, ramp.gave_up, ramp.settled), (4, False, True))

    def test_a_spinning_disk_gets_no_parallel_writers_and_the_step_stops(self):
        # Seeking between writers: two are slightly worse than one.
        ramp, submitted = self._simulate(lambda n: 150.0 if n == 1 else 135.0)
        self.assertTrue(ramp.gave_up)
        self.assertEqual(ramp.level, 1)
        # Patience costs three levels' windows (1, 2, 4 writers), then it stops.
        self.assertLess(submitted, 30, "a disk that cannot use writers kept being fed")

    def test_a_barely_better_disk_is_not_worth_the_second_read(self):
        ramp, _ = self._simulate(lambda n: 150.0 * (1 + 0.05 * (n - 1)))
        self.assertTrue(ramp.gave_up)

    def test_a_typed_count_is_used_as_it_is_and_never_measured(self):
        ramp = snap._Ramp(8, True, 0.0)
        for i in range(40):
            ramp.finished(float(i), 500, i + 1.0)
        self.assertEqual((ramp.level, ramp.settled, ramp.gave_up), (8, True, False))

    def test_work_started_before_a_window_does_not_count_towards_it(self):
        ramp = snap._Ramp(16, False, 0.0)
        for began in (0.0, 1.0, 2.0):
            ramp.finished(began, 500, began + 1.0)   # 500 files/s: the window closes at 3.0
        self.assertEqual(ramp.level, 2)
        ramp.finished(2.5, 500, 3.5)       # started before the window opened at 3.0
        self.assertEqual(ramp._batches, 0)


class IncrementalStagingTests(_SnapshotFixture):
    """``git add`` is given the paths that changed, not every path of the tree.

    A path list is matched against every index entry, so listing 222,000 paths
    on a tree that had 2,600 new ones cost 22 minutes (measured on scratch
    repositories: 20,000 / 40,000 / 80,000 listed paths took 3.9 / 18 / 76 s, a
    plain ``git add .`` 0.1 s).  These tests hold the list to what changed and
    the tree to what the full list would have produced.
    """

    EXCLUDE = ("--exclude", "node_modules")

    def setUp(self):
        super().setUp()
        for i in range(12):
            (self.code / f"f{i:02d}.txt").write_bytes(f"file {i}\n".encode() * 20)

    def _adds(self, *extra: object) -> tuple[int, str, list[set[str]]]:
        """Run the tool; return rc, stderr and the path list of each ``add`` it made."""
        lists: list[set[str]] = []
        real = snap.git

        def spy(git_dir, work_tree, args, **kw):
            if args[:1] == ["add"] and "--pathspec-from-file=-" in args:
                lists.append({p for p in kw["stdin"].split("\0") if p})
            return real(git_dir, work_tree, args, **kw)

        with unittest.mock.patch.object(snap, "git", spy):
            rc, _, err = self.snapshot(*self.EXCLUDE, *extra)
        return rc, err, lists

    def test_a_first_run_lists_every_kept_path(self):
        rc, err, lists = self._adds()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0], self.tree())
        self.assertIn(f"staging: {len(lists[0])} of {len(lists[0])} path(s) are new or changed", err)

    def test_a_later_run_lists_only_what_is_new_or_modified(self):
        self.assertEqual(self._adds()[0], 0)
        (self.code / "f03.txt").write_bytes(b"edited\n")
        (self.code / "brand-new.txt").write_bytes(b"new\n")
        rc, err, lists = self._adds()
        self.assertEqual(rc, 0, err)
        self.assertEqual(lists, [{"f03.txt", "brand-new.txt"}],
                         "unchanged paths were handed to git add again")
        total = len(self.tree())
        self.assertIn(f"staging: 2 of {total} path(s) are new or changed", err)

    def test_an_unchanged_tree_runs_no_add(self):
        self.assertEqual(self._adds()[0], 0)
        rc, err, lists = self._adds()
        self.assertEqual(rc, 0, err)
        self.assertEqual(lists, [])
        self.assertIn("staging: 0 of ", err)

    def test_the_tree_is_what_the_full_list_gives(self):
        """Edits, additions, a deletion and a new exclude across runs, against a
        vault that stages everything from nothing."""
        self.assertEqual(self._adds()[0], 0)
        (self.code / "f01.txt").write_bytes(b"edited\n")
        (self.code / "f02.txt").unlink()
        (self.code / "sub").mkdir()
        (self.code / "sub" / "n.txt").write_bytes(b"n\n")
        self.assertEqual(self._adds()[0], 0)
        (self.code / "sub" / "n.txt").write_bytes(b"n again\n")
        rc, err, _ = self._adds("--exclude", "dist")
        self.assertEqual(rc, 0, err)
        fresh = self.tmp / "fresh"
        rc, _, err = self.run_tool("--source", self.code, "--vault", fresh,
                                   *self.EXCLUDE, "--exclude", "dist")
        self.assertEqual(rc, 0, err)
        tree = lambda v: _git(v, "rev-parse", f"{BRANCH}^{{tree}}").stdout.strip()  # noqa: E731
        self.assertEqual(tree(self.vault), tree(fresh))
        self.assertNotIn("f02.txt", self.tree())
        self.assertNotIn("dist/bundle.js", self.tree())

    def test_a_touched_file_is_brought_up_to_date_not_read_every_run(self):
        """Same bytes, new modification time: ``ls-files -m`` alone would keep
        reporting it and re-read it on every run, as it never refreshes the index."""
        self.assertEqual(self._adds()[0], 0)
        target = self.code / "f05.txt"
        later = target.stat().st_mtime + 3600
        os.utime(target, (later, later))
        rc, err, lists = self._adds()
        self.assertEqual(rc, 0, err)
        self.assertEqual(lists, [], "a file with unchanged bytes was staged")
        shown = _git(self.vault, "--work-tree", str(self.code), "ls-files", "--debug",
                     "--", "f05.txt").stdout
        match = re.search(r"mtime:\s*(\d+):", shown)
        self.assertIsNotNone(match, shown)
        self.assertEqual(int(match.group(1)), int(later))

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "needs a filesystem that ignores case")
    def test_a_new_file_in_a_directory_renamed_by_case_is_not_lost(self):
        """Found by a review: the index keeps ``d/``, the listing says ``D/new.txt``, and the
        file just staged as ``d/new.txt`` was dropped as stale on every run.  The two routes
        agree about this case (both lost it), so only a comparison with the disk sees it."""
        (self.code / "d").mkdir()
        (self.code / "d" / "x.txt").write_bytes(b"x\n")
        self.assertEqual(self.snapshot(*self.EXCLUDE)[0], 0)
        os.rename(self.code / "d", self.code / "tmp_d")
        os.rename(self.code / "tmp_d", self.code / "D")
        (self.code / "D" / "new.txt").write_bytes(b"new\n")
        rc, _, err = self.snapshot(*self.EXCLUDE)
        self.assertEqual(rc, 0, err)
        recorded = {name.casefold() for name in self.tree()}
        self.assertIn("d/new.txt", recorded)
        self.assertIn("d/x.txt", recorded)

    @unittest.skipIf(os.name == "nt", "Windows always takes the filtered route (junction check)")
    def test_the_unfiltered_route_is_unchanged(self):
        """No excludes: ``add -A -f .`` as before, no path list."""
        lists: list[object] = []
        real = snap.git

        def spy(git_dir, work_tree, args, **kw):
            if args[:1] == ["add"]:
                lists.append(args)
            return real(git_dir, work_tree, args, **kw)

        with unittest.mock.patch.object(snap, "git", spy):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 0, err)
        self.assertEqual(lists, [["add", "-A", "-f", "."]])


class LongListStagingTests(_SnapshotFixture):
    """A long path list goes to ``update-index``, which does not match every path
    against the index the way ``add`` does (first run, objects written, scratch
    trees: 121.5 s against 43.0 s for 80,000 files, identical trees)."""

    EXCLUDE = ("--exclude", "node_modules")

    def setUp(self):
        super().setUp()
        for i in range(12):
            (self.code / f"f{i:02d}.txt").write_bytes(f"file {i}\n".encode() * 20)

    def _run(self, vault, threshold, *extra):
        """Run with the route threshold set; return rc, stderr, the staging verbs used."""
        verbs: list[list[str]] = []
        real = snap.git

        def spy(git_dir, work_tree, args, **kw):
            if args[:1] in (["add"], ["update-index"]) and "stdin" in kw and kw["stdin"]:
                if "--pathspec-from-file=-" in args or "--stdin" in args and "--add" in args:
                    verbs.append(list(args))
            return real(git_dir, work_tree, args, **kw)

        with unittest.mock.patch.object(snap, "_INDEX_ROUTE_MIN_PATHS", threshold):
            with unittest.mock.patch.object(snap, "git", spy):
                rc, _, err = self.run_tool("--source", self.code, "--vault", vault,
                                           *self.EXCLUDE, *extra)
        return rc, err, verbs

    def _tree_id(self, vault):
        return _git(vault, "rev-parse", f"{BRANCH}^{{tree}}").stdout.strip()

    def test_the_threshold_picks_the_command(self):
        few, _ = snap.staging_commands(["README.md", ".gitignore"], self.code)
        self.assertEqual([c[0][0] for c in few], ["add"])
        many, _ = snap.staging_commands(["README.md"] * snap._INDEX_ROUTE_MIN_PATHS, self.code)
        self.assertEqual([c[0] for c in many],
                         [["update-index", "--add", "--remove", "--replace", "-z", "--stdin"]])
        self.assertTrue(many[0][1].endswith("\0"))
        self.assertEqual(many[0][1].count("\0"), snap._INDEX_ROUTE_MIN_PATHS)

    def test_a_path_that_vanished_is_dropped_not_fatal_on_either_route(self):
        """The list is made before a pre-write that can take many minutes.  Both
        commands stop the whole run on a missing path (rc 128: ``add`` says the
        pathspec did not match, ``update-index`` that it does not exist), so a
        scratch file deleted in that time cost the run."""
        for threshold in (10 ** 9, 2):
            with self.subTest(threshold=threshold), unittest.mock.patch.object(
                snap, "_INDEX_ROUTE_MIN_PATHS", threshold
            ):
                commands, drop_first = snap.staging_commands(
                    ["README.md", "gone.txt", "also/gone.txt"], self.code)
                self.assertEqual([c[1] for c in commands], ["README.md\0"])
                self.assertEqual(drop_first, ["gone.txt", "also/gone.txt"])

    def test_a_path_whose_parent_became_a_file_counts_as_vanished(self):
        (self.code / "now-a-file").write_bytes(b"x\n")
        commands, drop_first = snap.staging_commands(["now-a-file/inner.txt"], self.code)
        self.assertEqual(drop_first, ["now-a-file/inner.txt"])
        self.assertEqual(commands, [], "an empty list must run no command: add stops on it")

    def _vanish_during_staging(self, threshold, tracked):
        """Run twice; the second run's listing names a file that is deleted right after."""
        vault = self.tmp / f"v-vanish-{threshold}-{tracked}"
        (self.code / "f09.txt").write_bytes(b"file 9\n" * 20)   # an earlier subtest deleted it
        (self.code / "fresh.txt").unlink(missing_ok=True)
        self.assertEqual(self._run(vault, threshold)[0], 0)
        (self.code / "f08.txt").write_bytes(b"edited\n")
        (self.code / "fresh.txt").write_bytes(b"fresh\n")
        real = snap.paths_to_stage

        def listed_then_deleted(git_dir, work_tree, kept):
            found = real(git_dir, work_tree, kept)
            os.unlink(self.code / ("f09.txt" if tracked else "fresh.txt"))
            return found + (["f09.txt"] if tracked else [])

        with unittest.mock.patch.object(snap, "paths_to_stage", listed_then_deleted):
            rc, err, _ = self._run(vault, threshold)
        self.assertEqual(rc, 0, err)
        return _git(vault, "ls-tree", "-r", "--name-only", BRANCH).stdout.split()

    def test_a_tracked_file_deleted_after_the_listing_leaves_the_snapshot(self):
        for threshold in (10 ** 9, 2):
            with self.subTest(threshold=threshold):
                tree = self._vanish_during_staging(threshold, tracked=True)
                self.assertNotIn("f09.txt", tree)
                self.assertIn("f08.txt", tree)

    def test_a_new_file_deleted_after_the_listing_is_just_not_there(self):
        for threshold in (10 ** 9, 2):
            with self.subTest(threshold=threshold):
                tree = self._vanish_during_staging(threshold, tracked=False)
                self.assertNotIn("fresh.txt", tree)
                self.assertIn("f09.txt", tree)

    def test_every_listed_path_vanishing_runs_no_staging_command(self):
        """``add`` stops on an empty list (``empty string is not a valid pathspec``)."""
        real = snap.paths_to_stage

        def listed_then_deleted(git_dir, work_tree, kept):
            found = real(git_dir, work_tree, kept)
            os.unlink(self.code / "fresh.txt")
            return found

        for threshold in (10 ** 9, 2):
            vault = self.tmp / f"v-all-gone-{threshold}"
            self.assertEqual(self._run(vault, threshold)[0], 0)
            (self.code / "fresh.txt").write_bytes(b"fresh\n")
            with self.subTest(threshold=threshold), unittest.mock.patch.object(
                snap, "paths_to_stage", listed_then_deleted
            ):
                rc, err, verbs = self._run(vault, threshold)
            self.assertEqual(rc, 0, err)
            self.assertEqual(verbs, [], "a staging command ran with nothing to stage")

    def test_update_index_is_told_to_remove_what_vanishes_after_the_check(self):
        """The few milliseconds the lstat cannot cover: without ``--remove``
        update-index stops the run on a path that is gone."""
        with unittest.mock.patch.object(snap, "_INDEX_ROUTE_MIN_PATHS", 1):
            commands, _drop = snap.staging_commands(["README.md"], self.code)
        self.assertIn("--remove", commands[0][0])

    def test_a_directory_goes_to_add_and_the_files_to_update_index(self):
        """``update-index`` refuses a directory, and leaving it out lost the files of a
        nested repository that took a file's place: the directory takes ``add``, as
        it always did, and runs first."""
        (self.code / "was-a-file").mkdir()
        (self.code / "was-a-file" / "inside.txt").write_bytes(b"x\n")
        with unittest.mock.patch.object(snap, "_INDEX_ROUTE_MIN_PATHS", 2):
            commands, drop_first = snap.staging_commands(["README.md", "was-a-file"], self.code)
        self.assertEqual([(c[0][0], c[1]) for c in commands],
                         [("add", "was-a-file\0"), ("update-index", "README.md\0")])
        self.assertEqual(drop_first, [])
        with unittest.mock.patch.object(snap, "_INDEX_ROUTE_MIN_PATHS", 10 ** 9):
            commands, _ = snap.staging_commands(["README.md", "was-a-file"], self.code)
        self.assertEqual([(c[0][0], c[1]) for c in commands], [("add", "README.md\0was-a-file\0")])

    def test_a_file_replaced_by_a_directory_with_nothing_to_stage_leaves_the_snapshot(self):
        """The case the route got wrong first: ``update-index`` has nothing to
        replace the old file entry with when the directory is empty, or holds only
        excluded files, so the entry stayed and the snapshot kept a file that no
        longer exists.  ``add`` drops it; both routes must give the same tree."""
        def evolve(vault, threshold, only_excluded):
            self.assertEqual(self._run(vault, threshold)[0], 0)
            (self.code / "f06.txt").unlink()
            (self.code / "f06.txt").mkdir()
            if only_excluded:
                (self.code / "f06.txt" / "node_modules").write_bytes(b"excluded\n")
            (self.code / "f07.txt").write_bytes(b"edited\n")
            rc, err, _ = self._run(vault, threshold)
            self.assertEqual(rc, 0, err)
            return self._tree_id(vault), _git(vault, "ls-tree", "-r", "--name-only", BRANCH).stdout.split()

        for only_excluded in (False, True):
            with self.subTest(only_excluded=only_excluded):
                def restore_files():
                    for i in range(12):
                        path = self.code / f"f{i:02d}.txt"
                        if path.is_dir():
                            shutil.rmtree(path)
                        path.write_bytes(f"file {i}\n".encode() * 20)

                restore_files()
                via_index = evolve(self.tmp / f"vi-{only_excluded}", 2, only_excluded)
                restore_files()
                via_add = evolve(self.tmp / f"va-{only_excluded}", 10 ** 9, only_excluded)
                self.assertEqual(via_index, via_add)
                self.assertNotIn("f06.txt", via_index[1])

    def test_a_long_list_uses_update_index_and_records_the_same_tree(self):
        rc, err, verbs = self._run(self.tmp / "v-index", 3)
        self.assertEqual(rc, 0, err)
        self.assertEqual([v[0] for v in verbs], ["update-index"])
        rc, err, verbs = self._run(self.tmp / "v-add", 10 ** 9)
        self.assertEqual(rc, 0, err)
        self.assertEqual([v[0] for v in verbs], ["add"])
        self.assertEqual(self._tree_id(self.tmp / "v-index"), self._tree_id(self.tmp / "v-add"))

    def test_the_two_routes_agree_across_edits_deletions_and_a_file_for_a_directory(self):
        def evolve(vault, threshold):
            self.assertEqual(self._run(vault, threshold)[0], 0)
            (self.code / "f01.txt").write_bytes(b"edited\n")
            (self.code / "f02.txt").unlink()
            (self.code / "tree").mkdir()
            (self.code / "tree" / "leaf.txt").write_bytes(b"leaf\n")
            self.assertEqual(self._run(vault, threshold)[0], 0)
            shutil.rmtree(self.code / "tree")
            (self.code / "tree").write_bytes(b"now a file\n")   # a file where a directory was
            (self.code / "f03.txt").unlink()
            (self.code / "f03.txt").mkdir()                      # and a directory where a file was
            (self.code / "f03.txt" / "inside.txt").write_bytes(b"inside\n")
            rc, err, _ = self._run(vault, threshold)
            self.assertEqual(rc, 0, err)
            return self._tree_id(vault)

        via_index = evolve(self.tmp / "v-index", 3)
        # Back to the starting tree for the other route.
        shutil.rmtree(self.code / "f03.txt")
        (self.code / "f03.txt").write_bytes(b"file 3\n" * 20)
        (self.code / "f02.txt").write_bytes(b"file 2\n" * 20)
        (self.code / "f01.txt").write_bytes(b"file 1\n" * 20)
        (self.code / "tree").unlink()
        via_add = evolve(self.tmp / "v-add", 10 ** 9)
        self.assertEqual(via_index, via_add)
        tree = _git(self.tmp / "v-index", "ls-tree", "-r", "--name-only", BRANCH).stdout.split()
        self.assertIn("tree", tree)
        self.assertIn("f03.txt/inside.txt", tree)
        self.assertNotIn("tree/leaf.txt", tree)

    def test_a_file_that_changes_under_update_index_is_retried(self):
        real = snap.git
        seen: list[int] = []

        def spy(git_dir, work_tree, args, **kw):
            if args[:2] == ["update-index", "--add"]:
                seen.append(1)
                if len(seen) == 1:
                    raise snap.SnapshotError(
                        "git update-index failed (rc=128)\nerror: short read while indexing "
                        "f04.txt\nfatal: Unable to process path f04.txt")
            return real(git_dir, work_tree, args, **kw)

        with unittest.mock.patch.object(snap, "_READ_RETRY_PAUSE_SECONDS", 0.0):
            with unittest.mock.patch.object(snap, "_INDEX_ROUTE_MIN_PATHS", 3):
                with unittest.mock.patch.object(snap, "git", spy):
                    rc, _, err = self.run_tool("--source", self.code, "--vault", self.vault,
                                               *self.EXCLUDE)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(seen), 2)
        self.assertIn("f04.txt changed while git was reading it", err)


def _holds_non_utf8_names() -> bool:
    """Can this filesystem hold a name that is not UTF-8?  NTFS cannot and APFS refuses (EILSEQ)."""
    probe = pathlib.Path(tempfile.mkdtemp(prefix="grs-name-probe-"))
    try:
        with open(os.path.join(os.fsencode(probe), b"bad_" + bytes([255]) + b".txt"), "wb") as handle:
            handle.write(b"x")
        return True
    except (OSError, ValueError):
        return False
    finally:
        remove_tree(probe)


def _can_symlink() -> bool:
    probe = pathlib.Path(tempfile.mkdtemp(prefix="grs-symlink-probe-"))
    try:
        os.symlink("target", probe / "link")
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        remove_tree(probe)


def _nested_repo(path: pathlib.Path, commit: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "inner.txt").write_bytes(b"inner\n")
    _git(path, "init", "-q")
    if commit:
        _git(path, "-c", "user.email=a@b", "-c", "user.name=n", "add", ".")
        _git(path, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-qm", "i")


def _clear(path: pathlib.Path) -> None:
    """Remove a file, link, fifo, socket or directory tree (see ``remove_tree``)."""
    remove_tree(path)


class _RouteComparison(_SnapshotFixture):
    """Fixture, no tests: run a scenario through both staging routes and compare.

    The route for a long list (``update-index``) and the one for a short list
    (``add``) are different commands.  A scenario is ``(setup, mutate)``: the
    base tree plus ``setup`` is snapshotted, ``mutate`` changes it, it is
    snapshotted again, and the recorded tree (modes included) is the result.  The
    comparison is the test, so a scenario both routes get wrong the same way
    still passes -- except that a run must succeed unless the scenario is named in
    ``EXPECTED_FAILURES`` (then both must fail, with the same exit code).
    """

    EXCLUDE = ("--exclude", "node_modules")
    EXPECTED_FAILURES: "dict[str, int]" = {}

    def setUp(self):
        super().setUp()
        for i in range(4):
            (self.code / f"f{i}.txt").write_bytes(f"file {i}\n".encode() * 5)
        (self.code / "d" / "e").mkdir(parents=True)
        (self.code / "d" / "x.txt").write_bytes(b"x\n")
        (self.code / "d" / "e" / "y.txt").write_bytes(b"y\n")
        self._base_names = {p.name for p in self.code.iterdir()}

    def _history(self, vault, threshold, scenario):
        setup, mutate = scenario
        # The same tree every time: whatever an earlier scenario or route left is
        # removed, and the base files are written again.
        for item in list(self.code.iterdir()):
            if item.name not in self._base_names:
                _clear(item)
        for name in ("f0.txt", "f1.txt", "f2.txt", "f3.txt", "d"):
            _clear(self.code / name)
        for i in range(4):
            (self.code / f"f{i}.txt").write_bytes(f"file {i}\n".encode() * 5)
        (self.code / "d" / "e").mkdir(parents=True)
        (self.code / "d" / "x.txt").write_bytes(b"x\n")
        (self.code / "d" / "e" / "y.txt").write_bytes(b"y\n")
        setup(self.code)
        with unittest.mock.patch.object(snap, "_INDEX_ROUTE_MIN_PATHS", threshold):
            rc0, _, err0 = self.run_tool("--source", self.code, "--vault", vault,
                                         *self.EXCLUDE, "--no-push")
            mutate(self.code)
            rc1, _, err1 = self.run_tool("--source", self.code, "--vault", vault,
                                         *self.EXCLUDE, "--no-push")
        # The text of a failure names the command that failed, which differs by route.
        listing = _git(vault, "ls-tree", "-r", BRANCH).stdout if rc1 == 0 else "<failed>"
        return rc0, rc1, listing, err1[-300:]

    def _check(self, scenarios):
        # A vault of its own per scenario and route: deleting one is not reliable on
        # Windows (git's object files are read-only), and a vault left behind with
        # history would make the next "first run" something else.
        for number, (name, scenario) in enumerate(scenarios.items()):
            with self.subTest(scenario=name):
                via_add = self._history(self.tmp / f"v-add-{number}", 10 ** 9, scenario)
                via_index = self._history(self.tmp / f"v-index-{number}", 1, scenario)
                self.assertEqual(via_index[:3], via_add[:3], f"add said: {via_add[3]!r}; "
                                 f"update-index said: {via_index[3]!r}")
                expected = self.EXPECTED_FAILURES.get(name)
                self.assertEqual(via_add[1], expected or 0, via_add[3])


class ExoticReplacementTests(_RouteComparison):
    """Whatever happens to a path between two runs, the two staging routes record the same tree.

    A wider set (27 scenarios, run on Windows and Debian) found three bugs in the
    first version of the long-list route (a nested repository that took a file's
    place recorded nothing, an empty directory kept the old file, a vanished path
    stopped the run); these are the replacements worth running every time.
    """

    def _scenarios(self):
        def nothing(code):
            pass

        def swap(old, new_text="flat\n"):
            def mutate(code):
                _clear(code / old)
                (code / old).write_bytes(new_text.encode())
            return mutate

        scenarios = {
            "a parent directory becomes a file": (nothing, swap("d")),
            "a middle directory becomes a file": (nothing, swap("d/e")),
            "a file becomes a directory three levels down": (
                nothing, lambda c: (_clear(c / "f3.txt"), (c / "f3.txt" / "a" / "b").mkdir(parents=True),
                                    (c / "f3.txt" / "a" / "b" / "c.txt").write_bytes(b"c\n"))),
            "a file becomes a directory of one empty directory": (
                nothing, lambda c: (_clear(c / "f3.txt"), (c / "f3.txt" / "a" / "b").mkdir(parents=True))),
            "a file becomes a nested repository with a commit": (
                nothing, lambda c: (_clear(c / "f1.txt"), _nested_repo(c / "f1.txt"))),
            "a nested repository with a commit becomes a file": (
                lambda c: _nested_repo(c / "nr"), swap("nr", "file now\n")),
            "a whole directory is deleted": (nothing, lambda c: _clear(c / "d")),
            "names with spaces, unicode, a dash and brackets": (
                nothing, lambda c: [
                    ((c / n).parent.mkdir(parents=True, exist_ok=True), (c / n).write_bytes(b"n\n"))
                    for n in ("a b.txt", "-dash.txt", "café.txt", "sp ace/in side.txt",
                              "[brackets].txt", "#hash.txt")]),
            "a file becomes empty": (nothing, lambda c: (c / "f2.txt").write_bytes(b"")),
        }
        if _can_symlink():
            def link(target):
                return lambda c: (_clear(c / "f1.txt"), os.symlink(target, c / "f1.txt"))
            scenarios.update({
                "a file becomes a symlink": (nothing, link("f2.txt")),
                "a symlink becomes a file": (lambda c: os.symlink("f2.txt", c / "lnk"), swap("lnk")),
                "a symlink becomes a directory with files": (
                    lambda c: os.symlink("f2.txt", c / "lnk"),
                    lambda c: (_clear(c / "lnk"), (c / "lnk").mkdir(), (c / "lnk" / "a.txt").write_bytes(b"a\n"))),
                "a symlink becomes an empty directory": (
                    lambda c: os.symlink("f2.txt", c / "lnk"),
                    lambda c: (_clear(c / "lnk"), (c / "lnk").mkdir())),
                "a directory becomes a symlink": (
                    nothing, lambda c: (_clear(c / "d"), os.symlink("f2.txt", c / "d"))),
            })
        return scenarios

    def test_both_routes_record_the_same_tree(self):
        self._check(self._scenarios())


class SpecialFileAndNameTests(_RouteComparison):
    """The same comparison for what is odd about a file rather than about where it sits.

    Attributes (the source's ``.gitattributes`` applies to the vault's index:
    line endings, ``-text``, ``ident``), a ``.git`` *file* as a submodule leaves
    in a directory, a path over 300 characters, bytes that changed under a restored
    size and time (neither route can see it: both trust git's stat cache, and
    ``--rebuild-index`` is the remedy), names that look like pathspec magic or are
    not UTF-8, and on POSIX a fifo, a socket and a file the user cannot read.
    Run on Windows and Debian by hand first (16 scenarios, no difference).
    """

    #: An unreadable file stops both routes (rc 1); nothing else here may.
    EXPECTED_FAILURES = {"a file the user cannot read": 1}

    def _scenarios(self):
        def nothing(code):
            pass

        def put(code, name, data=b"x\n"):
            path = code / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        def attributes_first(code):
            put(code, ".gitattributes", b"*.crlf text eol=crlf\n*.bin -text\n*.id ident\n*.lf text eol=lf\n")
            put(code, "a.crlf", b"one\ntwo\n")
            put(code, "b.bin", b"bin\r\nline\r\n")
            put(code, "c.id", b"$Id$\n")
            put(code, "d.lf", b"x\r\ny\r\n")

        def attributes_then(code):
            put(code, "a.crlf", b"one\ntwo\nthree\n")
            put(code, "b.bin", b"bin\r\nline\r\nmore\r\n")
            put(code, "c.id", b"$Id: stale $\n")
            put(code, "d.lf", b"x\r\ny\r\nz\r\n")
            put(code, "new.crlf", b"fresh\n")

        def restored_stamp(code):
            os.utime(code / "f2.txt", (1_000_000_000, 1_000_000_000))

        def same_stamp_other_bytes(code):
            # git compares timestamps in whole seconds (Debian's git has no nanosecond
            # support), so an edit in the same second as the run before it, with the old
            # size and mtime put back, is invisible to git -- but only sometimes, and which
            # route saw it was a coin toss that failed 2 of 15 runs (and 3 CI legs).  Wait
            # out the second: the comparison is then about the routes, deterministically.
            time.sleep(1.1)
            (code / "f2.txt").write_bytes(b"FILE 2\n" * 5)
            os.utime(code / "f2.txt", (1_000_000_000, 1_000_000_000))

        scenarios = {
            "attributes: eol, -text and ident": (attributes_first, attributes_then),
            "an attributes file that appears after the files": (
                lambda c: put(c, "e.crlf", b"a\nb\n"),
                lambda c: (put(c, ".gitattributes", b"*.crlf text eol=crlf\n"),
                           put(c, "e.crlf", b"a\nb\nc\n"))),
            "a directory with a .git file (submodule style)": (
                nothing, lambda c: (put(c, "sub/.git", b"gitdir: ../.git/modules/sub\n"),
                                    put(c, "sub/code.txt"))),
            "a tracked file becomes a directory with a .git file": (
                nothing, lambda c: (_clear(c / "f2.txt"),
                                    put(c, "f2.txt/.git", b"gitdir: ../.git/modules/f2\n"),
                                    put(c, "f2.txt/code.txt"))),
            "a path of more than 300 characters": (
                nothing, lambda c: put(c, "/".join(["d" * 40] * 7) + "/file.txt")),
            "same size and time, different bytes": (restored_stamp, same_stamp_other_bytes),
            "names that look like pathspec magic": (
                nothing, lambda c: [put(c, n) for n in ("!bang.txt", "@at.txt", "~tilde.txt", "%pct.txt")]),
            "two hundred new files at once": (
                nothing, lambda c: [put(c, f"bulk/{i % 20}/n{i}.txt", b"%d\n" % i) for i in range(200)]),
        }
        if os.name != "nt":
            def bad_utf8(code):
                with open(os.path.join(os.fsencode(code), b"bad_\xff.txt"), "wb") as fh:
                    fh.write(b"x\n")

            def socket_file(code):
                import socket
                sock = socket.socket(socket.AF_UNIX)
                try:
                    sock.bind(str(code / "sock"))
                finally:
                    sock.close()

            if _holds_non_utf8_names():          # APFS refuses one: the macOS CI legs errored
                scenarios["a name that is not UTF-8"] = (nothing, bad_utf8)
            scenarios.update({
                "a fifo": (nothing, lambda c: os.mkfifo(c / "pipe")),
                "a unix socket": (nothing, socket_file),
                "names with pathspec magic that only POSIX allows": (
                    nothing, lambda c: [put(c, n) for n in (":(exclude)x.txt", ":!y.txt", ":/top.txt", "a:b.txt")]),
            })
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                scenarios["a file the user cannot read"] = (
                    nothing, lambda c: (put(c, "secret.txt"), os.chmod(c / "secret.txt", 0)))
        return scenarios

    def test_both_routes_record_the_same_tree(self):
        self._check(self._scenarios())


class ChangedWhileReadTests(_SnapshotFixture):
    """A file that changed under ``git add`` is tried again before the run is lost.

    Reported on a 222,000-file tree: ``short read while indexing`` on a scratch
    file 22 minutes in, and the whole pass thrown away.
    """

    SHORT = ("git add -A -f . failed (rc=128)\nerror: short read while indexing "
             ".scratch/extend/lane_B.last\nfatal: updating files failed")

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(snap, "_READ_RETRY_PAUSE_SECONDS", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _flaky(self, failures: int, message: str):
        calls: list[int] = []

        def run():
            calls.append(1)
            if len(calls) <= failures:
                raise snap.SnapshotError(message)
            return "done"

        return run, calls

    def test_a_file_that_settles_lets_the_run_continue(self):
        run, calls = self._flaky(2, self.SHORT)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(snap.add_retrying(run), "done")
        self.assertEqual(len(calls), 3)
        self.assertIn("staging: .scratch/extend/lane_B.last changed while git was reading it; "
                      "trying again in 0 s (1 of 2)", err.getvalue())
        self.assertIn("(2 of 2)", err.getvalue())

    def test_a_file_that_keeps_changing_fails_with_gits_message(self):
        run, calls = self._flaky(99, self.SHORT)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(snap.SnapshotError) as caught:
                snap.add_retrying(run)
        self.assertEqual(len(calls), 3)
        self.assertIn("short read while indexing", str(caught.exception))

    def test_the_unnamed_variant_is_retried_too(self):
        run, calls = self._flaky(1, "git add failed\nfatal: unstable object source data for x")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(snap.add_retrying(run), "done")
        self.assertIn("staging: a file changed while git was reading it", err.getvalue())

    def test_any_other_failure_is_not_retried(self):
        run, calls = self._flaky(99, "git add failed\nfatal: Unable to create index.lock")
        with self.assertRaises(snap.SnapshotError):
            snap.add_retrying(run)
        self.assertEqual(len(calls), 1)

    def test_a_run_survives_one_short_read_end_to_end(self):
        real = snap.git
        seen: list[int] = []

        def spy(git_dir, work_tree, args, **kw):
            if args[:2] == ["add", "-A"]:
                seen.append(1)
                if len(seen) == 1:
                    raise snap.SnapshotError(self.SHORT)
            return real(git_dir, work_tree, args, **kw)

        with unittest.mock.patch.object(snap, "git", spy):
            rc, _, err = self.snapshot()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(seen), 2)
        self.assertIn("trying again", err)
        self.assertIn("README.md", self.tree())


class SilentInitFailureTests(unittest.TestCase):
    """``git init`` that exits 1 and says nothing is tried once more (ROADMAP W7).

    Seen once in a full test run on Windows: ``git init failed in <vault> (rc=1)``
    with no stderr, and the same test passed alone and on the next run.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-init-"))
        self.addCleanup(remove_tree, self.tmp)
        patcher = unittest.mock.patch.object(snap, "_INIT_RETRY_SECONDS", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fail_first(self, count, stderr=""):
        real = snap._run
        calls = []

        def run(argv, **kw):
            if argv[:2] == ["git", "init"]:
                calls.append(1)
                if len(calls) <= count:
                    return subprocess.CompletedProcess(argv, 1, "", stderr)
            return real(argv, **kw)

        return run, calls

    def test_a_silent_failure_is_tried_again_and_the_run_goes_on(self):
        run, calls = self._fail_first(1)
        with unittest.mock.patch.object(snap, "_run", run):
            git_dir = snap.ensure_vault(self.tmp / "vault", BRANCH)
        self.assertTrue(git_dir.is_dir())
        self.assertEqual(len(calls), 2)

    def test_two_silent_failures_say_that_git_printed_nothing(self):
        run, calls = self._fail_first(2)
        with unittest.mock.patch.object(snap, "_run", run):
            with self.assertRaises(snap.SnapshotError) as caught:
                snap.ensure_vault(self.tmp / "vault", BRANCH)
        self.assertEqual(len(calls), 2)
        self.assertIn("git printed nothing", str(caught.exception))

    def test_a_failure_with_a_message_is_not_retried(self):
        run, calls = self._fail_first(5, stderr="fatal: cannot mkdir .git: Permission denied")
        with unittest.mock.patch.object(snap, "_run", run):
            with self.assertRaises(snap.SnapshotError) as caught:
                snap.ensure_vault(self.tmp / "vault", BRANCH)
        self.assertEqual(len(calls), 1)
        self.assertIn("Permission denied", str(caught.exception))


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
            subprocess.run(["git", "-C", str(vault), "config", "snapshot.profilebytes", str(6 * gib)], check=True)
            src = self._tree(td)
            forced = {"total": 6.0 * gib, "payload": 6.0 * gib, "sampled": 1e6, "ratio": 0.99}
            cfg = snap._read_local_config_map(vault)
            with unittest.mock.patch.object(snap, "sample_payload", return_value=forced), \
                    contextlib.redirect_stdout(io.StringIO()):
                snap.choose_profile(vault, git_dir, src, [], cfg, write=True)             # not doubled: final
                self.assertEqual(snap._config(vault, "core.compression"), "")
                snap.choose_profile(vault, git_dir, src, [], cfg, write=True, recheck=True)
            self.assertEqual(snap._config(vault, "core.compression"), "0")
            self.assertEqual(snap._config(vault, "snapshot.profile"), "raw")

    def test_a_grown_default_vault_goes_raw_before_the_run_stages(self):
        # The regression: the doubling test used to run after staging, so the run
        # that grew the tree deflated all of it and only the next run was fast.
        gib = 1024 ** 3
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            subprocess.run(["git", "-C", str(vault), "config", "snapshot.profile", "default"], check=True)
            subprocess.run(["git", "-C", str(vault), "config", "snapshot.profilebytes", str(3 * gib)], check=True)
            src = self._tree(td)
            forced = {"total": 6.0 * gib, "payload": 6.0 * gib, "sampled": 1e6, "ratio": 0.99}
            cfg = snap._read_local_config_map(vault)
            with unittest.mock.patch.object(snap, "_walk_sizes", return_value=[(6 * gib, "big.bin")]), \
                    unittest.mock.patch.object(snap, "sample_payload", return_value=forced), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                snap.choose_profile(vault, git_dir, src, [], cfg, write=True)
            self.assertIn("large raw payload detected", out.getvalue())
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

    def _heartbeat_with(self, expect_bytes: int) -> str:
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
                    expect_bytes=expect_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            return err.getvalue()

    def test_the_staging_heartbeat_says_how_much_is_expected_and_how_long_is_left(self):
        text = self._heartbeat_with(30_000_000)
        self.assertIn("MiB written to objects; up to about 28.6 MiB expected, at most ", text)
        self.assertIn(" left)", text)

    def test_the_heartbeat_drops_an_expectation_the_step_has_passed_or_never_had(self):
        for expect in (0, 1_000_000):  # none, and already exceeded by the 3 MB written
            with self.subTest(expect=expect):
                text = self._heartbeat_with(expect)
                self.assertIn("MiB written to objects)", text)
                self.assertNotIn("expected", text)

    def test_check_free_space_hands_back_the_estimate_for_the_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            vault = pathlib.Path(td) / "v"
            git_dir = snap.ensure_vault(vault, "main")
            src = pathlib.Path(td) / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x" * 4096)
            need = snap.check_free_space(git_dir, vault, src, [], {})
        self.assertIsInstance(need, int)
        self.assertGreater(need, 0)

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
                self.assertIn("is not a positive number", err.getvalue())

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

    @unittest.skipUnless(_holds_non_utf8_names(), "this filesystem cannot hold a non-UTF-8 name")
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


class StatusTests(_SnapshotFixture):
    """``--status`` answers "is my backup current?" without staging anything.

    The comparison is deliberately stat-based and says so; these tests pin the
    answer it gives from the records a snapshot writes, and the shapes of the
    report a scheduler (or a person) reads.
    """

    def status(self, *extra: object) -> tuple[int, str, str]:
        return self.run_tool(
            "--source", self.code, "--vault", self.vault, "--status", *extra
        )

    def test_a_source_with_no_vault_reports_that_and_the_settings(self):
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("nothing has been snapshotted", out)
        # The tool prints the resolved path: /private/var on macOS, the long
        # form of an 8.3 short name (RUNNER~1) on a Windows runner.
        self.assertIn(f"source        : {self.code.resolve()}", out)
        self.assertIn("payload mode  : off", out)

    def test_a_fresh_snapshot_reports_zero_changes(self):
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("last snapshot :", out)
        self.assertIn("changed since: 0 file(s), 0 new, 0 deleted", out)
        self.assertIn("size-and-mtime comparison", out, "the heuristic must be named in the report")
        self.assertIn("lock          : free", out)

    def test_an_unreadable_directory_is_named_not_counted_as_deleted(self):
        """A real run stops on a directory git cannot open; the status that
        forecasts it must not call the files under it deleted and say nothing.
        Fed git's own warning so it runs on every platform."""
        (self.code / "locked").mkdir()
        (self.code / "locked" / "b.txt").write_bytes(b"b\n")
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        real_git = snap.git

        def git_with_a_warning(git_dir, work_tree, args, **kw):
            proc = real_git(git_dir, work_tree, args, **kw)
            if args[:1] == ["ls-files"] and "-o" in args and "-z" in args:
                kept = "\0".join(
                    p for p in proc.stdout.split("\0") if p and not p.startswith("locked/")
                ) + "\0"
                return subprocess.CompletedProcess(
                    proc.args, proc.returncode, kept,
                    "warning: could not open directory 'locked/': Permission denied\n",
                )
            return proc

        with unittest.mock.patch.object(snap, "git", git_with_a_warning):
            rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("0 deleted", out)
        self.assertNotIn("deleted: locked", out)
        self.assertIn("unreadable: locked/", out)
        self.assertIn("could not be read", out)
        self.assertIn("a snapshot run stops", out)

    def test_paths_no_run_would_capture_are_not_reported_as_new(self):
        """A run filters out probe debris, the reserved directory and anything
        behind a Windows junction; a status that listed them as new would never
        reach zero.  The junction predicate is injected so this runs everywhere."""
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        (self.code / ".grs-clock-probe-1-2").write_bytes(b"x")
        (self.code / ".seafile-payload").mkdir()
        (self.code / ".seafile-payload" / "README.txt").write_bytes(b"x")
        (self.code / "link").mkdir()
        (self.code / "link" / "deep.txt").write_bytes(b"x")

        def behind(path: str, self_too: bool = False) -> bool:
            return path.startswith("link/") or (self_too and path == "link")

        with unittest.mock.patch.object(snap, "_status_junction_filter", lambda _src: behind):
            rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("changed since: 0 file(s), 0 new, 0 deleted", out)
        # Without the junction predicate only that one path is a real "new" file.
        rc, out, err = self.status()
        self.assertIn("1 new", out)
        self.assertIn("new: link/deep.txt", out)

    def test_a_rewrite_that_keeps_the_mtime_but_not_the_size_is_seen(self):
        """The record held an mtime only, so a copy tool that preserves timestamps (or
        a deliberate utime) hid a rewrite of a different length: the report said
        nothing had changed and the next run committed it.  Three reviews found it."""
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        record = json.loads(_git(self.vault, "show", f"{BRANCH}:.seafile-payload/mtimes.json").stdout)
        self.assertEqual(record["sizes"]["README.md"], len((self.code / "README.md").read_bytes()))
        self.assertEqual(set(record["sizes"]) - set(record["files"]), set())
        before = (self.code / "README.md").stat()
        (self.code / "README.md").write_bytes(b"a much longer line than before, same mtime\n")
        os.utime(self.code / "README.md", ns=(before.st_atime_ns, before.st_mtime_ns))
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("changed since: 1 file(s), 0 new, 0 deleted", out)
        self.assertIn("changed: README.md", out)

    def test_a_snapshot_recorded_before_sizes_existed_is_compared_by_mtime_alone(self):
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        before = (self.code / "README.md").stat()
        (self.code / "README.md").write_bytes(b"a much longer line than before, same mtime\n")
        os.utime(self.code / "README.md", ns=(before.st_atime_ns, before.st_mtime_ns))
        with unittest.mock.patch.object(snap, "_recorded_sizes", return_value={}):
            rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("changed since: 0 file(s), 0 new, 0 deleted", out)
        self.assertIn("mtime alone for an older snapshot", out)

    def test_status_says_whether_the_newest_snapshot_left_this_vault(self):
        """After a ``--no-push`` run the local tip is a snapshot the remote never
        received, and ``--status`` printed its time and "0 changed" as if all were
        well.  The vault records the tip it last pushed; the report compares."""
        self.assertEqual(self.snapshot("--remote", str(self.remote), "--record-mtimes")[0], 0)
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("pushed        : yes", out)

        (self.code / "README.md").write_bytes(b"changed and not pushed\n")
        self.assertEqual(self.snapshot("--no-push")[0], 0)
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("pushed        : NO -- 1 snapshot(s) were taken after the last push", out)
        doc = json.loads(self.status("--json")[1])["result"]["status"]
        self.assertIs(doc["pushed"], False)
        self.assertEqual(doc["unpushed"], 1)

        # the next real run pushes it, and the report follows
        self.assertEqual(self.snapshot("--remote", str(self.remote))[0], 0)
        rc, out, err = self.status()
        self.assertIn("pushed        : yes", out)
        doc = json.loads(self.status("--json")[1])["result"]["status"]
        self.assertIs(doc["pushed"], True)
        self.assertEqual(doc["unpushed"], 0)

    def test_a_vault_that_never_recorded_a_push_says_so_instead_of_guessing(self):
        self.assertEqual(self.snapshot("--remote", str(self.remote))[0], 0)
        _git(self.vault, "config", "--unset", "snapshot.pushed")
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("pushed        : not recorded", out)
        doc = json.loads(self.status("--json")[1])["result"]["status"]
        self.assertIsNone(doc["pushed"])

    def test_a_vault_with_no_remote_prints_no_pushed_line(self):
        self.assertEqual(self.snapshot("--no-push")[0], 0)
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("pushed        :", out)

    def test_the_report_says_it_cannot_tell_whether_the_snapshot_was_pushed(self):
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("does not contact the remote", out)
        self.assertIn("not a look at the server", out)
        self.assertIn("for a git-stored one whose snapshot recorded sizes", out)

    def test_changes_are_named_and_counted(self):
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        (self.code / "README.md").write_bytes(b"changed\n")
        (self.code / "added.txt").write_bytes(b"new\n")
        (self.code / "app.log").unlink()
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("changed since: 1 file(s), 1 new, 1 deleted", out)
        self.assertIn("changed: README.md", out)
        self.assertIn("new: added.txt", out)
        self.assertIn("deleted: app.log", out)

    def test_a_touched_file_shows_as_changed_and_the_report_says_why(self):
        """A touch (same bytes, new time) is flagged: the heuristic errs loud.

        The run itself treats a byte-identical tree as nothing to do, so the
        report's Note names both directions of the error -- a change that keeps
        size and mtime hides, and a touch shows.
        """
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        (self.code / "README.md").write_bytes(b"hello\n")
        import os as _os
        _os.utime(self.code / "README.md", ns=(1, 1))
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("changed: README.md", out)
        self.assertIn("though the next run would find nothing new", out)

    def test_a_held_lock_is_reported(self):
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        lock = self.vault / ".git" / "seafile-snapshot.lock"
        lock.write_text(f"{socket.gethostname()} 999999 start\n", encoding="utf-8")
        rc, out, err = self.status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("lock          : held by", out)

    def test_status_is_json_too(self):
        self.assertEqual(self.snapshot("--no-push", "--record-mtimes")[0], 0)
        (self.code / "README.md").write_bytes(b"changed\n")
        rc, out, err = self.status("--json")
        self.assertEqual(rc, 0, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["mode"], "status")
        self.assertEqual(doc["result"]["status"]["changed"], 1)
        self.assertEqual(doc["result"]["status"]["new"], 0)

    def test_json_carries_every_key_even_when_nothing_can_be_compared(self):
        """No vault used to give ``{"snapshots": 0}`` alone, so a scheduler's
        ``.changed == 0`` read null; the counts are null, which never equals 0."""
        rc, out, err = self.status("--json")
        self.assertEqual(rc, 0, out + err)
        status = json.loads(out)["result"]["status"]
        for key in ("tip", "comparable", "changed", "new", "deleted", "unreadable", "uncompared"):
            self.assertIn(key, status)
        self.assertIs(status["comparable"], False)
        self.assertIsNone(status["changed"])
        self.assertEqual(status["snapshots"], 0)

    def test_status_refuses_to_combine_with_the_other_modes(self):
        rc, out, err = self.status("--restore")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("does not combine with", err)
        rc, out, err = self.status("--record-mtimes")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("only apply to a snapshot run", err)
        for flag in ("--no-push", "--dry-run"):
            rc, out, err = self.status(flag)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("does not combine with --no-push or --dry-run", err)


if __name__ == "__main__":
    unittest.main()
