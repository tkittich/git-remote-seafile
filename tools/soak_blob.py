"""soak_blob.py - several sfblob processes on one store, with random kills and injected faults, then an oracle (PLAN_0.12 D2).

    python tools/soak_blob.py --seconds 120 --workers 4 --seed 1          # against the in-process stub
    python tools/soak_blob.py --server http://127.0.0.1:18801 --library <name> --seconds 3600   # a throwaway container

Each worker is a loop that runs the real command line (``python -m git_remote_seafile.blob.cli``) as a subprocess: ``put`` (bare, with
``--pin``, ``-r``), ``pin``, ``get``, ``cat``, ``get --from``, ``verify`` (also ``--deep``), ``gc``, ``cache --clear``.  A random
fraction of the subprocesses is killed (SIGKILL) at a random moment, so an outcome is "unknown", and against the stub the controller
sets one-shot faults (a body stored one byte long, a refused upload).  Nothing is unpinned, so the oracle is simple.

After the workers stop, the oracle checks what must be true however the run went:
  1. ``verify --deep`` is clean: every object in the store hashes to its name and is listed at its size;
  2. every ``put`` that exited 0 left its object in the store (and every ``put --pin`` that exited 0 left its pin);
  3. ``gc`` names no pinned object as missing except one whose ``put --pin`` was killed or failed (a pin may name what was
     never uploaded: the unfinished batch of PROTOCOL.md);
  4. a fresh cache gets back byte-identical copies of a sample of the objects;
  5. every file in every worker's cache hashes to its name;
  6. no traceback on any worker's standard error.
The seed fixes the corpus and the schedule; kill times are random, so a failure is replayed approximately (the ledger says what ran).
Exit status 1 when the oracle finds anything.  It is the test that finds what a review cannot, and costs only machine time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

EDGE_SIZES = [0, 1, 2, 4095, 4096, 4098, 24578, 100_000]


def corpus(folder: Path, rng: random.Random, files: int = 40) -> "list[Path]":
    """Files of mixed sizes, 30 % of them sharing content with another (the same object under two names)."""
    folder.mkdir(parents=True, exist_ok=True)
    pool = [rng.randbytes(rng.choice(EDGE_SIZES + [rng.randint(0, 3 * 1024 * 1024) // 8])) for _ in range(12)]
    made = []
    for n in range(files):
        data = rng.choice(pool) if rng.random() < 0.3 else rng.randbytes(rng.choice(EDGE_SIZES + [rng.randint(0, 600_000)]))
        path = folder / f"f{n:03d}.bin"
        path.write_bytes(data)
        made.append(path)
    return made


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Soak:
    def __init__(self, args: argparse.Namespace, work: Path, server: str, store_url: str) -> None:
        self.args, self.work, self.server, self.store = args, work, server, store_url
        self.rng = random.Random(args.seed)
        self.files = corpus(work / "corpus", self.rng)
        self.lock = threading.Lock()
        self.ledger = (work / "ledger.jsonl").open("a", encoding="utf-8")
        self.stderr_seen: "list[str]" = []
        self.put_ok: "dict[str, str]" = {}          # sha256 -> path, for a put that exited 0
        self.pin_ok: "set[str]" = set()          # shas whose put --pin exited 0
        self.maybe_missing: "set[str]" = set()          # shas of a put --pin that was killed or failed
        self.deadline = time.monotonic() + args.seconds

    def env(self, worker: int) -> "dict[str, str]":
        env = dict(os.environ)
        env.update({"SEAFILE_TOKEN": os.environ.get("SEAFILE_TOKEN", "t"), "SEAFILE_SERVER": self.server,
                    "SFBLOB_CACHE": str(self.work / f"cache{worker}"), "SFBLOB_STORE": self.store, "SFBLOB_LOG": "off",
                    "PYTHONPATH": str(ROOT)})
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        return env

    def run(self, worker: int, argv: "list[str]", rng: random.Random, kill: bool) -> "tuple[int | None, str, str]":
        proc = subprocess.Popen([sys.executable, "-m", "git_remote_seafile.blob.cli", *argv], env=self.env(worker),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.work)
        killed = False
        if kill:
            try:
                out, err = proc.communicate(timeout=rng.uniform(0.1, 1.5))
            except subprocess.TimeoutExpired:
                proc.kill()
                killed = True
                out, err = proc.communicate()
        else:
            out, err = proc.communicate(timeout=300)
        out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")          # `cat` writes bytes
        with self.lock:
            self.ledger.write(json.dumps({"t": round(time.time(), 2), "worker": worker, "argv": argv, "rc": None if killed else proc.returncode,
                                          "killed": killed}) + "\n")
            self.ledger.flush()
            os.fsync(self.ledger.fileno())
            if "Traceback" in err:
                self.stderr_seen.append(f"worker {worker} {argv}: {err[-400:]}")
        return (None if killed else proc.returncode), out, err

    def worker(self, number: int) -> None:
        rng = random.Random(self.args.seed * 1000 + number)
        known: "list[str]" = []
        while time.monotonic() < self.deadline:
            kill = rng.random() < self.args.kill
            op = rng.choice(["put", "put", "pin_put", "tree", "get", "cat", "from", "verify", "deep", "gc", "clear", "pinls"])
            path = rng.choice(self.files)
            digest = sha(path)
            if op == "put":
                rc, _, _ = self.run(number, ["put", str(path)], rng, kill)
                if rc == 0:
                    with self.lock:
                        self.put_ok[digest] = str(path)
                    known.append(digest)
            elif op == "pin_put":
                name = f"pin{number}-{rng.randint(0, 5)}"
                rc, _, _ = self.run(number, ["put", "--pin", name, str(path)], rng, kill)
                with self.lock:
                    if rc == 0:
                        self.put_ok[digest] = str(path)
                        self.pin_ok.add(digest)
                        known.append(digest)
                    else:
                        self.maybe_missing.add(digest)
            elif op == "tree":
                folder = self.work / f"tree{number}"
                folder.mkdir(exist_ok=True)
                chosen = rng.sample(self.files, 6)
                for stale in folder.glob("*"):
                    stale.unlink()
                for item in chosen:
                    (folder / item.name).write_bytes(item.read_bytes())
                rc, _, _ = self.run(number, ["put", "-r", str(folder), "--record", str(self.work / f"rec{number}.txt"), "--jobs", "2"], rng, kill)
                if rc == 0:
                    with self.lock:
                        for item in chosen:
                            self.put_ok[sha(item)] = str(item)
            elif op == "get" and known:
                self.run(number, ["get", "-o", str(self.work / f"got{number}-{rng.randint(0, 99999)}.bin"), "sha256:" + rng.choice(known)], rng, kill)
            elif op == "cat" and known:
                self.run(number, ["cat", "sha256:" + rng.choice(known)], rng, kill)
            elif op == "from" and (self.work / f"rec{number}.txt").exists():
                out = self.work / f"restored{number}-{rng.randint(0, 99999)}"
                self.run(number, ["get", "--from", str(self.work / f"rec{number}.txt"), "--to", str(out)], rng, kill)
            elif op == "verify":
                self.run(number, ["verify"], rng, kill)
            elif op == "deep":
                self.run(number, ["verify", "--deep", "--subset", f"{rng.randint(1, 4)}/4"], rng, kill)
            elif op == "gc":
                self.run(number, ["gc"], rng, kill)
            elif op == "clear":
                self.run(number, ["cache", "--clear"], rng, kill)
            elif op == "pinls":
                self.run(number, ["pin", "ls"], rng, kill)

    def oracle(self) -> "list[str]":
        bad: "list[str]" = []
        quiet = random.Random(0)
        rc, out, err = self.run(0, ["verify", "--deep"], quiet, False)
        if rc != 0:
            bad.append(f"verify --deep is not clean (exit {rc}): {err[-300:]}")
        absent = []
        for digest in self.put_ok:
            rc, _, _ = self.run(0, ["exists", f"sha256:{digest}"], quiet, False)
            if rc != 0:
                absent.append(digest)
        if absent:
            bad.append(f"{len(absent)} object(s) of a put that exited 0 are not in the store, first sha256:{absent[0]}")
        rc, out, err = self.run(0, ["gc", "--json"], quiet, False)
        try:
            missing = set(json.loads(out).get("missing_list", []))
        except ValueError:
            missing = set()
            bad.append(f"gc --json did not answer JSON: {out[:200]!r} {err[-200:]}")
        unexplained = {m for m in missing if m.split(":")[1] not in self.maybe_missing}
        if unexplained:
            bad.append(f"gc names {len(unexplained)} pinned object(s) as missing that no killed or failed put --pin explains, first {sorted(unexplained)[0]}")
        for digest in self.pin_ok:
            if f"sha256:{digest}" in missing:
                bad.append(f"a put --pin that exited 0 left its object missing: sha256:{digest}")
        sample = self.rng.sample(sorted(self.put_ok), min(15, len(self.put_ok)))
        for digest in sample:
            target = self.work / f"oracle-{digest[:12]}.bin"
            env_cache = self.work / "oracle-cache"
            proc = subprocess.run([sys.executable, "-m", "git_remote_seafile.blob.cli", "get", "-o", str(target), f"sha256:{digest}"],
                                  env={**self.env(0), "SFBLOB_CACHE": str(env_cache)}, capture_output=True, text=True, cwd=self.work, timeout=300)
            if proc.returncode != 0 or not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                bad.append(f"a fresh get of sha256:{digest} did not give back the bytes ({proc.returncode}): {proc.stderr[-200:]}")
        for cache in sorted(self.work.glob("cache*")):
            for item in cache.glob("??/*"):
                if len(item.name) == 64 and hashlib.sha256(item.read_bytes()).hexdigest() != item.name:
                    bad.append(f"cache file {item} does not hash to its name")
        bad.extend(self.stderr_seen[:5])
        return bad


def _container_login(server: str, args: argparse.Namespace):
    """Against a throwaway container: log in as its admin, make a scratch library when none is named, and return the cleanup."""
    import requests
    http = requests.Session()
    http.trust_env = False
    reply = http.post(f"{server}/api2/auth-token/", data={"username": os.environ.get("GRS_CI_ADMIN_EMAIL", "ci@grs.invalid"),
                                                           "password": os.environ.get("GRS_CI_ADMIN_PASSWORD", "grs-ci-admin")}, timeout=30)
    reply.raise_for_status()
    token = reply.json()["token"]
    os.environ["SEAFILE_TOKEN"] = token
    if args.library:
        return None
    args.library = f"grs-soak-{os.urandom(3).hex()}"
    made = http.post(f"{server}/api2/repos/", headers={"Authorization": f"Token {token}"}, data={"name": args.library}, timeout=30)
    made.raise_for_status()
    rid = made.json()["repo_id"]

    def drop() -> None:
        http.delete(f"{server}/api2/repos/{rid}/", headers={"Authorization": f"Token {token}"}, timeout=30)

    return drop


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--kill", type=float, default=0.15, help="the fraction of commands killed at a random moment")
    parser.add_argument("--server", help="a throwaway container's address (default: the in-process stub)")
    parser.add_argument("--library", default=None, help="the library name (a container: default a scratch library made and deleted here)")
    args = parser.parse_args(argv)
    stub = None
    with tempfile.TemporaryDirectory(prefix="soak-blob-", ignore_cleanup_errors=True) as tmp:
        scratch = None
        if args.server:
            server = args.server.rstrip("/")
            scratch = _container_login(server, args)
        else:
            from e2e_harness import SeafileStub
            stub = SeafileStub().start()
            server = stub.base_url
        store_url = f"seafile://{server}/{args.library or 'testlib'}/soak-{args.seed}"
        soak = Soak(args, Path(tmp), server, store_url)
        threads = [threading.Thread(target=soak.worker, args=(n + 1,)) for n in range(args.workers)]
        stop = threading.Event()

        def faults() -> None:
            rng = random.Random(args.seed + 99)
            while not stop.is_set() and stub is not None:
                time.sleep(rng.uniform(3, 9))
                stub.faults[rng.choice(["store_plus_one", "upload_400_once"])] = 1

        injector = threading.Thread(target=faults, daemon=True)
        injector.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        if stub is not None:
            stub.faults.clear()
        problems = soak.oracle()
        soak.ledger.close()
        ops = sum(1 for _ in (Path(tmp) / "ledger.jsonl").open(encoding="utf-8"))
        sys.stdout.write(f"soak: {ops} commands by {args.workers} workers in {args.seconds:.0f} s (seed {args.seed}); "
                         f"{len(soak.put_ok)} object(s) put and confirmed, {len(soak.pin_ok)} pinned\n")
        if stub is not None:
            stub.stop()
        if scratch is not None:
            scratch()
        for problem in problems:
            sys.stdout.write(f"ORACLE: {problem}\n")
        sys.stdout.write("soak: oracle clean\n" if not problems else f"soak: {len(problems)} problem(s)\n")
        return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
