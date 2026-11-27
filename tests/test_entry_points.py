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

    def test_the_blob_names_run_the_same_function(self):
        scripts = self.scripts()
        for name in ("seafile-blob", "sfblob", "git-seafile-blob"):
            self.assertEqual(scripts[name], "git_remote_seafile.blob.cli:main")

    def test_no_command_is_named_like_seafile_itself(self):
        # 0.11.0 shipped an umbrella command called ``seafile`` and was withdrawn for it; ``sfgit`` followed and was dropped
        # as one name too many.  The names are the helper (git requires it), the snapshot tool and the blob store.
        self.assertEqual(
            sorted(self.scripts()),
            sorted(["git-remote-seafile", "git-seafile-snapshot", "sfsnap", "git-sfsnap", "seafile-blob", "sfblob", "git-seafile-blob"]),
        )

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


if __name__ == "__main__":
    unittest.main()
