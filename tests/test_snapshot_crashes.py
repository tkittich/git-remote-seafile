"""test_snapshot_crashes.py - input a person can type or a disk can hold must not end in a traceback.

Each case below is a finding of the c305b0a review round (fable-S2-6, S4-9, S4-10, S4-11,
gemini-S2-3, S4-1, S4-5): the tool raised ValueError, NotADirectoryError, RuntimeError,
UnicodeEncodeError or AttributeError where it had a message to give.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402

try:
    from _helpers import remove_tree  # noqa: E402
except ImportError:  # run as ``python -m unittest tests.<module>``
    from tests._helpers import remove_tree  # noqa: E402


def run_tool(*argv: object) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = snap.main([str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


class CrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-crash-"))
        self.addCleanup(remove_tree, self.tmp)

    def test_a_drill_sample_with_a_digit_int_cannot_read_is_a_message(self):
        with self.assertRaises(snap.SnapshotError):
            snap.drill_sample("²")            # SUPERSCRIPT TWO: isdigit() but not an int

    def test_restore_into_an_existing_file_is_a_message(self):
        target = self.tmp / "afile"
        target.write_bytes(b"x")
        rc, _out, err = run_tool("--restore", "--remote", self.tmp / "no-such-remote.git", "--into", target)
        self.assertNotEqual(rc, 0)
        self.assertIn("not a directory", err)
        self.assertNotIn("Traceback", err)

    def test_an_unknown_user_in_a_path_is_a_message(self):
        def refuse(self):                 # what POSIX raises for ~name; Windows just guesses a folder
            raise RuntimeError("Could not determine home directory.")
        with unittest.mock.patch.object(pathlib.Path, "expanduser", refuse):
            rc, _out, err = run_tool("--restore", "--remote", self.tmp / "r.git", "--into", "~nosuchuser-grs/x")
        self.assertNotEqual(rc, 0)
        self.assertIn("nosuchuser-grs", err)
        self.assertNotIn("Traceback", err)

    def test_a_name_that_is_not_utf8_is_judged_on_a_volume_that_counts_bytes(self):
        def keeps(_into, name):
            return "é" * 200 not in name          # only the 200-character probe is refused
        with unittest.mock.patch.object(snap, "_volume_keeps_name", keeps):
            problem = snap.probed_name_problem(self.tmp)
        self.assertIsNotNone(problem)
        self.assertIsNone(problem("dir/" + "\udcff" * 3))    # 3 raw bytes: well under 255

    def test_a_vault_beside_a_drive_root_is_a_message(self):
        root = pathlib.Path(self.tmp.anchor)
        with self.assertRaises(snap.SnapshotError):
            snap.default_vault_for(root)

    def test_the_snapshot_id_of_a_name_that_is_not_utf8_is_computed(self):
        entry = {"path": "a\udcffb", "size": 1, "mode": "100644", "sha256": "0" * 64}
        self.assertTrue(snap.manifest_snapshot_id([entry]).startswith("p-"))

    def test_a_cache_entry_that_is_not_an_object_is_hashed_afresh(self):
        (self.tmp / "f.bin").write_bytes(b"data")
        st = (self.tmp / "f.bin").stat()
        cands = {"f.bin": (st.st_size, st.st_mtime_ns, "100644", st.st_ino, st.st_dev, st.st_ctime_ns)}
        for bad in ([1, 2], 7, "x", None):
            manifest, _fresh = snap.build_manifest(cands, self.tmp, {"f.bin": bad}, 1, quiet=True)
            self.assertEqual(len(manifest["files"]), 1)


if __name__ == "__main__":
    unittest.main()
