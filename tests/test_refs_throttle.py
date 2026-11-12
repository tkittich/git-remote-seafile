"""AA23: a ref walk backs off when the server answers 429, and still returns every ref.

The server throttles a user's total request rate (measured on CE 13.0.29: ~140 req/s refused for
10 s).  The session retries a 429; the walk must also ask for less, or a few thousand refs at
8 workers keep tripping it.  The stub's ``throttle`` fault answers that many GETs 429.
"""

from __future__ import annotations

import concurrent.futures
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, SeafileStub  # noqa: E402

from git_remote_seafile import refs as refs_module  # noqa: E402
from git_remote_seafile.client import SeafileClient, throttle_answers  # noqa: E402
from git_remote_seafile.refs import iter_refs, next_worker_count  # noqa: E402


class TestNextWorkerCount(unittest.TestCase):
    def test_a_throttled_batch_halves_and_never_reaches_zero(self):
        self.assertEqual(next_worker_count(8, 8, True), 4)
        self.assertEqual(next_worker_count(1, 8, True), 1)

    def test_a_clean_batch_adds_one_up_to_the_ceiling(self):
        self.assertEqual(next_worker_count(4, 8, False), 5)
        self.assertEqual(next_worker_count(8, 8, False), 8)


class TestAWalkUnderAThrottle(unittest.TestCase):
    def setUp(self):
        self.stub = SeafileStub().start()
        self.addCleanup(self.stub.stop)
        patcher = patch("urllib3.util.retry.time.sleep")   # the retry backoff, not under test
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = SeafileClient(server_url=self.stub.base_url, token="t")
        self.addCleanup(self.client.close)
        self.names = [f"b{i:02d}" for i in range(40)]
        for name in self.names:
            self.stub.files.setdefault(REPO_ID, {})[f"/r/refs/heads/{name}"] = f"{name}\n".encode()

    def _workers_used(self, **kwargs):
        used: list[int] = []
        real = concurrent.futures.ThreadPoolExecutor

        def recording(max_workers=None, **kw):
            used.append(max_workers)
            return real(max_workers=max_workers, **kw)

        with patch.object(refs_module.concurrent.futures, "ThreadPoolExecutor", recording):
            got = dict(iter_refs(self.client, REPO_ID, "/r", "refs/heads", 2, **kwargs))
        return got, used

    def test_every_ref_is_returned_and_the_next_batch_uses_fewer_workers(self):
        self.stub.faults["throttle"] = 3    # in the first batch; fewer than the 4 tries one request gets
        before = throttle_answers()
        got, used = self._workers_used()
        self.assertEqual(sorted(got), [f"refs/heads/{n}" for n in self.names])
        self.assertGreater(throttle_answers(), before, "the 429 was not counted")
        self.assertEqual(used[:3], [2, 1, 2], used)

    def test_an_unthrottled_walk_keeps_its_workers(self):
        _, used = self._workers_used()
        self.assertEqual(set(used), {2}, used)


if __name__ == "__main__":
    unittest.main()
