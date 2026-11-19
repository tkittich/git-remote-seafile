"""``--status`` must count what the next snapshot would change (differential property).

``--status`` compares sizes and times recorded in the last snapshot with the source, without staging
anything.  This takes a snapshot of a random tree, makes a few random changes (new files, edited files,
deleted files), and checks that the new / changed / deleted counts it reports equal the real difference
between the snapshot's tree and the disk.  The seeds are fixed, so a failure names the seed.
``GRS_STATUS_SEEDS=30`` runs 30 more.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import sys
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_roundtrip_property import generate, identity_file  # noqa: E402
from test_snapshot_tool import _SnapshotFixture, _git  # noqa: E402

SEEDS = (5, 9)
EXTRA_SEEDS = tuple(range(3000, 3000 + int(os.environ.get("GRS_STATUS_SEEDS", "0") or 0)))


def _files(root: pathlib.Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for folder, dirs, names in os.walk(root):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in names:
            path = pathlib.Path(folder, name)
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


class TestStatusCountsWhatTheNextSnapshotChanges(_SnapshotFixture):
    def setUp(self):
        super().setUp()
        if sys.getfilesystemencoding().lower().replace("-", "") != "utf8":
            self.skipTest("this locale cannot name the non-ASCII files the generator makes")
        patcher = unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(identity_file(self.tmp))})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _change(self, src: pathlib.Path, rng: random.Random) -> None:
        for _ in range(rng.randint(1, 4)):
            files = sorted(_files(src))
            op = rng.choice(["new", "edit", "delete"])
            if op == "new" or not files:
                folder = rng.choice(["", *sorted({str(pathlib.PurePosixPath(f).parent) for f in files} - {"."})])
                path = src / folder / f"added-{rng.randint(0, 10 ** 6)}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(rng.randbytes(rng.randint(0, 30)))
            elif op == "edit":
                path = src / rng.choice(files)
                path.write_bytes(path.read_bytes() + b"+" + rng.randbytes(rng.randint(1, 9)))   # a new size
            elif op == "delete":
                (src / rng.choice(files)).unlink()

    def test_every_seed(self):
        for seed in SEEDS + EXTRA_SEEDS:
            with self.subTest(seed=seed):
                src = self.tmp / f"status-src-{seed}"
                vault = self.tmp / f"status-vault-{seed}"
                generate(src, seed)
                rc, out, err = self.run_tool("--source", src, "--vault", vault, "--no-push", "--record-mtimes")
                self.assertEqual(rc, 0, f"seed {seed}: snapshot failed\n{out}\n{err}")
                before = {
                    path: data for path, data in _files(src).items()
                }
                self._change(src, random.Random(seed))
                now = _files(src)
                recorded = set(_git(vault, "ls-tree", "-r", "-z", "--name-only", "snapshot").stdout.split("\0")) - {""}
                recorded = {p for p in recorded if not p.startswith(".seafile-payload/")}
                expect = {
                    "new": len(set(now) - recorded),
                    "deleted": len(recorded - set(now)),
                    "changed": len([p for p in recorded & set(now) if now[p] != before.get(p)]),
                }
                rc, out, err = self.run_tool("--source", src, "--vault", vault, "--status", "--json")
                self.assertEqual(rc, 0, f"seed {seed}: status failed\n{out}\n{err}")
                status = json.loads(out)["result"]["status"]
                got = {key: status[key] for key in expect}
                self.assertEqual(got, expect, f"seed {seed}: status counts differ from the real change")


if __name__ == "__main__":
    unittest.main()
