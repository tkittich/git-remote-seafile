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
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

try:
    from _helpers import remove_tree  # noqa: E402
except ImportError:  # run as ``python -m unittest tests.<module>``
    from tests._helpers import remove_tree  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

_LIBRARY = os.environ.get("SEAFILE_TEST_LIBRARY", "").strip()


def _helper_env(tmp: Path, server_url: str, base: str) -> dict:
    """The environment for the helper processes a test starts: the same server as ``self.client``.

    The harness points the helper at a stub (``SEAFILE_SERVER``/``SEAFILE_TOKEN``), which is
    dropped here so the helper finds the real server the way a user's would.  When the run
    itself was given ``SEAFILE_SERVER``/``SEAFILE_TOKEN``, those are what the in-process client
    used, so they are passed on: dropping them sent the git-driven tests to whatever the
    config file or the desktop client's ``accounts.db`` names -- a different server from the
    one the test reads back from and cleans up (2026-11-06, caught before any push).
    """
    from e2e_harness import make_helper_env

    env = make_helper_env(tmp / "bin", server_url, _ROOT)
    for key in ("SEAFILE_SERVER", "SEAFILE_TOKEN", "SEAFILE_TEST_LOCK_SETTLE",
                "SEAFILE_SKIP_SAFETY_CHECKS"):
        env.pop(key, None)
    for key in ("SEAFILE_SERVER", "SEAFILE_TOKEN"):
        if os.environ.get(key, "").strip():
            env[key] = os.environ[key]
    _require_the_same_server(env, tmp, server_url)
    return env


def _require_the_same_server(env: dict, tmp: Path, server_url: str) -> None:
    """Stop the class before any push unless a helper process finds ``server_url`` too.

    ``check-auth`` names the server the helper authenticated with and writes nothing.  The
    guard used ``doctor``, which uploads and deletes a probe -- on whichever server the helper
    found, which is the wrong one exactly when this guard fires (review 4f1cdd4, glm-5.3 G4.1).
    """
    proc = subprocess.run(
        ["git-remote-seafile", "check-auth"],
        cwd=str(tmp), env=env, capture_output=True, text=True, timeout=300,
    )
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    first = next((line for line in lines if "Authenticated successfully with" in line), "")
    if f"Authenticated successfully with {server_url.rstrip('/')} as" not in first:
        raise RuntimeError(
            f"refusing to run: the helper processes would log in elsewhere than {server_url} "
            f"({first or (lines[0] if lines else 'doctor printed nothing')!r}); pass the same "
            "SEAFILE_SERVER and SEAFILE_TOKEN to both, or put them in the config file"
        )


def _server_info(client) -> dict:
    """``/api2/server-info/`` as a dict (``{}`` for anything else)."""
    info = client.session.get(f"{client.server_url}/api2/server-info/", timeout=60).json()
    return info if isinstance(info, dict) else {}


def _server_is_pro(client) -> bool:
    """Whether the server is Seafile Professional, from its own features list.

    A module function, not a method: ``SharedUserTests`` derives from the fixture,
    not from ``RealServerTests``, and a method on the latter is not there when the
    former's ``setUpClass`` runs -- which only a real-server run shows, since the
    tier skips everywhere else (the second realserver.yml run, both legs).
    """
    return "seafile-pro" in (_server_info(client).get("features") or [])


class EditionProbeTests(unittest.TestCase):
    """Runs without a server, so a broken probe shows before a dispatch does."""

    def _client(self, payload):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.server_url = "https://seafile.example.com"
        client.session.get.return_value.json.return_value = payload
        return client

    def test_the_features_list_decides(self):
        self.assertTrue(_server_is_pro(self._client({"features": ["seafile-basic", "seafile-pro"]})))
        self.assertFalse(_server_is_pro(self._client({"features": ["seafile-basic"]})))
        self.assertFalse(_server_is_pro(self._client({})))
        self.assertFalse(_server_is_pro(self._client(["not", "a", "dict"])))

    def test_every_class_method_a_setup_calls_exists(self):
        # The slip this guards: setUpClass reached for a method of another class,
        # which nothing but a real-server run executes.  Every ``cls._name`` a
        # setUpClass/tearDownClass reads must be on the class, or assigned there.
        import ast
        import inspect
        import textwrap

        for cls in (RealServerTests, SharedUserTests, LibraryCreationTests, ProxyBodyLimitTests):
            for name in ("setUpClass", "tearDownClass"):
                if name not in vars(cls):
                    continue
                tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(cls, name))))
                assigned = {
                    t.attr for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Attribute)
                }
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                            and node.value.id == "cls" and node.attr.startswith("_")
                            and isinstance(node.ctx, ast.Load) and node.attr not in assigned):
                        self.assertTrue(hasattr(cls, node.attr),
                                        f"{cls.__name__}.{name} uses cls.{node.attr}, "
                                        "which the class does not have")


