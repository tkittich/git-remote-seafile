"""test_repair.py - Tests for the broken-ref repair command (RUNBOOK 3.2, automated).

The stub serves real pack bytes, so the second repair run genuinely
downloads, indexes and enumerates what the first run uploaded -- the "clean"
verdict on the third run is earned by the same machinery a real server would
run, not by the stub agreeing with itself.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_remote_seafile.config import RemoteConfig
from git_remote_seafile.repair import repair_remote

_REPO = "/git-repo"


def _git(clone: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True)


def _history(td: Path, name: str = "clone") -> tuple[Path, str]:
    """A real one-commit clone; returns (path, tip sha)."""
    clone = td / name
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    (clone / "a.txt").write_text("repair me\n", encoding="utf-8")
    _git(clone, "add", "a.txt")
    _git(clone, "-c", "user.email=repair@example.com", "-c", "user.name=Repair",
         "commit", "-q", "-m", "one")
    sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return clone, sha


class _StubClient:
    """Stateful stand-in for the Seafile API across repair's calls.

    ``pack_files`` maps names to real bytes, so a second run's scratch
    repository can index what the first run uploaded.
    """

    def __init__(self, refs: dict[str, str], pack_files: dict[str, bytes] | None = None):
        self.token = "token-r"
        self.refs = dict(refs)
        self.pack_files: dict[str, bytes] = dict(pack_files or {})
        self.uploaded: list[str] = []

    def list_dir(self, repo_id: str, path: str):
        p = "/" + str(path).strip("/")
        if p.endswith("/objects/pack"):
            return [
                {"name": n, "type": "file", "size": len(b)}
                for n, b in sorted(self.pack_files.items())
            ]
        if p.endswith("/refs/heads"):
            return [{"name": n, "type": "file", "size": 41} for n in sorted(self.refs)]
        return []

    def get_file_text(self, repo_id: str, path: str):
        p = "/" + str(path).strip("/")
        for name, sha in self.refs.items():
            if p == f"{_REPO}/refs/heads/{name}":
                return sha
        return None

    def upload_file(self, repo_id, dir_path, name, src_path, replace=True, progress_callback=None):
        self.pack_files[name] = Path(src_path).read_bytes()
        self.uploaded.append(name)

    def download_file_to(self, repo_id, path, dest, progress_callback=None, max_bytes=None):
        name = str(path).rstrip("/").rsplit("/", 1)[-1]
        data = self.pack_files.get(name)
        if data is None:
            return False
        Path(dest).write_bytes(data)
        return True


class _StubLock:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def maybe_renew(self, _interval):
        pass


_CFG = RemoteConfig(lock_timeout=15, lock_lease=60, lock_settle=0, auto_gc=False, gc_threshold=20)


def _repair(client, clone: Path, **kwargs):
    kwargs.setdefault("config", _CFG)
    kwargs.setdefault("verbose", False)
    kwargs.setdefault("local_repo", clone)
    with patch("git_remote_seafile.repair.RemoteLock", _StubLock):
        return repair_remote(client, "repo1", _REPO, **kwargs)


class TestRepair(unittest.TestCase):
    def test_broken_ref_is_repaired_and_then_the_remote_is_clean(self):
        """The full arc: broken -> dry-run -> repaired -> clean.

        The third run is the proof that matters: the coverage verdict comes
        from indexing the bytes the repair uploaded, so "clean" cannot be
        the stub agreeing with itself.
        """
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha})

            res = _repair(client, clone, dry_run=True)
            self.assertEqual(res["status"], "broken")
            self.assertEqual(res["broken_refs"], [("refs/heads/main", sha)])
            self.assertEqual(client.uploaded, [], "a dry run uploads nothing")

            res = _repair(client, clone)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["repaired"], ["refs/heads/main"])
            self.assertTrue(client.uploaded, "the repair uploaded packfiles")
            self.assertTrue(
                all(n in client.pack_files for n in client.uploaded),
                "the uploaded bytes are on the stub remote",
            )

            res = _repair(client, clone)
            self.assertEqual(res["status"], "clean", res)

    def test_a_healthy_remote_is_clean_without_uploading(self):
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha})

            # First repair the remote, so it starts healthy.
            self.assertEqual(_repair(client, clone)["status"], "ok")
            uploaded = list(client.uploaded)

            client.uploaded.clear()
            res = _repair(client, clone)
            self.assertEqual(res["status"], "clean")
            self.assertEqual(client.uploaded, [], "a clean remote is not written to")
            del uploaded

    def test_a_clone_without_the_objects_leaves_the_ref_unrepaired(self):
        """Recovery runs from a clone that holds the objects; say so when it does not."""
        with tempfile.TemporaryDirectory() as td:
            _history(Path(td), "holder")  # exists to show the objects live somewhere
            sha = subprocess.run(
                ["git", "-C", str(Path(td) / "holder"), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            empty = Path(td) / "empty"
            empty.mkdir()
            _git(empty, "init", "-q")
            client = _StubClient({"main": sha})

            res = _repair(client, empty, local_repo=empty)
            self.assertEqual(res["status"], "error")
            self.assertEqual(res["repaired"], [])
            self.assertEqual(len(res["unrepaired"]), 1)
            self.assertEqual(res["unrepaired"][0][0], "refs/heads/main")
            self.assertIn("cannot enumerate", res["unrepaired"][0][1])
            self.assertEqual(client.uploaded, [], "nothing was uploaded from a clone that has nothing")

    def test_an_upload_that_lands_nothing_is_reported_not_silent(self):
        """An upload the listing cannot confirm fails the repair out loud.

        The manual procedure's trap is a repair that looks done and is not
        ("Everything up-to-date"); the automated one must fail rather than
        claim a ref it did not restore.
        """
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha})

            def lying_upload(repo_id, dir_path, name, src_path, replace=True, progress_callback=None):
                client.uploaded.append(name)  # recorded, but nothing stored

            client.upload_file = lying_upload
            with patch("git_remote_seafile.repair.RemoteLock", _StubLock):
                with self.assertRaises(RuntimeError) as ctx:
                    repair_remote(
                        client, "repo1", _REPO,
                        config=_CFG, verbose=False, local_repo=clone,
                    )
            self.assertIn("could not be verified on the remote", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
