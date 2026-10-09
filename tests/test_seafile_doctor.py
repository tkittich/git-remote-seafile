"""Guards for the Seafile sync-client doctor.

``tools/seafile_doctor.py`` reads the *desktop client's* own state -- ``repo.db``,
``sync_error.db``, ``seafile.log``, the ``id`` file -- to answer questions no
``git`` command can.  The project's README calls keeping a repository inside a
synced library "**Fatal failure**" and describes the resulting ``(SFConflict ...)``
storms; this is the tool that shows someone already in that state what happened.

It was written untested and kept out of the repository on the grounds that
shipping it untested would be worse than not shipping it.  These tests are what
makes shipping it a fair trade.

Nothing here reads a real client.  Each case builds a miniature ``ccnet``
directory -- a ``seafile.ini`` pointing at a ``seafile-data`` directory, a
``repo.db`` with the two tables the tool queries, a worktree, and a log -- and
then drives the real command functions against it.  The schemas are the ones the
tool itself documents; if the client changes them, these fixtures are where the
assumption is written down.

The commands are called directly rather than through ``main()``: ``main()``
resolves ``ccnet_dir()`` from ``Path.home()``, and the commands already take the
directory as an argument, so there is nothing to patch.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import seafile_doctor as doc  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    """The attributes every command reaches for, plus any overrides."""
    base = {"days": 10, "limit": 10, "path": None}
    base.update(overrides)
    return argparse.Namespace(**base)


class DoctorFixture(unittest.TestCase):
    """Builds a throwaway client state directory and runs commands against it."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-doctor-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ccnet = self.tmp / "ccnet"
        self.ccnet.mkdir()
        self.data = self.tmp / "seafile-data"
        self.data.mkdir()
        # seafile.ini is one line: where seafile-data lives.
        (self.ccnet / "seafile.ini").write_text(str(self.data), encoding="utf-8")

    # -- building the state ---------------------------------------------

    def _repo_db(self, libs=(), errors=()):
        con = sqlite3.connect(self.data / "repo.db")
        con.execute("create table RepoProperty (repo_id text, key text, value text)")
        con.execute(
            "create table FileSyncError "
            "(repo_name text, path text, err_id integer, timestamp integer)"
        )
        for repo_id, props in libs:
            for key, value in props.items():
                con.execute("insert into RepoProperty values (?,?,?)", (repo_id, key, value))
        con.executemany("insert into FileSyncError values (?,?,?,?)", errors)
        con.commit()
        con.close()

    def _library(self, name: str, repo_id: str = "r1", user: str = "a@b"):
        """A synced library whose worktree exists on disk."""
        worktree = self.tmp / name
        worktree.mkdir(exist_ok=True)
        return (repo_id, {"worktree": str(worktree), "username": user,
                          "server-url": "https://seafile.example"}), worktree

    def _log(self, lines):
        logs = self.ccnet / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "seafile.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- running ---------------------------------------------------------

    def _run(self, fn, **overrides) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(_args(**overrides), self.ccnet)
        self.rc = rc
        return buf.getvalue()


class TestErrorCodeTable(unittest.TestCase):
    """The ids are the tool's whole value: a wrong one is a wrong diagnosis."""

    def test_the_table_matches_the_client_header(self):
        # haiwen/seafile include/seafile-error.h -- spot-checked at the ids that
        # change what someone would do about the error.
        self.assertEqual(doc.err_name(27), "CONFLICT")
        self.assertEqual(doc.err_name(12), "QUOTA_FULL")
        self.assertEqual(doc.err_name(22), "LOCAL_DATA_CORRUPT")
        self.assertEqual(doc.err_name(4), "INDEX_ERROR")

    def test_ids_are_contiguous_and_known(self):
        ids = sorted(doc.SYNC_ERRORS)
        self.assertEqual(ids, list(range(len(ids))), "the id table has a gap")

    def test_an_unknown_id_is_named_rather_than_guessed(self):
        self.assertEqual(doc.err_name(999), "UNKNOWN_999")
        self.assertIn("unrecognised", doc.err_text(999))


