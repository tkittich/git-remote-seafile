"""The search for how much parallelism a link can use (``68_climb.py``).

Adaptation is a design rule (DESIGN.md 3.5): what depends on the machine or the
link is measured during the run.  These tests hold the *measuring* to account:
the controller takes its readings as arguments, so each test describes a link
as a function from "parallel uploads" to "MiB/s" and watches what is chosen.
The curve called ``MEASURED`` is the real server's, from 2026-11-05.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import seafile_snapshot as snap  # noqa: E402

try:
    from _helpers import remove_tree  # noqa: E402
except ImportError:  # run as ``python -m unittest tests.<module>``
    from tests._helpers import remove_tree  # noqa: E402

MIB = 1024 * 1024

#: MiB/s by parallel uploads on the test server (32 MiB files): a shelf at 3-4,
#: a rise at 8, a plateau after.  Values between the measured points are
#: interpolated on the shelf so that doubling from 3 lands on a shelf too.
MEASURED = {1: 27, 2: 40, 3: 44, 4: 45, 6: 47, 8: 62, 12: 65, 16: 66}


def _curve(table):
    levels = sorted(table)

    def rate(level: int) -> float:
        below = [n for n in levels if n <= level]
        return float(table[below[-1]])

    return rate


def _drive(mib_per_s, ceiling=16, start=3, seconds=2000):
    """Run the climber against a link described by *mib_per_s*; no clock, no network."""
    climber = snap._Climber(ceiling, start)
    window = snap._RateWindow()
    now, total = 0.0, 0
    window.reset(now)
    while not climber.settled and now < seconds:
        now += 1.0
        total += int(mib_per_s(climber.level) * MIB)
        reading = window.sample(now, total, saturated=True)
        if reading is not None:
            climber.result(reading)
            window.reset(now)
    return climber


class ClimberTests(unittest.TestCase):
    def test_the_real_servers_shelf_is_crossed_and_twelve_is_chosen(self):
        climber = _drive(_curve(MEASURED))
        self.assertEqual(climber.level, 12)
        self.assertFalse(climber.gave_up)

    def test_without_patience_the_same_server_stops_on_the_shelf(self):
        """The guard test: with one miss allowed, 3 -> 6 gains 7% and the search ends at 3,
        leaving a third of the link unused.  Patience is what makes this test pass."""
        with unittest.mock.patch.object(snap, "_RAMP_PATIENCE", 1):
            climber = _drive(_curve(MEASURED))
        self.assertEqual(climber.level, 3)

    def test_a_link_one_stream_cannot_fill_gets_the_ceiling(self):
        climber = _drive(lambda n: 5.0 * n)        # a long fat pipe: each stream is slow
        self.assertEqual(climber.level, 16)

    def test_a_link_one_stream_fills_stays_at_the_first_guess(self):
        climber = _drive(lambda n: 30.0)
        self.assertEqual(climber.level, 3)

    def test_a_link_that_saturates_at_three_does_not_grow_past_it(self):
        climber = _drive(lambda n: min(n, 3) * 20.0)
        self.assertEqual(climber.level, 3)

    def test_the_search_ends_on_the_best_level_seen_not_the_last_tried(self):
        climber = _drive(lambda n: {3: 40.0, 6: 90.0, 12: 60.0, 16: 50.0}.get(n, 40.0))
        self.assertEqual(climber.level, 6)
        self.assertEqual(sorted(climber.rates), [3, 6, 12, 16])

    def test_a_push_back_halves_the_level_once_per_episode(self):
        climber = snap._Climber(16, 8)
        self.assertTrue(climber.throttled(100.0))
        self.assertEqual((climber.level, climber.ceiling, climber.settled), (4, 4, True))
        self.assertFalse(climber.throttled(101.0), "the same episode must not halve again")
        self.assertEqual(climber.level, 4)
        self.assertTrue(climber.throttled(120.0))
        self.assertEqual(climber.level, 2)

    def test_one_writer_is_never_throttled_below_one(self):
        climber = snap._Climber(16, 1)
        self.assertFalse(climber.throttled(0.0))
        self.assertEqual(climber.level, 1)

    def test_a_window_taken_with_empty_slots_is_not_a_reading(self):
        window = snap._RateWindow()
        window.reset(0.0)
        self.assertIsNone(window.sample(5.0, 10 * MIB, saturated=True))     # arms
        self.assertIsNone(window.sample(9.0, 20 * MIB, saturated=False))    # the tail: starts over
        self.assertIsNone(window.sample(30.0, 90 * MIB, saturated=True))    # arms again
        self.assertAlmostEqual(window.sample(41.0, 150 * MIB, saturated=True), 60 * MIB / 11)

    def test_the_summary_line_says_whether_the_search_finished(self):
        climber = snap._Climber(16, 3)
        climber.result(55 * MIB)
        climber.result(71 * MIB)
        self.assertIn("finished while still measuring, at 12", snap.climb_line(climber, "upload"))
        climber.result(82 * MIB)
        climber.result(91 * MIB)
        line = snap.climb_line(climber, "upload")
        self.assertIn("settled on 12 parallel upload(s)", line)
        self.assertIn("3: 55, 6: 71, 12: 82, 16: 91", line)

    def test_a_typed_count_is_exact_and_never_searched(self):
        climber = snap._Climber(8, 1, forced=True)
        self.assertEqual((climber.level, climber.settled), (8, True))

    def test_throttle_statuses_are_recognised_and_others_are_not(self):
        self.assertEqual(snap.throttle_status("x: HTTP 502 (POST https://s) Bad Gateway"), "502")
        self.assertEqual(snap.throttle_status("HTTP 429 slow down"), "429")
        self.assertIsNone(snap.throttle_status("HTTP 413 too large"))
        self.assertIsNone(snap.throttle_status("HTTP 5020 odd"))


class _FakeServer:
    """Just enough of the client for ``_upload_all``; thread-safe, with an optional limit.

    ``limit`` is how many uploads may run at once before the server answers 502.
    """

    def __init__(self, limit: int | None = None) -> None:
        self.files: dict[str, bytes] = {}
        self.limit = limit
        self.active = 0
        self.peak = 0
        self.pushed_back = 0
        self._lock = threading.Lock()

    def mkdir_p(self, rid, path):
        return True

    def upload_file(self, rid, directory, name, reader, **_):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            refuse = self.limit is not None and self.active > self.limit
            if refuse:
                self.pushed_back += 1
        try:
            if refuse:
                raise RuntimeError(f"Failed to upload {name}: HTTP 502 (POST https://s) Bad Gateway")
            body = b""
            while True:
                block = reader.read(64 * 1024)
                if not block:
                    break
                body += block
            time.sleep(0.02)
            with self._lock:
                self.files[f"{directory}/{name}"] = body
            return True
        finally:
            with self._lock:
                self.active -= 1

    def rename_entry(self, rid, path, new_name):
        directory = path.rsplit("/", 1)[0]
        with self._lock:
            self.files[f"{directory}/{new_name}"] = self.files.pop(path)
        return True

    def list_dir(self, rid, directory):
        with self._lock:
            return [
                {"name": p.rsplit("/", 1)[1], "size": len(b), "type": "file"}
                for p, b in self.files.items() if p.rsplit("/", 1)[0] == directory
            ]

    def delete_entry(self, rid, path):
        with self._lock:
            self.files.pop(path, None)
        return True


class AdaptiveUploadTests(unittest.TestCase):
    """``_upload_all`` with ``jobs=None`` against a fake server."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-climb-"))
        self.addCleanup(remove_tree, self.tmp)
        self.todo: dict[str, dict] = {}
        for i in range(24):
            data = f"object {i}\n".encode() * 200
            path = self.tmp / f"o{i:02d}.bin"
            path.write_bytes(data)
            sha = hashlib.sha256(data).hexdigest()
            self.todo[sha] = {"path": path.name, "size": len(data),
                              "mtime_ns": path.stat().st_mtime_ns}
        for name, value in (("_CLIMB_SETTLE_SECONDS", 0.0), ("_CLIMB_WINDOW_SECONDS", 0.05),
                            ("_THROTTLE_PAUSE_SECONDS", 0.05), ("_UPLOAD_START_JOBS", 4)):
            patcher = unittest.mock.patch.object(snap, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _upload(self, server: _FakeServer, jobs):
        out = io.StringIO()
        with unittest.mock.patch.object(snap, "_open_client", return_value=server):
            with contextlib.redirect_stdout(out):
                snap._upload_all(server, "rid", None, "base", self.tmp, self.todo, jobs)
        return out.getvalue()

    def _stored(self, server: _FakeServer) -> set[str]:
        return {p.rsplit("/", 1)[1] for p in server.files if "/objects/" in p
                and ".part-" not in p}

    def test_every_object_lands_under_its_hash_and_the_run_says_it_is_measuring(self):
        server = _FakeServer()
        said = self._upload(server, None)
        self.assertEqual(self._stored(server), set(self.todo))
        self.assertIn("found by measuring", said)

    def test_a_server_that_pushes_back_costs_parallelism_not_the_run(self):
        server = _FakeServer(limit=2)
        said = self._upload(server, None)
        self.assertEqual(self._stored(server), set(self.todo), "an object was lost to a 502")
        self.assertGreater(server.pushed_back, 0, "the fake never pushed back; the test is idle")
        self.assertIn("the server pushed back", said)
        self.assertIn("limiting this run to", said)

    def test_a_typed_count_is_used_exactly_and_nothing_is_measured(self):
        server = _FakeServer()
        said = self._upload(server, 2)
        self.assertEqual(self._stored(server), set(self.todo))
        self.assertLessEqual(server.peak, 2)
        self.assertNotIn("found by measuring", said)

    def test_a_push_back_that_never_ends_is_a_failure_after_the_tries_are_spent(self):
        server = _FakeServer(limit=0)
        with self.assertRaises(snap.SnapshotError) as ctx:
            self._upload(server, None)
        self.assertIn("payload upload(s) failed", str(ctx.exception))

    def test_uploads_that_finish_often_still_give_the_search_a_reading(self):
        """The first real run took no reading at all: every completion looked like an idle tail.

        Uploads here finish every few hundredths of a second while a window needs
        a third of a second, which is what 32 MiB files do against a window of
        seconds.
        """
        meter = snap._ByteMeter()

        def work(_item):
            time.sleep(0.03)
            meter.add(MIB)
            return None

        with unittest.mock.patch.object(snap, "_CLIMB_SETTLE_SECONDS", 0.15), \
                unittest.mock.patch.object(snap, "_CLIMB_WINDOW_SECONDS", 0.3):
            with contextlib.redirect_stdout(io.StringIO()):
                climber = snap.run_climbing(
                    [str(i) for i in range(150)], work, ceiling=4, meter=meter,
                    exhausted=lambda *_: None, start=1, poll=0.02,
                )
        self.assertGreaterEqual(len(climber.rates), 2, "no throughput reading was ever taken")

    def test_the_hashing_reader_counts_the_bytes_it_hands_out(self):
        meter = snap._ByteMeter()
        reader = snap._HashingFile(io.BytesIO(b"x" * 1000), 1000, meter)
        while reader.read(300):
            pass
        self.assertEqual(meter.total, 1000)



class ParallelHashingTests(unittest.TestCase):
    """Hashing the payload files several at a time (``67_hashing.py``)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-hash-"))
        self.addCleanup(remove_tree, self.tmp)
        for i in range(24):
            (self.tmp / f"p{i:02d}.bin").write_bytes(bytes([i]) * (1000 + 37 * (i % 5)))
        patcher = unittest.mock.patch.object(snap, "_HASH_MIN_BYTES", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _candidates(self):
        out = {}
        for path in sorted(self.tmp.glob("*.bin")):
            st = path.lstat()
            out[path.name] = (st.st_size, st.st_mtime_ns, "100644", st.st_ino, st.st_dev,
                              st.st_ctime_ns)
        return out

    def _build(self, workers: str):
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"SEAFILE_SNAPSHOT_HASH_WORKERS": workers}):
            with contextlib.redirect_stdout(out):
                manifest, fresh = snap.build_manifest(self._candidates(), self.tmp, {}, 1)
        return manifest, fresh, out.getvalue()

    def test_the_manifest_is_the_same_whatever_the_parallelism(self):
        serial, fresh_serial, said = self._build("1")
        self.assertNotIn("parallel hashers", said)
        parallel, fresh_parallel, said = self._build("4")
        self.assertIn("4 parallel hashers", said)
        self.assertEqual(parallel, serial)
        for entry in parallel["files"]:       # against hashlib itself, not against the other mode
            expected = hashlib.sha256((self.tmp / entry["path"]).read_bytes()).hexdigest()
            self.assertEqual(entry["sha256"], expected, entry["path"])
        self.assertEqual([f["path"] for f in parallel["files"]], sorted(f["path"] for f in parallel["files"]))
        strip = lambda cache: {k: {a: b for a, b in v.items() if a != "at"} for k, v in cache.items()}  # noqa: E731
        self.assertEqual(strip(fresh_parallel), strip(fresh_serial))

    def test_the_hash_cache_entries_carry_the_same_fields_either_way(self):
        _, fresh, _ = self._build("4")
        entry = fresh["p00.bin"]
        self.assertEqual(set(entry), {"size", "mtime_ns", "ino", "sha256", "at", "ctime", "dev"})

    def test_an_unreadable_file_stops_the_run_naming_it_in_both_modes(self):
        victim = self.tmp / "p07.bin"
        cands = self._candidates()
        victim.unlink()
        for workers in ("1", "4"):
            with unittest.mock.patch.dict(os.environ, {"SEAFILE_SNAPSHOT_HASH_WORKERS": workers}):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(snap.SnapshotError) as ctx:
                        snap.build_manifest(cands, self.tmp, {}, 1)
            self.assertIn("p07.bin", str(ctx.exception), workers)

    def test_a_set_cancel_stops_a_read_between_blocks(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(snap.SnapshotError) as ctx:
            snap._sha256_file(self.tmp / "p00.bin", None, stop)
        self.assertIn("was stopped", str(ctx.exception))

    def test_a_failure_calls_abort_before_waiting_for_the_workers(self):
        order = []

        def work(item):
            if item == "bad":
                raise snap.SnapshotError("boom")
            time.sleep(0.05)
            return None

        with self.assertRaises(snap.SnapshotError):
            snap.run_climbing(["bad", "x", "y"], work, ceiling=3, meter=snap._ByteMeter(),
                              exhausted=lambda *_: None, start=3, say=lambda _l: None,
                              abort=lambda: order.append("abort"))
        self.assertEqual(order, ["abort"])

    def _run(self, work, n=300, workers=None, cpus=8):
        sizes = {str(i): MIB for i in range(n)}
        said = []
        env = {"SEAFILE_SNAPSHOT_HASH_WORKERS": workers or ""}
        with unittest.mock.patch.dict(os.environ, env),                 unittest.mock.patch.object(snap.os, "cpu_count", return_value=cpus),                 unittest.mock.patch.object(snap, "_HASH_SETTLE_SECONDS", 0.05),                 unittest.mock.patch.object(snap, "_HASH_WINDOW_SECONDS", 0.12),                 contextlib.redirect_stdout(io.StringIO()) as out:
            meter = snap._ByteMeter()
            snap.run_hashing([str(i) for i in range(n)], sizes, lambda r: work(r, meter), meter,
                             threading.Event())
            said = out.getvalue()
        return said

    def test_a_typed_count_is_used_exactly_and_every_file_is_hashed_once(self):
        seen, live, peak, lock = [], [0], [0], threading.Lock()

        def work(rel, meter):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                seen.append(rel)
            time.sleep(0.01)
            meter.add(MIB)
            with lock:
                live[0] -= 1

        said = self._run(work, n=60, workers="3")
        self.assertEqual(sorted(seen), sorted(str(i) for i in range(60)))
        self.assertEqual(len(seen), 60)
        self.assertLessEqual(peak[0], 3)
        self.assertGreaterEqual(peak[0], 2)
        self.assertIn("3 parallel hashers", said)
        self.assertNotIn("found by measuring", said)

    def test_a_disk_that_serialises_every_reader_is_left_at_one(self):
        """The spinning-disk case: more readers only queue behind one another."""
        disk = threading.Lock()

        def work(rel, meter):
            with disk:
                time.sleep(0.01)
                meter.add(MIB)

        # Wall-clock windows of 0.12 s: on a loaded CI runner one window can read 15% better
        # than its neighbour by chance (it failed on two macOS legs).  The rule is that a
        # step up needs a margin, so a disk that gives no gain settles at one in at least
        # one of three tries; a rule that accepted noise would not.
        said = ""
        for _attempt in range(3):
            said = self._run(work, n=200)
            if "settled on 1 parallel hasher" in said:
                break
        self.assertIn("settled on 1 parallel hasher", said)

    def test_a_disk_that_scales_gets_more_readers(self):
        def work(rel, meter):
            time.sleep(0.01)
            meter.add(MIB)

        said = self._run(work, n=400)
        self.assertIn("found by measuring", said)
        self.assertNotIn("settled on 1 parallel hasher", said)

    def test_a_small_set_is_hashed_serially_in_the_order_given(self):
        order = []
        with unittest.mock.patch.object(snap, "_HASH_MIN_BYTES", 10 ** 9):
            meter = snap._ByteMeter()
            snap.run_hashing(["c", "a", "b"], {"a": 1, "b": 1, "c": 1},
                             lambda rel: order.append(rel), meter, threading.Event())
        self.assertEqual(order, ["c", "a", "b"])

    def test_the_stage_and_hash_settings_share_one_bad_value_warning(self):
        for name, fn in (("SEAFILE_SNAPSHOT_HASH_WORKERS", snap.hash_workers),
                         ("SEAFILE_SNAPSHOT_STAGE_WORKERS", snap.prewrite_workers)):
            err = io.StringIO()
            with unittest.mock.patch.dict(os.environ, {name: "lots"}),                     contextlib.redirect_stderr(err):
                count, typed = fn()
            self.assertFalse(typed)
            self.assertGreaterEqual(count, 1)
            self.assertIn(name, err.getvalue())


if __name__ == "__main__":
    unittest.main()
