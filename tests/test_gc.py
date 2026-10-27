"""test_gc.py - Remote compaction: fail-closed downloads, fencing, ref mirroring."""

from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import DEFAULT, MagicMock, patch

from git_remote_seafile import gc as gc_module
from git_remote_seafile.config import RemoteConfig
from git_remote_seafile.gc import (
    compact_repository,
    describe_size_delta,
    detect_object_format,
    tail_merge,
)
from git_remote_seafile.lock import LEASE_LOST


_mirror_check = patch.object(gc_module, "_verify_ref_mirror")


def setUpModule() -> None:
    """These tests mock ``subprocess.run`` wholesale, so git cannot read the mirror back.

    The read-back (``_verify_ref_mirror``) is exercised against real git in
    tests/test_gc_refs.py; here it is the compaction flow around it that is
    under test.
    """
    _mirror_check.start()


def tearDownModule() -> None:
    _mirror_check.stop()


def _uploaded_size(content) -> int:
    """The size a fixture should report for an upload argument.

    gc hands ``upload_file`` the *path* of the consolidated packfile so the
    client can stream it, while lock tickets arrive as bytes.  A fixture that
    models a remote has to answer for both -- and it has to answer with a real
    int.  gc compares the remote's reported size against the local file's
    before it deletes anything, so a MagicMock leaking into that comparison
    fails the check.  That is the guard working, not a fixture detail to paper
    over.
    """
    try:
        return content.stat().st_size
    except AttributeError:
        return len(content)


def _remember_bytes(store: dict[str, bytes], name: str, content) -> None:
    """What the remote now holds under *name*, for the read-back check."""
    try:
        store[name] = content.read_bytes()
    except AttributeError:
        store[name] = bytes(content)


def _read_back(store: dict[str, bytes]):
    """A ``download_file_to`` that serves what was uploaded and nothing else."""
    def download(repo_id, path, dest, *args, **kwargs):
        name = str(path).rsplit("/", 1)[-1]
        if name in store:
            Path(dest).write_bytes(store[name])
            return True
        return DEFAULT
    return download


def _pack_listing(base_entries: list[dict], uploaded_sizes: dict[str, int]) -> list[dict]:
    """A pack-directory listing that shows what was actually uploaded.

    gc verifies the consolidated packfile is present on the remote, at the size
    it has locally, *before* it deletes the packfiles it replaces.  A fixture
    whose listing is frozen at its pre-upload state therefore fails that check:
    correctly, but for a reason that has nothing to do with what the test is
    about.  Modelling the remote as "what was there, plus what we just sent it"
    keeps the guard exercised instead of stubbed out.
    """
    return base_entries + [{"name": n, "size": s} for n, s in uploaded_sizes.items()]


