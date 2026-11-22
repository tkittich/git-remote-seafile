"""A restore after a run of incremental snapshots must give back what the snapshot holds (differential fuzz).

``test_incremental_property`` proves the tree a vault records equals the tree a fresh vault records.  It never pushes
and never restores, so a bug that only shows after the push or in the checkout (a name the checkout writes under
another spelling, a file the tree names but the restore does not write) would pass it.  This runs the same random
changes over a source tree, pushes every snapshot to a local bare remote, restores the remote after each one and
checks, for every file the snapshot's tree holds, that the restored file exists and has the bytes the disk has, and
that the restore wrote nothing the tree does not hold.  Seeds are fixed, so a failure names the seed.
``GRS_RESTORE_SEEDS=30`` runs 30 more, from ``GRS_RESTORE_START`` (default 40000).
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_incremental_property as base  # noqa: E402  (a module, so its test class is not collected twice)

snap = base.snap
SEEDS = (11, 14)
_START = int(os.environ.get("GRS_RESTORE_START", "40000") or 40000)
EXTRA_SEEDS = tuple(range(_START, _START + int(os.environ.get("GRS_RESTORE_SEEDS", "0") or 0)))
_FOLD = base._FOLD
_LINK = b"\x00link"       # what ``_disk`` records for a symbolic link: its bytes are not compared


class RestoreMatchesTheSnapshot(base._IncrementalFixture):
    def _held(self, vault: pathlib.Path) -> dict[str, str]:
        """``{path: mode}`` of the blobs the vault's tree holds, payload manifests left out."""
        listing = base._git("--git-dir", str(vault / ".git"), "ls-tree", "-r", "-z", "snapshot", cwd=self.tmp).stdout
        held = {}
        for entry in listing.split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                mode = meta.split()[0]
                if mode != "160000" and not path.startswith(".seafile-payload/"):
                    held[path] = mode
        return held

    def _disk(self, root: pathlib.Path) -> dict[str, bytes]:
        found = {}
        for folder, dirs, names in os.walk(root):
            if ".git" in dirs:
                dirs.remove(".git")
            # ``os.walk`` lists a link to a folder among the folders, not the files: it is still an entry
            # of the tree (GRS_FUZZ_EXOTIC makes ``B.txt -> .``), and "left out" was a harness artifact.
            names = [*names, *(d for d in dirs if pathlib.Path(folder, d).is_symlink())]
            for name in names:
                path = pathlib.Path(folder, name)
                key = path.relative_to(root).as_posix().translate(_FOLD)
                if path.is_symlink():       # a link may come back as a link or as a file holding its target
                    found[key] = _LINK
                else:
                    found[key] = path.read_bytes()
        return found

    def _step_check(self, seed: int, step: int, remote: pathlib.Path, flags: list[str], where: str) -> None:
        out, err = io.StringIO(), io.StringIO()
        vault = self.tmp / "vault"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main(["--source", str(self.src), "--vault", str(vault), "--remote", str(remote), *flags])
        said = (err.getvalue() or out.getvalue()).strip()[-1500:]    # a long path pushed the reason out of 300
        if rc != 0:
            if ("does not have a commit checked out" in said or "repository with no commit" in said) and "--no-embedded-repos" in flags:
                raise unittest.SkipTest("documented refusal")       # a repository without a commit
            self.fail(f"{where}: the snapshot failed: {said}")
        held = self._held(vault)
        into = self.tmp / f"restored-{step}"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main(["--restore", "--remote", str(remote), "--into", str(into)])
        self.assertEqual(rc, 0, f"{where}: the restore failed: {(err.getvalue() or out.getvalue()).strip()[-300:]}")
        restored = self._disk(into)
        disk = self._disk(self.src)
        wanted = {path.translate(_FOLD) for path in held}
        self.assertEqual(sorted(set(restored) - wanted), [], f"{where}: the restore wrote what the tree lacks")
        self.assertEqual(sorted(wanted - set(restored)), [], f"{where}: the restore left out what the tree holds")
        wrong = [p for p in wanted & set(restored)
                 if p in disk and restored[p] != disk[p] and _LINK not in (restored[p], disk[p])]
        self.assertEqual(wrong, [], f"{where}: restored bytes differ from the disk's")

    def _run_restore_seed(self, seed: int) -> None:
        rng = random.Random(seed)
        remote = self.tmp / "remote.git"
        base._git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        no_embedded = rng.random() < 0.25
        log = []
        for step in range(base.STEPS):
            for _ in range(rng.randint(1, 3)):
                self._mutate(rng)
            flags = []
            for pattern in rng.choice(base.EXCLUDES):
                flags += ["--exclude", pattern]
            if no_embedded:
                flags.append("--no-embedded-repos")
            log.append(f"step {step} flags={flags}")
            try:
                self._step_check(seed, step, remote, flags, f"seed {seed}, " + "; ".join(log[-3:]))
            except unittest.SkipTest:
                return

    def test_fixed_seeds(self):
        for seed in SEEDS + EXTRA_SEEDS:
            with self.subTest(seed=seed):
                for leftover in [self.tmp / "vault", self.tmp / "remote.git", *self.tmp.glob("restored-*")]:
                    base.remove_tree(leftover)
                for child in list(self.src.iterdir()):
                    base.remove_tree(child)
                self._run_restore_seed(seed)


if __name__ == "__main__":
    unittest.main()