class TestLibs(DoctorFixture):
    def test_it_lists_synced_libraries_with_their_worktrees(self):
        lib, worktree = self._library("Documents", repo_id="abc123")
        self._repo_db(libs=[lib])

        out = self._run(doc.cmd_libs)

        self.assertIn("Documents", out)
        self.assertIn(str(worktree), out)
        self.assertIn("abc123", out)

    def test_a_repository_without_a_worktree_is_not_a_library(self):
        # RepoProperty rows exist for things that are not checked out.
        self._repo_db(libs=[("r9", {"server-url": "https://seafile.example"})])

        out = self._run(doc.cmd_libs)

        self.assertIn("no synced libraries", out)

    def test_a_missing_worktree_is_flagged(self):
        lib, worktree = self._library("Gone")
        self._repo_db(libs=[lib])
        worktree.rmdir()

        out = self._run(doc.cmd_libs)

        self.assertIn("MISSING", out)

    def test_a_missing_repo_db_does_not_crash(self):
        out = self._run(doc.cmd_libs)
        self.assertIn("no synced libraries", out)


class TestWhere(DoctorFixture):
    def test_a_path_inside_a_library_is_placed(self):
        lib, worktree = self._library("Documents")
        self._repo_db(libs=[lib])
        target = worktree / "code" / "app.py"
        target.parent.mkdir()
        target.write_text("x", encoding="utf-8")

        out = self._run(doc.cmd_where, path=str(target))

        self.assertIn("INSIDE SYNCED LIBRARY", out)
        self.assertIn("code/app.py", out)

    def test_a_path_outside_every_library_says_so(self):
        lib, _ = self._library("Documents")
        self._repo_db(libs=[lib])

        out = self._run(doc.cmd_where, path=str(self.tmp / "elsewhere"))

        self.assertIn("not inside any synced library", out)


class TestErrors(DoctorFixture):
    def _two_errors(self):
        now = int(dt.datetime.now().timestamp())
        return [
            ("Documents", "a/b.txt", 27, now),
            ("Documents", "c/d.txt", 27, now - 100),
            ("Documents", "<library level>", 12, now - 200),
        ]

    def test_it_groups_by_decoded_error_and_counts(self):
        self._repo_db(errors=self._two_errors())

        out = self._run(doc.cmd_errors)

        self.assertIn("3 sync errors", out)
        self.assertIn("CONFLICT", out)
        self.assertIn("QUOTA_FULL", out)
        self.assertIn("(2 files)", out)

    def test_it_names_the_files_and_the_library_level_row(self):
        self._repo_db(errors=self._two_errors())

        out = self._run(doc.cmd_errors)

        self.assertIn("a/b.txt", out)
        self.assertIn("<library level>", out)

    def test_a_row_older_than_the_window_is_dropped(self):
        old = int(dt.datetime.now().timestamp()) - 40 * 86400
        self._repo_db(errors=[("Documents", "old.txt", 27, old)])

        out = self._run(doc.cmd_errors, days=1)

        self.assertIn("0 shown", out)
        self.assertNotIn("old.txt", out)

    def test_a_missing_repo_db_is_reported(self):
        out = self._run(doc.cmd_errors)
        self.assertEqual(self.rc, 1)
        self.assertIn("not readable", out)


