"""test_config.py - Unit tests for configuration resolution."""

from __future__ import annotations

import io
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
            self.assertEqual(cfg.lock_settle, 1)
            self.assertFalse(cfg.auto_gc)
            self.assertEqual(cfg.gc_threshold, 20)

    def test_remote_config_load_custom(self):
        def mock_get(key: str, default: str | None = None) -> str | None:
            mapping = {
                "seafile.locktimeout": "30",
                "seafile.locklease": "120",
                "seafile.locksettle": "0",
                "seafile.autogc": "true",
                "seafile.gcthreshold": "50",
            }
            return mapping.get(key, default)

        with patch("git_remote_seafile.git_util.get_git_config", side_effect=mock_get):
            cfg = RemoteConfig.load()
            self.assertEqual(cfg.lock_timeout, 30)
            self.assertEqual(cfg.lock_lease, 120)
            self.assertEqual(cfg.lock_settle, 0)
            self.assertTrue(cfg.auto_gc)
            self.assertEqual(cfg.gc_threshold, 50)

    def test_a_zero_settlement_window_is_honoured(self):
        """0 switches the re-scan off, which is a legitimate choice.

        The settlement window is a race guard, not the lock itself: with one
        writer, or on a server whose listings are strongly consistent, the
        extra second per acquisition buys nothing.  Only a *negative* window
        -- one that would elapse before it started -- is refused.
        """
        def mock_get(key: str, default: str | None = None) -> str | None:
            return {"seafile.locksettle": "0"}.get(key, default)

        with patch("git_remote_seafile.git_util.get_git_config", side_effect=mock_get):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                cfg = RemoteConfig.load()

        self.assertEqual(cfg.lock_settle, 0)
        self.assertEqual(mock_err.getvalue(), "")

    def test_implausible_config_falls_back_to_documented_defaults(self):
        """A zero lease would expire every lock immediately; refuse to honor it.

        Values that defeat the mechanism outright fall back to the documented
        defaults with a warning instead of being used as-is.
        """
        def mock_get(key: str, default: str | None = None) -> str | None:
            mapping = {
                "seafile.locktimeout": "-5",
                "seafile.locklease": "0",
                "seafile.locksettle": "-2",
                "seafile.gcthreshold": "0",
            }
            return mapping.get(key, default)

        with patch("git_remote_seafile.git_util.get_git_config", side_effect=mock_get):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                cfg = RemoteConfig.load()

        self.assertEqual(cfg.lock_timeout, 15)
        self.assertEqual(cfg.lock_lease, 60)
        self.assertEqual(cfg.lock_settle, 1)
        self.assertEqual(cfg.gc_threshold, 20)
        self.assertIn("seafile.locklease=0 -> 60", mock_err.getvalue())
        self.assertIn("seafile.locksettle=-2 -> 1", mock_err.getvalue())
        self.assertIn("seafile.gcthreshold=0 -> 20", mock_err.getvalue())

    def test_get_git_config_helpers(self):
        with patch("git_remote_seafile.git_util.run_git", return_value=(b"hello\n", b"", 0)):
            self.assertEqual(get_git_config("test.str"), "hello")

        with patch("git_remote_seafile.git_util.run_git", return_value=(b"yes\n", b"", 0)):
            self.assertTrue(get_git_config_bool("test.bool"))

        with patch("git_remote_seafile.git_util.run_git", return_value=(b"42\n", b"", 0)):
            self.assertEqual(get_git_config_int("test.int", 0), 42)


if __name__ == "__main__":
    unittest.main()