class TestRemoteGC(unittest.TestCase):
    def test_a_growth_is_not_reported_as_a_saving(self):
        """Compaction can enlarge a repository, and the message must not lie.

        Repacking can produce a slightly larger pack when the existing packs
        were already tight.  The delta is signed because that is the honest
        value, but printing it raw produced ``(saved -12.0 KiB)`` -- which reads
        like a bug and calls a growth a saving.
        """
        self.assertEqual(describe_size_delta(100), "saved ~100.0 KiB")

        grown = describe_size_delta(-12)
        self.assertIn("grew", grown)
        self.assertIn("12.0 KiB", grown)
        self.assertNotIn("-12", grown)

    def test_an_unchanged_size_is_not_called_a_growth(self):
        self.assertEqual(describe_size_delta(0), "saved ~0 B")

    def test_gc_skipped_below_threshold(self):
        mock_client = MagicMock()
        mock_client.list_dir.return_value = [{"name": "pack-1.pack"}]

        res = compact_repository(mock_client, "repo1", "/path", min_packs=3, verbose=False)
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["count"], 1)

    def test_gc_defers_when_the_remote_store_exceeds_the_size_cap(self):
        """Compaction costs one full download + re-upload of the history.

        Above ``max_total_bytes`` that cost is no longer an in-band side task,
        so compaction must defer to a manual off-hours run even when the pack
        count alone says "go".  The deferral happens before any transfer:
        nothing may be downloaded or deleted on its way out.
        """
        mock_client = MagicMock()
        # Three packs of ~10 GiB each: past the cap although the count is fine.
        mock_client.list_dir.return_value = [
            {"name": f"pack-{i}.pack", "size": 10 * 1024**3} for i in range(3)
        ]

        res = compact_repository(
            mock_client, "repo1", "/path", min_packs=2, max_total_bytes=20 * 1024**3,
        )

        self.assertEqual(res["status"], "skipped")
        self.assertIn("deferred", res["message"])
        self.assertEqual(res["count"], 3)
        mock_client.download_file.assert_not_called()
        mock_client.delete_file.assert_not_called()

    def test_gc_without_a_size_cap_is_not_blocked_by_size(self):
        # ``max_total_bytes=None`` (the manual CLI path) must add no size
        # friction: with the cap absent, an oversized store still gets past
        # the gate and into the transfer machinery.  The gate's only other
        # skip reason is pack count, so below that threshold it skips -- which
        # proves the *size* check did not fire (it would skip regardless of
        # count).
        mock_client = MagicMock()
        mock_client.list_dir.return_value = [
            {"name": f"pack-{i}.pack", "size": 10 * 1024**3} for i in range(5)
        ]

        with patch("git_remote_seafile.gc.run_git_checked"):
            res = compact_repository(mock_client, "repo1", "/path", min_packs=9)

        self.assertEqual(res["status"], "skipped")  # skipped on count...
        self.assertNotIn("deferred", res.get("message", ""))  # ...not on size.

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_compaction_flow(self, mock_subprocess):
        mock_client = MagicMock()
        # list_dir must answer per path: the compactor reads both the pack
        # directory and the ref namespace, and it now refuses to run at all when
        # no refs can be read -- an empty ref listing is exactly what a failed
        # request looks like, and compacting on that guess destroys history.
        # The pack branch also has to show what was uploaded: gc verifies the
        # consolidated packfile landed, at the right size, before it deletes the
        # packfiles it replaces.
        uploaded_sizes: dict[str, int] = {}
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            _pack_listing([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}], uploaded_sizes)
            if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        mock_client.get_file_bytes.return_value = b"PACK-DATA"
        # renew()'s takeover fence reads the ticket back before rewriting it,
        # so the client must serve the lock payloads it stored; every other
        # read is a ref file ("sha-main").
        lock_payloads: list[str] = []
        ticket_paths: list[str] = []

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if ".git-lock" in filename or ".git-lock.d" in parent:
                lock_payloads.append(content.decode("utf-8"))
                ticket_paths.append(f"{parent.rstrip('/')}/{filename}")
            else:
                uploaded_sizes[filename] = _uploaded_size(content)
            return True

        mock_client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            if ".git-lock" in path:
                # Serve the stored ticket payload once one exists; before that
                # (including acquire's orphan-mirror cleanup probe) there is
                # nothing on the remote.
                return lock_payloads[-1] if lock_payloads else None
            return "sha-main"

        mock_client.get_file_text.side_effect = fake_get_text

        # Mock git pack-objects writing a packfile
        seen_cmds: list[list[str]] = []

        def fake_subprocess(cmd, _env=None, **kwargs):
            seen_cmds.append(list(cmd))
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"abcdef1234567890abcdef1234567890abcdef12\n")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake_subprocess

        with patch("pathlib.Path.glob") as mock_glob:
            p_pack = MagicMock()
            p_pack.name = "pack-abcdef1234567890abcdef1234567890abcdef12.pack"
            p_pack.read_bytes.return_value = b"NEW-PACK"
            p_pack.stat.return_value.st_size = 8

            p_idx = MagicMock()
            p_idx.name = "pack-abcdef1234567890abcdef1234567890abcdef12.idx"
            p_idx.read_bytes.return_value = b"NEW-IDX"
            p_idx.stat.return_value.st_size = 7
            # Neither staged pack derives an index of its own; the pair is
            # spelled out, so gc verifies exactly two files landed.
            p_pack.with_suffix.return_value.is_file.return_value = False
            p_idx.with_suffix.return_value.is_file.return_value = False

            mock_glob.return_value = [p_pack, p_idx]

            res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["old_packs"], 2)
            # The repack is told the size cap: a pack the server will not
            # accept in one upload is a compaction that cannot finish.
            repack = [c for c in seen_cmds if "repack" in c]
            self.assertTrue(repack)
            self.assertTrue(any(a.startswith("--max-pack-size=") for a in repack[0]))
            # Every pack download renews the lease as it goes: gc holds the
            # lock across multi-gigabyte transfers.
            for call in mock_client.download_file_to.call_args_list:
                self.assertIsNotNone(call.kwargs.get("progress_callback"))
            # Name what was deleted rather than counting it: a bare
            # "call_count == 5" passes just as happily if gc removed the wrong
            # five things.
            deleted = [call.args[1] for call in mock_client.delete_entry.call_args_list]
            self.assertEqual(
                sorted(p for p in deleted if ".git-lock.d" not in p),
                [
                    "/path/objects/pack/pack-1.idx",
                    "/path/objects/pack/pack-1.pack",
                    "/path/objects/pack/pack-2.idx",
                    "/path/objects/pack/pack-2.pack",
                ],
                "exactly the two obsolete packs and their indexes, and nothing else",
            )
            # ...plus the holder's own ticket, which the lock releases last.
            self.assertTrue(ticket_paths, "the holder's ticket was never uploaded")
            self.assertEqual(
                sorted(set(p for p in deleted if ".git-lock.d" in p)),
                sorted(set(ticket_paths)),
            )

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_compaction_aborts_on_failed_pack_download(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}, {"name": "pack-3.pack"}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        def fake_get_file_bytes(repo_id, path):
            if "pack-1.pack" in path:
                return b""
            return b"PACK-DATA"

        mock_client.get_file_bytes.side_effect = fake_get_file_bytes
        mock_client.download_file_to = MagicMock(side_effect=OSError("streaming unavailable"))
        mock_client.get_file_text.return_value = "sha-main"

        def fake_subprocess(cmd, _env=None, **kwargs):
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"abcdef1234567890abcdef1234567890abcdef12\n")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake_subprocess

        res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("Failed to download pack-1.pack", res["message"])
        # Ensure no remote pack files were deleted when download failed (N-2 fail-closed)
        deleted_paths = [call.args[1] for call in mock_client.delete_entry.call_args_list if len(call.args) > 1]
        self.assertFalse(any(p.endswith(".pack") for p in deleted_paths))

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_compaction_aborts_on_pack_size_mismatch(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack", "size": 1000}, {"name": "pack-2.pack", "size": 500}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        # Server returns fewer bytes than reported in list_dir
        mock_client.get_file_bytes.return_value = b"TRUNCATED"
        mock_client.download_file_to = MagicMock(side_effect=OSError("streaming unavailable"))

        res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("does not match expected size", res["message"])
        deleted_paths = [call.args[1] for call in mock_client.delete_entry.call_args_list if len(call.args) > 1]
        self.assertFalse(any(p.endswith(".pack") for p in deleted_paths))

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_compaction_aborts_when_pack_exceeds_memory_limit(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack", "size": 20 * 1024 * 1024}, {"name": "pack-2.pack", "size": 500}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        mock_client.download_file_to = MagicMock(side_effect=OSError("streaming unavailable"))

        res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("exceeds in-memory buffer limit", res["message"])
        # get_file_bytes must NOT be called for 20MB file
        mock_client.get_file_bytes.assert_not_called()
        deleted_paths = [call.args[1] for call in mock_client.delete_entry.call_args_list if len(call.args) > 1]
        self.assertFalse(any(p.endswith(".pack") for p in deleted_paths))

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_preserves_all_branches_and_tags(self, mock_subprocess):
        mock_client = MagicMock()
        uploaded_sizes: dict[str, int] = {}
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            _pack_listing([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}], uploaded_sizes)
            if "objects/pack" in path
            else [{"type": "file", "name": "main"}, {"type": "file", "name": "dev"}, {"type": "file", "name": "feature"}] if "refs/heads" in path
            else [{"type": "file", "name": "v1.0"}, {"type": "file", "name": "v2.0"}] if "refs/tags" in path
            else []
        )
        mock_client.get_file_bytes.return_value = b"PACK-DATA"
        # Same lock-payload plumbing as test_gc_compaction_flow: the takeover
        # fence in renew() reads the ticket back before rewriting it.
        lock_payloads: list[str] = []

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if ".git-lock" in filename or ".git-lock.d" in parent:
                lock_payloads.append(content.decode("utf-8"))
            else:
                uploaded_sizes[filename] = _uploaded_size(content)
            return True

        mock_client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            if ".git-lock" in path and lock_payloads:
                return lock_payloads[-1]
            return "sha000111"

        mock_client.get_file_text.side_effect = fake_get_text

        def fake_subprocess(cmd, _env=None, **kwargs):
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"sha123\n")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake_subprocess

        with patch("pathlib.Path.glob") as mock_glob:
            p_pack = MagicMock()
            p_pack.name = "pack-sha123.pack"
            p_pack.read_bytes.return_value = b"NEW-PACK"
            p_pack.stat.return_value.st_size = 10
            p_pack.with_suffix.return_value.is_file.return_value = False
            mock_glob.return_value = [p_pack]

            res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
            self.assertEqual(res["status"], "ok")

            # Check that repack was called
            repack_called = any("repack" in cmd for cmd, _ in [(c.args[0], c) for c in mock_subprocess.call_args_list if c.args])
            self.assertTrue(repack_called)

            # What "preserves all branches and tags" has to mean: every one of
            # them is in the mirror the repack computed reachability from.  A
            # ref missing here is a branch whose commits the repack prunes.
            mirrored = gc_module._verify_ref_mirror.call_args.args[1]
            self.assertEqual(
                sorted(mirrored),
                ["refs/heads/dev", "refs/heads/feature", "refs/heads/main",
                 "refs/tags/v1.0", "refs/tags/v2.0"],
            )

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_aborts_and_preserves_packs_if_lock_lost_before_cleanup(self, mock_subprocess):
        """GC must verify lock ownership before deleting old remote packs and abort if lost (N-4)."""
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}] if "objects/pack" in path
            else [{"type": "file", "name": "main"}] if "refs/heads" in path
            else []
        )
        mock_client.get_file_bytes.return_value = b"PACK-DATA"
        mock_client.get_file_text.return_value = "sha000111"
        mock_subprocess.return_value = MagicMock(returncode=0)

        with patch("pathlib.Path.glob") as mock_glob, patch("git_remote_seafile.gc.RemoteLock") as mock_lock_cls:
            p_pack = MagicMock()
            p_pack.name = "pack-sha123.pack"
            p_pack.stat.return_value.st_size = 10
            mock_glob.return_value = [p_pack]

            mock_lock = MagicMock()
            mock_lock.__enter__.return_value = mock_lock
            # Verify ownership fails right before step 7
            mock_lock.verify_ownership.return_value = False
            mock_lock_cls.return_value = mock_lock

            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)

            self.assertIn("refusing to delete obsolete packfiles", str(ctx.exception))
            # Remote packs must NOT be deleted!
            mock_client.delete_entry.assert_not_called()


