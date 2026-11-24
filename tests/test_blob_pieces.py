"""The blob store's pure pieces: references, the record file, the cache (STORE.md D3, D4, R3, R7)."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from git_remote_seafile.blob import cache, record, refs

DATA = b"blob bytes " * 100
SHA = hashlib.sha256(DATA).hexdigest()
OTHER = hashlib.sha256(b"another").hexdigest()


class RefTests(unittest.TestCase):
    def test_a_full_and_a_short_reference_parse(self):
        self.assertTrue(refs.parse(f"sha256:{SHA}").is_full)
        short = refs.parse(f"sha256:{SHA[:8]}")
        self.assertFalse(short.is_full)
        self.assertEqual(str(short), f"sha256:{SHA[:8]}")

    def test_only_lowercase_hex_after_the_prefix_is_accepted(self):
        for bad in ("", "sha256:", f"sha256:{SHA.upper()}", f"sha256:{SHA}0", "sha1:" + SHA, f"sha256:../{SHA[:10]}",
                    "sha256:" + "g" * 10, SHA, f"sha256:{SHA[:5]}", "sha256:ab cd12"):
            with self.subTest(text=bad), self.assertRaises(refs.RefError):
                refs.parse(bad)

    def test_a_record_needs_the_whole_reference(self):
        with self.assertRaises(refs.RefError):
            refs.full(f"sha256:{SHA[:12]}")
        self.assertEqual(refs.full(f"sha256:{SHA}").hex, SHA)

    def test_a_short_reference_resolves_only_when_exactly_one_object_matches(self):
        names = [SHA, OTHER, "x" * 64, "ab" + "0" * 62]
        self.assertEqual(refs.resolve(refs.parse(f"sha256:{SHA[:7]}"), names).hex, SHA)
        with self.assertRaises(refs.RefError) as none:
            refs.resolve(refs.parse("sha256:ffffff"), names)
        self.assertIn("no object", str(none.exception))
        clash = [SHA, SHA[:10] + "0" * 54]
        with self.assertRaises(refs.RefError) as many:
            refs.resolve(refs.parse(f"sha256:{SHA[:8]}"), clash)
        self.assertIn("ambiguous", str(many.exception))
        self.assertIn(SHA, str(many.exception))

    def test_a_whole_reference_is_returned_as_it_is(self):
        ref = refs.parse(f"sha256:{SHA}")
        self.assertIs(refs.resolve(ref, []), ref)


class RecordTests(unittest.TestCase):
    def entry(self, path: str, sha: str = SHA, size: int = 5) -> record.Entry:
        return record.Entry(path, size, refs.full(f"sha256:{sha}"))

    def test_a_record_is_sorted_one_line_per_entry_and_reads_back(self):
        text = record.render([self.entry("b/two.bin"), self.entry("a.bin", OTHER, 9)])
        self.assertEqual(text.splitlines(), [f"a.bin\t9\tsha256:{OTHER}", f"b/two.bin\t5\tsha256:{SHA}"])
        self.assertEqual([e.path for e in record.parse(text)], ["a.bin", "b/two.bin"])

    def test_the_same_input_in_any_order_is_the_same_file(self):
        one = [self.entry("z"), self.entry("a", OTHER), self.entry("m")]
        self.assertEqual(record.render(one), record.render(list(reversed(one))))

    def test_a_path_is_stored_in_nfc_with_forward_slashes(self):
        decomposed = "caf\u0065\u0301/x.bin"
        text = record.render([self.entry(decomposed.replace("/", "\\"))])
        self.assertEqual(text.split("\t")[0], "caf\u00e9/x.bin")

    def test_two_spellings_of_one_path_are_one_entry_or_a_conflict(self):
        record.render([self.entry("caf\u00e9"), self.entry("caf\u0065\u0301")])      # the same entry twice: one line
        with self.assertRaises(record.RecordError):
            record.render([self.entry("caf\u00e9"), self.entry("caf\u0065\u0301", OTHER)])

    def test_a_hostile_path_is_refused(self):
        for bad in ("", "/etc/passwd", "../x", "a/../b", "a//b", "C:/x", "a\tb", "a\nb", "./x", "a/."):
            with self.subTest(path=bad), self.assertRaises(record.RecordError):
                record.render([self.entry(bad)])

    def test_a_bad_file_names_the_line(self):
        for text, fragment in (("a\t1\n", "line 1"), (f"a\tx\tsha256:{SHA}\n", "not a whole number"),
                               (f"a\t1\tsha256:{SHA[:10]}\n", "line 1"),
                               (f"a\t1\tsha256:{SHA}\na\t1\tsha256:{SHA}\n", "twice"),
                               (f"../a\t1\tsha256:{SHA}\n", "line 1"),
                               (f"a\\b\t1\tsha256:{SHA}\n", "normal form")):
            with self.subTest(text=text), self.assertRaises(record.RecordError) as caught:
                record.parse(text)
            self.assertIn(fragment, str(caught.exception))

    def test_blank_lines_are_ignored_and_an_empty_file_is_no_entries(self):
        self.assertEqual(record.parse(""), [])
        self.assertEqual(len(record.parse(f"\na\t1\tsha256:{SHA}\n\n")), 1)


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cache = cache.Cache(self.root / "cache")
        self.ref = refs.full(f"sha256:{SHA}")

    def admit(self, data: bytes = DATA) -> Path:
        temp = self.root / "incoming.part"
        temp.write_bytes(data)
        return self.cache.admit(refs.full(f"sha256:{hashlib.sha256(data).hexdigest()}"), temp)

    def test_an_admitted_object_is_found_read_only_and_with_the_fixed_mtime(self):
        path = self.admit()
        self.assertEqual(self.cache.lookup(self.ref, len(DATA)), path)
        st = os.lstat(path)
        self.assertEqual(st.st_mtime_ns, cache.ADMITTED_MTIME_NS)
        self.assertFalse(st.st_mode & stat.S_IWRITE)
        self.assertEqual(path.read_bytes(), DATA)

    def test_nothing_is_under_the_final_name_until_it_is_admitted(self):
        self.assertIsNone(self.cache.lookup(self.ref))
        self.assertFalse(self.cache.path_of(self.ref).exists())

    def test_a_file_of_another_size_is_dropped(self):
        path = self.admit()
        self.assertIsNone(self.cache.lookup(self.ref, len(DATA) + 1))
        self.assertFalse(path.exists())

    def test_a_file_written_through_is_hashed_again_and_dropped_when_it_is_wrong(self):
        path = self.admit()
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        path.write_bytes(b"X" * len(DATA))          # same size, other bytes, mtime moved
        self.assertIsNone(self.cache.lookup(self.ref, len(DATA)))
        self.assertFalse(path.exists())

    def test_a_file_that_was_touched_but_still_hashes_right_is_served_and_sealed_again(self):
        path = self.admit()
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        os.utime(path, ns=(time.time_ns(), time.time_ns()))         # the mtime moved, the bytes did not
        self.assertEqual(self.cache.lookup(self.ref, len(DATA)), path)
        self.assertEqual(os.lstat(path).st_mtime_ns, cache.ADMITTED_MTIME_NS)

    @unittest.skipIf(os.name == "nt", "hard links of a read-only file need privileges on Windows")
    def test_a_hard_linked_file_is_hashed_again(self):
        path = self.admit()
        os.link(path, self.root / "elsewhere")
        os.chmod(self.root / "elsewhere", stat.S_IWRITE | stat.S_IREAD)
        (self.root / "elsewhere").write_bytes(b"Y" * len(DATA))
        self.assertIsNone(self.cache.lookup(self.ref, len(DATA)))

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_symlink_under_an_object_name_is_never_served(self):
        path = self.cache.path_of(self.ref)
        path.parent.mkdir(parents=True)
        target = self.root / "real"
        target.write_bytes(DATA)
        os.symlink(target, path)
        self.assertIsNone(self.cache.lookup(self.ref))

    def test_eviction_removes_the_least_recently_used_first_and_spares_a_fresh_path(self):
        old = self.admit(b"a" * 100)
        mid = self.admit(b"b" * 100)
        fresh = self.admit(b"c" * 100)
        long_ago = time.time_ns() - 10 * 60 * 10**9
        os.utime(old, ns=(long_ago, cache.ADMITTED_MTIME_NS))
        os.utime(mid, ns=(long_ago + 10**9, cache.ADMITTED_MTIME_NS))
        removed = self.cache.evict(limit=200)
        self.assertEqual(removed, [old])
        self.assertTrue(mid.exists() and fresh.exists())
        removed = self.cache.evict(limit=0)
        self.assertEqual(removed, [mid])            # the fresh one was just handed out: not evicted for a minute
        self.assertTrue(fresh.exists())

    def test_no_limit_evicts_nothing_and_clear_empties_the_cache(self):
        self.admit()
        self.assertEqual(self.cache.evict(), [])
        self.assertEqual(self.cache.clear(), 1)
        self.assertEqual(self.cache.objects(), [])

    def test_the_partial_folder_counts_in_the_size_but_is_not_an_object(self):
        self.admit(b"a" * 10)
        self.cache.partial_dir().mkdir(parents=True)
        (self.cache.partial_dir() / ("f" * 64 + ".part")).write_bytes(b"p" * 25)
        self.assertEqual(len(self.cache.objects()), 1)
        self.assertEqual(self.cache.used(), 35)

    def test_the_cache_is_addressed_by_a_full_reference_only(self):
        with self.assertRaises(refs.RefError):
            self.cache.path_of(refs.parse(f"sha256:{SHA[:8]}"))


class FetchLockTests(unittest.TestCase):
    """The lock that makes one fetch of an object at a time across processes (RESUME.md D4)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = cache.Cache(Path(self._tmp.name) / "cache")

    def holder(self, seconds: float) -> "subprocess.Popen[str]":
        code = "\n".join([
            "import sys, time",
            "from pathlib import Path",
            "from git_remote_seafile.blob import cache",
            "c = cache.Cache(Path(sys.argv[1]))",
            "with c.fetch_lock(sys.argv[2]):",
            "    print('held', flush=True)",
            "    time.sleep(float(sys.argv[3]))",
        ])
        proc = subprocess.Popen([sys.executable, "-c", code, str(self.cache.root), SHA, str(seconds)],
                                stdout=subprocess.PIPE, text=True, cwd=str(Path(__file__).resolve().parent.parent))
        self.addCleanup(lambda: (proc.kill(), proc.wait()))      # the lock file is released before the folder goes
        assert proc.stdout is not None
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_a_second_process_waits_for_the_first_and_then_gets_the_lock(self):
        proc = self.holder(1.0)
        began = time.monotonic()
        with self.cache.fetch_lock(SHA, timeout=20):
            waited = time.monotonic() - began
        self.assertGreater(waited, 0.3)
        proc.wait(timeout=20)

    def test_a_lock_held_too_long_times_out_with_a_message(self):
        self.holder(6.0)
        with self.assertRaises(TimeoutError) as caught:
            with self.cache.fetch_lock(SHA, timeout=0.5):
                pass
        self.assertIn(SHA, str(caught.exception))

    def test_a_killed_holder_releases_the_lock(self):
        proc = self.holder(60.0)
        proc.kill()
        proc.wait(timeout=20)
        with self.cache.fetch_lock(SHA, timeout=10):
            pass

    def test_objects_are_locked_independently(self):
        self.holder(3.0)
        with self.cache.fetch_lock(OTHER, timeout=5):
            pass

    def test_the_sweep_removes_old_partials_and_locks_and_keeps_recent_ones(self):
        folder = self.cache.partial_dir()
        folder.mkdir(parents=True)
        old, fresh, lock = folder / "a.part", folder / "b.part", folder / "c.lock"
        for item in (old, fresh, lock):
            item.write_bytes(b"x")
        long_ago = time.time() - 10 * 24 * 3600
        os.utime(old, (long_ago, long_ago))
        os.utime(lock, (long_ago, long_ago))
        self.assertEqual(self.cache.sweep_partials(), 2)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["b.part"])


if __name__ == "__main__":
    unittest.main()
