"""The short command names are the same program as the long ones (``sfsnap`` is ``git-seafile-snapshot``)."""

from __future__ import annotations

import pathlib
import unittest

try:
    import tomllib
except ImportError:          # Python 3.10: no TOML reader in the standard library
    tomllib = None  # type: ignore[assignment]

ROOT = pathlib.Path(__file__).resolve().parent.parent


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
class EntryPointTests(unittest.TestCase):
    def scripts(self) -> "dict[str, str]":
        return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]

    def test_the_umbrella_is_an_entry_point(self):
        self.assertEqual(self.scripts()["seafile"], "git_remote_seafile.umbrella:main")

    def test_the_snapshot_short_names_run_the_same_function_as_the_long_one(self):
        scripts = self.scripts()
        target = scripts["git-seafile-snapshot"]
        self.assertEqual(target, "git_remote_seafile.snapshot.cli:main")
        self.assertEqual(scripts["sfsnap"], target)
        self.assertEqual(scripts["git-sfsnap"], target)

    def test_every_script_names_a_function_that_exists(self):
        import importlib
        for name, spec in self.scripts().items():
            module, _, func = spec.partition(":")
            with self.subTest(script=name):
                self.assertTrue(callable(getattr(importlib.import_module(module), func)))

    def test_help_names_the_long_program_whatever_it_was_started_as(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from git_remote_seafile.snapshot import cli
        out = io.StringIO()
        with patch("sys.argv", ["sfsnap", "--help"]), redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertIn("git-seafile-snapshot", out.getvalue())
        self.assertNotIn("usage: sfsnap", out.getvalue())


class UmbrellaTests(unittest.TestCase):
    """``seafile`` forwards to the existing mains and holds no logic of its own."""

    def run_umbrella(self, *args: str) -> "tuple[int, str, str]":
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from git_remote_seafile import umbrella
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = umbrella.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_snapshot_and_snap_hand_the_rest_to_the_snapshot_tool(self):
        from unittest.mock import patch
        for word in ("snapshot", "snap"):
            with self.subTest(word=word), patch("git_remote_seafile.snapshot.cli.main", return_value=7) as main:
                self.assertEqual(self.run_umbrella(word, "restore", "--remote", "x")[0], 7)
                main.assert_called_once_with(["restore", "--remote", "x"])

    def test_a_helper_command_runs_the_helper_with_its_own_program_name(self):
        import sys
        from unittest.mock import patch
        seen: "list[list[str]]" = []

        def fake_main() -> int:
            seen.append(list(sys.argv))
            return 3

        with patch("git_remote_seafile.cli.main", fake_main), patch.object(sys, "argv", list(sys.argv)):
            self.assertEqual(self.run_umbrella("doctor", "seafile://lib/repo")[0], 3)
        self.assertEqual(seen, [["git-remote-seafile", "doctor", "seafile://lib/repo"]])

    def test_the_blob_store_is_named_but_not_there_yet(self):
        code, _, err = self.run_umbrella("blob", "put", "x")
        self.assertEqual(code, 2)
        self.assertIn("not part of this version", err)

    def test_help_lists_the_tools_and_no_arguments_is_a_usage_error(self):
        code, out, _ = self.run_umbrella("--help")
        self.assertEqual(code, 0)
        for text in ("seafile snapshot", "seafile blob", "git-remote-seafile"):
            self.assertIn(text, out)
        self.assertEqual(self.run_umbrella()[0], 2)

    def test_an_unknown_word_is_refused_with_the_help(self):
        code, _, err = self.run_umbrella("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("unknown tool or command 'frobnicate'", err)

    def test_every_helper_command_is_reachable_and_the_snapshot_word_is_not_shadowed(self):
        from git_remote_seafile import cli, umbrella
        self.assertEqual(umbrella._helper_commands(), frozenset(cli._HANDLERS) - {"snapshot"})
        self.assertTrue(set(umbrella.SNAPSHOT_WORDS).isdisjoint(umbrella._helper_commands()))


if __name__ == "__main__":
    unittest.main()
