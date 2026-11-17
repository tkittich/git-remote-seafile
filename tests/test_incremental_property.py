"""An incremental snapshot must record the tree a fresh vault records (differential fuzz).

Ported from the review c305b0a Fable slice-1 script that found S1-4, S1-9 and S1-10 in twelve seeds
(a round-trip test cannot: it never runs the second snapshot over a changed tree).  After every few
random changes to a source tree the tree the *same* vault records must equal the tree a *new* vault
records with the same flags, compared with ASCII case folded (the index keeps a first-met spelling by
design).  Seeds are fixed, so a failure names the seed.  ``GRS_FUZZ_SEEDS=30`` runs 30 more.

Left out on purpose, both recorded in ROADMAP AA73: a file replaced by a directory holding a commitless
repository (fable-S1-4) and a rename that only changes the case of a non-ASCII letter (fable-S1-10).
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402

try:
    from _helpers import remove_tree  # noqa: E402
except ImportError:  # run as ``python -m unittest tests.<module>``
    from tests._helpers import remove_tree  # noqa: E402

SEEDS = (3, 8)
EXTRA_SEEDS = tuple(range(2000, 2000 + int(os.environ.get("GRS_FUZZ_SEEDS", "0") or 0)))
STEPS = 5

NAMES = ["a.txt", "B.txt", "d", "D2", "build", "x[1].txt", "-dash", "sp ace.txt", "e.txt",
         "node_modules", "k.pyc", "sub", "inner", "f", "g.bin", ".env"]
EXCLUDES = [[], ["build"], ["*.pyc"], ["sub/inner"], ["d"], ["node_modules", "*.txt"]]
_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


class IncrementalMatchesFresh(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-fuzz-"))
        self.addCleanup(remove_tree, self.tmp)
        self.src = self.tmp / "src"
        self.src.mkdir()

    def _walk(self):
        dirs, files = [self.src], []
        for dirpath, dirnames, filenames in os.walk(self.src):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            dirs += [pathlib.Path(dirpath) / d for d in dirnames]
            files += [pathlib.Path(dirpath) / f for f in filenames]
        return dirs, files

    def _mutate(self, rng: random.Random) -> None:
        op = rng.choice(["new", "new", "new", "mod", "mod", "del", "f2d", "d2f", "case", "repo",
                         "commit", "rmdir"])
        dirs, files = self._walk()
        d = rng.choice(dirs)
        try:
            if op == "new":
                p = d / rng.choice(NAMES)
                if p.is_dir():
                    p = p / rng.choice(NAMES)
                if not p.is_dir():
                    p.write_bytes(rng.randbytes(rng.randint(0, 40)))
            elif op == "mod" and files:
                rng.choice(files).write_bytes(rng.randbytes(rng.randint(0, 40)))
            elif op == "del" and files:
                rng.choice(files).unlink()
            elif op == "f2d" and files:
                p = rng.choice(files)
                p.unlink()
                p.mkdir()
                for n in rng.sample(NAMES, 2):
                    (p / n).write_bytes(rng.randbytes(5))      # no repository inside (fable-S1-4)
            elif op == "d2f" and d != self.src:
                remove_tree(d)
                d.write_bytes(b"was a dir")
            elif op == "rmdir" and d != self.src:
                remove_tree(d)
            elif op == "case":
                cands = [p for p in files + dirs[1:] if p.name.isascii() and p.name.swapcase() != p.name]
                if cands:
                    p = rng.choice(cands)
                    os.rename(p, p.with_name(p.name.swapcase()))
            elif op == "repo" and d != self.src and not (d / ".git").exists():
                _git("init", "-q", ".", cwd=d)
            elif op == "commit":
                repos = [x for x in dirs[1:] if (x / ".git").is_dir()]
                if repos:
                    r = rng.choice(repos)
                    _git("add", "-A", ".", cwd=r)
                    _git("-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "-m", "c",
                         "--allow-empty", cwd=r)
        except OSError:
            pass

    def _snapshot(self, vault, flags):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = snap.main(["--source", str(self.src), "--vault", str(vault), "--no-push", *flags])
        if rc != 0:
            return None, (err.getvalue() or out.getvalue()).strip()[-300:]
        listing = _git("--git-dir", str(vault / ".git"), "ls-tree", "-r", "-z", "snapshot", cwd=self.tmp).stdout
        entries = {}
        for entry in listing.split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                entries[path.translate(_FOLD)] = meta
        return entries, ""

    def _run_seed(self, seed: int) -> None:
        rng = random.Random(seed)
        log = []
        no_embedded = rng.random() < 0.25     # one choice per vault: switching it between runs is refused by design
        for step in range(STEPS):
            for _ in range(rng.randint(1, 3)):
                self._mutate(rng)
            flags = []
            for pattern in rng.choice(EXCLUDES):
                flags += ["--exclude", pattern]
            if no_embedded:
                flags.append("--no-embedded-repos")
            incremental, said_inc = self._snapshot(self.tmp / "vault", flags)
            fresh_dir = self.tmp / "fresh"
            if fresh_dir.exists():
                remove_tree(fresh_dir)
            fresh, said_fresh = self._snapshot(fresh_dir, flags)
            log.append(f"step {step} flags={flags}")
            where = f"seed {seed}, " + "; ".join(log[-3:])
            self.assertEqual(incremental is None, fresh is None, f"{where}: {said_inc or said_fresh}")
            if incremental is not None:
                differ = {k: (incremental.get(k), fresh.get(k))
                          for k in set(incremental) | set(fresh) if incremental.get(k) != fresh.get(k)}
                self.assertEqual(differ, {}, f"{where}: incremental vs fresh")

    def test_fixed_seeds(self):
        for seed in SEEDS + EXTRA_SEEDS:
            with self.subTest(seed=seed):
                for leftover in (self.tmp / "vault", self.tmp / "fresh"):
                    remove_tree(leftover)
                for child in list(self.src.iterdir()):
                    remove_tree(child)
                self._run_seed(seed)


if __name__ == "__main__":
    unittest.main()
