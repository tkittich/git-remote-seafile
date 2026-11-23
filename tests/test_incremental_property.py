"""An incremental snapshot must record the tree a fresh vault records (differential fuzz).

Ported from the review c305b0a Fable slice-1 script that found S1-4, S1-9 and S1-10 in twelve seeds
(a round-trip test cannot: it never runs the second snapshot over a changed tree).  After every few
random changes to a source tree the tree the *same* vault records must equal the tree a *new* vault
records with the same flags, compared with ASCII case folded (the index keeps a first-met spelling by
design).  Seeds are fixed, so a failure names the seed.  ``GRS_FUZZ_SEEDS=30`` runs 30 more.

Left out on purpose, recorded in ROADMAP AA73: a rename that only changes the case of a non-ASCII letter
(fable-S1-10; fixed on NTFS, open on macOS).  Renames, ``.gitignore`` edits and a file replaced by a
repository are in the mix (the widened run found a tracked file replaced by a folder holding one).
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
import unittest.mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402

try:
    from _helpers import remove_tree  # noqa: E402
except ImportError:  # run as ``python -m unittest tests.<module>``
    from tests._helpers import remove_tree  # noqa: E402

SEEDS = (3, 8)
_START = int(os.environ.get("GRS_FUZZ_START", "2000") or 2000)      # parallel runs take disjoint ranges
EXTRA_SEEDS = tuple(range(_START, _START + int(os.environ.get("GRS_FUZZ_SEEDS", "0") or 0)))
STEPS = 5

NAMES = ["a.txt", "B.txt", "d", "D2", "build", "x[1].txt", "-dash", "sp ace.txt", "e.txt",
         "node_modules", "k.pyc", "sub", "inner", "f", "g.bin", ".env"]
#: ``GRS_FUZZ_NONASCII=1`` adds non-ASCII names and renames that only change their case (fable-S1-10: fixed on
#: NTFS, open on macOS, so the default run leaves them out).
NON_ASCII = bool(os.environ.get("GRS_FUZZ_NONASCII"))
if NON_ASCII:
    NAMES += ["é.txt", "Über", "übung.txt", "straße.txt"]
#: ``GRS_FUZZ_EXOTIC=1`` adds symbolic links (to a file, a folder, nothing, a path outside the tree), links replaced by
#: files and back, the executable bit and a path past 260 characters.  Off by default: they need a platform that
#: can make links, and a long path is slow to walk on a Windows without long-path support.
EXOTIC = bool(os.environ.get("GRS_FUZZ_EXOTIC"))
EXCLUDES = [[], ["build"], ["*.pyc"], ["sub/inner"], ["d"], ["node_modules", "*.txt"]]
_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


class _IncrementalFixture(unittest.TestCase):   # no tests of its own: other properties build on it
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-fuzz-"))
        self.addCleanup(remove_tree, self.tmp)
        self.src = self.tmp / "src"
        self.src.mkdir()
        identity = self.tmp / "identity.gitconfig"      # a CI runner has none; without it every run fails alike
        identity.write_text("[user]\n\tname = t\n\temail = t@example.com\n", encoding="utf-8")
        patcher = unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(identity)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _walk(self):
        dirs, files = [self.src], []
        for dirpath, dirnames, filenames in os.walk(self.src):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            dirs += [pathlib.Path(dirpath) / d for d in dirnames]
            files += [pathlib.Path(dirpath) / f for f in filenames]
        return dirs, files

    def _mutate(self, rng: random.Random) -> None:
        op = rng.choice(["new", "new", "new", "mod", "mod", "del", "f2d", "d2f", "case", "repo",
                         "commit", "rmdir", "mv", "mv", "ignore", "f2repo"])
        if EXOTIC and rng.random() < 0.3:
            op = rng.choice(["link", "link", "relink", "unlink2f", "exec", "deep"])
        dirs, files = self._walk()
        d = rng.choice(dirs)
        try:
            if op == "link":
                target = rng.choice(["a.txt", "sub", "missing", "../outside", str(self.tmp / "outside.txt"), "."])
                os.symlink(target, d / rng.choice(NAMES), target_is_directory=(target in ("sub", ".")))
            elif op == "relink" and files:
                p = rng.choice(files)           # a file, or a link, becomes a link to something else
                p.unlink()
                os.symlink(rng.choice(["a.txt", "sub", "missing", str(self.tmp / "outside.txt")]), p)
            elif op == "unlink2f":
                links = [x for x in files + dirs[1:] if x.is_symlink()]
                if links:
                    x = rng.choice(links)
                    x.unlink()
                    if rng.random() < 0.5:
                        x.write_bytes(rng.randbytes(rng.randint(0, 20)))
                    else:
                        x.mkdir()
                        (x / rng.choice(NAMES)).write_bytes(b"in")
            elif op == "exec" and files:
                x = rng.choice([f for f in files if not f.is_symlink()] or files)
                if not x.is_symlink():
                    x.chmod(x.stat().st_mode ^ 0o111)
            elif op == "deep":
                chain = d
                for _ in range(rng.randint(5, 8)):
                    chain = chain / ("deep" + "x" * 40)
                chain.mkdir(parents=True, exist_ok=True)
                (chain / rng.choice(NAMES)).write_bytes(rng.randbytes(rng.randint(0, 20)))
            elif op == "new":
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
            elif op == "mv" and files:
                p = rng.choice(files + dirs[1:])
                target = rng.choice(dirs) / rng.choice(NAMES)
                if not target.exists() and p not in target.parents and p != target:
                    os.rename(p, target)
            elif op == "ignore":
                # the snapshot is of the whole tree, ignored files included, so editing .gitignore must change nothing
                (d / ".gitignore").write_text("\n".join(rng.sample(NAMES + ["*.txt", "*.bin"], 3)) + "\n")
            elif op == "f2repo" and files:
                p = rng.choice(files)
                p.unlink()
                p.mkdir()
                (p / rng.choice(NAMES)).write_bytes(rng.randbytes(5))
                _git("init", "-q", ".", cwd=p)
            elif op == "case":
                cands = [p for p in files + dirs[1:]
                         if (NON_ASCII or p.name.isascii()) and p.name.swapcase() != p.name]
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
            return None, (err.getvalue() or out.getvalue()).strip()[-1500:]    # the tail holds git's reason, behind a long path
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
        default_excludes = rng.random() < 0.3     # what it expands to is worked out again on every run
        for step in range(STEPS):
            for _ in range(rng.randint(1, 3)):
                self._mutate(rng)
            flags = []
            for pattern in rng.choice(EXCLUDES):
                flags += ["--exclude", pattern]
            if no_embedded:
                flags += ["--no-embedded-repos", "--allow-gitlinks"]
            if default_excludes:
                flags.append("--default-excludes")
            incremental, said_inc = self._snapshot(self.tmp / "vault", flags)
            fresh_dir = self.tmp / "fresh"
            if fresh_dir.exists():
                remove_tree(fresh_dir)
            fresh, said_fresh = self._snapshot(fresh_dir, flags)
            log.append(f"step {step} flags={flags}")
            where = f"seed {seed}, " + "; ".join(log[-3:])
            self.assertEqual(incremental is None, fresh is None, f"{where}: {said_inc or said_fresh}")
            if incremental is None and no_embedded and "does not have a commit checked out" in (said_inc + said_fresh):
                return          # by design: --no-embedded-repos cannot record a repository without a commit
            if step == 0:
                self.assertIsNotNone(incremental, f"{where}: the first snapshot failed, so nothing was compared: {said_inc}")
            if incremental is not None:
                differ = {k: (incremental.get(k), fresh.get(k))
                          for k in set(incremental) | set(fresh) if incremental.get(k) != fresh.get(k)}
                self.assertEqual(differ, {}, f"{where}: incremental vs fresh")

class IncrementalMatchesFresh(_IncrementalFixture):
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
