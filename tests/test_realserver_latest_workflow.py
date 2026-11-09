"""The unpinned monthly real-server run stays what ROADMAP Z37 made it: unpinned, monthly, non-blocking."""

from __future__ import annotations

import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LATEST = (_ROOT / ".github" / "workflows" / "realserver-latest.yml").read_text(encoding="utf-8")
_PINNED = (_ROOT / ".github" / "workflows" / "realserver.yml").read_text(encoding="utf-8")


class UnpinnedRealServerWorkflowTests(unittest.TestCase):
    def test_it_tests_the_floating_tags_and_pins_nothing(self):
        images = re.findall(r"seafileltd/[\w-]+:[\w.-]+(?:@sha256:[0-9a-f]+)?", _LATEST)
        self.assertEqual(sorted(images), ["seafileltd/seafile-mc:13.0-latest",
                                          "seafileltd/seafile-pro-mc:13.0-latest"])
        self.assertNotIn("@sha256:", _LATEST.replace("uses:", ""), "a digest pin defeats the point")

    def test_the_pinned_gate_still_pins_both_images(self):
        pinned = re.findall(r"seafileltd/[\w-]+:13\.0-latest@sha256:[0-9a-f]{64}", _PINNED)
        self.assertEqual(len(pinned), 2, "realserver.yml is the gate: its images stay pinned")

    def test_it_runs_monthly_and_by_hand(self):
        self.assertIn("workflow_dispatch:", _LATEST)
        cron = re.search(r'cron:\s*"([^"]+)"', _LATEST)
        self.assertIsNotNone(cron)
        minute, hour, day, month, weekday = cron.group(1).split()
        self.assertEqual((day, month, weekday), ("1", "*", "*"), "once a month, on the 1st")

    def test_it_can_never_gate_anything(self):
        self.assertRegex(_LATEST, r"(?m)^    continue-on-error: true$")
        release_check = (_ROOT / "tools" / "release_check.py").read_text(encoding="utf-8")
        self.assertNotIn("realserver-latest", release_check, "a floating-tag run must not gate a tag")

    def test_it_runs_the_same_suite_on_both_editions_and_records_the_digests(self):
        self.assertIn("edition: [ce, pro]", _LATEST)
        self.assertIn("python tools/realserver_ci.py", _LATEST)
        self.assertIn("Record the image digests", _LATEST)

    def test_every_action_is_pinned_by_commit_like_the_other_workflows(self):
        for line in _LATEST.splitlines():
            if "uses:" in line:
                self.assertRegex(line, r"@[0-9a-f]{40}\s+#", line)


if __name__ == "__main__":
    unittest.main()
