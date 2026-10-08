"""test_seafile_paths.py - Tests for Seafile client data and DB path discovery."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from git_remote_seafile.seafile_paths import (
    SeafileClientNotFoundError,
    ccnet_dir,
    get_candidate_db_paths,
    get_seafile_data_dirs,
    seafile_data,
)


class TestSeafilePaths(unittest.TestCase):
    def test_ccnet_dir_found(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = pathlib.Path(td)
            ccnet = fake_home / "ccnet"
            ccnet.mkdir()
            with patch("pathlib.Path.home", return_value=fake_home):
                self.assertEqual(ccnet_dir(), ccnet)

    def test_ccnet_dir_dot_found(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = pathlib.Path(td)
            ccnet = fake_home / ".ccnet"
            ccnet.mkdir()
            with patch("pathlib.Path.home", return_value=fake_home):
                self.assertEqual(ccnet_dir(), ccnet)

    def test_ccnet_dir_not_found_raises_exception(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = pathlib.Path(td)
            with patch("pathlib.Path.home", return_value=fake_home):
                with self.assertRaises(SeafileClientNotFoundError):
                    ccnet_dir()

    def test_seafile_data_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            ccnet = pathlib.Path(td) / "ccnet"
            ccnet.mkdir()
            data_dir = pathlib.Path(td) / "custom-seafile-data"
            data_dir.mkdir()
            (ccnet / "seafile.ini").write_text(str(data_dir), encoding="utf-8")

            resolved = seafile_data(ccnet)
            self.assertEqual(resolved, data_dir)

    def test_seafile_data_missing_raises_exception(self):
        with tempfile.TemporaryDirectory() as td:
            ccnet = pathlib.Path(td) / "ccnet"
            ccnet.mkdir()
            with self.assertRaises(SeafileClientNotFoundError):
                seafile_data(ccnet)

    def test_get_seafile_data_dirs_includes_ini_and_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = pathlib.Path(td)
            ccnet = fake_home / "ccnet"
            ccnet.mkdir()
            custom_data = fake_home / "my-seafile-data"
            (ccnet / "seafile.ini").write_text(str(custom_data), encoding="utf-8")

            with patch("pathlib.Path.home", return_value=fake_home):
                dirs = get_seafile_data_dirs()
                self.assertIn(custom_data, dirs)
                self.assertIn(fake_home / "ccnet", dirs)
                self.assertIn(fake_home / ".ccnet", dirs)

    def test_get_candidate_db_paths(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = pathlib.Path(td)
            with patch("pathlib.Path.home", return_value=fake_home):
                accounts_cands = get_candidate_db_paths("accounts.db")
                self.assertTrue(all(p.name == "accounts.db" for p in accounts_cands))
                self.assertIn(fake_home / "ccnet" / "accounts.db", accounts_cands)

                repo_cands = get_candidate_db_paths("repo.db")
                self.assertTrue(all(p.name == "repo.db" for p in repo_cands))
                self.assertIn(fake_home / "ccnet" / "repo.db", repo_cands)


if __name__ == "__main__":
    unittest.main()
