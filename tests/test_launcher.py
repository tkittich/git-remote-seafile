"""The source-checkout launcher has to find an interpreter by itself.

``bin/git-remote-seafile`` is what someone runs straight from a clone, without
installing the package.  It used to ``exec python``, which is not a name you can
rely on: on macOS and on most Linux distributions bare ``python`` is absent, or
is still Python 2, and only ``python3`` is guaranteed.  Getting it wrong fails
deep inside a git operation with ``python: command not found``, which is a long
way from the cause.

The preference order is checked by execution rather than by reading the script:
each case puts a stub interpreter on a stripped ``PATH`` and asserts which one
the launcher actually ran, by the marker it printed.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = _ROOT / "bin" / "git-remote-seafile"


def _find_bash() -> str | None:
    """Locate a POSIX bash, preferring the one that ships with git.

    ``shutil.which("bash")`` can resolve to ``System32\\bash.exe`` on Windows,
    which is the WSL launcher rather than a shell, so look next to git first and
    reject the WSL one.
    """
    override = os.environ.get("GRS_BASH")
    if override:
        return override if pathlib.Path(override).is_file() else None

    git = shutil.which("git")
    if git:
        git_dir = pathlib.Path(git).resolve().parent
        for candidate in (git_dir / "bash.exe", git_dir.parent / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)

    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


class TestSourceCheckoutLauncher(unittest.TestCase):
    def setUp(self):
        self.bash = _find_bash()
        if self.bash is None:
            self.skipTest("no POSIX bash found (set GRS_BASH to point at one)")
        self.assertTrue(LAUNCHER.is_file(), f"missing launcher: {LAUNCHER}")

        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-launcher-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stub_dir = self.tmp / "stub-bin"
        self.stub_dir.mkdir()

    def _stub(self, name: str) -> None:
        """Create an executable stand-in that announces itself."""
        path = self.stub_dir / name
        path.write_text(f'#!/bin/sh\necho "GRS-PICKED-{name}"\nexit 0\n', encoding="utf-8")
        path.chmod(0o755)

    def _run(self):
        """Run the launcher with only the stubs (plus bash's own dir) on PATH.

        Bash's directory has to stay: the launcher calls ``dirname``.  It ships
        no ``python``, so the stubs remain the only candidates.
        """
        path = os.pathsep.join([str(self.stub_dir), str(pathlib.Path(self.bash).parent)])
        env = dict(os.environ)
        env["PATH"] = path
        return subprocess.run(
            [self.bash, str(LAUNCHER), "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_python3_is_used_when_only_python3_exists(self):
        self._stub("python3")
        proc = self._run()
        self.assertIn("GRS-PICKED-python3", proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0)

    def test_python_is_the_fallback(self):
        """A machine with only the old name must still work."""
        self._stub("python")
        proc = self._run()
        self.assertIn("GRS-PICKED-python", proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0)

    def test_python3_wins_when_both_exist(self):
        """`python` may still be Python 2; the launcher must not pick it."""
        self._stub("python3")
        self._stub("python")
        proc = self._run()
        output = proc.stdout + proc.stderr
        self.assertIn("GRS-PICKED-python3", output)
        self.assertNotIn("GRS-PICKED-python\n", output)

    def test_missing_interpreter_is_reported_not_guessed(self):
        """With neither present, say so -- do not exec a name that may not exist."""
        proc = self._run()
        self.assertEqual(proc.returncode, 127)
        self.assertIn("python3", proc.stderr)
        self.assertIn("python", proc.stderr)


if __name__ == "__main__":
    unittest.main()
