"""test_sqlite_read.py - reading a live Seafile client database safely.

The desktop client keeps ``repo.db``/``accounts.db`` open and writes to them.
Opening those files in place risks blocking the client or reading a torn state,
so the helper copies them to a private temp directory first and opens the copy.

The copy MUST include SQLite's sidecar files.  In WAL mode -- the default for
modern SQLite -- committed rows live in ``<db>-wal`` until a checkpoint, so a
copy of the main file alone can contain no rows at all.  Measured by hand
against a populated WAL database: copying just ``repo.db`` raises
``no such table``, because the table itself is still in the log.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_remote_seafile.sqlite_read import open_live_sqlite_ro

ROWS = [("r1", "worktree", "/tmp/wt"), ("r1", "server-url", "https://s.example")]


def _make_db(path: Path, journal: str = "wal") -> sqlite3.Connection:
    """Create a populated database and return a still-open connection.

    Keeping the connection open stands in for the Seafile client holding the
    file, and leaves a WAL-mode database uncheckpointed.
    """
    con = sqlite3.connect(str(path))
    con.execute(f"PRAGMA journal_mode={journal}")
    con.execute("CREATE TABLE RepoProperty (repo_id TEXT, key TEXT, value TEXT)")
    con.executemany("INSERT INTO RepoProperty VALUES (?,?,?)", ROWS)
    con.commit()
    return con


def _read(con) -> list:
    return sorted(con.execute("SELECT repo_id, key, value FROM RepoProperty"))


class TestOpenLiveSqliteReadOnly(unittest.TestCase):
    def test_reads_wal_rows_that_live_only_in_the_write_ahead_log(self):
        """Regression: a main-file-only copy of a WAL db sees nothing."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "repo.db"
            live = _make_db(db, "wal")
            try:
                self.assertTrue(
                    (Path(td) / "repo.db-wal").is_file(),
                    "precondition: the rows must still be sitting in the WAL",
                )
                with open_live_sqlite_ro(db) as con:
                    self.assertIsNotNone(con)
                    self.assertEqual(_read(con), sorted(ROWS))
            finally:
                live.close()

    def test_reads_rollback_journal_database(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "repo.db"
            live = _make_db(db, "delete")
            try:
                with open_live_sqlite_ro(db) as con:
                    self.assertIsNotNone(con)
                    self.assertEqual(_read(con), sorted(ROWS))
            finally:
                live.close()

    def test_missing_file_yields_none(self):
        with tempfile.TemporaryDirectory() as td:
            with open_live_sqlite_ro(Path(td) / "nope.db") as con:
                self.assertIsNone(con)

    def test_directory_yields_none(self):
        with tempfile.TemporaryDirectory() as td:
            with open_live_sqlite_ro(Path(td)) as con:
                self.assertIsNone(con)

    def test_does_not_modify_the_original_database(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "repo.db"
            live = _make_db(db, "wal")
            try:
                before = hashlib.sha256(db.read_bytes()).hexdigest()
                with open_live_sqlite_ro(db) as con:
                    _read(con)
                self.assertEqual(before, hashlib.sha256(db.read_bytes()).hexdigest())
            finally:
                live.close()

    def test_reading_does_not_block_the_live_client(self):
        """The client must still be able to commit after we have read."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "repo.db"
            live = _make_db(db, "wal")
            try:
                with open_live_sqlite_ro(db) as con:
                    _read(con)
                live.execute("INSERT INTO RepoProperty VALUES ('r2','k','v')")
                live.commit()
                self.assertEqual(
                    live.execute("SELECT COUNT(*) FROM RepoProperty").fetchone()[0],
                    len(ROWS) + 1,
                )
            finally:
                live.close()

    def test_creates_no_files_beside_the_original(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "repo.db"
            live = _make_db(db, "wal")
            try:
                before = sorted(p.name for p in Path(td).iterdir())
                with open_live_sqlite_ro(db) as con:
                    _read(con)
                self.assertEqual(sorted(p.name for p in Path(td).iterdir()), before)
            finally:
                live.close()

    def test_temp_copy_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "repo.db"
            live = _make_db(db, "wal")
            try:
                scratch = Path(td) / "scratch"
                scratch.mkdir()
                with patch("tempfile.tempdir", str(scratch)):
                    # The empty-after assertion below only means something if
                    # the copy actually landed in *scratch* (tempfile.tempdir
                    # is read by gettempdir() ahead of TEMP/TMP): assert the
                    # precondition while the connection is open, or a test
                    # whose copy went elsewhere passes vacuously.
                    with open_live_sqlite_ro(db) as con:
                        _read(con)
                        copies = list(scratch.rglob("*"))
                    self.assertTrue(
                        copies, "the scratch dir saw no copy: this test proves nothing"
                    )
                self.assertEqual(
                    list(scratch.iterdir()), [],
                    "the temporary copy of the database was not cleaned up",
                )
            finally:
                live.close()


if __name__ == "__main__":
    unittest.main()