@unittest.skipUnless(_LIBRARY, "set SEAFILE_TEST_LIBRARY to a scratch library to run these")
class RealServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from git_remote_seafile.client import SeafileClient

        cls.client = SeafileClient()  # server and token from the usual sources
        cls.rid = cls.client.get_repo_id(_LIBRARY)
        cls.base = "grs-realserver-" + time.strftime("%Y%m%d%H%M%S") + f"-{os.getpid()}"
        cls.tmp = Path(tempfile.mkdtemp(prefix="grs-real-"))
        try:
            cls.env = _helper_env(cls.tmp, cls.client.server_url, cls.base)
        except BaseException:
            # unittest does not call tearDownClass when setUpClass raises.
            remove_tree(cls.tmp)
            raise

    @classmethod
    def tearDownClass(cls):
        url = (
            f"{cls.client.server_url}/api2/repos/{cls.rid}/file/"
            f"?p={quote('/' + cls.base, safe='/')}"
        )
        try:
            cls.client.session.delete(url, timeout=120)
        finally:
            remove_tree(cls.tmp)

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

    def _server_info(self) -> dict:
        return _server_info(self.client)

    def _is_pro(self) -> bool:
        return _server_is_pro(self.client)

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
        # which is why the helper reads a ref before deleting it.  Every delete the
        # helper makes is inside a folder that exists, where both editions say 200.
        self.assertEqual(self._api("DELETE", "file", base + "/gone.txt").status_code, 200)
        # Under a folder that is itself gone, Professional answers the way it answers a
        # file read there (issue 6): 403 one level down, a 500 page two levels down
        # (measured on 13.0.29); Community Edition still says 200.
        deep = self._api("DELETE", "file", base + "/a/b/f.txt").status_code
        self.assertEqual(deep, 500 if self._is_pro() else 200)

    def test_a_listing_of_more_than_a_thousand_entries_is_complete_and_unpaged(self):
        # The helper treats the array a listing returns as the whole directory.
        # The stub never truncates, so only a real server can say whether that
        # holds; 1,200 entries was complete on CE 13.0.28 (2026-10-28).
        from concurrent.futures import ThreadPoolExecutor

        many = f"/{self.base}/many"
        count = 1100
        self.client.mkdir_p(self.rid, many)
        with ThreadPoolExecutor(max_workers=8) as pool:
            done = list(pool.map(
                lambda i: self.client.upload_file(self.rid, many, f"f{i:04d}.txt", b"x"),
                range(count),
            ))
        self.assertTrue(all(done))
        self.assertEqual(len(self.client.list_dir(self.rid, many)), count)
        # The endpoint ignores paging parameters rather than rejecting them, so
        # a reader that asked for "page 2" would get page 1 again.
        for query in ("&page=2", "&per_page=100&page=1"):
            url = (f"{self.client.server_url}/api2/repos/{self.rid}/dir/"
                   f"?p={quote(many, safe='/')}{query}")
            resp = self.client.session.get(url, timeout=60)
            self.assertEqual((resp.status_code, len(resp.json())), (200, count), query)

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

    def test_a_missing_folder_lists_as_404_and_its_files_read_as_absent(self):
        """What the issue 6 fix rests on, on the server itself.

        A file read under a missing folder is 404 on Community Edition and 403 (one level
        missing) or a 500 page (two or more) on Professional; the listing of the missing
        folder is 404 on both, at any depth.  The stub's ``pro_missing_parent`` fault copies
        the Pro answers; if a release changes them, this is the test that says so.
        """
        info = self._server_info()
        pro = self._is_pro()
        base = f"/{self.base}/missing-folder"
        self.client.mkdir_p(self.rid, base)
        for depth, folder in ((1, base + "/gone"), (2, base + "/gone/deeper")):
            with self.subTest(depth=depth):
                self.assertEqual(self._api("GET", "dir", folder).status_code, 404)
                status = self._api("GET", "file", folder + "/HEAD").status_code
                expected = (403 if depth == 1 else 500) if pro else 404
                self.assertEqual(
                    status, expected,
                    f"file read {depth} level(s) under a missing folder on "
                    f"{'Pro' if pro else 'CE'} {info.get('version')}",
                )
                self.assertIsNone(self.client.get_file_bytes(self.rid, folder + "/HEAD"))
        # A file that is missing from a folder that exists is a plain 404 on both.
        self.assertEqual(self._api("GET", "file", base + "/HEAD").status_code, 404)

    def test_round_trip_sha1(self):
        self._round_trip("rt_sha1", "sha1")

    def test_round_trip_sha256(self):
        self._round_trip("rt_sha256", "sha256")

    def _packs(self, name):
        entries = self.client.list_dir(self.rid, f"/{self.base}/{name}/objects/pack")
        return sorted(e["name"] for e in entries if e["name"].endswith(".pack"))

    def _gc(self, repo, name, *args):
        p = subprocess.run(["git-remote-seafile", "gc", self._url(name), *args], cwd=str(repo),
                           env=self.env, capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            self.fail(f"gc {args} rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        return p.stdout + p.stderr

    def test_tail_merge_and_full_gc_leave_a_repository_that_still_clones(self):
        """The two manual compactions on a real server, each followed by a clone that must be whole.

        Auto-gc is off so each push leaves its own pack; ``gc --tail`` (what
        auto-gc runs) merges them below the threshold of 3, and plain ``gc``
        rewrites everything into one.  After each the remote is cloned and
        checked, because a merge that deleted a pack too early shows up there.
        """
        name = "gc_manual"
        src = self._repo(f"src_{name}")
        self._git(["config", "seafile.autogc", "false"], src)
        self._git(["config", "seafile.gcthreshold", "3"], src)
        self._git(["remote", "add", "origin", self._url(name)], src)
        for n in range(5):
            self._commit(src, f"f{n}.txt", f"{n}\n" * 400, f"c{n}")
            self._git(["push", "-q", "origin", "main"], src)
        self.assertEqual(len(self._packs(name)), 5)

        def cloned(label, commits):
            dst = self.tmp / f"dst_{name}_{label}"
            self._git(["clone", "-q", self._url(name), str(dst)], self.tmp)
            self.assertEqual(self._git(["rev-list", "--count", "HEAD"], dst).stdout.strip(),
                             str(commits))
            self.assertEqual(self._git(["fsck", "--no-dangling"], dst, check=False).returncode, 0)

        out = self._gc(src, name, "--tail")
        self.assertEqual(len(self._packs(name)), 1, out)
        cloned("tail", 5)

        for n in range(5, 7):
            self._commit(src, f"f{n}.txt", f"{n}\n" * 400, f"c{n}")
            self._git(["push", "-q", "origin", "main"], src)
        self.assertEqual(len(self._packs(name)), 3)
        out = self._gc(src, name)
        self.assertEqual(len(self._packs(name)), 1, out)
        cloned("full", 7)
        names = [e["name"] for e in self.client.list_dir(self.rid, f"/{self.base}/{name}")]
        self.assertFalse([n for n in names if "SFConflict" in n], names)

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

    def test_doctor_measures_an_upload_and_finds_the_conflict_copies_it_left_alone(self):
        """The doctor's two server checks against the real thing (ROADMAP S14, S15).

        A 16 MiB ceiling keeps the run short; the container workflow measures a
        proxy limit separately.  The conflict walk is pointed at the directory
        the same-path race test leaves, so a real server's copies are what it finds.
        """
        from types import SimpleNamespace
        from unittest.mock import patch

        from git_remote_seafile import cli, diagnose

        helper = SimpleNamespace(
            client=self.client, repo_id=self.rid, repo_path=f"/{self.base}/doctor-probe",
            library_name=_LIBRARY,
        )
        configured = SimpleNamespace(gc_max_pack_mib=1024)
        with patch("git_remote_seafile.config.RemoteConfig.load", return_value=configured):
            code = cli._doctor_upload_limit(helper, 16)
        self.assertEqual(code, 0, "a plain container should take a 16 MiB body")
        self.assertEqual(
            [e["name"] for e in self.client.list_dir(self.rid, f"/{self.base}/doctor-probe")], [],
            "the probe left files behind",
        )

        race = f"/{self.base}/doctor-race"
        self.client.mkdir_p(self.rid, race)
        from git_remote_seafile.client import SeafileClient

        def writer(i):
            SeafileClient(server_url=self.client.server_url).upload_file(
                self.rid, race, "main", f"w{i}\n".encode(), replace=True
            )

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        found, _capped = diagnose.find_conflict_copies(self.client, self.rid, race)
        self.assertTrue(found, "no conflict copy was found under a directory that has them")
        self.assertTrue(all("SFConflict" in name for name in found), found)

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

    # -- the snapshot tool's payload store ----------------------------------------
    def _snap(self, *argv):
        """``git seafile-snapshot`` in-process, finding the helper and the server as a user's would."""
        from unittest.mock import patch

        sys.path.insert(0, str(_ROOT / "tools"))
        import seafile_snapshot as snap

        with patch.dict(os.environ, self.env):
            return snap.main([str(a) for a in argv])

    def _objects(self, name):
        """Every payload object the vault ``name`` holds on the server, as ``{sha: size}``."""
        root = f"/{self.base}/{name}/payload/objects"
        found = {}
        for sub in self.client.list_dir(self.rid, root):
            for entry in self.client.list_dir(self.rid, f"{root}/{sub['name']}"):
                found[entry["name"]] = entry["size"]
        return found

    @staticmethod
    def _tree(root):
        import hashlib

        return {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and not {".git", ".seafile-payload"} & set(p.relative_to(root).parts)
        }

    def _payload_source(self, name):
        src = self.tmp / name
        (src / "sub").mkdir(parents=True)
        (src / ".gitignore").write_bytes(b"*.bin\n")
        (src / "small.txt").write_bytes(b"small\n")
        big = os.urandom(3 * 1024 * 1024)
        (src / "big1.bin").write_bytes(big)
        (src / "sub" / "same-as-big1.bin").write_bytes(big)     # one object, two paths
        (src / "sub" / "big2.bin").write_bytes(os.urandom(5 * 1024 * 1024))
        self._git(["init", "-q", "-b", "main", "."], src)
        self._git(["config", "user.email", "t@t"], src)
        self._git(["config", "user.name", "T"], src)
        self._git(["add", ".gitignore", "small.txt"], src)
        self._git(["commit", "-q", "-m", "init"], src)
        return src

    def test_a_payload_snapshot_restores_byte_for_byte_and_is_found_whole_by_verify(self):
        """Payload mode against the real server: the object store, the manifest, a restore.

        What the stub cannot say is whether 3-5 MiB bodies, two-level object
        directories and the git push of the manifest behave the same on the
        real thing.  Two paths with one content must be one object.
        """
        src = self._payload_source("payload_src")
        vault, remote = self.tmp / "payload_vault", self._url("pvault")
        args = (src, "--vault", vault, "--remote", remote, "--payload-min-size", "1000000")
        self.assertEqual(self._snap(*args), 0)
        stored = self._objects("pvault")
        self.assertEqual(len(stored), 2, stored)
        self.assertEqual(sorted(stored.values()), [3 * 1024 * 1024, 5 * 1024 * 1024])

        into = self.tmp / "payload_restored"
        self.assertEqual(self._snap("--restore", "--remote", remote, "--into", into), 0)
        self.assertEqual(self._tree(into), self._tree(src))
        self.assertEqual(self._snap("--payload-verify", "all", "--remote", remote), 0)

        # An unchanged payload uploads nothing: a second run adds no object.
        (src / "small.txt").write_bytes(b"changed\n")
        self.assertEqual(self._snap(*args), 0)
        self.assertEqual(self._objects("pvault"), stored)
        again = self.tmp / "payload_restored_again"
        self.assertEqual(self._snap("--restore", "--remote", remote, "--into", again), 0)
        self.assertEqual((again / "small.txt").read_bytes(), b"changed\n")

    def test_payload_gc_removes_only_the_unreferenced_object_after_the_grace_period(self):
        """Mark, wait out the grace period, delete -- the delete path on a real server.

        The scratch library is the only place this may run (AGENTS.md); the
        orphan is uploaded here and the pending list is back-dated by rewriting
        it, which is all a grace period is.
        """
        import json

        src = self._payload_source("gc_src")
        vault, remote = self.tmp / "gc_vault", self._url("gcvault")
        self.assertEqual(self._snap(
            src, "--vault", vault, "--remote", remote, "--payload-min-size", "1000000"), 0)
        kept = self._objects("gcvault")
        self.assertEqual(len(kept), 2)

        import hashlib

        orphan = os.urandom(2048)
        sha = hashlib.sha256(orphan).hexdigest()
        objects = f"/{self.base}/gcvault/payload/objects"
        self.client.mkdir_p(self.rid, f"{objects}/{sha[:2]}")
        self.client.upload_file(self.rid, f"{objects}/{sha[:2]}", sha, orphan, replace=True)

        gc = ("--payload-gc", "--remote", remote, "--payload-delete",
              "--payload-grace-days", "1", "--payload-no-vault")
        self.assertEqual(self._snap(*gc), 0)
        self.assertIn(sha, self._objects("gcvault"), "the first run may only mark")

        pending = f"/{self.base}/gcvault/payload/gc-pending.json"
        marks = json.loads(self.client.get_file_text(self.rid, pending))
        self.assertEqual(list(marks), [sha])
        old = {key: value - 2 * 86400 for key, value in marks.items()}
        self.client.upload_file(
            self.rid, pending.rsplit("/", 1)[0], "gc-pending.json",
            json.dumps(old).encode(), replace=True,
        )
        self.assertEqual(self._snap(*gc), 0)
        self.assertEqual(self._objects("gcvault"), kept)

        into = self.tmp / "gc_restored"
        self.assertEqual(self._snap("--restore", "--remote", remote, "--into", into), 0)
        self.assertEqual(self._tree(into), self._tree(src))



class _RealServerFixture(unittest.TestCase):
    """The credentials, a scratch folder and a helper environment; no tests of its own.

    ``RealServerTests`` defines tests and so may not be a base class (the parallel
    runner schedules by class and would run them twice).  The classes below need the
    same few lines and add their own gates.
    """

    @classmethod
    def setUpClass(cls):
        from git_remote_seafile.client import SeafileClient

        cls.client = SeafileClient()
        cls.rid = cls.client.get_repo_id(_LIBRARY)
        cls.base = "grs-realserver-" + time.strftime("%Y%m%d%H%M%S") + f"-{os.getpid()}"
        cls.tmp = Path(tempfile.mkdtemp(prefix="grs-real-"))
        try:
            cls.env = _helper_env(cls.tmp, cls.client.server_url, cls.base)
        except BaseException:
            # unittest does not call tearDownClass when setUpClass raises.
            remove_tree(cls.tmp)
            raise

    @classmethod
    def tearDownClass(cls):
        url = (
            f"{cls.client.server_url}/api2/repos/{cls.rid}/file/"
            f"?p={quote('/' + cls.base, safe='/')}"
        )
        try:
            cls.client.session.delete(url, timeout=120)
        finally:
            remove_tree(cls.tmp)

    def _git(self, args, cwd, check=True):
        p = subprocess.run(["git", *args], cwd=str(cwd), env=self.env,
                           capture_output=True, text=True, timeout=1800)
        if check and p.returncode != 0:
            self.fail(f"git {' '.join(args)} rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        return p


@unittest.skipUnless(
    _LIBRARY and os.environ.get("SEAFILE_TEST_ALLOW_LIBRARY_CREATE") == "1",
    "set SEAFILE_TEST_LIBRARY and SEAFILE_TEST_ALLOW_LIBRARY_CREATE=1: this test creates and deletes a "
    "whole library on the account, which the other tests (one folder in a scratch library) never do",
)
class LibraryCreationTests(_RealServerFixture):
    """``doctor --create-library`` against a real server, and the cleanup of what it made.

    The container workflow sets the opt-in (the container is thrown away). Against a server
    that holds anything real, the delete below is guarded to the one library this test
    created: its name must carry the test's prefix and be unique, and it may hold nothing
    but the empty probe folder doctor wrote.
    """

    PREFIX = "grs-create-"

    def _libraries_named(self, name):
        resp = self.client.session.get(f"{self.client.server_url}/api2/repos/", timeout=60)
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        return [r for r in resp.json() if isinstance(r, dict) and r.get("name") == name]

    def _delete_only_what_this_test_created(self, name):
        if not name.startswith(self.PREFIX) or len(name) != len(self.PREFIX) + 8:
            self.fail(f"refusing to delete a library called {name!r}")
        found = self._libraries_named(name)
        if not found:
            return
        self.assertEqual(len(found), 1, f"{len(found)} libraries are called {name!r}; deleting none")
        rid = found[0]["id"]
        top = {e.get("name") for e in self.client.list_dir(rid, "/")}
        self.assertLessEqual(top, {"probe"}, f"{name!r} holds {top}, which this test did not put there")
        self.client.session.delete(f"{self.client.server_url}/api2/repos/{rid}/", timeout=120)

    def test_doctor_create_library_makes_an_unencrypted_library_with_a_short_history(self):
        import secrets

        from e2e_harness import run_helper

        name = self.PREFIX + secrets.token_hex(4)
        self.addCleanup(self._delete_only_what_this_test_created, name)
        proc = run_helper(["doctor", f"seafile://{name}/probe", "--create-library"],
                          self.tmp, self.env, check=False)
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn(f"created library '{name}', keeping 30 days of history", output)
        self.assertIn("can write and delete", output)

        found = self._libraries_named(name)
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].get("encrypted"), "the library must be unencrypted")
        self.assertEqual(self.client.get_history_limit(found[0]["id"]), 30)

        # A second run finds it and does not create another.
        again = run_helper(["doctor", f"seafile://{name}/probe", "--create-library"],
                           self.tmp, self.env, check=False)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotIn("created library", again.stdout)
        self.assertEqual(len(self._libraries_named(name)), 1)

    def test_the_cleanup_refuses_a_library_it_did_not_create(self):
        with self.assertRaises(AssertionError):
            self._delete_only_what_this_test_created("Documents")
        with self.assertRaises(AssertionError):
            self._delete_only_what_this_test_created(self.PREFIX + "toolongforthis")


@unittest.skipUnless(
    _LIBRARY and os.environ.get("SEAFILE_TEST_BODY_LIMIT_MIB"),
    "set SEAFILE_TEST_LIBRARY and SEAFILE_TEST_BODY_LIMIT_MIB to the proxy's client_max_body_size "
    "(the container workflow does, after lowering nginx's)",
)
class ProxyBodyLimitTests(_RealServerFixture):
    """A real ``git push`` through a real proxy's request-size limit (the 0.9.19 pack split).

    The stub's ``max_upload`` fault answers 413 the way nginx does; this is the check that a
    real nginx in front of a real Seafile agrees: the push is refused, rebuilt in smaller
    packs, and arrives whole.
    """

    MIB = 1024 * 1024

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.limit = int(os.environ["SEAFILE_TEST_BODY_LIMIT_MIB"]) * cls.MIB

    def _repo(self, name):
        path = self.tmp / name
        path.mkdir()
        self._git(["init", "-q", "-b", "main", "."], path)
        self._git(["config", "user.email", "t@t"], path)
        self._git(["config", "user.name", "T"], path)
        self._git(["config", "core.autocrlf", "false"], path)
        return path

    def _add_random(self, repo, prefix, count, size):
        import hashlib

        made = {}
        for n in range(count):
            data = os.urandom(size)
            name = f"{prefix}{n}.bin"
            (repo / name).write_bytes(data)
            made[name] = hashlib.sha256(data).hexdigest()
            self._git(["add", "--", name], repo)
        self._git(["commit", "-q", "-m", f"add {prefix}"], repo)
        return made

    def _pack_sizes(self, name):
        listing = self.client.list_dir(self.rid, f"/{self.base}/{name}/objects/pack")
        return {e["name"]: e["size"] for e in listing if e.get("name", "").endswith(".pack")}

    def test_a_push_larger_than_the_proxy_limit_arrives_whole_in_smaller_packs(self):
        import hashlib

        name = "split"
        repo = self._repo("src_split")
        # Twice the limit in incompressible bytes: one pack of it cannot get through.
        count = 6
        size = int(self.limit * 2 / count)
        made = self._add_random(repo, "big", count, size)
        self._git(["remote", "add", "origin", f"seafile://{_LIBRARY}/{self.base}/{name}"], repo)

        proc = self._git(["push", "-u", "origin", "main"], repo)
        # Kept in the log: which refusal a real nginx produces (a 413, or a closed connection)
        # decides whether the working cap is remembered, and a pass says neither.
        sys.stderr.write("\n--- push stderr ---\n" + proc.stderr + "--- end ---\n")

        self.assertIn("the server refused an upload of", proc.stderr)
        self.assertIn("Notice: this push went through in packs of at most", proc.stderr)
        sizes = self._pack_sizes(name)
        self.assertGreaterEqual(len(sizes), 2, sizes)
        self.assertTrue(all(sz <= self.limit for sz in sizes.values()),
                        f"a pack larger than the proxy limit ({self.limit}) is on the server: {sizes}")

        dst = self.tmp / "clone_split"
        self._git(["clone", f"seafile://{_LIBRARY}/{self.base}/{name}", str(dst)], self.tmp)
        for fname, digest in made.items():
            self.assertEqual(hashlib.sha256((dst / fname).read_bytes()).hexdigest(), digest, fname)

        # After an explicit 413 the working cap is kept, and the next push starts from it.
        remembered = self._git(["config", "--local", "--get", "seafile.gcmaxpackmib"], repo, check=False)
        if remembered.returncode == 0:
            self.assertLess(int(remembered.stdout.strip()) * self.MIB, self.limit + 1)
            self._add_random(repo, "more", 3, size)
            second = self._git(["push", "origin", "main"], repo)
            self.assertNotIn("refused an upload", second.stderr)


_SECOND_TOKEN = os.environ.get("SEAFILE_TEST_SECOND_TOKEN", "").strip()
_SECOND_EMAIL = os.environ.get("SEAFILE_TEST_SECOND_EMAIL", "").strip()


@unittest.skipUnless(
    _LIBRARY and _SECOND_TOKEN and _SECOND_EMAIL,
    "set SEAFILE_TEST_LIBRARY, SEAFILE_TEST_SECOND_TOKEN and SEAFILE_TEST_SECOND_EMAIL (a second "
    "account on the same server; the container workflow creates one)",
)
class SharedUserTests(_RealServerFixture):
    """A second account: sub-folder permissions, groups, and two first pushes at once.

    The scratch library is shared with the second account read-write (unless it already is,
    in which case the share is left as found), and on Professional each folder under
    ``<base>/rules`` gets one folder permission (``/api2/repos/<id>/user-folder-perm/``, a
    Pro feature; a share of the sub-folder with a lesser permission does *not* restrict a
    user who has the library read-write, measured on Pro 13.0.29).  On Seafile Pro a file read under a missing folder answers 403
    whatever the permissions (issue 6), so these are the cases where "missing" and "denied"
    look alike from the file read and only the listing tells them apart: a push must work
    where the rules allow it and fail loudly, naming the 403, where they do not.

    The group cases create a group, a library and (on Pro) a department, so they also need
    ``SEAFILE_TEST_ALLOW_LIBRARY_CREATE=1``; everything they make is removed.
    """

    RULES = ("r", "rw", "preview", "cloud-edit", "invisible")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env2 = dict(cls.env)
        cls.env2["SEAFILE_SERVER"] = cls.client.server_url
        cls.env2["SEAFILE_TOKEN"] = _SECOND_TOKEN
        cls._rules: list[str] = []
        # Folder permissions are a Pro feature.  Ask the edition rather than read
        # the endpoint's status: CE 13.0 answers it 403 "Permission denied.", the
        # same body as a real refusal (measured in the CE container, first run of
        # realserver.yml), so a status cannot tell "no such feature" from "denied".
        cls._rules_supported = _server_is_pro(cls.client)
        cls._made_library_share = False
        try:
            existing = cls._admin("GET", f"/api2/repos/{cls.rid}/dir/shared_items/",
                                  params={"p": "/", "share_type": "user"})
            shared = existing.status_code == 200 and any(
                (s.get("user_info") or {}).get("name") == _SECOND_EMAIL for s in existing.json()
            )
            if not shared:
                cls._share("/", "rw")
                cls._made_library_share = True
            for perm in cls.RULES + ("none",):
                folder = f"/{cls.base}/rules/{perm}"
                cls.client.mkdir_p(cls.rid, folder)
                if perm != "none" and cls._rules_supported:
                    cls._rule(folder, perm)
            _require_the_same_server(cls.env2, cls.tmp, cls.client.server_url)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            for folder in getattr(cls, "_rules", []):
                cls._admin("DELETE", f"/api2/repos/{cls.rid}/user-folder-perm/",
                           data={"user_email": _SECOND_EMAIL, "folder_path": folder})
            if getattr(cls, "_made_library_share", False):
                cls._admin("DELETE", f"/api2/repos/{cls.rid}/dir/shared_items/",
                           params={"p": "/", "share_type": "user", "username": _SECOND_EMAIL})
        finally:
            super().tearDownClass()

    # -- helpers --------------------------------------------------------------
    @classmethod
    def _admin(cls, method, path, **kw):
        return cls.client.session.request(method, cls.client.server_url + path, timeout=120, **kw)

    @classmethod
    def _share(cls, path, permission, repo_id=None):
        resp = cls._admin(
            "PUT", f"/api2/repos/{repo_id or cls.rid}/dir/shared_items/", params={"p": path},
            data={"share_type": "user", "username": _SECOND_EMAIL, "permission": permission},
        )
        if resp.status_code != 200 or resp.json().get("failed"):
            raise AssertionError(f"sharing {path} as {permission!r} failed: {resp.status_code} "
                                 f"{resp.text[:300]}")

    @classmethod
    def _rule(cls, folder, permission):
        """A folder permission for the second account (Pro only; setUpClass asks the edition).

        A 404 is still read as "no folder permissions here", for a server whose
        features list says Pro but whose route is missing."""
        resp = cls._admin("POST", f"/api2/repos/{cls.rid}/user-folder-perm/",
                          data={"user_email": _SECOND_EMAIL, "folder_path": folder,
                                "permission": permission})
        if resp.status_code == 404:
            cls._rules_supported = False
            return
        if resp.status_code != 200 or resp.json().get("failed"):
            raise AssertionError(f"folder permission {permission!r} on {folder} failed: "
                                 f"{resp.status_code} {resp.text[:300]}")
        cls._rules.append(folder)

    def _new_repo(self, name, content):
        path = self.tmp / name
        path.mkdir()
        for args in (["init", "-q", "-b", "main", "."], ["config", "user.email", "t@t"],
                     ["config", "user.name", "T"], ["config", "core.autocrlf", "false"]):
            self._git(args, path)
        (path / "a.txt").write_text(content)
        self._git(["add", "a.txt"], path)
        self._git(["commit", "-q", "-m", content.strip()], path)
        return path

    def _as(self, env, args, cwd):
        return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=1800)

    def _round_trip(self, env, url, tag):
        """First push, clone, second push, fetch: the whole first-use path for one account."""
        src = self._new_repo(f"src_{tag}", f"{tag}\n")
        push = self._as(env, ["push", url, "main"], src)
        self.assertEqual(push.returncode, 0, f"{tag}: first push\n{push.stdout}{push.stderr}")
        dst = self.tmp / f"clone_{tag}"
        clone = self._as(env, ["clone", "-q", url, str(dst)], self.tmp)
        self.assertEqual(clone.returncode, 0, f"{tag}: clone\n{clone.stderr}")
        (src / "b.txt").write_text("two\n")
        self._git(["add", "b.txt"], src)
        self._git(["commit", "-q", "-m", "two"], src)
        second = self._as(env, ["push", url, "main"], src)
        self.assertEqual(second.returncode, 0, f"{tag}: second push\n{second.stderr}")
        fetch = self._as(env, ["pull", "-q", "--ff-only", url, "main"], dst)
        self.assertEqual(fetch.returncode, 0, f"{tag}: pull\n{fetch.stderr}")
        self.assertTrue((dst / "b.txt").is_file(), tag)

    # -- tests ----------------------------------------------------------------
    def test_measure_what_an_invisible_rule_shows_the_account(self):
        """ROADMAP B2, a measurement: does a Pro ``invisible`` folder rule hide the folder
        from the account's listing of its parent?  If it does, a ``gc`` or
        ``--payload-delete`` by that account would read refs under it as absent; the
        answer decides whether those refuse for an account that is not the owner.
        It records what the server does (the ``MEASURED`` line in the log) and fails only
        on an answer that is neither of the two shapes the decision is written for."""
        if not self._rules_supported:
            self.skipTest("no folder permissions on this server (user-folder-perm is Pro only)")
        from git_remote_seafile.client import SeafileAPIError, SeafileClient

        second = SeafileClient(server_url=self.client.server_url, token=_SECOND_TOKEN)
        parent = f"/{self.base}/rules"
        listed = sorted(str(e.get("name")) for e in second.list_dir(self.rid, parent))
        try:
            inside = f"lists ({len(second.list_dir(self.rid, parent + '/invisible'))} entries)"
        except SeafileAPIError as ex:
            inside = f"refused ({str(ex)[:120]})"
        hidden = "invisible" not in listed
        print(f"\nMEASURED (B2, {self._edition()}): the parent listing "
              f"{'omits' if hidden else 'shows'} the invisible folder; the folder itself {inside}; "
              f"the account sees {listed}", flush=True)
        self.assertIn("rw", listed, "the account must see a folder it may write")
        # The refusal B2 led to rests on the server naming the library's owner
        # (``/api2/repos/<id>/``) and the account: the owner is not at risk, the
        # second account is.
        self.assertIsNone(self.client.hidden_history_risk(self.rid))
        self.assertIn("is not the library's owner", second.hidden_history_risk(self.rid) or "")
        if not hidden:
            self.assertTrue(inside.startswith("refused"),
                            "an invisible folder that is listed must at least refuse its contents")

    @classmethod
    def _edition(cls) -> str:
        info = _server_info(cls.client)
        return f"{'Pro' if _server_is_pro(cls.client) else 'CE'} {info.get('version', '?')}"

    def test_a_shared_user_pushes_where_the_rules_allow_and_is_refused_loudly_elsewhere(self):
        if not self._rules_supported:
            self.skipTest("no folder permissions on this server (user-folder-perm is Pro only)")
        for perm in self.RULES + ("none",):
            with self.subTest(rule=perm):
                url = f"seafile://{_LIBRARY}/{self.base}/rules/{perm}/grs/repo"
                if perm in ("rw", "none"):
                    self._round_trip(self.env2, url, f"rule_{perm.replace('-', '_')}")
                    continue
                src = self._new_repo(f"src_denied_{perm.replace('-', '_')}", f"{perm}\n")
                push = self._as(self.env2, ["push", url, "main"], src)
                out = push.stdout + push.stderr
                self.assertNotEqual(push.returncode, 0, f"{perm}: the push must be refused\n{out}")
                self.assertIn("HTTP 403", out, f"{perm}: the refusal must name the server's answer")
                self.assertIn("This account may not", out,
                              f"{perm}: the refusal must point at the permissions, not the lock")
                listed = self.client.list_dir(self.rid, f"/{self.base}/rules/{perm}")
                self.assertNotIn("grs", [e["name"] for e in listed],
                                 f"{perm}: a refused push must leave nothing in the folder")

    def test_two_accounts_pushing_first_to_one_new_path_leave_one_history_and_no_copies(self):
        for rnd in range(3):
            with self.subTest(round=rnd):
                path = f"{self.base}/race{rnd}/repo"
                url = f"seafile://{_LIBRARY}/{path}"
                repos = {"owner": (self.env, self._new_repo(f"race{rnd}_owner", f"owner {rnd}\n")),
                         "second": (self.env2, self._new_repo(f"race{rnd}_second", f"second {rnd}\n"))}
                results: dict[str, subprocess.CompletedProcess] = {}

                def push(who, env, repo, url=url, results=results):
                    results[who] = self._as(env, ["push", url, "main"], repo)

                threads = [threading.Thread(target=push, args=(w, e, r)) for w, (e, r) in repos.items()]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                codes = {w: p.returncode for w, p in results.items()}
                self.assertEqual(sorted(codes.values()), [0, 1],
                                 f"exactly one first push wins: {codes}\n" +
                                 "\n".join(p.stderr for p in results.values()))
                loser = next(p for p in results.values() if p.returncode)
                self.assertIn("rejected", loser.stdout + loser.stderr)
                dst = self.tmp / f"race{rnd}_clone"
                self._git(["clone", "-q", url, str(dst)], self.tmp)
                self._git(["fsck", "--strict"], dst)
                # Inside the repository no copy may stay: its folders are the helper's
                # own, so a copy of one is removed.
                names = [e["name"] for e in self.client.list_dir(self.rid, "/" + path)]
                self.assertFalse([n for n in names if " (" in n or "SFConflict" in n],
                                 f"the race left copies in /{path}: {names}")
                # Above it the folders are the ones a person named (`repo`, `race<N>`),
                # and a copy of one is named and left, not removed (the maintainer's
                # review of 0fca23d).  A simultaneous mkdir leaves `repo (1)` in
                # `race<N>` and `race<N> (1)` in the base, which a listing of the
                # repository alone never sees (review 4f1cdd4, glm-5.3 G4.2): each such
                # copy must be empty and named in a push's warning.
                # Only this round's folders: the base also holds the copies an earlier
                # round left and named (round 1 found round 0's `race0 (1)`, Pro, the
                # third realserver.yml run).
                said = "".join(p.stderr for p in results.values())
                for folder, made in ((f"{self.base}/race{rnd}", "repo"), (self.base, f"race{rnd}")):
                    for entry in self.client.list_dir(self.rid, "/" + folder):
                        name = entry["name"]
                        self.assertNotIn("SFConflict", name, f"/{folder}: {name}")
                        if not name.startswith(f"{made} ("):
                            continue
                        copy = f"/{folder}/{name}"
                        self.assertEqual(self.client.list_dir(self.rid, copy), [],
                                         f"{copy} is not empty")
                        self.assertIn(copy, said, f"{copy} was left without a warning")

    def _require_library_create(self):
        if os.environ.get("SEAFILE_TEST_ALLOW_LIBRARY_CREATE") != "1":
            self.skipTest("creates a group and a library: set SEAFILE_TEST_ALLOW_LIBRARY_CREATE=1")

    def _new_group(self, name):
        resp = self._admin("POST", "/api/v2.1/groups/", data={"name": name})
        self.assertEqual(resp.status_code, 201, resp.text[:300])
        gid = resp.json()["id"]
        self.addCleanup(self._admin, "DELETE", f"/api/v2.1/groups/{gid}/")
        resp = self._admin("POST", f"/api/v2.1/groups/{gid}/members/", data={"email": _SECOND_EMAIL})
        self.assertEqual(resp.status_code, 201, resp.text[:300])
        return gid

    def test_a_library_shared_to_a_group_works_for_a_member(self):
        import secrets

        self._require_library_create()
        name = "grs-group-" + secrets.token_hex(4)
        gid = self._new_group(name)
        resp = self._admin("POST", "/api2/repos/", data={"name": name})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        repo_id = resp.json()["repo_id"]
        self.addCleanup(self._admin, "DELETE", f"/api2/repos/{repo_id}/")
        resp = self._admin("PUT", f"/api2/repos/{repo_id}/dir/shared_items/", params={"p": "/"},
                           data={"share_type": "group", "group_id": str(gid), "permission": "rw"})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertFalse(resp.json().get("failed"), resp.text[:300])
        self._round_trip(self.env2, f"seafile://{name}/deep/repo", "group_shared")

    def _measure_group_owner(self, repo_id: str) -> None:
        """ROADMAP Z22/Z36, a measurement: what ``owner`` a group-owned library reports to a member
        and to the admin who made it, and what the owner guard says to each.  Records the
        ``MEASURED`` line and never fails: the reviewers' value for it was never read from a server."""
        from git_remote_seafile.client import SeafileClient

        try:
            second = SeafileClient(server_url=self.client.server_url, token=_SECOND_TOKEN)
            as_member = (second._json_or_none(f"/api2/repos/{repo_id}/") or {}).get("owner")
            as_admin = (self.client._json_or_none(f"/api2/repos/{repo_id}/") or {}).get("owner")
            print(f"\nMEASURED (Z22/Z36, {self._edition()}): a group-owned library's owner reads "
                  f"{as_member!r} to a member and {as_admin!r} to the admin who made it; the guard "
                  f"says member: {second.hidden_history_risk(repo_id)!r}; admin: "
                  f"{self.client.hidden_history_risk(repo_id)!r}", flush=True)
        except Exception as ex:  # a measurement, not an assertion
            print(f"\nMEASURED (Z22/Z36): could not read it ({type(ex).__name__}: {str(ex)[:160]})", flush=True)

    def _measure_owner_fields(self, label: str, repo_id: str) -> None:
        """ROADMAP Z22, a measurement: what tells a group-owned library from one that is only shared?

        ``/api2/repos/<id>/`` answers ``owner: "share"`` to *every* account that is not the owner
        (the owner reads ``"self"``), so that field cannot.  Prints every key naming an owner or a
        group in the v2.1 detail and in the account's repo list, as the member and as the admin;
        never fails."""
        from git_remote_seafile.client import SeafileClient

        def wanted(info: object) -> dict:
            if not isinstance(info, dict):
                return {"unreadable": repr(info)[:80]}
            return {k: v for k, v in info.items() if "owner" in k or "group" in k or k == "type"}

        try:
            second = SeafileClient(server_url=self.client.server_url, token=_SECOND_TOKEN)
            for who, client in (("member", second), ("admin", self.client)):
                detail = client._json_or_none(f"/api/v2.1/repos/{repo_id}/")
                listing = client._json_or_none("/api/v2.1/repos/?type=mine") or {}
                shared = client._json_or_none("/api/v2.1/repos/?type=shared") or {}
                entries = [r for part in (listing, shared) if isinstance(part, dict)
                           for r in (part.get("repos") or []) if isinstance(r, dict) and r.get("repo_id") == repo_id]
                print(f"\nMEASURED (Z22, {self._edition()}): {label} library, as {who}: v2.1 detail "
                      f"{wanted(detail)}; in the account's repo lists {[wanted(e) for e in entries]}", flush=True)
        except Exception as ex:  # a measurement, not an assertion
            print(f"\nMEASURED (Z22): {label}: could not read it ({type(ex).__name__}: {str(ex)[:160]})", flush=True)

    def test_measure_a_delete_of_a_parent_that_holds_an_invisible_child(self):
        """ROADMAP Z29, a measurement: ``_prune_empty_ref_parents`` deletes a parent it sees as empty.
        A Pro non-owner sees a folder as empty when its only child is ``invisible`` to them; does
        a ``DELETE`` of that parent take the hidden child with it?  Skipping the prune leaves
        folders the refs listing reads as failures, so the answer decides which risk to take.
        Records the ``MEASURED`` line; fails only if the fixture itself cannot be built."""
        if not self._rules_supported:
            self.skipTest("no folder permissions on this server (user-folder-perm is Pro only)")
        from git_remote_seafile.client import SeafileClient

        parent, hidden = f"/{self.base}/z29", f"/{self.base}/z29/hidden"
        self.client.mkdir_p(self.rid, hidden)
        self.client.upload_file(self.rid, hidden, "secret.txt", b"kept?\n", replace=True)
        self._rule(hidden, "invisible")
        second = SeafileClient(server_url=self.client.server_url, token=_SECOND_TOKEN)
        seen = sorted(str(e.get("name")) for e in second.list_dir(self.rid, parent))
        try:
            deleted = second.delete_entry(self.rid, parent)
            outcome = f"answered {deleted!r}"
        except Exception as ex:
            outcome = f"raised {type(ex).__name__}: {str(ex)[:100]}"
        after = sorted(str(e.get("name")) for e in self.client.list_dir(self.rid, hidden)) \
            if self.client.dir_exists(self.rid, hidden) else "gone"
        print(f"\nMEASURED (Z29, {self._edition()}): the non-owner lists {seen} under the parent; "
              f"DELETE of the parent {outcome}; the hidden folder afterwards: {after}", flush=True)

    def test_measure_the_mtime_a_listing_reports_for_an_upload(self):
        """ROADMAP Z36 (Z14), a measurement: is a listed ``mtime`` assigned by the server's clock,
        and how far is it from this machine's?  The placement probe sweep skips a listed
        ``mtime`` of 0 or less on that assumption.  Records the ``MEASURED`` line."""
        import time as _time

        folder = f"/{self.base}/z36"
        self.client.mkdir_p(self.rid, folder)
        before = _time.time()
        self.client.upload_file(self.rid, folder, "probe.txt", b"x\n", replace=True)
        after = _time.time()
        listed = next((e for e in self.client.list_dir(self.rid, folder) if e.get("name") == "probe.txt"), {})
        mtime = listed.get("mtime")
        verdict = ("not listed" if not isinstance(mtime, (int, float)) else
                   f"{mtime - after:+.1f} s from this machine's clock after the upload "
                   f"(window {after - before:.1f} s)")
        print(f"\nMEASURED (Z36, {self._edition()}): the listed mtime of a fresh upload is {mtime!r}: {verdict}",
              flush=True)

    def test_a_group_owned_library_works_for_a_member_and_the_owner(self):
        """Group-owned libraries live in departments, a Professional feature: skipped on CE."""
        import secrets

        self._require_library_create()
        name = "grs-dept-" + secrets.token_hex(4)
        resp = self._admin("POST", "/api/v2.1/admin/address-book/groups/",
                           data={"group_name": name, "group_owner": "system admin",
                                 "parent_group": "-1"})
        if resp.status_code != 200:
            self.skipTest(f"no departments on this server (HTTP {resp.status_code}): Pro only")
        did = resp.json()["id"]
        self.addCleanup(self._admin, "DELETE", f"/api/v2.1/admin/address-book/groups/{did}/")
        owner = self._admin("GET", "/api2/account/info/").json()["email"]
        for email in (owner, _SECOND_EMAIL):
            resp = self._admin("POST", f"/api/v2.1/admin/groups/{did}/members/", data={"email": email})
            self.assertEqual(resp.status_code, 200, resp.text[:300])
        resp = self._admin("PUT", f"/api/v2.1/admin/groups/{did}/members/{owner}/",
                           data={"is_admin": "true"})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        # The field is ``name``: ``repo_name`` answers 400 "name invalid." (Pro 13.0.29).
        resp = self._admin("POST", f"/api/v2.1/groups/{did}/group-owned-libraries/",
                           data={"name": name, "permission": "rw"})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        repo_id = resp.json()["id"]
        # Cleanups run last-in first-out: the library goes before its department.
        self.addCleanup(self._admin, "DELETE",
                        f"/api/v2.1/groups/{did}/group-owned-libraries/{repo_id}/")
        self._measure_group_owner(repo_id)
        self._measure_owner_fields("group-owned", repo_id)
        self._measure_owner_fields("shared user-owned", self.rid)
        self._round_trip(self.env2, f"seafile://{name}/repo", "dept_member")
        self._round_trip(self.env, f"seafile://{name}/deep/repo", "dept_owner")


if __name__ == "__main__":
    unittest.main()
