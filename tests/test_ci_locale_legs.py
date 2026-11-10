"""The locale legs of ci.yml (ROADMAP Z39) keep testing what they were added to test."""

from __future__ import annotations

import pathlib
import re
import unittest

_CI = (pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _job(name: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z-]*:\n|\Z)", _CI)
    assert match, f"ci.yml has no job {name!r}"
    return match.group(1)


class LocaleLegsTests(unittest.TestCase):
    def test_there_is_a_turkish_leg_and_an_ascii_only_leg(self):
        job = _job("locale-legs")
        self.assertIn("locale: tr_TR.UTF-8", job)
        self.assertIn("locale: C\n", job)
        self.assertIn('utf8: "0"', job, "the C leg must switch UTF-8 mode off")

    def test_python_is_not_allowed_to_coerce_the_c_locale_away(self):
        job = _job("locale-legs")
        self.assertGreaterEqual(job.count('PYTHONCOERCECLOCALE: "0"'), 2, "PEP 538 would turn C into C.UTF-8")

    def test_the_whole_suite_runs_under_those_variables(self):
        job = _job("locale-legs")
        run_step = job[job.index("- name: Run unit tests"):]
        for variable in ("LANG:", "LC_ALL:", "PYTHONUTF8:", "PYTHONCOERCECLOCALE:"):
            self.assertIn(variable, run_step.split("- name: Report test results")[0])
        self.assertIn("tools/run_tests_parallel.py", run_step)

    def test_the_actions_are_pinned_by_commit_like_the_rest(self):
        for line in _job("locale-legs").splitlines():
            if "uses:" in line:
                self.assertRegex(line, r"@[0-9a-f]{40}\s+#", line)


if __name__ == "__main__":
    unittest.main()
