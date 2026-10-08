"""Guards for the CI failure reporter.

``.github/ci_report_failures.py`` exists so that a failing test is *readable* by
people without admin rights: workflow logs need admin to download, check-run
annotations do not.  It emits one ``::error::`` per failing test and a second
carrying the line that explains it.

The second annotation is the whole point, and it was picking the wrong line.  A
test that passes a message to its assertion produces a block whose last line is
the message's *tail*::

    AssertionError: 'actual' != 'expected'
    - actual
    + expected
     : deliberate probe failure

"Last non-empty line" returned ``: deliberate probe failure`` -- the least
informative line in the block -- for every test that passed a message, which is
exactly the case where the assertion itself is most useful.

The script is exercised as a subprocess rather than imported, so the assertions
cover the real entry point including its exit code, which is what CI keys off.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "ci_report_failures.py"

# Captured verbatim from a real run, so the fixture cannot drift from the format
# unittest actually emits.
_FAILING_RUN = """\
test_a_deliberate_failure (test_probe.TestProbeFails.test_a_deliberate_failure) ... FAIL

======================================================================
FAIL: test_a_deliberate_failure (test_probe.TestProbeFails.test_a_deliberate_failure)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/repo/tests/test_probe.py", line 18, in test_a_deliberate_failure
    self.assertEqual("actual", "expected", "deliberate probe failure")
    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: 'actual' != 'expected'
- actual
+ expected
 : deliberate probe failure

----------------------------------------------------------------------
Ran 1 test in 0.001s

FAILED (failures=1)
"""

_PASSING_RUN = """\
----------------------------------------------------------------------
Ran 2 tests in 0.001s

OK
"""

_CRASHED_RUN = """\
Traceback (most recent call last):
  File "run_tests.py", line 1, in <module>
ImportError: cannot import name 'thing' from 'module'
"""


class TestCiReportFailures(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), f"missing reporter: {SCRIPT}")
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-report-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, output: str | None) -> subprocess.CompletedProcess:
        path = self.tmp / "unittest-output.txt"
        if output is not None:
            path.write_text(output, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(path)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )

    @staticmethod
    def _annotations(proc: subprocess.CompletedProcess) -> list[str]:
        return [ln for ln in proc.stdout.splitlines() if ln.startswith("::error::")]

    # -- the exit code is what turns the job red -------------------------

    def test_a_passing_run_is_not_an_error(self):
        proc = self._run(_PASSING_RUN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._annotations(proc), [])

    def test_a_failing_run_fails(self):
        proc = self._run(_FAILING_RUN)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

    def test_a_run_with_no_summary_at_all_fails(self):
        # A crash or an import error produces no FAIL: lines and no OK line.  It
        # must still be red, and must say so rather than staying silent.
        proc = self._run(_CRASHED_RUN)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        joined = "\n".join(self._annotations(proc))
        self.assertIn("crashed or could not collect tests", joined)

    def test_a_missing_output_file_fails(self):
        proc = self._run(None)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertTrue(self._annotations(proc))

    # -- the annotations are the point ----------------------------------

    def test_it_prints_the_output_verbatim(self):
        proc = self._run(_FAILING_RUN)
        for line in _FAILING_RUN.splitlines():
            with self.subTest(line=line):
                self.assertIn(line, proc.stdout)

    def test_it_annotates_the_failing_test_by_name(self):
        proc = self._run(_FAILING_RUN)
        annotations = self._annotations(proc)
        self.assertTrue(annotations, "no annotation for a failing run")
        self.assertIn(
            "FAIL: test_a_deliberate_failure "
            "(test_probe.TestProbeFails.test_a_deliberate_failure)",
            annotations[0],
        )

    def test_the_detail_is_the_assertion_not_the_message_tail(self):
        proc = self._run(_FAILING_RUN)
        annotations = self._annotations(proc)
        self.assertGreaterEqual(
            len(annotations), 2,
            f"expected a detail annotation; got {annotations}",
        )
        detail = annotations[1]
        self.assertIn(
            "AssertionError: 'actual' != 'expected'", detail,
            "the detail annotation must carry the assertion, which is what "
            "explains the failure",
        )
        self.assertNotIn(
            ": deliberate probe failure", detail,
            "the detail annotation picked the assertion message's tail, the "
            "least informative line in the block",
        )


    def test_ordinary_failures_are_reported_by_their_exception_line(self):
        # The heuristic prefers a column-0 "Identifier: ..." line.  These are
        # the shapes it must not get wrong while doing so.
        dash = "-" * 70
        cases = {
            "plain assertion": (
                f"FAIL: test_x (m.C.test_x)\n{dash}\n"
                "Traceback (most recent call last):\n"
                '  File "/repo/tests/t.py", line 5, in test_x\n'
                "    self.assertEqual(1, 2)\n"
                "AssertionError: 1 != 2\n\n"
                f"{dash}\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n",
                "AssertionError: 1 != 2",
            ),
            "raised exception": (
                f"ERROR: test_y (m.C.test_y)\n{dash}\n"
                "Traceback (most recent call last):\n"
                '  File "/repo/tests/t.py", line 9, in test_y\n'
                '    raise ValueError("boom")\n'
                "ValueError: boom\n\n"
                f"{dash}\nRan 1 test in 0.001s\n\nFAILED (errors=1)\n",
                "ValueError: boom",
            ),
            "self.fail()": (
                f"FAIL: test_z (m.C.test_z)\n{dash}\n"
                "Traceback (most recent call last):\n"
                '  File "/repo/tests/t.py", line 2, in test_z\n'
                "    self.fail()\n"
                "AssertionError: None\n\n"
                f"{dash}\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n",
                "AssertionError: None",
            ),
        }
        for name, (output, expected) in cases.items():
            with self.subTest(case=name):
                proc = self._run(output)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                annotations = self._annotations(proc)
                self.assertGreaterEqual(len(annotations), 2, annotations)
                self.assertIn(expected, annotations[1])


if __name__ == "__main__":
    unittest.main()
