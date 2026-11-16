"""test_settings_report.py - ``git-remote-seafile config`` reports what is in force."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_remote_seafile.cli import _cmd_config, main
from git_remote_seafile.config import RemoteConfig
from git_remote_seafile.settings_report import SETTINGS, effective_settings, format_report


class _IsolatedGit(unittest.TestCase):
    """Git sees only the config this test writes, and no repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.global_cfg = Path(self._tmp.name) / "gitconfig"
        self.global_cfg.write_text("", encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SEAFILE_") and not k.startswith("GIT_DIR")}
        env.update(GIT_CONFIG_GLOBAL=str(self.global_cfg), GIT_CONFIG_NOSYSTEM="1")
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, cwd)

    def rows(self):
        return {s.key: (value, source) for s, value, source in effective_settings()}


class TestSettingsReport(_IsolatedGit):
    def test_the_defaults_listed_are_the_defaults_the_helper_uses(self):
        cfg = RemoteConfig.load()
        used = {
            "seafile.autogc": str(cfg.auto_gc).lower(),
            "seafile.gcthreshold": str(cfg.gc_threshold),
            "seafile.gcmaxpackmib": str(cfg.gc_max_pack_mib),
            "seafile.locktimeout": str(cfg.lock_timeout),
            "seafile.locklease": str(cfg.lock_lease),
        }
        for setting in SETTINGS:
            if setting.key in used:
                self.assertEqual(setting.default, used[setting.key], setting.key)
        self.assertTrue(all(source == "default" for _, source in self.rows().values()))

    def test_a_git_config_value_and_its_source_are_shown(self):
        self.global_cfg.write_text(
            "[seafile]\n\tautogc = false\n\tgittimeout = 900\n", encoding="utf-8"
        )
        rows = self.rows()
        self.assertEqual(rows["seafile.autogc"], ("false", "git config"))
        self.assertEqual(rows["seafile.gittimeout"], ("900", "global/system git config"))

    def test_the_environment_wins_and_is_named(self):
        self.global_cfg.write_text("[seafile]\n\tgittimeout = 900\n", encoding="utf-8")
        with patch.dict(os.environ, {"SEAFILE_GIT_TIMEOUT": "1800"}):
            value, source = self.rows()["seafile.gittimeout"]
        self.assertEqual((value, source), ("1800", "environment (SEAFILE_GIT_TIMEOUT)"))

    def test_an_environment_value_the_helper_ignores_is_not_shown_as_in_force(self):
        """fable-B10: ``SEAFILE_GIT_TIMEOUT=abc`` was printed as the value in force."""
        with patch.dict(os.environ, {"SEAFILE_GIT_TIMEOUT": "abc", "SEAFILE_FORCE_FILE_HOST": "maybe"}):
            rows = self.rows()
        value, source = rows["seafile.gittimeout"]
        self.assertEqual(value, "300")
        self.assertIn("SEAFILE_GIT_TIMEOUT is set but not a whole number, so it is ignored", source)
        value, source = rows["seafile.forcefilehost"]
        self.assertEqual(value, "false")
        self.assertIn("not true or false", source)

    def test_an_implausible_value_shows_what_the_helper_falls_back_to(self):
        self.global_cfg.write_text("[seafile]\n\tlocklease = -5\n", encoding="utf-8")
        with patch("sys.stderr", io.StringIO()):
            value, source = self.rows()["seafile.locklease"]
        self.assertEqual((value, source), ("60", "git config"))

    def test_the_report_tells_you_how_to_change_one(self):
        text = format_report(effective_settings())
        self.assertIn("Hint: change one with", text)
        self.assertIn("seafile.gcmaxpackmib", text)

    def test_a_cap_a_remote_learned_is_listed(self):
        """fable-G8.1 (review 134c8af): the report showed only the repository-wide key, so a push
        that split because of a learned per-remote cap looked unexplained."""
        from git_remote_seafile.settings_report import format_learned_caps, learned_pack_caps

        self.assertEqual(format_learned_caps([]), "")
        self.assertIn("remote.origin.seafilegcmaxpackmib  512", format_learned_caps([("origin", "512")]))
        fake = (b"remote.origin.seafilegcmaxpackmib 512" + bytes([10]) + b"remote.team/a.b.seafilegcmaxpackmib 100" + bytes([10]), b"", 0)
        with patch("git_remote_seafile.settings_report.run_git", return_value=fake):
            self.assertEqual(learned_pack_caps(), [("origin", "512"), ("team/a.b", "100")])
        with patch("git_remote_seafile.settings_report.run_git", return_value=(b"", b"", 1)):
            self.assertEqual(learned_pack_caps(), [])

    def test_the_command_prints_the_report_and_refuses_an_operand(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(_cmd_config([]), 0)
        self.assertIn("seafile.autogc", out.getvalue())
        with patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(_cmd_config(["seafile://x"]), 2)
        self.assertIn("Usage: git-remote-seafile config", err.getvalue())

    def test_it_is_a_listed_subcommand(self):
        out = io.StringIO()
        with patch.object(sys, "argv", ["git-remote-seafile", "config"]), patch("sys.stdout", out):
            self.assertEqual(main(), 0)
        self.assertIn("seafile.locktimeout", out.getvalue())


if __name__ == "__main__":
    unittest.main()
