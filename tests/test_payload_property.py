"""Payload mode must give back every byte after a run of incremental snapshots (differential fuzz).

Large files go to the server's payload store and the git tree holds only a manifest, so a file that changes size
class, is renamed, is deleted or comes back is where the manifest, the hash cache and the stored objects could
disagree with the disk.  This changes a source tree at random (new, edited, deleted and renamed files, some above
the threshold and some below, an edit that moves a file across it), snapshots after each change against the stub
server with a 1000-byte threshold, restores the remote and compares every file with the disk.  Seeds are fixed, so a
failure names the seed.  ``GRS_PAYLOAD_SEEDS=30`` runs 30 more, from ``GRS_PAYLOAD_START`` (default 50000).
"""

from __future__ import annotations

import os
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_snapshot_payload as payload  # noqa: E402  (a module: its fixture and tests are not collected here)

snap = payload.snap
SEEDS = (21, 22)
_START = int(os.environ.get("GRS_PAYLOAD_START", "50000") or 50000)
EXTRA_SEEDS = tuple(range(_START, _START + int(os.environ.get("GRS_PAYLOAD_SEEDS", "0") or 0)))
STEPS = 4
THRESHOLD = "1000"
NAMES = ["a.bin", "b.dat", "c.txt", "Big.bin", "d", "e.bin", "sp ace.bin", "k.txt"]


def _disk(root: pathlib.Path) -> dict[str, bytes]:
    found = {}
    for folder, dirs, names in os.walk(root):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in names:
            path = pathlib.Path(folder, name)
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


class PayloadRoundTrip(payload._PayloadFixture):
    def _size(self, rng: random.Random) -> int:
        return rng.choice([0, 10, 400, 999, 1000, 1001, 2500, 6000])        # both sides of the threshold

    def _mutate(self, rng: random.Random) -> None:
        files = sorted(p for p in self.plain.rglob("*") if p.is_file())
        folders = [self.plain, *sorted(p for p in self.plain.rglob("*") if p.is_dir())]
        op = rng.choice(["new", "new", "mod", "mod", "del", "mv", "case", "grow"])
        try:
            if op == "new":
                target = rng.choice(folders) / rng.choice(NAMES)
                if not target.is_dir():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(rng.randbytes(self._size(rng)))
            elif op == "mod" and files:
                rng.choice(files).write_bytes(rng.randbytes(self._size(rng)))
            elif op == "grow" and files:
                path = rng.choice(files)
                path.write_bytes(path.read_bytes() + rng.randbytes(1500))      # an edit that crosses the threshold
            elif op == "del" and files:
                rng.choice(files).unlink()
            elif op == "mv" and files:
                path = rng.choice(files)
                target = rng.choice(folders) / rng.choice(NAMES)
                if not target.exists() and path != target:
                    os.rename(path, target)
            elif op == "case" and files:
                path = rng.choice(files)
                if path.name.swapcase() != path.name:
                    os.rename(path, path.with_name(path.name.swapcase()))
        except OSError:
            pass

    def _run_seed(self, seed: int) -> None:
        rng = random.Random(seed)
        self.plain = self.case / f"plain-{seed}"
        self.plain.mkdir()
        (self.plain / "sub").mkdir()
        (self.plain / "seed.txt").write_bytes(b"seed" + bytes([10]))
        vault = self.case / f"vault-{seed}"
        for step in range(STEPS):
            for _ in range(rng.randint(1, 3)):
                self._mutate(rng)
            where = f"seed {seed}, step {step}"
            rc, out, err = self.run_tool(
                "--source", self.plain, "--vault", vault, "--remote", self.remote,
                "--payload-min-size", THRESHOLD)
            self.assertEqual(rc, 0, f"{where}: the snapshot failed\n{out[-300:]}\n{err[-300:]}")
            into = self.case / f"restored-{seed}-{step}"
            rc, out, err = self.run_tool("--restore", "--remote", self.remote, "--into", into)
            self.assertEqual(rc, 0, f"{where}: the restore failed\n{out[-300:]}\n{err[-300:]}")
            # a folding disk keeps the index's first spelling of a renamed-by-case file (core.ignorecase): compare folded
            disk = {p.lower(): d for p, d in _disk(self.plain).items()}
            restored = {p.lower(): d for p, d in _disk(into).items() if not p.startswith(".seafile-payload/")}
            self.assertEqual(sorted(set(disk) ^ set(restored)), [], f"{where}: the file sets differ")
            self.assertEqual([p for p in disk if disk[p] != restored.get(p)], [], f"{where}: bytes differ")

    def test_fixed_seeds(self):
        for seed in SEEDS + EXTRA_SEEDS:
            with self.subTest(seed=seed):
                self.stub.reset()
                self._run_seed(seed)


if __name__ == "__main__":
    unittest.main()