class TestConflicts(DoctorFixture):
    def test_it_finds_conflict_copies_and_buckets_them_by_month(self):
        lib, worktree = self._library("Documents")
        self._repo_db(libs=[lib])
        (worktree / "app (SFConflict me@host 2026-03-04-05-06-07).py").write_text(
            "x", encoding="utf-8"
        )
        (worktree / "notes (SFConflict me@host 2026-03-09-01-02-03).txt").write_text(
            "x", encoding="utf-8"
        )

        out = self._run(doc.cmd_conflicts)

        self.assertIn("total conflict copies: 2", out)
        self.assertIn("2026-03  x2", out)

    def test_a_conflict_inside_dot_git_is_ignored(self):
        lib, worktree = self._library("Documents")
        self._repo_db(libs=[lib])
        git = worktree / ".git"
        git.mkdir()
        (git / "index (SFConflict me@host 2026-03-04-05-06-07)").write_text(
            "x", encoding="utf-8"
        )

        out = self._run(doc.cmd_conflicts)

        self.assertIn("total conflict copies: 0", out)

    def test_a_clean_tree_reports_nothing(self):
        lib, _ = self._library("Documents")
        self._repo_db(libs=[lib])

        out = self._run(doc.cmd_conflicts)

        self.assertIn("total conflict copies: 0", out)


class TestChurn(DoctorFixture):
    def test_it_counts_re_commit_cycles_per_day(self):
        self._repo_db()
        self._log([
            "[03/04/26 10:00:00] Removing blocks for repo Documents(abc12345-0000)",
            "[03/04/26 10:00:05] Removing blocks for repo Documents(abc12345-0000)",
            "[03/05/26 11:00:00] Removing blocks for repo Documents(abc12345-0000)",
        ])

        out = self._run(doc.cmd_churn)

        self.assertIn("03/04/26", out)
        self.assertIn("2", out)
        self.assertIn("03/05/26", out)

    def test_a_thrashing_day_is_called_out(self):
        self._repo_db()
        self._log(
            ["[03/04/26 10:00:00] Removing blocks for repo Documents(abc12345-0000)"] * 200
        )

        out = self._run(doc.cmd_churn)

        self.assertIn("THRASHING", out)

    def test_a_missing_log_is_reported_not_guessed(self):
        self._repo_db()

        out = self._run(doc.cmd_churn)

        self.assertEqual(self.rc, 1)
        self.assertIn("no seafile.log", out)


class TestIdentity(DoctorFixture):
    def test_it_reports_the_current_id(self):
        self._repo_db()
        (self.data / "id").write_text("deadbeef", encoding="utf-8")
        self._log([
            "[03/01/26 09:00:00] starting seafile client 9.0.0",
            "[03/01/26 09:00:01] client id = deadbeef",
        ])

        out = self._run(doc.cmd_identity)

        self.assertIn("deadbeef", out)
        self.assertIn("<-- current", out)

    def test_without_a_log_it_says_the_history_is_unavailable(self):
        # The id file alone answers "which identity is this machine using", but
        # not "has it ever used another" -- which is the question that matters.
        self._repo_db()
        (self.data / "id").write_text("deadbeef", encoding="utf-8")

        out = self._run(doc.cmd_identity)

        self.assertIn("deadbeef", out)
        self.assertIn("cannot build a history", out)

    def test_a_second_identity_is_warned_about(self):
        # A new identity is a NEW DEVICE to the server, which re-syncs every
        # library -- the most expensive thing a user can do by accident.
        self._repo_db()
        (self.data / "id").write_text("beef", encoding="utf-8")
        self._log([
            "[03/01/26 09:00:00] starting seafile client 9.0.0",
            "[03/01/26 09:00:01] client id = aaaa1111",
            "[03/02/26 09:00:00] starting seafile client 9.0.1",
            "[03/02/26 09:00:01] client id = beef",
        ])

        out = self._run(doc.cmd_identity)

        self.assertIn("WARNING", out)
        self.assertIn("2 identities", out)
        self.assertIn("9.0.1", out)

    def test_one_identity_is_not_warned_about(self):
        self._repo_db()
        (self.data / "id").write_text("aaaa1111", encoding="utf-8")
        self._log([
            "[03/01/26 09:00:00] starting seafile client 9.0.0",
            "[03/01/26 09:00:01] client id = aaaa1111",
        ])

        out = self._run(doc.cmd_identity)

        self.assertNotIn("WARNING", out)

    def test_a_missing_identity_file_is_flagged(self):
        self._repo_db()
        self._log(["[03/01/26 09:00:00] starting seafile client 9.0.0"])

        out = self._run(doc.cmd_identity)

        self.assertIn("MISSING", out)


