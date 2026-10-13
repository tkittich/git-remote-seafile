"""test_config.py - Unit tests for configuration resolution."""

from __future__ import annotations

import io
import os
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
            self.assertEqual(cfg.gc_max_mib, 2048)  # 2 x the 1024 MiB pack size cap
            self.assertEqual(cfg.gc_max_pack_mib, 1024)

    def test_remote_config_load_custom(self):
        def mock_get(key: str, default: str | None = None) -> str | None:
            mapping = {
                "seafile.locktimeout": "30",
                "seafile.locklease": "120",
                "seafile.autogc": "true",
                "seafile.gcthreshold": "50",
                "seafile.gcmaxpackmib": "256",
            }
            return mapping.get(key, default)

        with patch("git_remote_seafile.git_util.get_git_config", side_effect=mock_get):
            cfg = RemoteConfig.load()
            self.assertEqual(cfg.lock_timeout, 30)
            self.assertEqual(cfg.lock_lease, 120)
            self.assertEqual(cfg.lock_settle, 1)
            self.assertTrue(cfg.auto_gc)
            self.assertEqual(cfg.gc_threshold, 50)
            self.assertEqual(cfg.gc_max_mib, 2 * 256)  # derived, not a second key
            self.assertEqual(cfg.gc_max_pack_mib, 256)

    def test_the_settlement_window_is_not_a_git_config_setting(self):
        """It is a race guard, fixed at one second; only the test suite moves it.

        ``seafile.locksettle`` used to be a documented key whose only
        legitimate use was ``0`` in tests, and a user setting it traded a
        correctness property for a second.  A value still present is reported
        and ignored.
        """
        def mock_get(key: str, default: str | None = None) -> str | None:
            return {"seafile.locksettle": "0"}.get(key, default)

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("SEAFILE_TEST_LOCK_SETTLE", None)
            with patch("git_remote_seafile.git_util.get_git_config", side_effect=mock_get):
                cfg = RemoteConfig.load()
        self.assertEqual(cfg.lock_settle, 1)

    def test_the_test_override_moves_the_window(self):
        with patch.dict("os.environ", {"SEAFILE_TEST_LOCK_SETTLE": "0"}):
            self.assertEqual(RemoteConfig.load().lock_settle, 0)
        with patch.dict("os.environ", {"SEAFILE_TEST_LOCK_SETTLE": "not-a-number"}):
            self.assertEqual(RemoteConfig.load().lock_settle, 1)

    def test_retired_keys_are_reported_not_silently_ignored(self):
        out = b"seafile.locksettle 0\nseafile.gcmaxmib 4096\n"
        with patch("git_remote_seafile.config.run_git", return_value=(out, b"", 0)):
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                cfg = RemoteConfig.load()
        text = err.getvalue()
        self.assertIn("seafile.locksettle is no longer a setting", text)
        self.assertIn("seafile.gcmaxmib is no longer a setting", text)
        self.assertEqual(cfg.lock_settle, 1)
        self.assertEqual(cfg.gc_max_mib, 2 * cfg.gc_max_pack_mib)

    def test_implausible_config_falls_back_to_documented_defaults(self):
        """A zero lease would expire every lock immediately; refuse to honor it.

        Values that defeat the mechanism outright fall back to the documented
        defaults with a warning instead of being used as-is.
        """
        def mock_get(key: str, default: str | None = None) -> str | None:
            mapping = {
                "seafile.locktimeout": "-5",
                "seafile.locklease": "0",
                "seafile.gcthreshold": "0",
                "seafile.gcmaxpackmib": "0",
            }
            return mapping.get(key, default)

        with patch("git_remote_seafile.git_util.get_git_config", side_effect=mock_get):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                cfg = RemoteConfig.load()

        self.assertEqual(cfg.lock_timeout, 15)
        self.assertEqual(cfg.lock_lease, 60)
        self.assertEqual(cfg.lock_settle, 1)
        self.assertEqual(cfg.gc_threshold, 20)
        self.assertEqual(cfg.gc_max_mib, 2048)
        self.assertEqual(cfg.gc_max_pack_mib, 1024)
        self.assertIn("seafile.locklease=0 -> 60", mock_err.getvalue())
        self.assertIn("seafile.gcthreshold=0 -> 20", mock_err.getvalue())
        self.assertIn("seafile.gcmaxpackmib=0 -> 1024", mock_err.getvalue())

    def test_get_git_config_helpers(self):
        with patch("git_remote_seafile.git_util.run_git", return_value=(b"hello\n", b"", 0)):
            self.assertEqual(get_git_config("test.str"), "hello")

        with patch("git_remote_seafile.git_util.run_git", return_value=(b"yes\n", b"", 0)):
            self.assertTrue(get_git_config_bool("test.bool"))

        with patch("git_remote_seafile.git_util.run_git", return_value=(b"42\n", b"", 0)):
            self.assertEqual(get_git_config_int("test.int", 0), 42)


if __name__ == "__main__":
    unittest.main()
