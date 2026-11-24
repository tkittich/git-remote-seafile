"""``restore --only PATH``: restore part of a snapshot, byte for byte (RESTORE_ONLY.md).

Each test asserts what was written and what was not, never just the exit code.  The fixture is the payload
fixture (a stub server, a small git-stored file, two payload files) plus a few git-stored names that a
glob or a prefix match would get wrong.
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT / "tests"))

import seafile_snapshot as snap  # noqa: E402
from test_snapshot_payload import _PayloadFixture, _git  # noqa: E402

WHOLE_SECOND_NS = 1_600_000_000 * 1_000_000_000


class _OnlyFixture(_PayloadFixture):
    def setUp(self):
        super().setUp()
        for name, data in (
            ("docs/a.txt", b"docs a\n"), ("docs/b.txt", b"docs b\n"), ("raw", b"top raw\n"),
            ("deep/x/raw", b"deep raw\n"), ("report[1].txt", b"bracket\n"), ("report1.txt", b"plain\n"),
        ):
            path = self.src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        os.utime(self.src / "small.txt", ns=(WHOLE_SECOND_NS, WHOLE_SECOND_NS))
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)

    def restore_only(self, name: str, *only: str, extra: tuple[str, ...] = ()):
        into = self.case / name
        argv: list[str] = ["restore", "--remote", self.remote, "--into", into]
        for value in only:
            argv += ["--only", value]
        rc, out, err = self.run_tool(*argv, *extra)
        return rc, out + err, into

    @staticmethod
    def written(into: pathlib.Path) -> list[str]:
        found = []
        for path in sorted(into.rglob("*")):
            relative = path.relative_to(into)
            if path.is_file() and relative.parts[0] != ".git":
                found.append(relative.as_posix())
        return found


class SelectionTests(_OnlyFixture):
    def test_a_file_restores_alone_and_the_clone_stays_consistent(self):
        rc, text, into = self.restore_only("one", "small.txt")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.written(into), ["small.txt"])
        self.assertEqual((into / "small.txt").read_bytes(), b"small\n")
        self.assertEqual(_git(into, "status", "--porcelain"), "")
        (into / "small.txt").write_bytes(b"edited\n")
        _git(into, "checkout", "--", "small.txt")
        self.assertEqual((into / "small.txt").read_bytes(), b"small\n")
        before = _git(into, "ls-tree", "-r", "HEAD", "--name-only")
        _git(into, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-qm", "x")
        self.assertEqual(_git(into, "ls-tree", "-r", "HEAD", "--name-only"), before)
        self.assertIn("docs/a.txt", before)

    def test_a_folder_restores_everything_under_it_and_nothing_else(self):
        rc, text, into = self.restore_only("folder", "docs/")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.written(into), ["docs/a.txt", "docs/b.txt"])

    def test_several_values_add_up(self):
        rc, text, into = self.restore_only("several", "small.txt", "docs/b.txt")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.written(into), ["docs/b.txt", "small.txt"])

    def test_a_payload_only_selection_restores_its_files_and_downloads_no_other(self):
        rc, text, into = self.restore_only("payload", "sub")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.written(into), ["sub/big2.bin"])
        self.assertEqual((into / "sub" / "big2.bin").read_bytes(), self.big2)
        self.assertIn("restoring 1 of 2 payload file(s)", text)

    def test_a_path_is_literal_and_whole_component(self):
        rc, text, into = self.restore_only("literal", "raw")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.written(into), ["raw"])
        rc, text, into = self.restore_only("bracket", "report[1].txt")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.written(into), ["report[1].txt"])
        rc, text, into = self.restore_only("prefix", "doc")
        self.assertNotEqual(rc, 0)
        self.assertIn("--only 'doc'", text)

    def test_the_recorded_modification_time_comes_back(self):
        rc, text, into = self.restore_only("mtime", "small.txt")
        self.assertEqual(rc, 0, text)
        self.assertEqual((into / "small.txt").stat().st_mtime_ns // 1_000_000_000, WHOLE_SECOND_NS // 1_000_000_000)


class RefusalTests(_OnlyFixture):
    def test_a_value_that_matches_nothing_is_refused_before_anything_is_written(self):
        rc, text, into = self.restore_only("missing", "small.txt", "nothing-here.txt")
        self.assertNotEqual(rc, 0)
        self.assertIn("--only 'nothing-here.txt' matches nothing", text)
        self.assertEqual(self.written(into), [])

    def test_a_near_miss_names_the_spelling_the_snapshot_has(self):
        rc, text, _ = self.restore_only("near", "SMALL.TXT")
        self.assertNotEqual(rc, 0)
        self.assertIn("small.txt", text)

    def test_a_trailing_slash_means_a_folder(self):
        rc, text, _ = self.restore_only("slash", "small.txt/")
        self.assertNotEqual(rc, 0)
        self.assertIn("is a file", text)
        self.assertIn("'small.txt'", text)

    def test_paths_that_could_leave_the_destination_are_refused(self):
        for value in ("../x", "/etc/passwd", "a/../../x", "", "/", ".", ".git", ".git/config",
                      ".seafile-payload", ".seafile-payload/manifest.json", "C:/x", "\\\\?\\C:\\x"):
            with self.subTest(value=value):
                rc, text, into = self.restore_only("bad", value)
                self.assertEqual(rc, snap.EXIT_USAGE, text)
                self.assertIn("--only", text)
                self.assertFalse(into.exists())

    def test_only_belongs_to_restore(self):
        rc, out, err = self.run_tool(self.src, "--only", "x", "--no-push")
        self.assertEqual(rc, snap.EXIT_USAGE)
        self.assertIn("--only does not apply to snapshot", out + err)
        rc, out, err = self.run_tool("drill", "--remote", self.remote, "--only", "x")
        self.assertEqual(rc, snap.EXIT_USAGE)
        self.assertIn("--only does not apply to drill", out + err)


class MessageTests(_OnlyFixture):
    def test_the_proof_says_it_covers_the_selection_only(self):
        rc, text, into = self.restore_only("proof", "docs")
        self.assertEqual(rc, 0, text)
        self.assertIn("verified byte-exact for the 2 selected file(s)", text)
        self.assertNotIn("verified byte-exact against", text)


if __name__ == "__main__":
    unittest.main()
