"""AA24: the source walks enter a directory past 260 characters on every Windows.

A plain ``os.walk`` treats such a directory as unreadable when ``LongPathsEnabled`` is off, so
the files under it were left out of a snapshot without an error.  ``walk_long`` walks the
extended-length form and hands the callers plain paths.  This machine has long paths on, so the
tests cannot show the plain walk failing; they show what is under the helper's control: it finds a
file 300 characters deep, yields no ``\\\\?\\`` prefix, and is ``os.walk`` elsewhere.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402


class TestWalkLong(unittest.TestCase):
    def test_a_file_past_260_characters_is_found_and_the_paths_are_plain(self):
        base = pathlib.Path(tempfile.mkdtemp(prefix="grs-long-"))
        self.addCleanup(lambda: shutil.rmtree(snap.extended_path(base), ignore_errors=True))
        deep = base
        while len(str(deep)) < 300:
            deep = deep / ("d" * 40)
        os.makedirs(snap.extended_path(deep))
        with open(snap.extended_path(deep / "f.txt"), "w") as fh:
            fh.write("x")
        seen = []
        for dirpath, _dirs, files in snap.walk_long(base):
            self.assertFalse(dirpath.startswith("\\\\?\\"), dirpath)
            seen.extend(os.path.join(dirpath, name) for name in files)
        self.assertEqual(seen, [str(deep / "f.txt")])

    def test_the_walk_reports_a_directory_it_cannot_open(self):
        errors = []
        list(snap.walk_long(pathlib.Path(tempfile.gettempdir()) / "grs-no-such-dir", onerror=errors.append))
        self.assertEqual(len(errors), 1)

    def test_a_walk_can_prune_its_directories_in_place(self):
        base = pathlib.Path(tempfile.mkdtemp(prefix="grs-prune-"))
        self.addCleanup(shutil.rmtree, base, True)
        (base / "keep").mkdir()
        (base / "skip").mkdir()
        (base / "skip" / "x").write_text("x")
        walked = []
        for dirpath, dirs, _files in snap.walk_long(base):
            walked.append(os.path.basename(dirpath))
            dirs[:] = [d for d in dirs if d != "skip"]
        self.assertIn("keep", walked)
        self.assertNotIn("skip", walked)

    @unittest.skipIf(sys.platform == "win32", "elsewhere the helper is os.walk itself")
    def test_elsewhere_it_is_os_walk(self):
        base = pathlib.Path(tempfile.mkdtemp(prefix="grs-posix-"))
        self.addCleanup(shutil.rmtree, base, True)
        (base / "a").mkdir()
        self.assertEqual(list(snap.walk_long(base)), list(os.walk(base)))


if __name__ == "__main__":
    unittest.main()
