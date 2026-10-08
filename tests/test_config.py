"""test_config.py - Unit tests for configuration resolution."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from git_remote_seafile.config import (
    RemoteConfig,
    get_git_config,
    get_git_config_bool,
    get_git_config_int,
)


class TestConfigModule(unittest.TestCase):
    """Direct tests for config.py configuration loader."""

    def test_remote_config_load_defaults(self):
        with patch("git_remote_seafile.git_util.get_git_config", return_value=None):
            cfg = RemoteConfig.load()
            self.assertEqual(cfg.lock_timeout, 15)
            self.assertEqual(cfg.lock_lease, 60)
            self.assertFalse(cfg.auto_gc)
            self.assertEqual(cfg.gc_threshold, 20)

    def test_remote_config_load_custom(self):
        def mock_get(key: str, default: str | None = None) -> str | None:
            mapping = {
                "seafile.locktimeout": "30",
                "seafile.locklease": "120",
                "seafile.autogc": "true",
                "seafile.gcthreshold": "50",
            }
            return mapping.get(key, default)

        with patch("git_remote_seafile.git_util.get_git_config", side_effect=mock_get):
            cfg = RemoteConfig.load()
            self.assertEqual(cfg.lock_timeout, 30)
            self.assertEqual(cfg.lock_lease, 120)
            self.assertTrue(cfg.auto_gc)
            self.assertEqual(cfg.gc_threshold, 50)

    def test_get_git_config_helpers(self):
        with patch("git_remote_seafile.git_util.run_git", return_value=(b"hello\n", b"", 0)):
            self.assertEqual(get_git_config("test.str"), "hello")

        with patch("git_remote_seafile.git_util.run_git", return_value=(b"yes\n", b"", 0)):
            self.assertTrue(get_git_config_bool("test.bool"))

        with patch("git_remote_seafile.git_util.run_git", return_value=(b"42\n", b"", 0)):
            self.assertEqual(get_git_config_int("test.int", 0), 42)


if __name__ == "__main__":
    unittest.main()
