"""Opt-in tests against a **real** Seafile server.

Everything else in this suite validates the helper against a stub that encodes
the authors' beliefs about the server.  These tests are the check on those
beliefs.  They are skipped unless ``SEAFILE_TEST_LIBRARY`` names a library you
are happy to have scratch data written to::

    SEAFILE_TEST_LIBRARY=test python -m unittest tests.test_realserver -v

Credentials come from the usual places (the desktop client's ``accounts.db``,
``~/.git-seafile.json``, ``SEAFILE_SERVER`` + ``SEAFILE_TOKEN``).  Every test
works under one unique top-level folder, removed at the end; nothing else in the
library is read or written.

What they pin down is what the stub *cannot* predict:

* the API semantics the stub copies (``DELETE /file/`` removes directories,
  ``mkdir`` of an existing directory succeeds, a missing-file delete is 200);
* a full push / clone / fetch / delete / tail-merge / gc round trip, SHA-1 and
  SHA-256;
* the lock under real latency and real processes;
* the result that matters most: **concurrent same-path uploads silently produce
  ``(SFConflict ...)`` copies**, which is the reason the lock exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

_LIBRARY = os.environ.get("SEAFILE_TEST_LIBRARY", "").strip()


@unittest.skipUnless(_LIBRARY, "set SEAFILE_TEST_LIBRARY to a scratch library to run these")
class RealServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from e2e_harness import make_helper_env
        from git_remote_seafile.client import SeafileClient

        cls.client = SeafileClient()  # server and token from the usual sources
        cls.rid = cls.client.get_repo_id(_LIBRARY)
        cls.base = "grs-realserver-" + time.strftime("%Y%m%d%H%M%S") + f"-{os.getpid()}"
        cls.tmp = Path(tempfile.mkdtemp(prefix="grs-real-"))
        env = make_helper_env(cls.tmp / "bin", cls.client.server_url, _ROOT)
        for key in ("SEAFILE_SERVER", "SEAFILE_TOKEN", "SEAFILE_TEST_LOCK_SETTLE",
                    "SEAFILE_SKIP_SAFETY_CHECKS"):
            env.pop(key, None)
        # The harness points the helper at a stub; here it must find the real
        # server the same way a user's would.
        cls.env = env

    @classmethod
    def tearDownClass(cls):
        url = (
            f"{cls.client.server_url}/api2/repos/{cls.rid}/file/"
            f"?p={quote('/' + cls.base, safe='/')}"
        )
        try:
            cls.client.session.delete(url, timeout=120)
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers --------------------------------------------------------------
    def _git(self, args, cwd, check=True):
        p = subprocess.run(["git", *args], cwd=str(cwd), env=self.env,
                           capture_output=True, text=True, timeout=600)
        if check and p.returncode != 0:
            self.fail(f"git {' '.join(args)} rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def _repo(self, name, fmt="sha1"):
        path = self.tmp / name
        path.mkdir()
        self._git(["init", "-q", "-b", "main", f"--object-format={fmt}", "."], path)
        self._git(["config", "user.email", "t@t"], path)
        self._git(["config", "user.name", "T"], path)
        self._git(["config", "core.autocrlf", "false"], path)
        return path

    def _commit(self, repo, name, content, message):
        (repo / name).write_text(content)
        self._git(["add", "--", name], repo)
        self._git(["commit", "-q", "-m", message], repo)

    def _url(self, name):
        return f"seafile://{_LIBRARY}/{self.base}/{name}"

    def _api(self, method, kind, path, **kw):
        url = (f"{self.client.server_url}/api2/repos/{self.rid}/{kind}/"
               f"?p={quote(path, safe='/')}")
        return self.client.session.request(method, url, timeout=60, **kw)

    # -- API semantics the stub copies -----------------------------------------
    def test_api_semantics_the_stub_encodes(self):
        base = f"/{self.base}/api"
        self.client.mkdir_p(self.rid, base + "/a/b")
        self.assertEqual(
            self._api("POST", "dir", base + "/a/b", data={"operation": "mkdir"}).status_code, 201,
            "mkdir of an existing directory",
        )
        self.assertEqual(self._api("GET", "dir", base + "/missing").status_code, 404)

        self.client.upload_file(self.rid, base + "/a/b", "f.txt", b"x", replace=True)
        entry = self.client.list_dir(self.rid, base + "/a/b")[0]
        self.assertEqual((entry["name"], entry["type"], entry["size"]), ("f.txt", "file", 1))
        self.assertIsInstance(entry["mtime"], int)

        # /dir/ cannot delete a file; /file/ deletes files *and* directories.
        self.assertEqual(self._api("DELETE", "dir", base + "/a/b/f.txt").status_code, 404)
        self.assertEqual(self._api("DELETE", "file", base + "/a").status_code, 200)
        self.assertEqual(self.client.list_dir(self.rid, base + "/a"), [])
        # ... and a delete of something that is not there still answers 200,
        # which is why the helper reads a ref before deleting it.
        self.assertEqual(self._api("DELETE", "file", base + "/a/b/f.txt").status_code, 200)

    # -- the round trip ----------------------------------------------------------
    def _round_trip(self, name, fmt):
        url = self._url(name)
        src = self._repo(f"src_{name}", fmt)
        self._git(["config", "seafile.autogc", "true"], src)
        self._git(["config", "seafile.gcthreshold", "3"], src)
        self._git(["remote", "add", "origin", url], src)
        for n in range(4):
            self._commit(src, f"f{n}.txt", f"{n}\n" * 40, f"c{n}")
            self._git(["push", "-q", "origin", "main"], src)

        dst = self.tmp / f"dst_{name}"
        self._git(["clone", "-q", url, str(dst)], self.tmp)
        self.assertEqual(self._git(["rev-list", "--count", "HEAD"], dst).stdout.strip(), "4")
        self.assertEqual(self._git(["fsck", "--no-dangling"], dst, check=False).returncode, 0)
        self.assertEqual(
            self._git(["rev-parse", "--show-object-format"], dst).stdout.strip(), fmt
        )

        # nested branch: create, list, delete, and the empty parent is pruned
        self._git(["checkout", "-q", "-b", "feature/x"], src)
        self._commit(src, "x.txt", "x\n", "fx")
        self._git(["push", "-q", "origin", "feature/x"], src)
        refs = [l.split("\t")[1] for l in self._git(["ls-remote", url], self.tmp).stdout.splitlines()]
        self.assertIn("refs/heads/feature/x", refs)
        self._git(["push", "-q", "origin", ":feature/x"], src)
        names = [e["name"] for e in self.client.list_dir(self.rid, f"/{self.base}/{name}/refs/heads")]
        self.assertEqual(names, ["main"])

        # fetch picks up a new commit from another clone
        self._git(["checkout", "-q", "main"], src)
        self._commit(src, "late2.txt", "late\n", "late2")
        self._git(["push", "-q", "origin", "main"], src)
        self._git(["pull", "-q", "--ff-only", "origin", "main"], dst)
        self.assertEqual(self._git(["rev-list", "--count", "HEAD"], dst).stdout.strip(), "5")

    def test_round_trip_sha1(self):
        self._round_trip("rt_sha1", "sha1")

    def test_round_trip_sha256(self):
        self._round_trip("rt_sha256", "sha256")

    # -- concurrency ----------------------------------------------------------------
    def test_concurrent_pushers_through_the_lock_leave_no_conflict_copies(self):
        url = self._url("fan")
        seed = self._repo("fan_seed")
        self._commit(seed, "a.txt", "seed\n", "seed")
        self._git(["remote", "add", "origin", url], seed)
        self._git(["push", "-q", "origin", "main"], seed)

        n = 5
        clones = []
        for i in range(n):
            d = self.tmp / f"fan_{i}"
            self._git(["clone", "-q", url, str(d)], self.tmp)
            self._git(["config", "user.email", "t@t"], d)
            self._git(["config", "user.name", "T"], d)
            self._git(["checkout", "-q", "-b", f"b{i}"], d)
            self._commit(d, f"n{i}.txt", f"{i}\n", f"b{i}")
            clones.append(d)

        codes: dict[int, int] = {}

        def push(i, d):
            p = subprocess.run(["git", "push", "origin", f"b{i}:b{i}"], cwd=str(d), env=self.env,
                               capture_output=True, text=True, timeout=600)
            codes[i] = p.returncode

        threads = [threading.Thread(target=push, args=(i, d)) for i, d in enumerate(clones)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes.values()), [0] * n, codes)
        heads = [e["name"] for e in self.client.list_dir(self.rid, f"/{self.base}/fan/refs/heads")]
        self.assertEqual(sorted(heads), sorted(["main"] + [f"b{i}" for i in range(n)]))
        self.assertFalse([h for h in heads if "SFConflict" in h], heads)
        self.assertEqual(self.client.list_dir(self.rid, f"/{self.base}/fan/.git-lock.d"), [])

    def test_concurrent_same_path_uploads_report_success_and_leave_conflict_copies(self):
        """The reason the lock exists, observed rather than assumed."""
        from git_remote_seafile.client import SeafileClient

        base = f"/{self.base}/race"
        self.client.mkdir_p(self.rid, base)
        results: dict[int, str] = {}

        def writer(i):
            try:
                SeafileClient(server_url=self.client.server_url).upload_file(
                    self.rid, base, "ref", f"writer-{i}\n".encode(), replace=True
                )
                results[i] = "ok"
            except Exception as ex:  # noqa: BLE001
                results[i] = f"ERR {ex}"

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(set(results.values()), {"ok"}, results)
        names = [e["name"] for e in self.client.list_dir(self.rid, base)]
        self.assertIn("ref", names)
        copies = [n for n in names if "SFConflict" in n]
        self.assertTrue(
            copies,
            "expected Seafile to keep conflict copies of simultaneous writes; if this server no "
            "longer does, the lock's reason to exist has changed -- update DESIGN.md section 3.3",
        )
        final = self.client.get_file_bytes(self.rid, base + "/ref")
        self.assertTrue(final.startswith(b"writer-"))

    def test_conflict_copies_of_a_dotted_ref_name_are_recognised(self):
        """Measured: Seafile names the loser ``<name> (SFConflict <user> <date>).<last ext>``.

        0.9.1 listed "the conflict-copy warning missed refs with a dot" as fixed
        on the strength of two reviews' inference; this is the measurement, and
        it is why ``conflict_copies_of`` has to match whatever the server does.
        """
        from git_remote_seafile.client import SeafileClient
        from git_remote_seafile.refs import conflict_copies_of

        base = f"/{self.base}/dotted"
        self.client.mkdir_p(self.rid, base)

        def writer(i):
            SeafileClient(server_url=self.client.server_url).upload_file(
                self.rid, base, "v1.0.0", f"w{i}\n".encode(), replace=True
            )

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = self.client.list_dir(self.rid, base)
        copies = [e["name"] for e in entries if "SFConflict" in e["name"]]
        self.assertTrue(copies, "no conflict copies were produced")
        self.assertEqual(sorted(conflict_copies_of(entries, "v1.0.0")), sorted(copies))

    def test_the_snapshot_tools_link_probe_measures_and_leaves_nothing_behind(self):
        """``probe_link`` uploads four 8 MiB files (one, then three at once) and deletes them.

        The stub cannot say what a real upload costs or whether a delete leaves a
        trace.  Measured here: the rates are positive and in the neighbourhood of
        what DESIGN/PAYLOAD record (one stream ~32 MiB/s, three ~53 MiB/s on the
        authors' server -- a different link may differ), the store is reported as
        not in use, and the probe's directory listing is empty afterwards.
        """
        sys.path.insert(0, str(_ROOT / "tools"))
        import seafile_snapshot as snap

        remote = self._url("probe-vault")
        result = snap.probe_link(remote)
        self.assertIsNotNone(result, "the probe could not run against the real server")
        self.assertFalse(result["in_use"])
        self.assertGreater(result["single"], 0)
        self.assertGreater(result["upload"], 0)
        left = self.client.list_dir(self.rid, f"/{self.base}/probe-vault/payload/objects")
        self.assertEqual([e["name"] for e in left], [], "the probe left files behind")
        print(f"\nprobe: one stream {result['single']:.1f} MiB/s, "
              f"{snap._PROBE_STREAMS} at once {result['upload']:.1f} MiB/s -> "
              f"cut-off {snap.tuned_ratio(48.0, result['upload'], result['single']):.2f} "
              "at 48 MiB/s deflate")

        # a store that already holds an object is reported as in use, uploading nothing
        self.client.mkdir_p(self.rid, f"/{self.base}/probe-vault/payload/objects/ab")
        again = snap.probe_link(remote)
        self.assertEqual(again, {"in_use": True})


if __name__ == "__main__":
    unittest.main()