class _StubLock:
    """A minimal stand-in for ``RemoteLock`` that always proves ownership."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def maybe_renew(self, _interval):
        pass

    def verify_ownership(self):
        return True

    def ownership_state(self):
        return "held"


class TestGcRecoveryAndFencing(unittest.TestCase):
    """The compaction branches a healthy remote never reaches.

    gc is the module that *deletes* things, so its error paths are the ones
    worth pinning.  A corrupt index, an index too large to buffer, a lock
    object from an older caller, and a delete the server refuses each have to
    end in a predictable outcome -- never in an exception thrown from the
    middle of the deletion loop, with the remote half-compacted.
    """

    def _client(self, entries, *, delete_ok=True, body=b"PACK-DATA"):
        """A client whose list_dir answers per path, as the compactor asks it.

        The lock plumbing mirrors test_gc_compaction_flow: renew()'s takeover
        fence reads its own ticket back before rewriting it, so uploads of
        lock files have to be replayable through get_file_text.  Pack uploads
        are recorded by name and size so the pack listing can show them -- gc
        checks they landed before it deletes anything.
        """
        client = MagicMock()
        client.get_file_bytes.return_value = body
        client.delete_entry.return_value = delete_ok

        lock_payloads: list[str] = []
        uploaded_sizes: dict[str, int] = {}
        uploaded_bytes: dict[str, bytes] = {}
        client.download_file_to.side_effect = _read_back(uploaded_bytes)

        client.list_dir.side_effect = lambda repo_id, path: (
            _pack_listing(entries, uploaded_sizes)
            if "objects/pack" in path
            else [{"type": "file", "name": "main"}]
            if "refs/heads" in path
            else []
        )

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if ".git-lock" in filename or ".git-lock.d" in parent:
                lock_payloads.append(content.decode("utf-8"))
            else:
                uploaded_sizes[filename] = _uploaded_size(content)
                _remember_bytes(uploaded_bytes, filename, content)
            return True

        client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            if ".git-lock" in path:
                return lock_payloads[-1] if lock_payloads else None
            return "sha-main"

        client.get_file_text.side_effect = fake_get_text
        # Exposed so a test that overrides list_dir or upload_file can still
        # keep the remote consistent with what was sent: gc verifies the
        # consolidated packfile landed before it deletes anything, so a listing
        # that forgets the upload fails that check.
        client.uploaded_sizes = uploaded_sizes
        return client

    @staticmethod
    def _subprocess_fake(verify_pack_rc=0):
        def fake(cmd, _env=None, **kwargs):
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"abcdef1234567890abcdef1234567890abcdef12\n")
            if "verify-pack" in cmd:
                return MagicMock(returncode=verify_pack_rc, stderr=b"")
            return MagicMock(returncode=0)

        return fake

    @staticmethod
    def _one_new_pack(mock_glob):
        """Stage a single consolidated pack under a name unlike the old ones.

        The new name has to differ from every old pack, or step 7 would find
        nothing to delete and the assertions about deletion would be vacuous.
        No index is staged, so the pack is the only thing gc has to verify
        landed before it deletes anything -- and ``with_suffix`` is pinned to
        answer that honestly, because an auto-created MagicMock there would be
        truthy and stage a phantom ``.idx`` under a name no listing can show.
        """
        p_pack = MagicMock()
        p_pack.name = "pack-abcdef1234567890abcdef1234567890abcdef12.pack"
        p_pack.read_bytes.return_value = b"NEW-PACK"
        p_pack.stat.return_value.st_size = 8
        p_pack.with_suffix.return_value.is_file.return_value = False
        mock_glob.return_value = [p_pack]

    @staticmethod
    def _deleted_paths(client):
        return [c.args[1] for c in client.delete_entry.call_args_list if len(c.args) > 1]

    # --- a remote whose index is wrong -----------------------------------

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_regenerates_a_truncated_remote_index(self, mock_subprocess):
        """A short .idx is regenerable, so gc warns and carries on.

        The index is derivable from the pack; the pack is not derivable from
        anything.  Treating a bad index as fatal would refuse a compaction that
        could have succeeded, so this path discards it and rebuilds locally.
        """
        client = self._client(
            [
                {"name": "pack-1.pack"},
                {"name": "pack-2.pack"},
                {"name": "pack-1.idx", "size": 999},
                {"name": "pack-2.idx", "size": 999},
            ]
        )
        mock_subprocess.side_effect = self._subprocess_fake()

        err = io.StringIO()
        with patch("pathlib.Path.glob") as mock_glob, contextlib.redirect_stderr(err):
            self._one_new_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        self.assertIn("will regenerate locally", err.getvalue())
        # Regeneration means index-pack actually ran, not merely that gc
        # continued: the pack is unusable without an index beside it.
        regenerated = [c for c in mock_subprocess.call_args_list if c.args and "index-pack" in c.args[0]]
        self.assertTrue(regenerated, "the truncated index was never rebuilt")

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_a_ref_that_moves_during_the_repack_stops_every_deletion(self, mock_subprocess):
        """A writer that ignores the lock pushed while gc was repacking.

        The consolidated pack was computed from the refs read before the
        repack, so it does not carry the newcomer's objects; deleting the old
        packs would delete the only ones that do, and the ref would survive,
        naming nothing (reproduced against the stub by an independent review).
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        original = client.get_file_text.side_effect

        def moved_after_upload(repo_id, path):
            if ".git-lock" not in path and client.uploaded_sizes:
                return "sha-pushed-meanwhile"
            return original(repo_id, path)

        client.get_file_text.side_effect = moved_after_upload
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob") as mock_glob:
            self._one_new_pack(mock_glob)
            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)
        self.assertIn("refs changed while", str(ctx.exception))
        packs = [p for p in self._deleted_paths(client) if "objects/pack" in p]
        self.assertEqual(packs, [], "an old pack was deleted")

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_regenerates_an_index_that_fails_verification(self, mock_subprocess):
        """A present-but-corrupt .idx is rebuilt, not trusted.

        "The file downloaded" is not the same as "the file is an index".  gc
        verifies what it fetched and regenerates when the check fails, because
        repacking against a bad index is how a compaction quietly loses objects.
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        mock_subprocess.side_effect = self._subprocess_fake(verify_pack_rc=1)

        with patch("pathlib.Path.glob") as mock_glob:
            self._one_new_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        regenerated = [c for c in mock_subprocess.call_args_list if c.args and "index-pack" in c.args[0]]
        self.assertTrue(regenerated, "the corrupt index was never rebuilt")

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_aborts_when_an_index_exceeds_the_in_memory_ceiling(self, mock_subprocess):
        """An index too large to buffer, with streaming unavailable, is fatal.

        fetch_pack_artifact raises rather than silently skipping the index, and
        gc must convert that into the same fail-closed error dict as any other
        download failure -- with nothing deleted.
        """
        client = self._client(
            [
                {"name": "pack-1.pack"},
                {"name": "pack-2.pack"},
                {"name": "pack-1.idx", "size": 20 * 1024 * 1024},
            ]
        )
        # streaming unavailable -> the in-memory path.  A side_effect (not `del`):
        # a spec-less MagicMock regenerates deleted attributes, so `del` never
        # actually disabled streaming and these cases passed for the wrong reason.
        client.download_file_to = MagicMock(side_effect=OSError("streaming unavailable"))
        mock_subprocess.return_value = MagicMock(returncode=0)

        res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "error")
        self.assertIn("exceeds in-memory buffer limit", res["message"])
        self.assertFalse([p for p in self._deleted_paths(client) if p.endswith(".pack")])

    # --- reporting -------------------------------------------------------

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_names_the_files_it_could_not_delete(self, mock_subprocess):
        """A refused delete must be reported, not counted as a success.

        delete_entry returning False means the obsolete pack is *still on the
        remote*.  gc uploads the consolidated pack before deleting, so a silent
        failure here leaves duplicates behind -- recoverable, but only if the
        user is actually told which files survived.
        """
        client = self._client(
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}], delete_ok=False
        )
        mock_subprocess.side_effect = self._subprocess_fake()

        err = io.StringIO()
        with patch("pathlib.Path.glob") as mock_glob, contextlib.redirect_stderr(err):
            self._one_new_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=True)

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["deleted_packs"], 0)
        self.assertTrue(res["deletion_failures"])
        self.assertIn("could not delete obsolete remote files", err.getvalue())
        self.assertIn("pack-1.pack", err.getvalue())

    def test_gc_says_why_it_skipped_when_verbose(self):
        """The skip is not an error, but silent skipping looks like a no-op bug."""
        client = MagicMock()
        client.list_dir.return_value = [{"name": "pack-1.pack"}]

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res = compact_repository(client, "repo1", "/path", min_packs=3, verbose=True)

        self.assertEqual(res["status"], "skipped")
        self.assertIn("requires at least 3", err.getvalue())

    # --- lease renewal during transfers ----------------------------------

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_renews_the_lease_while_transferring(self, mock_subprocess):
        """Both transfer callbacks must renew, or the lease lapses mid-transfer.

        A multi-gigabyte download is the longest phase gc holds the lock for.
        The callbacks exist precisely so a long transfer cannot outlive its own
        lease and be lawfully taken over while it is still writing.
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        renewals: list[float] = []

        class _CountingLock(_StubLock):
            def maybe_renew(self, interval):
                renewals.append(interval)

        def fake_download(repo_id, remote_path, dest, progress_callback=None):
            if progress_callback is not None:
                progress_callback(1, 2)  # drives gc's download callback
            Path(dest).write_bytes(b"PACK-DATA")
            return True

        client.download_file_to.side_effect = fake_download

        upload_callbacks: list[int] = []

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if progress_callback is not None:
                upload_callbacks.append(1)
                progress_callback(1, 2)  # drives gc's upload callback
            client.uploaded_sizes[filename] = _uploaded_size(content)
            return True

        client.upload_file.side_effect = fake_upload
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob") as mock_glob, patch(
            "git_remote_seafile.gc.RemoteLock", _CountingLock
        ):
            self._one_new_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        self.assertTrue(renewals, "the lease was never renewed")
        self.assertTrue(upload_callbacks, "the upload callback never fired")

    # --- the fail-closed guards ------------------------------------------

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_rejects_a_remote_pack_name_it_cannot_trust(self, mock_subprocess):
        """A name that could escape objects/pack aborts before anything is read.

        The pack name is interpolated into a remote path *and* a local
        filename, so a traversal attempt is not a naming quirk -- it is the
        input that decides where gc reads and writes.  Refuse the whole run.
        """
        client = self._client([{"name": "pack-../../escape.pack"}, {"name": "pack-1.pack"}])
        mock_subprocess.return_value = MagicMock(returncode=0)

        res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "error")
        self.assertIn("Invalid packfile name", res["message"])
        client.get_file_bytes.assert_not_called()

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_refuses_when_no_refs_can_be_read(self, mock_subprocess):
        """An empty ref listing is far more likely a failed request than an empty repo.

        With no refs mirrored, ``git repack -a -d`` treats every object as
        unreachable and the cleanup step deletes the lot.  A skipped compaction
        is cheap; deleted history is not, so gc refuses rather than guesses.
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}] if "objects/pack" in path else []
        )
        mock_subprocess.return_value = MagicMock(returncode=0)

        with self.assertRaises(RuntimeError) as ctx:
            compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("no refs could be read", str(ctx.exception))
        self.assertFalse([p for p in self._deleted_paths(client) if p.endswith(".pack")])

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_refuses_a_ref_outside_heads_and_tags(self, mock_subprocess):
        """A ref the mirror cannot carry means objects it cannot prove reachable.

        The remote is a plain file store, so a hand-created ``refs/notes/commits``
        (or any other off-namespace ref) can exist.  The mirror walks only
        refs/heads and refs/tags, so that ref's objects look unreachable in the
        scratch repo and step 7 would delete them permanently while the ref
        file survived, broken.  gc must refuse before downloading anything.
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}]
            if "objects/pack" in path
            else [{"type": "file", "name": "commits"}]
            if path.endswith("/refs/notes")
            else [{"type": "dir", "name": "notes"}]
            if path.endswith("/refs")
            else []
        )
        mock_subprocess.return_value = MagicMock(returncode=0)

        res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "error")
        self.assertIn("refs/notes/commits", res["message"])
        self.assertIn("outside refs/heads and refs/tags", res["message"])
        # the refusal happens before a single pack is downloaded
        client.get_file_bytes.assert_not_called()
        self.assertFalse(
            [p for p in self._deleted_paths(client) if p.endswith(".pack")]
        )

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_refuses_a_ref_name_git_could_not_honour(self, mock_subprocess):
        """An invalid ref name under a mirrored namespace is the same hazard.

        ``iter_refs`` warns and skips names git-check-ref-format rejects, so
        the mirror cannot carry them either -- and their objects would be
        deleted exactly like an off-namespace ref's.
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}]
            if "objects/pack" in path
            else [{"type": "dir", "name": "heads"}]
            if path.endswith("/refs")
            else [{"type": "file", "name": ".hidden.lock"}]
            if path.endswith("/refs/heads")
            else []
        )
        mock_subprocess.return_value = MagicMock(returncode=0)

        res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "error")
        self.assertIn("refs/heads/.hidden.lock", res["message"])
        client.get_file_bytes.assert_not_called()

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_accepts_a_remote_with_only_mirrored_refs(self, mock_subprocess):
        """The new guard must not refuse the repositories gc exists for.

        Heads and tags -- including nested names -- are exactly what the
        mirror carries, so they must never trip the off-namespace refusal.
        The subprocess stub makes repack a no-op, so the downloaded packs are
        re-uploaded as "consolidated" and the run reports ok: the point is
        that the guard passed, not the compaction arithmetic.
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        client.list_dir.side_effect = lambda repo_id, path: (
            _pack_listing([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}], client.uploaded_sizes)
            if "objects/pack" in path
            else [{"type": "dir", "name": "heads"}]
            if path.endswith("/refs")
            else []
        )
        mock_subprocess.return_value = MagicMock(returncode=0)
        with patch(
            "git_remote_seafile.gc.iter_refs",
            side_effect=lambda client, repo_id, base, ns, **kw: iter(
                [("refs/heads/feature/auth", "sha-a"), ("refs/heads/main", "sha-b")]
            ),
        ):
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["old_packs"], 2)

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_refuses_a_ref_with_an_empty_sha(self, mock_subprocess):
        """gc's own boundary check: an empty SHA must never be mirrored.

        ``iter_refs`` already filters these -- it yields a ref only when the
        stored content is non-empty (``refs.py:116-117`` and ``128-129``) -- so
        this guard cannot fire today.  It is kept, and pinned here, because what
        it protects is not a nicety: a ref file written with an empty SHA makes
        every object reachable from that ref look unreachable to
        ``repack -a -d``.  The test drives gc's boundary directly rather than
        pretending the producer can emit one.
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        mock_subprocess.return_value = MagicMock(returncode=0)

        with patch(
            "git_remote_seafile.gc.iter_refs",
            return_value=iter([("refs/heads/main", "   ")]),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("empty SHA", str(ctx.exception))

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_refuses_when_repack_fails(self, mock_subprocess):
        """A failed repack means the new pack is untrustworthy, so nothing is deleted."""
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])

        def fake(cmd, _env=None, **kwargs):
            if "repack" in cmd:
                return MagicMock(returncode=1, stderr="fatal: bad object refs/heads/main")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake

        with self.assertRaises(RuntimeError) as ctx:
            compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("git repack failed", str(ctx.exception))
        self.assertFalse([p for p in self._deleted_paths(client) if p.endswith(".pack")])

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_refuses_when_repack_produces_no_pack(self, mock_subprocess):
        """Repacking that yields nothing leaves the old packs unproven-superseded."""
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("produced no packfile", str(ctx.exception))

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_carries_on_when_a_bad_index_cannot_be_unlinked(self, mock_subprocess):
        """A locked index file must not abort the compaction.

        Windows holds pack/idx files open, so unlink can fail for a reason that
        has nothing to do with the compaction's correctness.  gc discards a bad
        index on a best-effort basis and regenerates it either way; the cleanup
        failing must not become a failed gc.
        """
        client = self._client(
            [
                {"name": "pack-1.pack"},
                {"name": "pack-2.pack"},
                {"name": "pack-1.idx", "size": 999},
                {"name": "pack-2.idx", "size": 999},
            ]
        )
        mock_subprocess.side_effect = self._subprocess_fake(verify_pack_rc=1)

        err = io.StringIO()
        with patch("pathlib.Path.glob") as mock_glob, patch(
            "pathlib.Path.unlink", side_effect=OSError("in use")
        ), contextlib.redirect_stderr(err):
            self._one_new_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        self.assertIn("will regenerate locally", err.getvalue())

    # --- object format (D18) ---------------------------------------------

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_gc_initialises_a_sha256_scratch_repo_for_a_sha256_remote(self, mock_subprocess):
        """A 64-hex pack name means the scratch repo must be SHA-256 too.

        The pack *names* are the only format signal available before any pack is
        read.  Initialising SHA-1 and then handing it SHA-256 packs is what made
        compaction fail outright on SHA-256 remotes.  ``test_e2e`` proves the
        end-to-end fix; this pins the detection itself, so a regression names
        the format instead of surfacing later as an index-pack error.
        """
        client = self._client([{"name": f"pack-{'a' * 64}.pack"}, {"name": "pack-1.pack"}])
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob") as mock_glob:
            self._one_new_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        init_calls = [c for c in mock_subprocess.call_args_list if c.args and "init" in c.args[0]]
        self.assertTrue(init_calls, "the scratch repository was never initialised")
        self.assertIn("--object-format=sha256", init_calls[0].args[0])
        # ...and the format is not the only half of D18: the verify/index-pack
        # calls must also carry -C, or they have no repository context to
        # inherit the format from and silently fall back to SHA-1.
        pack_calls = [
            c for c in mock_subprocess.call_args_list
            if c.args and ("verify-pack" in c.args[0] or "index-pack" in c.args[0])
        ]
        self.assertTrue(pack_calls, "no pack was verified or indexed")
        for call in pack_calls:
            self.assertIn("-C", call.args[0])


class _FencedOnceLock:
    """A lock that proves ownership once, then reports it lost.

    gc fences before the upload verification and again before every deletion.
    This models the *second* call failing, which is the case that used to go
    unnoticed: the delete loop ignored its renewal result, so a lease lost
    mid-cleanup kept deleting packfiles inside another client's push.
    """

    def __init__(self, *args, **kwargs):
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def maybe_renew(self, _interval):
        pass

    def verify_ownership(self):
        self.calls += 1
        return self.calls == 1

    def ownership_state(self):
        return LEASE_LOST


class TestContentReadBackIsCapped(unittest.TestCase):
    def test_the_read_back_stops_at_the_size_that_was_uploaded(self):
        """A body longer than the pack just sent is not that pack; the probe
        download must not stream it to disk to find that out."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pack = Path(td) / "pack-a.pack"
            idx = Path(td) / "pack-a.idx"
            pack.write_bytes(b"P" * 10)
            idx.write_bytes(b"I" * 4)
            client = MagicMock()
            caps: list[int | None] = []

            def download(repo_id, path, dest, *args, max_bytes=None, **kwargs):
                caps.append(max_bytes)
                Path(dest).write_bytes((pack if str(path).endswith(".pack") else idx).read_bytes())
                return True

            client.download_file_to.side_effect = download
            gc_module._verify_content(
                client, "repo1", "/r/objects/pack", [pack], Path(td),
                old_adj="old", new_desc="new packfile",
            )
        self.assertEqual(sorted(caps), [4, 10])


