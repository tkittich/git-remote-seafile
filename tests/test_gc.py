"""test_gc.py - Remote compaction: fail-closed downloads, fencing, ref mirroring."""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.gc import compact_repository, describe_size_delta

class TestRemoteGC(unittest.TestCase):
    def test_a_growth_is_not_reported_as_a_saving(self):
        """Compaction can enlarge a repository, and the message must not lie.

        Repacking can produce a slightly larger pack when the existing packs
        were already tight.  The delta is signed because that is the honest
        value, but printing it raw produced ``(saved -12 KB)`` -- which reads
        like a bug and calls a growth a saving.
        """
        self.assertEqual(describe_size_delta(100), "saved ~100 KB")

        grown = describe_size_delta(-12)
        self.assertIn("grew", grown)
        self.assertIn("12", grown)
        self.assertNotIn("-12", grown)

    def test_an_unchanged_size_is_not_called_a_growth(self):
        self.assertEqual(describe_size_delta(0), "saved ~0 KB")

    def test_gc_skipped_below_threshold(self):
        mock_client = MagicMock()
        mock_client.list_dir.return_value = [{"name": "pack-1.pack"}]

        res = compact_repository(mock_client, "repo1", "/path", min_packs=3, verbose=False)
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["count"], 1)

    @patch("subprocess.run")
    def test_gc_compaction_flow(self, mock_subprocess):
        mock_client = MagicMock()
        # list_dir must answer per path: the compactor reads both the pack
        # directory and the ref namespace, and it now refuses to run at all when
        # no refs can be read -- an empty ref listing is exactly what a failed
        # request looks like, and compacting on that guess destroys history.
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}] if "objects/pack" in path
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
        def fake_subprocess(cmd, **kwargs):
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

            mock_glob.return_value = [p_pack, p_idx]

            res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["old_packs"], 2)
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

    @patch("subprocess.run")
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

        def fake_subprocess(cmd, **kwargs):
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
    def test_gc_preserves_all_branches_and_tags(self, mock_subprocess):
        mock_client = MagicMock()
        mock_client.list_dir.side_effect = lambda repo_id, path: (
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}] if "objects/pack" in path
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
            return True

        mock_client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            if ".git-lock" in path and lock_payloads:
                return lock_payloads[-1]
            return "sha000111"

        mock_client.get_file_text.side_effect = fake_get_text

        def fake_subprocess(cmd, **kwargs):
            if "pack-objects" in cmd:
                return MagicMock(returncode=0, stdout=b"sha123\n")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake_subprocess

        with patch("pathlib.Path.glob") as mock_glob:
            p_pack = MagicMock()
            p_pack.name = "pack-sha123.pack"
            p_pack.read_bytes.return_value = b"NEW-PACK"
            p_pack.stat.return_value.st_size = 10
            mock_glob.return_value = [p_pack]

            res = compact_repository(mock_client, "repo1", "/path", min_packs=2, verbose=False)
            self.assertEqual(res["status"], "ok")

            # Check that repack was called
            repack_called = any("repack" in cmd for cmd, _ in [(c.args[0], c) for c in mock_subprocess.call_args_list if c.args])
            self.assertTrue(repack_called)

    @patch("git_remote_seafile.gc.subprocess.run")
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


class _RenewOnlyLock:
    """The lock interface as it was before ownership fencing: renew() and no more.

    ``compact_repository`` prefers ``verify_ownership`` and falls back to
    ``renew``, so a caller holding a lock object from before the fencing was
    added must still be able to compact rather than crash on a missing method.
    """

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def maybe_renew(self, _interval):
        pass

    def renew(self):
        return True


