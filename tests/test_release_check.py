"""tools/release_check.py answers, before the tag exists, what the release job asks after it."""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_check", _ROOT / "tools" / "release_check.py")
assert _spec is not None and _spec.loader is not None
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

REPO = "https://github.com/tkittich/git-remote-seafile"


def _git(cwd: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@x", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="grs-relcheck-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _git(self.root, "init", "-q", "-b", "main", ".")
        _write(self.root / "git_remote_seafile" / "__init__.py", '__version__ = "0.2.0"\n')
        _write(
            self.root / "CHANGELOG.md",
            "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-01-02\n\nx\n\n"
            f"[0.2.0]: {REPO}/compare/v0.1.0...v0.2.0\n",
        )
        _write(
            self.root / ".github" / "release-notes" / "v0.2.0.md",
            "# v0.2.0 - A Title\n\n## What's Changed in v0.2.0\n\nstuff\n\n---\n\n"
            f"**Full Changelog**: {REPO}/compare/v0.1.0...v0.2.0\n",
        )
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "one")
        _git(self.root, "tag", "v0.1.0")
        _git(self.root, "tag", "v0.9.0")  # numeric order: v0.9.0 is above v0.2.0

    def failed(self, results) -> dict[str, str]:
        return {name: detail for status, name, detail in results if status == "FAIL"}

    def test_a_consistent_release_passes(self):
        self.assertEqual(self.failed(rc.check(self.root)), {})

    def test_the_previous_tag_is_the_highest_below_not_the_latest(self):
        self.assertEqual(rc.previous_tag(self.root, "v0.2.0"), "v0.1.0")
        self.assertEqual(rc.previous_tag(self.root, "v0.10.0"), "v0.9.0")

    def test_a_tag_that_differs_from_the_version_fails(self):
        self.assertIn("version", self.failed(rc.check(self.root, "v0.2.1")))

    def test_an_existing_tag_fails(self):
        _git(self.root, "tag", "v0.2.0")
        self.assertIn("tag", self.failed(rc.check(self.root)))

    def test_a_compare_link_from_the_wrong_base_fails(self):
        notes = self.root / ".github" / "release-notes" / "v0.2.0.md"
        notes.write_bytes(notes.read_bytes().replace(b"v0.1.0...", b"v0.0.9..."))
        _git(self.root, "commit", "-qam", "x")
        self.assertIn("notes", self.failed(rc.check(self.root)))

    def test_a_missing_changelog_link_fails(self):
        cl = self.root / "CHANGELOG.md"
        cl.write_bytes(cl.read_bytes().replace(b"[0.2.0]: ", b"[0.2.0] "))
        _git(self.root, "commit", "-qam", "x")
        self.assertIn("changelog link", self.failed(rc.check(self.root)))

    def test_a_missing_changelog_section_fails(self):
        cl = self.root / "CHANGELOG.md"
        cl.write_bytes(cl.read_bytes().replace(b"## [0.2.0] - 2026-01-02", b"## [0.2.0]"))
        _git(self.root, "commit", "-qam", "x")
        self.assertIn("changelog", self.failed(rc.check(self.root)))

    def test_a_dirty_tree_fails(self):
        _write(self.root / "scratch.tmp", "x")
        self.assertIn("tree", self.failed(rc.check(self.root)))

    def test_missing_notes_fail(self):
        (self.root / ".github" / "release-notes" / "v0.2.0.md").unlink()
        _git(self.root, "commit", "-qam", "x")
        self.assertIn("notes", self.failed(rc.check(self.root)))


if __name__ == "__main__":
    unittest.main()
