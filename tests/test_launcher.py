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

Two of those cases need the stripped ``PATH`` to contain *no* real interpreter:
"only ``python`` exists" and "neither exists".  That is harder than it looks.
The launcher calls ``dirname``, so something has to be on ``PATH`` besides the
stubs -- and the obvious candidate, bash's own directory, is wrong.  On Linux
bash is ``/usr/bin/bash`` and ``/usr/bin`` also holds the real ``python3``, so
both cases found the host's interpreter and failed on CI while passing on
Windows, where Git Bash's ``bin/`` happens to hold no python at all.

So these tests build a private directory holding a ``dirname`` and nothing else.
``_leaked_interpreters`` asserts that precondition, and
``test_the_launcher_resolves_its_own_directory`` asserts that the ``dirname`` in
it really runs.  Both are needed: a symlink to the real binary looked right, was
not executable through Git Bash, and left every other test passing with the
launcher exporting ``PYTHONPATH=/``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

try:
    # top-level import under `unittest discover tests` and the parallel runner
    from _helpers import find_bash as _find_bash  # noqa: E402
except ImportError:  # package-style: python -m unittest tests.test_launcher
    from tests._helpers import find_bash as _find_bash  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = _ROOT / "bin" / "git-remote-seafile"

# `dirname` is the only thing the launcher needs from outside the shell.  A shim
# rather than a copy or a link: the `dirname` that ships with Git for Windows is
# linked against a DLL that stays behind, so a copy exits 127 and a symlink to
# it is not executable at all.  The real path travels in the environment, which
# also keeps it out of the script where it would need quoting.
_DIRNAME_SHIM = '#!/bin/sh\nexec "$GRS_REAL_DIRNAME" "$@"\n'




def _leaked_interpreters(path: str, stub_dir: pathlib.Path) -> list[str]:
    """Interpreters reachable on *path* that are not one of the stubs.

    Every case here depends on the launcher seeing only the stubs it was given.
    If a real interpreter is reachable, the "only python" and "neither" cases
    stop testing the launcher and start testing the host -- which is exactly
    what happened on the Linux runners.
    """
    leaked = []
    for name in ("python3", "python"):
        found = shutil.which(name, path=path)
        if found is not None and pathlib.Path(found).parent != stub_dir:
            leaked.append(found)
    return leaked


def _tool_dir(base: pathlib.Path, real_dirname: str | None) -> pathlib.Path | None:
    """A directory holding a ``dirname`` shim and nothing else, or ``None``.

    Widening this directory is how a real interpreter gets onto the stripped
    ``PATH``, so it is kept as narrow as the launcher's needs allow.
    """
    if real_dirname is None:
        return None
    target = base / "tool-bin"
    target.mkdir()
    shim = target / "dirname"
    shim.write_text(_DIRNAME_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return target


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

        self.real_dirname = shutil.which("dirname")
        self.tool_dir = _tool_dir(self.tmp, self.real_dirname)

    def _stub(self, name: str, body: str | None = None) -> None:
        """Create an executable stand-in.

        By default it announces itself, which is how the preference-order cases
        tell which interpreter the launcher ran.
        """
        script = f'echo "GRS-PICKED-{name}"' if body is None else body
        path = self.stub_dir / name
        path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        path.chmod(0o755)

    def _path(self) -> str:
        """A PATH holding the stubs and a way to call ``dirname``, nothing else."""
        if self.tool_dir is not None:
            extras = [str(self.tool_dir)]
        else:
            extras = [str(pathlib.Path(self.bash).parent)]
        return os.pathsep.join([str(self.stub_dir), *extras])

    def _run(self):
        """Run the launcher with no real interpreter reachable.

        The precondition is asserted rather than assumed.  It is the thing that
        broke on CI, and the failure it produced -- "GRS-PICKED-python3" missing
        from the output -- said nothing about the real cause.
        """
        path = self._path()
        leaked = _leaked_interpreters(path, self.stub_dir)
        self.assertEqual(
            leaked, [],
            "the stripped PATH reaches %s, so the launcher would find the "
            "host's interpreter instead of the stubs" % ", ".join(leaked),
        )
        env = dict(os.environ)
        env["PATH"] = path
        env["PYTHONPATH"] = ""
        if self.real_dirname is not None:
            env["GRS_REAL_DIRNAME"] = pathlib.Path(self.real_dirname).as_posix()
        return subprocess.run(
            [self.bash, str(LAUNCHER), "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    # -- the preference order -------------------------------------------

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

    # -- the environment those four depend on ---------------------------

    def test_the_tool_directory_holds_only_dirname(self):
        """`dirname` is the one external command the launcher needs."""
        if self.tool_dir is None:
            self.skipTest("no private tool directory on this platform")
        self.assertEqual(
            sorted(p.name for p in self.tool_dir.iterdir()), ["dirname"]
        )

    def test_the_launcher_resolves_its_own_directory(self):
        """`dirname` has to really run, or DIR and PYTHONPATH come out wrong.

        The launcher starts ``DIR="$(cd "$(dirname "$0")/.." && pwd)"``.  When
        the shim was a symlink to the real binary, Git Bash could not execute
        it, so ``dirname`` produced nothing, ``cd`` landed on ``/`` and the
        launcher exported ``PYTHONPATH=/`` -- and every other test in this file
        still passed, because a stub does not care what PYTHONPATH is.
        """
        self._stub(
            "python3",
            body=(
                'dir="${PYTHONPATH%%:*}"\n'
                'if [ -f "$dir/pyproject.toml" ]; then\n'
                '  echo "GRS-ROOT-OK"\n'
                "else\n"
                '  echo "GRS-ROOT-BAD:$PYTHONPATH"\n'
                "fi"
            ),
        )
        proc = self._run()
        self.assertIn(
            "GRS-ROOT-OK", proc.stdout + proc.stderr,
            "the launcher did not put its own directory on PYTHONPATH, so "
            "`dirname` did not run",
        )

    def test_the_leak_check_would_notice_a_real_interpreter(self):
        """A guard that cannot fail is not a guard.

        This is the CI failure in miniature: hand the check a directory holding
        a real interpreter and it has to complain.  On the Linux runner that
        directory was bash's own, ``/usr/bin``.
        """
        real = shutil.which("python3") or shutil.which("python")
        if real is None:
            self.skipTest("no interpreter on the host PATH to use as a leak")
        leaked = _leaked_interpreters(str(pathlib.Path(real).parent), self.stub_dir)
        self.assertTrue(
            leaked,
            "the leak check did not notice %s, so it would not have caught the "
            "leak that broke the Linux jobs" % real,
        )


if __name__ == "__main__":
    unittest.main()