class _UnfencedLock:
    """A lock object offering no ownership check at all.

    gc treats "cannot ask" as "do not block": the fence exists to stop a *known*
    loss of ownership, and a caller that never supplied a fenced lock is not
    evidence of one.
    """

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def maybe_renew(self, _interval):
        pass


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
        lock files have to be replayable through get_file_text.
        """
        client = MagicMock()
        client.list_dir.side_effect = lambda repo_id, path: (
            entries
            if "objects/pack" in path
            else [{"type": "file", "name": "main"}]
            if "refs/heads" in path
            else []
        )
        client.get_file_bytes.return_value = body
        client.delete_entry.return_value = delete_ok

        lock_payloads: list[str] = []

        def fake_upload(repo_id, parent, filename, content, replace=True, progress_callback=None):
            if ".git-lock" in filename or ".git-lock.d" in parent:
                lock_payloads.append(content.decode("utf-8"))
            return True

        client.upload_file.side_effect = fake_upload

        def fake_get_text(repo_id, path):
            if ".git-lock" in path:
                return lock_payloads[-1] if lock_payloads else None
            return "sha-main"

        client.get_file_text.side_effect = fake_get_text
        return client

    @staticmethod
    def _subprocess_fake(verify_pack_rc=0):
        def fake(cmd, **kwargs):
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
        """
        p_pack = MagicMock()
        p_pack.name = "pack-abcdef1234567890abcdef1234567890abcdef12.pack"
        p_pack.read_bytes.return_value = b"NEW-PACK"
        p_pack.stat.return_value.st_size = 8
        mock_glob.return_value = [p_pack]

    @staticmethod
    def _deleted_paths(client):
        return [c.args[1] for c in client.delete_entry.call_args_list if len(c.args) > 1]

    # --- a remote whose index is wrong -----------------------------------

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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

    # --- lock objects gc did not construct -------------------------------

    @patch("subprocess.run")
    def test_gc_compacts_with_a_lock_that_can_only_renew(self, mock_subprocess):
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob") as mock_glob, patch(
            "git_remote_seafile.gc.RemoteLock", _RenewOnlyLock
        ):
            self._one_new_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        self.assertTrue([p for p in self._deleted_paths(client) if p.endswith(".pack")])

    @patch("subprocess.run")
    def test_gc_compacts_with_a_lock_that_has_no_fence(self, mock_subprocess):
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob") as mock_glob, patch(
            "git_remote_seafile.gc.RemoteLock", _UnfencedLock
        ):
            self._one_new_pack(mock_glob)
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        self.assertTrue([p for p in self._deleted_paths(client) if p.endswith(".pack")])

    # --- reporting -------------------------------------------------------

    @patch("subprocess.run")
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

    @patch("subprocess.run")
    def test_gc_renews_the_lease_while_transferring(self, mock_subprocess):
        """Both transfer callbacks must renew, or the lease lapses mid-transfer.

        A multi-gigabyte download is the longest phase gc holds the lock for.
        The callbacks exist precisely so a long transfer cannot outlive its own
        lease and be lawfully taken over while it is still writing.
        """
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        renewals: list[float] = []

        class _CountingLock(_UnfencedLock):
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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
            [{"name": "pack-1.pack"}, {"name": "pack-2.pack"}]
            if "objects/pack" in path
            else [{"type": "dir", "name": "heads"}]
            if path.endswith("/refs")
            else []
        )
        mock_subprocess.return_value = MagicMock(returncode=0)
        with patch(
            "git_remote_seafile.gc.iter_refs",
            side_effect=lambda client, repo_id, base, ns: iter(
                [("refs/heads/feature/auth", "sha-a"), ("refs/heads/main", "sha-b")]
            ),
        ):
            res = compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["old_packs"], 2)

    @patch("subprocess.run")
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

    @patch("subprocess.run")
    def test_gc_refuses_when_repack_fails(self, mock_subprocess):
        """A failed repack means the new pack is untrustworthy, so nothing is deleted."""
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])

        def fake(cmd, **kwargs):
            if "repack" in cmd:
                return MagicMock(returncode=1, stderr="fatal: bad object refs/heads/main")
            return MagicMock(returncode=0)

        mock_subprocess.side_effect = fake

        with self.assertRaises(RuntimeError) as ctx:
            compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("git repack failed", str(ctx.exception))
        self.assertFalse([p for p in self._deleted_paths(client) if p.endswith(".pack")])

    @patch("subprocess.run")
    def test_gc_refuses_when_repack_produces_no_pack(self, mock_subprocess):
        """Repacking that yields nothing leaves the old packs unproven-superseded."""
        client = self._client([{"name": "pack-1.pack"}, {"name": "pack-2.pack"}])
        mock_subprocess.side_effect = self._subprocess_fake()

        with patch("pathlib.Path.glob", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                compact_repository(client, "repo1", "/path", min_packs=2, verbose=False)

        self.assertIn("produced no packfile", str(ctx.exception))

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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