class TestReport(DoctorFixture):
    def test_it_runs_every_section(self):
        lib, worktree = self._library("Documents")
        self._repo_db(
            libs=[lib],
            errors=[("Documents", "a.txt", 27, int(dt.datetime.now().timestamp()))],
        )
        (worktree / "app (SFConflict me@host 2026-03-04-05-06-07).py").write_text(
            "x", encoding="utf-8"
        )
        (self.data / "id").write_text("beef", encoding="utf-8")
        self._log(["[03/04/26 10:00:00] Removing blocks for repo Documents(abc12345-0000)"])

        out = self._run(doc.cmd_report)

        for heading in ("LIBRARIES", "MACHINE IDENTITY", "SYNC ERRORS",
                        "CONFLICT COPIES", "CHURN"):
            with self.subTest(section=heading):
                self.assertIn(heading, out)


class TestCliSurface(unittest.TestCase):
    """The subcommands and their defaults are a promise to whoever runs it."""

    def _parse(self, argv):
        import subprocess
        return subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).resolve().parent.parent
                                 / "tools" / "seafile_doctor.py"), *argv],
            capture_output=True, text=True, timeout=60,
        )

    def test_no_subcommand_prints_help_and_exits_2(self):
        proc = self._parse([])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage", (proc.stdout + proc.stderr).lower())

    def test_the_help_lists_every_subcommand(self):
        proc = self._parse(["--help"])
        self.assertEqual(proc.returncode, 0)
        for name in ("libs", "identity", "where", "errors", "conflicts", "churn", "report"):
            with self.subTest(command=name):
                self.assertIn(name, proc.stdout)


class TestDoctorReadsLiveDatabases(unittest.TestCase):
    """D22: one implementation of "read a live client db", and it closes up.

    The doctor had its own copy of the copy-then-open-read-only logic, built on
    the private ``_copy_with_sidecars``: two implementations of one rule, and a
    connection closed by hand at two of its three exit paths.  It now goes
    through the public context manager the rest of the package uses.
    """

    def _wal_db(self, td):
        db = pathlib.Path(td) / "repo.db"
        writer = sqlite3.connect(str(db))
        writer.execute("PRAGMA journal_mode=wal")
        writer.execute("CREATE TABLE Test (val TEXT)")
        writer.execute("INSERT INTO Test VALUES ('wal_entry')")
        writer.commit()
        self.assertTrue((pathlib.Path(td) / "repo.db-wal").is_file())
        return db, writer

    def test_the_public_reader_sees_rows_still_in_the_wal(self):
        with tempfile.TemporaryDirectory() as td:
            db, writer = self._wal_db(td)
            try:
                with doc.open_live_sqlite_ro(db) as con:
                    self.assertIsNotNone(con)
                    self.assertEqual(list(con.execute("SELECT val FROM Test")), [("wal_entry",)])
            finally:
                writer.close()

    def test_the_connection_is_closed_when_the_block_ends(self):
        with tempfile.TemporaryDirectory() as td:
            db, writer = self._wal_db(td)
            try:
                with doc.open_live_sqlite_ro(db) as con:
                    self.assertIsNotNone(con)
                with self.assertRaises(sqlite3.ProgrammingError):
                    con.execute("SELECT 1")
            finally:
                writer.close()

    def test_an_absent_database_yields_none(self):
        with tempfile.TemporaryDirectory() as td:
            with doc.open_live_sqlite_ro(pathlib.Path(td) / "missing.db") as con:
                self.assertIsNone(con)

    def test_the_doctor_does_not_reach_for_the_private_helper(self):
        source = (
            pathlib.Path(__file__).resolve().parent.parent / "tools" / "seafile_doctor.py"
        ).read_text(encoding="utf-8")
        self.assertIn("open_live_sqlite_ro", source)
        self.assertNotIn("_copy_with_sidecars", source)


if __name__ == "__main__":
    unittest.main()
