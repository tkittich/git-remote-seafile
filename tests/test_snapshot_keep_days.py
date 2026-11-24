"""``payload gc --payload-keep-days D``: retention by age (PAYLOAD.md D9a).

A snapshot counts as using its objects when it is the newest of a ref, or among the newest ``--payload-keep``
N, or at most D days old by its committer date *and* by when this tool first saw it.  The fixture takes a
snapshot at today's date in ``setUp``; the tests add more, dated in the past or the future with
``GIT_COMMITTER_DATE``, and write the first-seen record the server keeps by hand.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import unittest
import unittest.mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT / "tests"))

import seafile_snapshot as snap  # noqa: E402
from e2e_harness import REPO_ID  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402
from test_snapshot_payload import _PayloadFixture, _git  # noqa: E402

OLD = "2020-01-01T00:00:00+00:00"
OLD_SECONDS = 1_577_836_800
FUTURE = "2036-01-01T00:00:00+00:00"


class _AgeFixture(_PayloadFixture):
    def setUp(self):
        super().setUp()
        self.record_key = f"{self.base}/gc-commit-seen.json"
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.generation = 0

    def take(self, date: str | None = None) -> str:
        """A new snapshot (one changed file), committed at *date* when given; its commit id."""
        self.generation += 1
        (self.src / "small.txt").write_bytes(f"generation {self.generation}\n".encode())
        env = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else {}
        with unittest.mock.patch.dict(os.environ, env):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        return _git(self.vault, "rev-parse", "snapshot").strip()

    def write_record(self, mapping: dict[str, float], key: str | None = None) -> None:
        self.stub.files.setdefault(REPO_ID, {})[key or self.record_key] = json.dumps(mapping).encode()

    def record(self) -> dict[str, float]:
        return json.loads(self.server_files()[self.record_key])

    def days(self, *extra: object) -> tuple[int, str]:
        rc, out, err = self.run_tool("payload", "gc", "--remote", self.remote, "--payload-keep-days", "30", *extra)
        return rc, out + err

    def expired(self, text: str) -> int:
        marker = "older snapshot(s) become"
        line = next(line for line in text.splitlines() if marker in line)
        return int(line.split(marker)[0].split()[-1])


class RuleTests(_AgeFixture):
    def test_an_old_snapshot_that_is_not_the_newest_is_released(self):
        old = [self.take(OLD) for _ in range(3)]
        self.write_record({commit: OLD_SECONDS for commit in old})
        rc, text = self.days()
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.expired(text), 2)        # the two before the tip; the first snapshot is young

    def test_a_snapshot_first_seen_recently_is_kept_whatever_its_date(self):
        for _ in range(3):
            self.take(OLD)
        rc, text = self.days()                           # no record: every commit is first seen now
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.expired(text), 0)
        self.assertIn("nothing is released for 30 day(s)", text)

    def test_a_past_dated_tip_is_kept(self):
        tip = self.take(OLD)
        self.write_record({tip: OLD_SECONDS})
        rc, text = self.days()
        self.assertEqual(self.expired(text), 0)

    def test_a_future_date_is_aged_from_the_first_seen_time(self):
        far = self.take(FUTURE)
        self.take()
        self.write_record({far: OLD_SECONDS})
        rc, text = self.days()
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.expired(text), 1)
        self.write_record({far: time.time()})            # known recently: young whatever it says
        self.assertEqual(self.expired(self.days()[1]), 0)

    def test_the_newest_n_are_counted_as_well(self):
        old = [self.take(OLD) for _ in range(3)]
        self.write_record({commit: OLD_SECONDS for commit in old})
        rc, text = self.days("--payload-keep", "2")
        self.assertEqual(self.expired(text), 1)          # only the oldest of the three

    def test_a_tag_target_is_kept(self):
        old = [self.take(OLD) for _ in range(3)]
        self.write_record({commit: OLD_SECONDS for commit in old})
        _git(self.vault, "tag", "-a", "-m", "keep", "pinned", old[0])
        pushed = subprocess.run(
            ["git", "-C", str(self.vault), "push", "-q", self.remote, "refs/tags/pinned"],
            env={**os.environ, **self.env}, capture_output=True, text=True)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        rc, text = self.days()
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.expired(text), 1)          # old[1] only; old[0] is a tag's target, old[2] the tip


class GuardTests(_AgeFixture):
    def test_the_days_must_be_at_least_one(self):
        rc, out, err = self.run_tool("payload", "gc", "--remote", self.remote, "--payload-keep-days", "0")
        self.assertEqual(rc, snap.EXIT_USAGE)
        self.assertIn("--payload-keep-days must be at least 1", out + err)

    def test_only_payload_gc_reads_it(self):
        rc, out, err = self.run_tool(self.src, "--payload-keep-days", "3", "--no-push")
        self.assertEqual(rc, snap.EXIT_USAGE)
        self.assertIn("--payload-keep-days does not apply to snapshot", out + err)

    def test_releasing_snapshots_needs_accept_loss(self):
        old = [self.take(OLD) for _ in range(3)]
        self.write_record({commit: OLD_SECONDS for commit in old})
        before = set(self.server_files())
        rc, text = self.days("--payload-delete", "--payload-no-vault")
        self.assertNotEqual(rc, 0)
        self.assertIn("--payload-keep-days 30 would make 2 older snapshot(s) unrestorable", text)
        self.assertIn("--payload-accept-loss", text)
        self.assertEqual(set(self.server_files()), before)
        rc, text = self.days("--payload-delete", "--payload-no-vault", "--payload-accept-loss")
        self.assertEqual(rc, 0, text)

    def test_without_a_server_clock_the_age_rule_is_not_applied(self):
        old = [self.take(OLD) for _ in range(3)]
        self.write_record({commit: OLD_SECONDS for commit in old})
        no_clock = unittest.mock.patch.object(
            SeafileClient, "server_time_measured", new_callable=unittest.mock.PropertyMock, return_value=False)
        with no_clock:
            rc, out, err = self.run_tool("payload", "gc", "--remote", self.remote, "--payload-keep-days", "30")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("the age rule of --payload-keep-days 30 was not applied", out + err)
        self.assertNotIn("older snapshot(s) become", out + err)


class RecordTests(_AgeFixture):
    def test_a_report_writes_no_record_and_every_delete_run_writes_one(self):
        tip = self.take()
        self.days()
        self.assertNotIn(self.record_key, self.server_files())
        self.write_record({"a" * 40: OLD_SECONDS, tip: OLD_SECONDS})
        rc, out, err = self.run_tool("payload", "gc", "--remote", self.remote, "--payload-delete", "--payload-no-vault")
        self.assertEqual(rc, 0, out + err)
        record = self.record()
        self.assertEqual(record[tip], OLD_SECONDS)            # kept as it was first seen
        self.assertNotIn("a" * 40, record)                   # pruned: not reachable from a ref
        self.assertGreaterEqual(len(record), 2)               # the first snapshot is in it too

    def test_an_unreadable_record_stops_a_delete_run_and_an_age_report(self):
        self.stub.files.setdefault(REPO_ID, {})[self.record_key] = b"not json"
        rc, text = self.days()
        self.assertNotEqual(rc, 0)
        self.assertIn("gc-commit-seen.json is unreadable", text)
        rc, out, err = self.run_tool("payload", "gc", "--remote", self.remote, "--payload-delete", "--payload-no-vault")
        self.assertNotEqual(rc, 0)
        self.assertIn("gc-commit-seen.json is unreadable", out + err)

    def test_a_conflict_copy_is_merged_by_the_latest_time_and_removed_at_the_next_write(self):
        old = [self.take(OLD) for _ in range(3)]
        copy = f"{self.base}/gc-commit-seen (SFConflict ci 2026-11-01).json"
        self.write_record({commit: OLD_SECONDS for commit in old})
        self.write_record({old[0]: time.time()}, key=copy)    # another machine saw old[0] recently
        rc, text = self.days()
        self.assertEqual(self.expired(text), 1)               # old[0] is young by the latest time
        self.assertIn(copy, self.server_files())              # a report removes nothing
        rc, out, err = self.run_tool("payload", "gc", "--remote", self.remote, "--payload-delete", "--payload-no-vault")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(copy, self.server_files())
        self.assertGreater(self.record()[old[0]], OLD_SECONDS)


if __name__ == "__main__":
    unittest.main()
