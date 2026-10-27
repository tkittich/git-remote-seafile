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
from git_remote_seafile.client import SeafileAPIError
from git_remote_seafile.repair import _int_size, repair_remote

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

    def test_a_listed_pack_name_that_is_a_path_is_never_downloaded(self):
        """A name like ``..\\..\\evil.pack`` becomes a local path on Windows.

        Found by a real-server run: repair wrote the planted file outside its
        scratch directory because, unlike gc and fetch, it never validated the
        name it took from the listing.
        """
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha})
            hostile = ["..\\..\\evil.pack", "../evil.pack", "pack-x\n.pack"]
            for name in hostile:
                client.pack_files[name] = b"not a pack"
            requested: list[str] = []
            real_download = client.download_file_to

            def spy(repo_id, path, dest, progress_callback=None, max_bytes=None):
                requested.append(str(path))
                return real_download(repo_id, path, dest, progress_callback, max_bytes)

            client.download_file_to = spy
            with patch("sys.stderr") as err:
                res = _repair(client, clone, dry_run=True)
            self.assertEqual(res["status"], "broken")
            self.assertEqual(requested, [], "no hostile name reached a download")
            written = "".join(c.args[0] for c in err.write.call_args_list)
            self.assertIn("invalid name", written)
            self.assertIn("ignoring pack file", written)

    def test_a_ref_that_is_listed_but_unreadable_is_an_error_not_a_clean_bill(self):
        """Skipping it with a warning let ``--dry-run`` say "clean" over the very
        ref that was broken; the tips are read the way ``gc`` reads its mirror."""
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha, "damaged": ""})
            with self.assertRaises(SeafileAPIError) as ctx:
                _repair(client, clone, dry_run=True)
            self.assertIn("refs/heads/damaged", str(ctx.exception))
            self.assertIn("could not be read", str(ctx.exception))

    def test_the_pack_listing_is_taken_under_the_lock(self):
        """A listing taken before the lock can be reshaped by a compaction that
        finishes while we wait; the docstring promises the lock throughout."""
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha})
            events: list[str] = []
            real_list = client.list_dir

            def spy(repo_id, path):
                if str(path).endswith("/objects/pack"):
                    events.append("list packs")
                return real_list(repo_id, path)

            class Lock(_StubLock):
                def __enter__(self):
                    events.append("lock")
                    return self

            client.list_dir = spy
            with patch("git_remote_seafile.repair.RemoteLock", Lock):
                repair_remote(client, "repo1", _REPO, config=_CFG, verbose=False,
                              local_repo=clone, dry_run=True)
            self.assertEqual(events[0], "lock", events)
            self.assertIn("list packs", events)

    def test_a_transient_empty_pack_listing_is_asked_again_once(self):
        """200-with-empty for a directory that holds packs made every ref look
        broken, and a real run would re-upload the whole history."""
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha})
            self.assertEqual(_repair(client, clone)["status"], "ok")  # packs now exist
            real_list = client.list_dir
            answers = {"empty_first": True}

            def flaky(repo_id, path):
                if str(path).endswith("/objects/pack") and answers["empty_first"]:
                    answers["empty_first"] = False
                    return []
                return real_list(repo_id, path)

            client.list_dir = flaky
            with patch("sys.stderr") as err:
                res = _repair(client, clone, dry_run=True)
            self.assertEqual(res["status"], "clean", res)
            self.assertNotIn("lists no packfiles", "".join(c.args[0] for c in err.write.call_args_list))

    def test_a_remote_that_really_lists_no_packs_is_repaired_and_says_so(self):
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha})
            with patch("sys.stderr") as err:
                res = _repair(client, clone, dry_run=True)
            self.assertEqual(res["status"], "broken")
            self.assertIn("lists no packfiles at all", "".join(c.args[0] for c in err.write.call_args_list))

    def test_the_read_back_is_capped_at_the_size_that_was_uploaded(self):
        """A body longer than the pack just sent is not that pack; it must not be
        streamed to disk to find out (a hostile or broken server)."""
        with tempfile.TemporaryDirectory() as td:
            clone, sha = _history(Path(td))
            client = _StubClient({"main": sha})
            caps: dict[str, object] = {}
            real = client.download_file_to

            def spy(repo_id, path, dest, progress_callback=None, max_bytes=None):
                if Path(dest).name.startswith("verify-"):
                    caps[Path(dest).name] = (max_bytes, client.pack_files[str(path).rsplit("/", 1)[-1]])
                return real(repo_id, path, dest, progress_callback, max_bytes)

            client.download_file_to = spy
            self.assertEqual(_repair(client, clone)["status"], "ok")
            self.assertTrue(caps, "the repair read its upload back")
            for name, (cap, data) in caps.items():
                self.assertEqual(cap, len(data), name)

    def test_a_listed_size_that_is_not_a_number_counts_as_zero(self):
        for raw, want in (("12", 12), (7, 7), ("abc", 0), (None, 0), (True, 0), (3.5, 0)):
            self.assertEqual(_int_size({"size": raw}), want, raw)
        self.assertEqual(_int_size({}), 0)

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
