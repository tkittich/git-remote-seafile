"""Fail each write of a *snapshot* push, once, and require that no snapshot is lost or recorded twice.

``test_fault_injection`` does this for the helper's push.  The snapshot tool wraps that push in a retry
that decides from the remote tip whether the remote moved (fable-E1, S3-5 of the c305b0a round both lived
there), so the same mechanical question is asked one layer up: after one failed write during the second
snapshot, the remote is at the old or the new snapshot, never anywhere else, and running the snapshot
again with a healthy server ends with the remote and the vault on the same tip and no tree recorded twice.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import run_git  # noqa: E402
from test_e2e import E2ETestCase  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_WRITES = 20


class TestASnapshotPushThatFailsOnceLosesNothing(E2ETestCase):
    def _snapshot(self, src, vault, name):
        return subprocess.run(
            [sys.executable, str(_ROOT / "tools" / "seafile_snapshot.py"), "--source", str(src),
             "--vault", str(vault), "--remote", self.url(name), "--no-payload"],
            env={**os.environ, **self.env}, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300,
        )

    def _remote_tip(self, name):
        out = run_git(["ls-remote", self.url(name)], self.work, self.env, check=False)
        for line in out.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == "refs/heads/snapshot":
                return sha
        return None

    def _scenario(self, name):
        src = self.work / f"snap-src-{name}"
        src.mkdir()
        (src / "a.txt").write_bytes(b"one\n")
        vault = self.work / f"snap-vault-{name}"
        first = self._snapshot(src, vault, name)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        old = self._remote_tip(name)
        (src / "b.txt").write_bytes(b"two\n")
        return src, vault, old

    def test_each_write_of_the_second_snapshot_push_fails_once(self):
        src, vault, _old = self._scenario("count")
        self.stub.write_calls = 0
        clean = self._snapshot(src, vault, "count")
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        total = self.stub.write_calls
        self.assertGreater(total, 0)
        self.assertLessEqual(total, MAX_WRITES, "the push makes more writes than the test fails")
        for n in range(1, total + 1):
            with self.subTest(failing_write=n, of=total):
                name = f"snapfault-{n}"
                src, vault, old = self._scenario(name)
                self.stub.write_calls = 0
                self.stub.faults["fail_write_n"] = n
                try:
                    failed = self._snapshot(src, vault, name)
                finally:
                    self.stub.faults.pop("fail_write_n", None)
                tip = self._remote_tip(name)
                vault_tip = run_git(["rev-parse", "snapshot"], vault, self.env).stdout.strip()
                self.assertIn(tip, {old, vault_tip}, "the remote is neither at the old snapshot nor the new one")
                if failed.returncode == 0:
                    self.assertEqual(tip, vault_tip, "success was reported but the remote is behind the vault")
                again = self._snapshot(src, vault, name)
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
                final = self._remote_tip(name)
                self.assertEqual(final, run_git(["rev-parse", "snapshot"], vault, self.env).stdout.strip())
                trees = run_git(["log", "--format=%T", "snapshot"], vault, self.env).stdout.split()
                self.assertEqual(len(trees), len(set(trees)), "a snapshot was recorded twice")
                self.assertEqual(len(trees), 2, "a snapshot was lost")


if __name__ == "__main__":
    unittest.main()
