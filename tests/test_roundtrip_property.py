"""Random trees must come back from a snapshot and a restore byte for byte.

Many review findings were the same shape: a tree with an awkward name or an awkward neighbour (a case
twin, a name that is not ASCII, a space, a leading dash, an empty file, bytes git would convert) lost or
changed a file on its way through the snapshot tool and back.  This generates such trees from a seed,
takes a snapshot of each into a local bare repository, restores it, and compares every file's name and
bytes with the source.  The seeds are fixed, so a failure names the seed that reproduces it.
"""

from __future__ import annotations

import os
import pathlib
import random
import sys
import unicodedata
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_snapshot_tool import _SnapshotFixture  # noqa: E402

SEEDS = (11, 23, 42, 77)
#: ``GRS_PROPERTY_SEEDS=40`` runs 40 more seeds (about four seconds each): the way to look for a new
#: shape before a release, without making every run longer.
EXTRA_SEEDS = tuple(range(1000, 1000 + int(os.environ.get("GRS_PROPERTY_SEEDS", "0") or 0)))

def identity_file(folder: pathlib.Path) -> pathlib.Path:
    """A global git config with an identity: a CI runner has none, and the vault needs one to commit."""
    path = folder / "identity.gitconfig"
    path.write_text("[user]\n\tname = t\n\temail = t@example.com\n", encoding="utf-8")
    return path


_PLAIN = ["a", "b", "note", "README", "data", "x1"]
_UNICODE = ["café", "ß", "日本語", "naïve", "über", "Ωmega"]
_AWKWARD = ["with space", "-dash", ".dot", "UPPER", "name.with.dots", "a b c", "'quoted'", "semi;colon", "100%"]
_EXT = ["", ".txt", ".bin", ".md", ".TXT"]


def _name(rng: random.Random) -> str:
    pool = rng.choice([_PLAIN, _PLAIN, _UNICODE, _AWKWARD])
    return unicodedata.normalize("NFC", rng.choice(pool) + rng.choice(_EXT))


def _bytes(rng: random.Random) -> bytes:
    kind = rng.choice(["empty", "text", "crlf", "binary", "nul", "no-final-newline"])
    if kind == "empty":
        return b""
    if kind == "text":
        return ("line one\nline two\n" * rng.randint(1, 40)).encode()
    if kind == "crlf":
        return b"dos\r\nline\r\n" * rng.randint(1, 20)
    if kind == "nul":
        return b"\x00\x01\x02" * rng.randint(1, 30)
    if kind == "no-final-newline":
        return b"no newline at the end"
    return bytes(rng.randrange(256) for _ in range(rng.randint(1, 3000)))


def _case_sensitive(folder: pathlib.Path) -> bool:
    probe = folder / "CaseProbe"
    probe.write_bytes(b"x")
    try:
        return not (folder / "caseprobe").exists()
    finally:
        probe.unlink()


def generate(root: pathlib.Path, seed: int) -> dict[str, bytes]:
    """Write a random tree under *root*; returns ``relative path -> bytes``."""
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, bytes] = {}
    folders = [pathlib.PurePosixPath(".")]
    for _ in range(rng.randint(3, 7)):
        parent = rng.choice(folders)
        folders.append(parent / _name(rng))
    twins = _case_sensitive(root)
    for _ in range(rng.randint(12, 24)):
        folder = rng.choice(folders)
        rel = str(folder / _name(rng)) if str(folder) != "." else _name(rng)
        if rel.casefold() in {str(f).casefold() for f in folders}:
            continue          # a file named like a folder
        if any(rel.casefold() == other.casefold() and rel != other for other in wanted):
            continue          # two names that fold together: only a case-sensitive disk keeps both, below
        wanted[rel] = _bytes(rng)
    if twins:
        for rel in list(wanted)[:2]:
            twin = str(pathlib.PurePosixPath(rel).with_name(pathlib.PurePosixPath(rel).name.swapcase()))
            if twin != rel and twin not in wanted:
                wanted[twin] = b"the twin of " + rel.encode()
    for rel, data in wanted.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return wanted


class TestARandomTreeComesBackUnchanged(_SnapshotFixture):
    def _files(self, root: pathlib.Path) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        for folder, dirs, names in os.walk(root):
            if ".git" in dirs:
                dirs.remove(".git")
            for name in names:
                path = pathlib.Path(folder, name)
                found[path.relative_to(root).as_posix()] = path.read_bytes()
        return found

    def setUp(self):
        super().setUp()
        if sys.getfilesystemencoding().lower().replace("-", "") != "utf8":
            self.skipTest("this locale cannot name the non-ASCII files the generator makes")
        patcher = unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(identity_file(self.tmp))})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_seed(self):
        for seed in SEEDS + EXTRA_SEEDS:
            with self.subTest(seed=seed):
                src = self.tmp / f"prop-src-{seed}"
                vault = self.tmp / f"prop-vault-{seed}"
                remote = self.tmp / f"prop-remote-{seed}.git"
                restored = self.tmp / f"prop-restored-{seed}"
                wanted = generate(src, seed)
                from test_snapshot_tool import _git  # noqa: E402
                _git(self.tmp, "init", "-q", "--bare", str(remote))
                rc, out, err = self.run_tool("--source", src, "--vault", vault, "--remote", remote)
                self.assertEqual(rc, 0, f"seed {seed}: snapshot failed\n{out}\n{err}")
                rc, out, err = self.run_tool("--restore", "--remote", remote, "--into", restored)
                self.assertEqual(rc, 0, f"seed {seed}: restore failed\n{out}\n{err}")
                got = self._files(restored)
                self.assertEqual(sorted(got), sorted(wanted), f"seed {seed}: the set of files differs")
                for rel, data in wanted.items():
                    self.assertEqual(got[rel], data, f"seed {seed}: {rel!r} differs")


if __name__ == "__main__":
    unittest.main()