class TestGcVerifiesTheUploadBeforeDeletingAnything(unittest.TestCase):
    """A5: the packs gc deletes are the ones the repository is served from.

    The upload returning True is not evidence that the bytes reached the
    server.  A truncated write, a quota error a proxy answered 2xx, or an .idx
    that never landed all look exactly like a success from here, and what gc
    would then do is delete the only complete copy of the repository.  So the
    consolidated packfiles have to be *proven* present -- listed, at the size
    they have locally -- before a single old pack goes.
    """

    _NEW_PACK = "pack-abcdef1234567890abcdef1234567890abcdef12.pack"

    def _client_with_listing(self, base_entries):
        """A client whose pack listing grows to include what it was sent.

        gc reads the pack directory twice -- once to discover what is there,
        and again to prove the consolidated packfile landed -- so a listing
        frozen at either state models the wrong remote.  This one starts from
        ``base_entries`` and adds each upload as it happens, and
        ``remote_size_override`` describes a remote that stored a file *wrong*:
        the size it claims is the size gc has to trust.
        """
        client = MagicMock()
        client.get_file_bytes.return_value = b"PACK-DATA"
        client.delete_entry.return_value = True
        #: name -> the size the remote will claim, or None for "never landed".
        client.remote_size_override = {}
        uploaded_sizes: dict[str, int] = {}
        lock_payloads: list[str] = []

        def list_dir(repo_id, path):
            if "objects/pack" in path:
                entries = list(base_entries)
                for name, size in uploaded_sizes.items():
                    claimed = client.remote_size_override.get(name, size)
                    if claimed is None:
                        continue  # the write never landed, so it is not listed
                    entries.append({"name": name, "size": claimed})
                return entries
            if "refs/heads" in path:
                return [{"type": "file", "name": "main"}]
            return []

        client.list_dir.side_effect = list_dir

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if ".git-lock" in filename or ".git-lock.d" in parent:
                lock_payloads.append(content.decode("utf-8"))
            else:
                uploaded_sizes[filename] = _uploaded_size(content)
            return True

        client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            # Ref files carry a SHA; the lock plumbing has to replay its own
            # ticket so renew()'s takeover fence can read it back.
            if ".git-lock" in path:
                return lock_payloads[-1] if lock_payloads else None
            return "sha-main"

        client.get_file_text.side_effect = fake_get_text
        return client

    @staticmethod
    def _deleted_paths(client):
        """Pack paths the run tried to delete, ignoring the lock's own ticket."""
        return [
            c.args[1] for c in client.delete_entry.call_args_list
            if len(c.args) > 1 and ".git-lock" not in c.args[1]
        ]

    @staticmethod
    def _subprocess_fake():
        """A no-op git: these tests are about the checks, not the repacking."""

        def fake(cmd, _env=None, **kwargs):
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"abcdef1234567890abcdef1234567890abcdef12\n")
            return MagicMock(returncode=0)

        return fake

    @staticmethod
    def _staged_pack(mock_glob):
        """The consolidated pack the scratch repack is supposed to have made."""
        p_pack = MagicMock()
        p_pack.name = TestGcVerifiesTheUploadBeforeDeletingAnything._NEW_PACK
        p_pack.read_bytes.return_value = b"NEW-PACK"
        p_pack.stat.return_value.st_size = 8
        p_pack.with_suffix.return_value.is_file.return_value = False
        mock_glob.return_value = [p_pack]

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_a_truncated_upload_leaves_the_old_packs_alone(self, mock_subprocess):
        """A pack that landed short must not count as landed.

        The fixture uploads the pack in full and then reports it short on the
        remote, which is what a write the server never finished looks like.
        """
        client = self._client_with_listing([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        client.remote_size_override[self._NEW_PACK] = 3
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob") as mock_glob:
            self._staged_pack(mock_glob)
            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("could not be verified", str(ctx.exception))
        self.assertIn("3 bytes on the remote but 8 locally", str(ctx.exception))
        self.assertEqual(self._deleted_paths(client), [], "nothing may be deleted on an unverified upload")

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_a_pack_absent_from_the_listing_leaves_the_old_packs_alone(self, mock_subprocess):
        """The upload reported success and the file is simply not there."""
        client = self._client_with_listing([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        client.remote_size_override[self._NEW_PACK] = None
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob") as mock_glob:
            self._staged_pack(mock_glob)
            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("not listed on the remote with a readable size", str(ctx.exception))
        self.assertEqual(self._deleted_paths(client), [], "nothing may be deleted on an unverified upload")

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_a_listing_failure_during_verification_leaves_the_old_packs_alone(self, mock_subprocess):
        """Cannot verify is not the same as verified, and must not be read as such."""
        client = self._client_with_listing([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        mock_subprocess.side_effect = self._subprocess_fake()

        calls = {"n": 0}
        real_list_dir = client.list_dir.side_effect

        def flaky_list_dir(repo_id, path):
            if "objects/pack" in path:
                calls["n"] += 1
                # The first read discovers the packs; the verification read is
                # the one that fails.
                if calls["n"] >= 2:
                    raise RuntimeError("connection reset")
            return real_list_dir(repo_id, path)

        client.list_dir.side_effect = flaky_list_dir

        with patch("pathlib.Path.glob") as mock_glob:
            self._staged_pack(mock_glob)
            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("could not list the remote pack directory", str(ctx.exception))
        self.assertEqual(self._deleted_paths(client), [], "nothing may be deleted when the check cannot run")

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_a_lease_lost_mid_cleanup_stops_deleting_and_says_so(self, mock_subprocess):
        """The delete loop must fence every deletion, not just the first.

        maybe_renew() cannot serve here -- its False also means "the renewal
        interval has not elapsed", so a lost lease read as an ordinary no-op and
        the loop kept deleting.  The consolidated packfiles are already verified
        by this point, so the run stops short of finishing its cleanup rather
        than failing: nothing is lost, and the leftovers are just clutter.  It
        still must not report plain success -- a scheduled job reading only
        ``status`` (or the exit code) cannot tell a finished compaction from one
        that left orphans behind.
        """
        client = self._client_with_listing([
            {"name": "pack-1.pack"}, {"name": "pack-2.pack"}, {"name": "pack-3.pack"},
        ])
        mock_subprocess.side_effect = self._subprocess_fake()
        err = io.StringIO()

        with patch("pathlib.Path.glob") as mock_glob, patch(
            "git_remote_seafile.gc.RemoteLock", _FencedOnceLock
        ), contextlib.redirect_stderr(err):
            self._staged_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(
            res["status"],
            "incomplete",
            "a run that could not finish its cleanup must not report success",
        )
        self.assertEqual(res["deleted_packs"], 0, "the lease was lost, so no pack may be deleted")
        self.assertTrue(res["cleanup_stopped"])
        self.assertIn("another client took over the lock", err.getvalue())
        self.assertIn("stopped deleting obsolete packfiles", err.getvalue())


class TestGcSubprocessTimeout(unittest.TestCase):
    """H-24 -- compaction holds the remote lease while it works, so a git child
    that never returns must fail the run rather than hold the lease open past
    its expiry, where the queue will lawfully hand it to another client."""

    def test_run_git_converts_a_timeout_into_a_named_failure(self):
        with patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="git repack", timeout=1),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                gc_module.run_git_checked(["-C", "scratch", "repack", "-a", "-d", "-l"], {})
        self.assertIn("did not finish within", str(ctx.exception))
        self.assertIn("repack", str(ctx.exception))


class _FencedOnceTailLock:
    """A lock whose pre-delete fence passes and every per-deletion fence fails.

    tail_merge asks twice in a row: once before the deletion loop, then before
    each individual deletion.  Answering True exactly once models a lease that
    lapses between those two moments -- the merge is already uploaded and
    verified, so the correct outcome is "incomplete", not success and not an
    exception thrown mid-loop.
    """

    def __init__(self, *args, **kwargs):
        self._checks = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def maybe_renew(self, _interval):
        pass

    def verify_ownership(self):
        self._checks += 1
        return self._checks == 1

    def ownership_state(self):
        return LEASE_LOST


class TestTailMerge(unittest.TestCase):
    """DESIGN.md §4.5 -- the automatic tier: bounded, frozen-aware consolidation.

    The invariants worth pinning are the ones that separate tail merge from a
    full compaction run: big packs never count toward the threshold, never get
    downloaded and never get deleted; an empty or unverifiable merge deletes
    nothing; and a lease lost mid-cleanup reports "incomplete" rather than
    success.  The pack-objects fake writes a *real* file so the before/after
    glob diff that names the merged packfile runs against actual paths.
    """

    @staticmethod
    def _cfg(threshold=3, max_mib=8192, max_pack_mib=1024):
        return RemoteConfig(
            lock_timeout=15, lock_lease=60, lock_settle=0, auto_gc=True,
            gc_threshold=threshold, gc_max_mib=max_mib, gc_max_pack_mib=max_pack_mib,
        )

    @staticmethod
    def _client(entries, *, body=b"PACK-DATA", drop_uploads=False):
        """A remote whose pack listing is honest about what was really sent.

        Mirrors the compact_repository fixtures: lock tickets uploaded through
        upload_file are replayable via get_file_text, and non-lock uploads show
        up in later listings at their true size -- unless ``drop_uploads``
        models a server that accepted nothing, which is exactly the case the
        pre-deletion verification exists to catch.
        """
        client = MagicMock()
        client.get_file_bytes.return_value = body
        client.delete_entry.return_value = True

        lock_payloads: list[str] = []
        uploaded_sizes: dict[str, int] = {}
        uploaded_bytes: dict[str, bytes] = {}
        client.download_file_to.side_effect = _read_back(uploaded_bytes)
        hidden = {"v": False}

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if ".git-lock" in filename or ".git-lock.d" in parent:
                lock_payloads.append(content.decode("utf-8"))
            else:
                uploaded_sizes[filename] = _uploaded_size(content)
                _remember_bytes(uploaded_bytes, filename, content)
                if drop_uploads:
                    hidden["v"] = True
            return True

        client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            if ".git-lock" in path:
                return lock_payloads[-1] if lock_payloads else None
            return "sha-main"

        client.get_file_text.side_effect = fake_get_text

        def listing(repo_id, path):
            if "objects/pack" not in path:
                return []
            return _pack_listing(entries, uploaded_sizes) if not hidden["v"] else list(entries)

        client.list_dir.side_effect = listing
        client.uploaded_sizes = uploaded_sizes
        return client

    @staticmethod
    def _subprocess_fake(*, objects=b"aa11 commit 120\nbb22 blob 5\n"):
        def fake(cmd, _env=None, **kwargs):
            if "cat-file" in cmd:
                return MagicMock(returncode=0, stdout=objects.decode())
            if "pack-objects" in cmd:
                base = Path(cmd[-1])
                merged = base.parent / (base.name + "-" * 1 + "ab" * 20 + ".pack")
                merged.write_bytes(b"M" * 40)
                merged.with_suffix(".idx").write_bytes(b"I" * 7)
                return MagicMock(returncode=0)
            if "verify-pack" in cmd:
                return MagicMock(returncode=0, stderr=b"")
            return MagicMock(returncode=0)

        return fake

    @staticmethod
    def _deleted_pack_paths(client):
        """Everything deleted that is not the holder's own lock ticket."""
        return [c.args[1] for c in client.delete_entry.call_args_list if ".git-lock.d" not in c.args[1]]

    # --- the gates: count and budget are both about movable packs only -----

    def test_tail_merge_skips_below_the_movable_threshold(self):
        client = self._client([{"name": "pack-1.pack", "size": len(b"PACK-DATA")}, {"name": "pack-2.pack", "size": len(b"PACK-DATA")}])

        res = tail_merge(client, "repo1", "/path", config=self._cfg(threshold=3), verbose=False)

        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["movable_packs"], 2)
        self.assertEqual(res["frozen_packs"], 0)
        client.get_file_bytes.assert_not_called()
        self.assertEqual(self._deleted_pack_paths(client), [])

    def test_tail_merge_frozen_packs_never_count_toward_the_threshold(self):
        """Eight packs exist but only three are movable: the threshold must not fire.

        Counting frozen packs would re-arm the trigger on every push in a
        repository with a big consolidated floor, which is the exact churn the
        freeze exists to end.
        """
        small = [{"name": f"pack-{i}.pack", "size": len(b"PACK-DATA")} for i in range(3)]
        frozen = [{"name": f"pack-big{i}.pack", "size": 4 * 1024**3} for i in range(5)]
        client = self._client(small + frozen)

        res = tail_merge(client, "repo1", "/path", config=self._cfg(threshold=4), verbose=False)

        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["movable_packs"], 3)
        self.assertEqual(res["frozen_packs"], 5)
        client.get_file_bytes.assert_not_called()
        self.assertEqual(self._deleted_pack_paths(client), [])

    def test_tail_merge_defers_when_not_even_two_packs_fit_the_budget(self):
        """Five 3 MiB packs against a 2 MiB budget: defer, transferring nothing.

        The budget is a download cap on the *movable* set only; frozen bytes
        never enter it because they would never be downloaded anyway.  A merge
        of fewer than two packs removes no file, so that is the only case left
        that defers (a set that merely totals more than the budget is merged in
        part: see the next test).
        """
        client = self._client([{"name": f"pack-{i}.pack", "size": 3 * 1024**2} for i in range(5)])

        res = tail_merge(client, "repo1", "/path", config=self._cfg(threshold=3, max_mib=2), verbose=False)

        self.assertEqual(res["status"], "skipped")
        self.assertIn("deferred", res["message"])
        client.get_file_bytes.assert_not_called()
        self.assertEqual(self._deleted_pack_paths(client), [])

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_tail_merge_over_the_budget_merges_the_smallest_packs_that_fit(self, mock_subprocess):
        """Six 1 MiB packs against a 3 MiB budget: three are merged, three wait.

        Deferring every push once the movable set outgrew the budget let the
        pack count grow without bound (reproduced on a real server: pushes 20-26
        all deferred, 20 -> 26 packs).  The merge is a bag union, so any subset
        is safe; it only has to fit what one run may download.
        """
        body = b"x" * (1024**2)
        client = self._client(
            [{"name": f"pack-{i}.pack", "size": len(body)} for i in range(6)], body=body
        )
        mock_subprocess.side_effect = self._subprocess_fake()

        res = tail_merge(client, "repo1", "/path", config=self._cfg(threshold=3, max_mib=3), verbose=False)

        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["merged_packs"], 3)
        self.assertEqual(res["left_for_later"], 3)
        self.assertEqual(
            sorted(self._deleted_pack_paths(client)),
            [f"/path/objects/pack/pack-{i}.{ext}" for i in range(3) for ext in ("idx", "pack")],
            "exactly the three chosen pairs; the other three are untouched",
        )
        downloaded = [c.args[1] for c in client.download_file_to.call_args_list]
        self.assertFalse(any("pack-3" in str(p) or "pack-4" in str(p) or "pack-5" in str(p)
                             for p in downloaded), "a pack left for later is not downloaded")

    def test_select_within_budget_takes_the_smallest_first_and_needs_two(self):
        from git_remote_seafile.gc import select_within_budget

        packs = [("a", 5), ("b", 1), ("c", 2), ("d", 3), ("e", None)]
        self.assertEqual(
            [n for n, _ in select_within_budget(packs, 6)], ["e", "b", "c", "d"]
        )
        self.assertEqual([n for n, _ in select_within_budget(packs, 3)], ["e", "b", "c"])
        # the two smallest (1 + 2) do not fit 2 -> defer; one pack is not a merge
        self.assertEqual(select_within_budget([("b", 1), ("c", 2)], 2), [])
        self.assertEqual(select_within_budget([("b", 1)], 100), [])
        self.assertEqual(select_within_budget([], 100), [])

    def test_tail_merge_refuses_an_untrusted_pack_name(self):
        """A name that could not be a real packfile aborts before any transfer.

        is_valid_pack_name is the only thing standing between a hostile listing
        and a path join; an error here costs nothing, so it must fire first.
        """
        client = self._client([
            {"name": "pack-1.pack", "size": len(b"PACK-DATA")},
            {"name": "pack-../../evil.pack", "size": len(b"PACK-DATA")},
        ])

        res = tail_merge(client, "repo1", "/path", config=self._cfg(threshold=1), verbose=False)

        self.assertEqual(res["status"], "error")
        self.assertIn("Invalid packfile name", res["message"])
        client.get_file_bytes.assert_not_called()
        self.assertEqual(self._deleted_pack_paths(client), [])

    # --- the merge itself ---------------------------------------------------

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_tail_merge_caps_the_size_of_what_it_writes(self, mock_subprocess):
        """A pack above the server's per-upload ceiling cannot be uploaded at all
        (2 GiB was reset on a real server), so the merge hands git the cap."""
        small = [{"name": "pack-1.pack", "size": len(b"PACK-DATA")}, {"name": "pack-2.pack", "size": len(b"PACK-DATA")}]
        client = self._client(small)
        commands = []
        inner = self._subprocess_fake()

        def recording(cmd, _env=None, **kwargs):
            commands.append(list(cmd))
            return inner(cmd, **kwargs)

        mock_subprocess.side_effect = recording
        tail_merge(client, "repo1", "/path", config=self._cfg(threshold=2, max_pack_mib=7), verbose=False)

        pack_objects = [c for c in commands if "pack-objects" in c]
        self.assertTrue(pack_objects)
        self.assertIn(f"--max-pack-size={7 * 1024 * 1024}", pack_objects[0])

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_tail_merge_consolidates_only_the_movable_packs(self, mock_subprocess):
        small = [{"name": "pack-1.pack", "size": len(b"PACK-DATA")}, {"name": "pack-2.pack", "size": len(b"PACK-DATA")}]
        frozen = {"name": "pack-frozen.pack", "size": 5 * 1024**3}
        client = self._client(small + [frozen])
        mock_subprocess.side_effect = self._subprocess_fake()

        res = tail_merge(client, "repo1", "/path", config=self._cfg(threshold=2), verbose=False)

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["merged_packs"], 2)
        self.assertEqual(res["frozen_packs"], 1)
        # deleted_packs counts .pack files; each had its .idx removed as well.
        self.assertEqual(res["deleted_packs"], 2)
        self.assertEqual(
            sorted(self._deleted_pack_paths(client)),
            [
                "/path/objects/pack/pack-1.idx",
                "/path/objects/pack/pack-1.pack",
                "/path/objects/pack/pack-2.idx",
                "/path/objects/pack/pack-2.pack",
            ],
            "exactly the two movable pairs -- the frozen pack was never touched",
        )
        self.assertFalse(
            any("pack-frozen" in str(c.args) for c in client.get_file_bytes.call_args_list),
            "a frozen pack must never be downloaded",
        )
        merged = [n for n in client.uploaded_sizes if "tail" in n]
        self.assertEqual(len(merged), 2, "the merged .pack and its .idx were uploaded")

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_tail_merge_keeps_the_old_packs_when_the_new_one_has_the_right_size_and_other_bytes(
        self, mock_subprocess
    ):
        """The listing proves a size.  A same-length substitution (a padded
        partial write, a damaged block, a hostile operator) used to pass it and
        the originals were deleted (reproduced in the third review round)."""
        small = [{"name": "pack-1.pack", "size": len(b"PACK-DATA")},
                 {"name": "pack-2.pack", "size": len(b"PACK-DATA")}]
        client = self._client(small)
        served = client.download_file_to.side_effect

        def damaged(repo_id, path, dest, *args, **kwargs):
            ok = served(repo_id, path, dest, *args, **kwargs)
            if ok is True:
                data = Path(dest).read_bytes()
                Path(dest).write_bytes(b"\x00" * len(data))
            return ok

        client.download_file_to.side_effect = damaged
        mock_subprocess.side_effect = self._subprocess_fake()

        with self.assertRaises(RuntimeError) as ctx:
            tail_merge(client, "repo1", "/path", config=self._cfg(threshold=2), verbose=False)
        self.assertIn("different content", str(ctx.exception))
        self.assertEqual(
            [p for p in self._deleted_pack_paths(client) if "pack-1" in p or "pack-2" in p], []
        )

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_tail_merge_deletes_nothing_when_the_tail_holds_no_objects(self, mock_subprocess):
        """An empty bag union has nothing to merge -- so nothing may be deleted.

        The old packfiles are the only copy of their (zero) objects; deleting
        them on a vacuous merge would trade clutter for data loss.
        """
        client = self._client([{"name": "pack-1.pack", "size": len(b"PACK-DATA")}, {"name": "pack-2.pack", "size": len(b"PACK-DATA")}])
        mock_subprocess.side_effect = self._subprocess_fake(objects=b"")

        res = tail_merge(client, "repo1", "/path", config=self._cfg(threshold=2), verbose=False)

        self.assertEqual(res["status"], "skipped")
        self.assertIn("no objects", res["message"])
        self.assertEqual(self._deleted_pack_paths(client), [])

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_tail_merge_keeps_the_old_packs_when_the_upload_cannot_be_proven(self, mock_subprocess):
        """Upload success is not evidence the bytes are there; the check fails closed.

        The server accepted nothing, so the merged packfile cannot be listed at
        its local size -- and until it can be proven present, the old packs are
        still the repository's only complete copy.  Nothing may be deleted.
        """
        client = self._client(
            [{"name": "pack-1.pack", "size": len(b"PACK-DATA")}, {"name": "pack-2.pack", "size": len(b"PACK-DATA")}],
            drop_uploads=True,
        )
        mock_subprocess.side_effect = self._subprocess_fake()

        with self.assertRaises(RuntimeError) as ctx:
            tail_merge(client, "repo1", "/path", config=self._cfg(threshold=2), verbose=False)

        self.assertIn("refusing to delete", str(ctx.exception))
        self.assertEqual(self._deleted_pack_paths(client), [])
        # The attempt itself did happen: the merge was built and sent.
        self.assertTrue(any("tail" in n for n in client.uploaded_sizes))

    @patch("git_remote_seafile.gc.run_git_checked")
    def test_tail_merge_reports_incomplete_when_the_lease_is_lost_mid_cleanup(self, mock_subprocess):
        """A lost lease after verification stops cleanup; it is not a success.

        The merged packfile is already uploaded and verified, so nothing was
        lost -- but a scheduled job reading only ``status`` must be able to tell
        this run from one that finished its deletions.
        """
        client = self._client([{"name": "pack-1.pack", "size": len(b"PACK-DATA")}, {"name": "pack-2.pack", "size": len(b"PACK-DATA")}])
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("git_remote_seafile.gc.RemoteLock", _FencedOnceTailLock):
            res = tail_merge(client, "repo1", "/path", config=self._cfg(threshold=2), verbose=False)

        self.assertEqual(res["status"], "incomplete")
        self.assertEqual(res["deleted_packs"], 0, "the lease was lost, so no pack may be deleted")
        self.assertTrue(res["cleanup_stopped"])
        self.assertEqual(self._deleted_pack_paths(client), [])


class TestObjectFormatDetection(unittest.TestCase):
    """The scratch repository's format comes from the pack *names*.

    Tail merge writes ``pack-tail-<hash>.pack``; a detector that measured what
    was left after stripping ``pack-`` saw ``tail-<64 hex>`` (69 characters) and
    answered SHA-1, so a manual ``gc`` of a SHA-256 remote failed once tail merge
    had been the only writer.
    """

    def test_a_plain_pack_name_decides_by_hash_length(self):
        self.assertEqual(detect_object_format(["pack-" + "a" * 40 + ".pack"]), "sha1")
        self.assertEqual(detect_object_format(["pack-" + "a" * 64 + ".pack"]), "sha256")

    def test_a_tail_merge_pack_name_is_read_from_its_end(self):
        self.assertEqual(detect_object_format(["pack-tail-" + "b" * 64 + ".pack"]), "sha256")
        self.assertEqual(detect_object_format(["pack-tail-" + "b" * 40 + ".pack"]), "sha1")

    def test_any_sha256_name_in_a_mixed_listing_decides(self):
        names = ["pack-" + "c" * 40 + ".pack", "pack-tail-" + "d" * 64 + ".pack"]
        self.assertEqual(detect_object_format(names), "sha256")

    def test_no_recognisable_name_defaults_to_sha1(self):
        self.assertEqual(detect_object_format([]), "sha1")
        self.assertEqual(detect_object_format(["pack-oddname.pack", "readme.txt"]), "sha1")


class TestGitFailuresNameTheCause(unittest.TestCase):
    """A failed scratch-repo command reports git's words, not its command line."""

    def test_check_true_failure_carries_stderr(self):
        with self.assertRaises(RuntimeError) as ctx:
            gc_module.run_git_checked(
                ["config", "--get", "--bogus-flag"],
                # Not env={}: an empty environment block is invalid to
                # CreateProcess on Windows and Python 3.10 raises
                # OSError WinError 87 before git ever runs.
                env={"GIT_TERMINAL_PROMPT": "0"}, check=True, capture_output=True,
            )
        text = str(ctx.exception)
        self.assertIn("failed (exit", text)
        self.assertNotIn("CalledProcessError", text)
        self.assertNotIn("returned non-zero exit status", text)



class TestPhasePulse(unittest.TestCase):
    """The long captured phases say they are alive (ROADMAP S13).

    gc and the tail merge print one header per step and then nothing while
    the repack or pack-objects runs captured; on a slow link that reads as a
    hang.  The pulse prints a `progress:` line at most once a minute.
    """

    def test_the_pulse_throttles_to_its_interval(self):
        import io
        from git_remote_seafile.gc import _phase_pulse

        sink = io.StringIO()
        clock = {"now": 1000.0}
        real = gc_module.time.monotonic
        gc_module.time.monotonic = lambda: clock["now"]
        try:
            pulse = _phase_pulse(sink.write, "repacking objects")
            pulse()
            self.assertEqual(sink.getvalue(), "",
                             "the first call must not print before the interval")
            clock["now"] = 1070.0
            pulse()
            self.assertIn("progress: repacking objects ... 70s", sink.getvalue())
            clock["now"] = 1080.0
            pulse()
            self.assertEqual(sink.getvalue().count("progress:"), 1,
                             "a call inside the interval must not print")
        finally:
            gc_module.time.monotonic = real
