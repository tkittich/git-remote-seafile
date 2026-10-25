"""test_helper.py - Remote-helper protocol tests: list, push, fetch, staging."""

from __future__ import annotations

import ast
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    # top-level import under `unittest discover tests` and the parallel runner
    from _helpers import make_helper
except ImportError:  # package-style: python -m unittest tests.test_helper
    from tests._helpers import make_helper

from git_remote_seafile.client import SeafileAPIError
from git_remote_seafile.config import RemoteConfig
from git_remote_seafile.safety import SafetyError, check_preflight_safety
from git_remote_seafile.git_util import GitError
from git_remote_seafile.lock import LEASE_LOST, LEASE_UNKNOWN, RepositoryLockedError

class CheckoutStallWarningTests(unittest.TestCase):
    def _warn(self, nbytes, *, os_name="nt", autocrlf="true"):
        import io
        from unittest.mock import patch

        from git_remote_seafile import helper as helper_mod

        err = io.StringIO()
        with patch.object(helper_mod.os, "name", os_name),              patch.object(helper_mod, "get_git_config", return_value=autocrlf),              patch("sys.stderr", err):
            helper_mod._warn_if_checkout_may_stall(nbytes)
        return err.getvalue()

    def test_a_4_gib_pack_on_windows_with_autocrlf_warns(self):
        out = self._warn(5 * 1024**3)
        self.assertIn("core.autocrlf=false", out)
        self.assertIn("5.0 GiB", out)

    def test_it_stays_quiet_when_it_cannot_matter(self):
        self.assertEqual(self._warn(3 * 1024**3), "")
        self.assertEqual(self._warn(5 * 1024**3, os_name="posix"), "")
        self.assertEqual(self._warn(5 * 1024**3, autocrlf="false"), "")
        self.assertEqual(self._warn(5 * 1024**3, autocrlf="input"), "")


def _pack_dir_shows_uploads(client):
    """A ``list_dir`` that lists the pack files the helper uploaded, at their sizes.

    The push now proves its packs landed before it writes a ref, so a fixture
    has to model a remote that kept what it was sent.  Any other directory
    lists empty.
    """

    def listing(repo_id, path):
        if "objects/pack" not in path:
            return []
        out = []
        for call in client.upload_file.call_args_list:
            name, content = call.args[2], call.args[3]
            if not (name.endswith(".pack") or name.endswith(".idx")):
                continue
            size = len(content) if isinstance(content, (bytes, bytearray)) else content.stat().st_size
            out.append({"name": name, "size": size})
        return out

    return listing


class TestRemoteHelper(unittest.TestCase):
    def test_capabilities(self):
        h = make_helper()
        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_capabilities()
        lines = out.getvalue().splitlines()
        self.assertIn("push", lines)
        self.assertIn("fetch", lines)
        self.assertIn("option", lines)
        self.assertIn("object-format", lines)

    def test_object_format_negotiation(self):
        h = make_helper()
        # Git sends 'option object-format true'
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option object-format true\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "ok\n")

        # gitremote-helpers(7) also documents an explicit algorithm pin.  The
        # helper cannot enforce one -- it detects the format from the refs --
        # so accepting and advertising the real format lets git's own check
        # catch a genuine mismatch instead of failing operations that would
        # have worked.
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option object-format sha1\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "ok\n")
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option object-format sha256\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "ok\n")

        # Unsupported option replies 'unsupported\n'
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option unknown-opt value\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "unsupported\n")

    def test_dry_run_option_is_accepted(self):
        """The reply that makes `git push --dry-run` possible at all.

        Git sends ``option dry-run true`` and *dies* on "unsupported" --
        "helper seafile does not support dry-run", exit 128 -- before it writes
        a single push command, so a helper that refuses this option cannot be
        dry-run at all.
        """
        h = make_helper()
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option dry-run true\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "ok\n")
        self.assertTrue(h.dry_run)

    def test_dry_run_option_false_clears_the_flag(self):
        """The value is honoured rather than assumed, so the flag cannot latch."""
        h = make_helper()
        h.dry_run = True
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option dry-run false\n")), patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue(), "ok\n")
        self.assertFalse(h.dry_run)

    def test_unknown_command_fails_loudly_on_stderr_and_stops(self):
        """An unrecognized top-level command must fail loudly, not reply with an empty no-op line.

        The failure belongs on stderr and must end the loop:
        "error unsupported" on stdout is not a line git's helper
        reader recognizes, so writing it and continuing to read commands
        left the protocol in an undefined state.  GitError propagating out
        of run() is what makes the helper exit non-zero.
        """
        h = make_helper()
        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdin", io.StringIO("frobnicate\n")), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            with self.assertRaises(GitError) as ctx:
                h.run()
        self.assertIn("frobnicate", str(ctx.exception))
        self.assertIn("unsupported helper command", err.getvalue())
        # Nothing fake went onto the protocol stream for git to misparse.
        self.assertEqual(out.getvalue(), "")

    def test_cmd_list_detects_sha256_format(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        sha256_hash = "f" * 64
        h.client.list_dir.side_effect = lambda repo_id, path: (
            [{"type": "file", "name": "main"}] if "heads" in path else []
        )
        h.client.get_file_text.return_value = sha256_hash

        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list()

        output = out.getvalue()
        self.assertIn(":object-format sha256\n", output)
        self.assertIn(f"{sha256_hash} refs/heads/main\n", output)

    def test_detect_object_format_uses_refs_only(self):
        """Format comes from the ref SHAs, with no remote/local fallback.

        The pack-name and ``rev-parse --show-object-format`` fallbacks were
        unreachable from the only call site (refs are always 40 or 64 hex), so
        they were removed rather than left to read as a live safety net.
        """
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"

        self.assertEqual(h._detect_remote_object_format([("refs/heads/main", "a" * 40)]), "sha1")
        self.assertEqual(h._detect_remote_object_format([("refs/heads/main", "b" * 64)]), "sha256")
        h.client.list_dir.assert_not_called()

    def test_cmd_list_refs(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        # Mock heads, tags, and HEAD
        h.client.list_dir.side_effect = lambda repo_id, path: (
            [{"type": "file", "name": "main"}, {"type": "file", "name": "feature"}]
            if "heads" in path
            else [{"type": "file", "name": "v1.0"}]
            if "tags" in path
            else []
        )
        sha_main = "a" * 40
        sha_feat = "b" * 40
        sha_tag = "c" * 40
        h.client.get_file_text.side_effect = lambda repo_id, path: (
            sha_main if "heads/main" in path
            else sha_feat if "heads/feature" in path
            else sha_tag if "tags/v1.0" in path
            else "ref: refs/heads/main" if path.endswith("HEAD")
            else None
        )

        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list()

        output = out.getvalue()
        self.assertIn(f"{sha_main} refs/heads/main", output)
        self.assertIn(f"{sha_feat} refs/heads/feature", output)
        self.assertIn(f"{sha_tag} refs/tags/v1.0", output)
        self.assertIn("@refs/heads/main HEAD", output)

    def test_cmd_list_ignores_a_head_target_that_would_inject_a_protocol_line(self):
        """A HEAD target carrying a newline must not become a second ref line.

        The symref line is written straight to git's stdout and git reads the
        listing one line at a time, so a HEAD file holding
        ``ref: refs/heads/main\\nrefs/heads/evil`` used to append an invented
        ref to the advert.  The target is validated like every other ref name
        now, and a malformed HEAD is skipped -- which leaves the remote with no
        default branch, exactly what an unparseable HEAD means.
        """
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.library_name = "lib"
        h._refs_cache = {}
        h._repository_has_objects = MagicMock(return_value=False)

        sha_main = "a" * 40
        h.client.list_dir.side_effect = lambda repo_id, path: (
            [{"type": "file", "name": "main"}] if "heads" in path else []
        )
        h.client.get_file_text.side_effect = lambda repo_id, path: (
            sha_main if "heads/main" in path
            else "ref: refs/heads/main\nrefs/heads/evil" if path.endswith("HEAD")
            else None
        )

        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            h.cmd_list()

        output = out.getvalue()
        self.assertIn(f"{sha_main} refs/heads/main", output)
        self.assertNotIn("@", output, "no symref line may be emitted for a malformed HEAD")
        self.assertNotIn("evil", output, "the injected second line must not reach git")
        self.assertIn("malformed HEAD target", err.getvalue())

    def test_cmd_list_skips_invalid_sha(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.library_name = "lib"
        h._refs_cache = {}
        h._repository_has_objects = MagicMock(return_value=False)

        h.client.list_dir.side_effect = lambda repo_id, path: (
            [{"type": "file", "name": "main"}, {"type": "file", "name": "corrupt"}]
            if "heads" in path
            else []
        )
        valid_sha = "1" * 40
        h.client.get_file_text.side_effect = lambda repo_id, path: (
            valid_sha if "heads/main" in path
            else "<html>error</html>" if "heads/corrupt" in path
            else None
        )

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            h.cmd_list()

        self.assertIn(f"{valid_sha} refs/heads/main", out.getvalue())
        self.assertNotIn("corrupt", out.getvalue())
        self.assertIn("Warning: ignoring invalid SHA", err.getvalue())

    def test_cmd_list_for_push_allows_empty_refs_with_packs(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.library_name = "lib"
        h._refs_cache = {}
        h._repository_has_objects = MagicMock(return_value=True)
        h.client.list_dir.return_value = []
        h.client.get_file_text.return_value = None

        # Without for_push, raises SeafileAPIError
        with self.assertRaises(SeafileAPIError):
            h.cmd_list(for_push=False)

        # With for_push=True, succeeds and reports empty list
        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list(for_push=True)
        self.assertEqual(out.getvalue(), "\n")

    def test_cmd_push_rejects_disallowed_namespaces(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["HEAD:refs/notes/commits", ":refs/stash"])

        output = out.getvalue()
        self.assertIn("error refs/notes/commits refusing to push outside refs/heads and refs/tags", output)
        self.assertIn("error refs/stash refusing to push outside refs/heads and refs/tags", output)

    def test_upload_aborts_when_the_lease_is_provably_lost(self):
        """A lost lease must stop the transfer, not just pace the renewal.

        The rest of a multi-gigabyte upload to a push that is already doomed
        is cost and a late error (ROADMAP S5); only a state that *proves* the
        lease gone aborts, so an ambiguous renewal keeps streaming.
        """
        from git_remote_seafile.client import TransferAborted
        from git_remote_seafile.helper import LEASE_LOST, _PendingUpdate

        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        lock = MagicMock()
        lock.maybe_renew.return_value = False
        lock.ownership_state.return_value = LEASE_LOST

        def upload_and_report(_rid, _dir, name, content, **_kw):
            # The real upload invokes the progress callback as bytes stream.
            cb = _kw.get("progress_callback")
            if cb:
                cb(1, 2)
            return True

        h.client.upload_file.side_effect = upload_and_report
        pending = [_PendingUpdate("refs/heads/main", "1" * 40, None, False)]

        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile",
                      return_value=("a" * 40, b"pack", b"idx")):
            with self.assertRaises(TransferAborted) as caught:
                h._upload_push_pack(lock, pending)
        self.assertIn("aborting the upload", str(caught.exception))

    def test_upload_keeps_streaming_when_renewal_is_merely_not_due(self):
        """maybe_renew's False also means "not due yet"; that must not abort."""
        from git_remote_seafile.lock import LEASE_HELD
        from git_remote_seafile.helper import _PendingUpdate

        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        lock = MagicMock()
        lock.maybe_renew.return_value = False
        lock.ownership_state.return_value = LEASE_HELD
        # The listing must show the staged pair, or the push fails verification
        # for a reason this test is not about.
        sha = "a" * 40
        h.client.list_dir.return_value = [
            {"name": f"pack-{sha}.pack", "size": 4},
            {"name": f"pack-{sha}.idx", "size": 3},
        ]

        pending = [_PendingUpdate("refs/heads/main", "1" * 40, None, False)]
        with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["x"]),                 patch("git_remote_seafile.helper.create_packfile",
                      return_value=("a" * 40, b"pack", b"idx")):
            h._upload_push_pack(lock, pending)   # must not raise
        h.client.upload_file.assert_called()

    def test_cmd_push_refuses_a_name_the_server_stores_differently(self):
        """An NFD branch and its NFC twin are two refs here, one file there.

        The server stores names NFC-normalised (measured on CE 13.0.28), so
        pushing the decomposed spelling silently overwrote the composed one's
        value (REVIEW.sonnet-5.5 A6, reproduced on the real server through the
        helper).  A quoted component and one over 255 UTF-8 bytes are refused
        for the same reason: the server keeps a different name than the one
        asked for.
        """
        from git_remote_seafile.helper import _destination_error

        composed = "caf" + chr(233)              # U+00E9 (precomposed)
        decomposed = "cafe" + chr(769)           # e + U+0301 (decomposed)
        self.assertNotEqual(composed, decomposed)
        why = _destination_error(f"refs/heads/{decomposed}")
        self.assertIsNotNone(why)
        self.assertIn("server stores differently", why)
        self.assertIn("Normalization Form C", why)
        # The composed spelling is fine, and so is the same name elsewhere in
        # the ref (only the component is judged).
        self.assertIsNone(_destination_error(f"refs/heads/{composed}"))
        self.assertIsNone(_destination_error(f"refs/heads/{composed}/{decomposed}-dir".replace(decomposed, composed)))
        # A quote and an over-long component are refused with the same shape.
        self.assertIn("server stores differently", _destination_error('refs/heads/a"b'))
        self.assertIn("server stores differently", _destination_error("refs/heads/" + "x" * 256))

    def test_cmd_push_refuses_a_shallow_repository(self):
        """A shallow push publishes a ref whose parents do not exist.

        `pack-objects` stops at the shallow boundary without complaint, so the
        remote would hold a ref that fails every clone with "Failed to traverse
        parents" (REVIEW.sonnet-5.5 A3, reproduced; a plain bare remote rejects
        the same push with "shallow update not allowed").
        """
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("git_remote_seafile.helper.is_shallow_repository", return_value=True), \
                patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
            h.cmd_push(["HEAD:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("error refs/heads/main refusing to push from a shallow repository", output)
        self.assertIn("git fetch --unshallow", output)
        h.client.upload_file.assert_not_called()

    def test_cmd_push_does_not_duplicate_error_for_successful_ref(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h._full_path = lambda p: f"/git-repo/{p.lstrip('/')}"
        h.client.get_file_text.return_value = None

        # Ref 1 succeeds, Ref 2 fails
        call_count = [0]
        def mock_rev_parse(ref):
            call_count[0] += 1
            if call_count[0] == 1:
                return "1" * 40
            raise RuntimeError("Failure on ref 2")

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"), \
             patch("git_remote_seafile.helper.rev_parse", side_effect=mock_rev_parse), \
             patch("git_remote_seafile.helper.get_objects_to_push", return_value=[]):
            h.cmd_push(["HEAD:refs/heads/ok-branch", "HEAD:refs/heads/fail-branch"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/ok-branch\n", output)
        self.assertIn("error refs/heads/fail-branch", output)
        self.assertNotIn("error refs/heads/ok-branch", output)

    def test_cmd_push_delete_branch(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        # A SHA learned from the ref advertisement.  It is only usable as a push
        # exclusion while the ref exists on the remote, so a delete has to drop
        # it -- leaving it behind would have a later push exclude objects on the
        # authority of a ref that is gone.
        h._refs_cache = {"refs/heads/old-branch": "stale-sha"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/old-branch"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/old-branch", output)
        h.client.delete_entry.assert_called_with("repo1", "/git-repo/refs/heads/old-branch")
        self.assertNotIn("refs/heads/old-branch", h._refs_cache)

    def test_a_failed_delete_keeps_the_cached_sha(self):
        """Nothing was deleted, so the cache is still an accurate picture."""
        h = make_helper()
        h.client = MagicMock()
        h.client.delete_entry.return_value = False
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/old-branch": "still-there"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/old-branch"])

        self.assertIn("failed to delete", out.getvalue())
        self.assertEqual(h._refs_cache["refs/heads/old-branch"], "still-there")

    def test_a_helper_that_never_saw_the_option_is_not_a_dry_run(self):
        """The flag defaults off.  If it did not, every push would be a preview."""
        h = make_helper()
        self.assertFalse(h.dry_run)

    @patch("git_remote_seafile.helper.rev_parse", return_value="newsha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    def test_dry_run_reports_an_update_and_writes_nothing(self, mock_ancestor, mock_rev):
        """`git push --dry-run`: the answer of a push, the footprint of a read."""
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "oldsha456"}
        h.dry_run = True

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock") as mock_lock:
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main\n", output)
        # Git reads the response block until the blank line, so it has to be there.
        self.assertTrue(output.endswith("\n\n"))
        h.client.upload_file.assert_not_called()
        h.client.delete_entry.assert_not_called()
        # The lock is itself a side effect, and taking it would park a real push
        # behind the settlement window to answer a question nobody acts on.
        mock_lock.assert_not_called()

    @patch("git_remote_seafile.helper.rev_parse", return_value="localsha")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=False)
    def test_dry_run_reports_a_non_fast_forward(self, mock_ancestor, mock_rev):
        """A rejected update must preview as rejected, not as ok."""
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "newersha"}
        h.dry_run = True

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock") as mock_lock:
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("error refs/heads/main non-fast-forward", out.getvalue())
        h.client.upload_file.assert_not_called()
        mock_lock.assert_not_called()

    def test_dry_run_reports_a_deletion_without_deleting(self):
        """The deletion preview deletes nothing, and does not rewrite the cache."""
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/old-branch": "stale-sha"}
        h.client.get_file_text.return_value = "stale-sha"  # the ref is there
        h.dry_run = True

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock") as mock_lock:
            h.cmd_push([":refs/heads/old-branch"])

        self.assertIn("ok refs/heads/old-branch", out.getvalue())
        h.client.delete_entry.assert_not_called()
        # _refs_cache is a push exclusion list, and nothing was pushed.
        self.assertIn("refs/heads/old-branch", h._refs_cache)
        mock_lock.assert_not_called()

    def _with_lease(self, remote_sha, expected, local_sha="local_new"):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.client.get_file_text.return_value = remote_sha
        h.cas_expect = {"refs/heads/main": expected}
        return h

    def test_a_lease_that_matches_permits_a_non_fast_forward(self):
        h = self._with_lease("remote_sha", "remote_sha")
        with patch("git_remote_seafile.helper.rev_parse", return_value="local_new"), \
                patch("git_remote_seafile.helper.is_ancestor", return_value=False) as anc:
            verdict = h._decide_update("refs/heads/main", "refs/heads/main", False)
        self.assertEqual(verdict, ("local_new", "remote_sha"))
        anc.assert_not_called()

    def test_a_lease_that_does_not_match_is_stale_info(self):
        h = self._with_lease("moved_sha", "remote_sha")
        with patch("git_remote_seafile.helper.rev_parse", return_value="local_new"):
            verdict = h._decide_update("refs/heads/main", "refs/heads/main", False)
        self.assertEqual(verdict, "stale info")

    def test_an_empty_lease_means_the_ref_must_not_exist(self):
        h = self._with_lease(None, "")
        with patch("git_remote_seafile.helper.rev_parse", return_value="local_new"):
            self.assertEqual(
                h._decide_update("refs/heads/main", "refs/heads/main", False), ("local_new", None)
            )
        h = self._with_lease("exists", "")
        with patch("git_remote_seafile.helper.rev_parse", return_value="local_new"):
            self.assertEqual(h._decide_update("refs/heads/main", "refs/heads/main", False), "stale info")

    def test_option_cas_is_accepted_and_recorded(self):
        h = make_helper()
        sha = "a" * 40
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(f"option cas refs/heads/main:{sha}\noption cas refs/heads/x:\n")), \
                patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue().split(), ["ok", "ok"])
        self.assertEqual(h.cas_expect, {"refs/heads/main": sha, "refs/heads/x": ""})

    def test_a_malformed_cas_is_unsupported_not_guessed_at(self):
        h = make_helper()
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("option cas main\noption cas refs/heads/m:nothex\n")), \
                patch("sys.stdout", out):
            h.run()
        self.assertEqual(out.getvalue().split(), ["unsupported", "unsupported"])
        self.assertEqual(h.cas_expect, {})

    def test_a_deletion_under_a_stale_lease_is_refused(self):
        h = self._with_lease("moved_sha", "remote_sha")
        lock = MagicMock()
        out = io.StringIO()
        with patch("sys.stdout", out):
            h._delete_remote_ref(lock, "refs/heads/main")
        self.assertIn("error refs/heads/main stale info", out.getvalue())
        h.client.delete_entry.assert_not_called()

    def test_dry_run_answers_a_missing_deletion_as_a_real_push_does(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.client.get_file_text.return_value = None
        h.dry_run = True

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/never-existed"])

        self.assertIn("error refs/heads/never-existed remote ref does not exist", out.getvalue())
        self.assertNotIn("ok refs/heads/never-existed", out.getvalue())
        h.client.delete_entry.assert_not_called()

    def test_dry_run_still_refuses_a_disallowed_namespace(self):
        """It previews a push, so it refuses exactly what a push would refuse."""
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.dry_run = True

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["HEAD:refs/notes/commits"])

        self.assertIn("refusing to push outside refs/heads and refs/tags", out.getvalue())
        h.client.upload_file.assert_not_called()

    @patch("git_remote_seafile.helper.rev_parse", return_value=None)
    def test_dry_run_reports_a_missing_local_ref(self, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.dry_run = True

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/gone:refs/heads/gone"])

        self.assertIn("error refs/heads/gone local ref does not exist", out.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="newsha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=False)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_force_push_allowed(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "oldsha456"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["+refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main", output)

    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=["obj1"])
    @patch("git_remote_seafile.helper.create_packfile", return_value=("packsha9", b"PACK", b"IDX"))
    def test_pack_upload_progress_callback_renews_the_lock(self, mock_pack, mock_objs, mock_rev, mock_ancestor):
        """The pack upload must renew the lease while it streams.

        on_upload_progress -> lock.maybe_renew(20.0) is what keeps a long push
        from outliving its lease while the bytes are still streaming; gc's
        equivalent callback is pinned by test_gc.py, but the push side -- where
        the transfer is the whole point of the lock -- was not pinned at all.
        A regression dropping progress_callback from the packfile upload_file
        call would let a slow push hold a lapsed lease, and the fence would
        then refuse the very ref write the uploaded pack was for.
        """
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        mock_lock = MagicMock()
        mock_lock.__enter__.return_value = mock_lock

        out = io.StringIO()
        with patch("sys.stdout", out),                 patch("git_remote_seafile.helper.RemoteLock", return_value=mock_lock):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        pack_calls = [c for c in h.client.upload_file.call_args_list
                      if len(c.args) > 2 and c.args[2] == "pack-packsha9.pack"]
        self.assertTrue(pack_calls, "the .pack upload was never issued")
        cb = pack_calls[0].kwargs.get("progress_callback")
        self.assertIsNotNone(
            cb,
            "the pack upload was issued without a progress callback -- "
            "a long push can no longer renew its lease mid-stream",
        )
        # The callback must be the renewing one: invoking it mid-transfer
        # pings the lock (a real transfer calls it on every chunk).
        with patch("sys.stderr", io.StringIO()):
            cb(1, 10)
        mock_lock.maybe_renew.assert_called()

    @patch("git_remote_seafile.helper.rev_parse", return_value="commit50sha")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.create_packfile", return_value=("packsha123", b"PACKBYTES50COMMITS", b"IDXBYTES"))
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=["obj1", "obj2", "obj3", "obj4"])
    def test_cmd_push_batch_multiple_commits(self, mock_objs, mock_pack, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "commit0sha"}
        h.client.list_dir.side_effect = _pack_dir_shows_uploads(h.client)

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main", output)
        uploaded_files = [call.args[2] for call in h.client.upload_file.call_args_list]
        self.assertIn("pack-packsha123.pack", uploaded_files)
        self.assertIn("pack-packsha123.idx", uploaded_files)
        self.assertIn("main", uploaded_files)

    @patch("git_remote_seafile.helper.rev_parse", return_value="earliersha100")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=False)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_rollback_force_push(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        # Remote was ahead at newersha200; local rolled back to earliersha100
        h._refs_cache = {"refs/heads/main": "newersha200"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["+refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main", output)
        # Verified no packfile was uploaded because no new objects were needed
        uploaded_files = [call.args[2] for call in h.client.upload_file.call_args_list]
        self.assertEqual(uploaded_files, ["main"])
        ref_content = h.client.upload_file.call_args_list[0].args[3]
        self.assertEqual(ref_content, b"earliersha100\n")

    @patch("git_remote_seafile.helper.rev_parse", side_effect=lambda ref: f"sha_{ref.split('/')[-1]}")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_multiple_branches(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([
                "refs/heads/main:refs/heads/main",
                "refs/heads/feature-auth:refs/heads/feature-auth",
            ])

        output = out.getvalue()
        self.assertIn("ok refs/heads/main", output)
        self.assertIn("ok refs/heads/feature-auth", output)
        uploaded_files = [call.args[2] for call in h.client.upload_file.call_args_list]
        self.assertIn("main", uploaded_files)
        self.assertIn("feature-auth", uploaded_files)

    @patch("git_remote_seafile.helper.rev_parse", side_effect=lambda ref: f"sha_{ref.split('/')[-1]}")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.create_packfile", return_value=("batch1234", b"BATCH_PACK", b"BATCH_IDX"))
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=["objA", "objB"])
    def test_cmd_push_multiple_branches_batches_single_pack(self, mock_objs, mock_pack, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.client.list_dir.side_effect = _pack_dir_shows_uploads(h.client)
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([
                "refs/heads/b1:refs/heads/b1",
                "refs/heads/b2:refs/heads/b2",
            ])

        output = out.getvalue()
        self.assertIn("ok refs/heads/b1", output)
        self.assertIn("ok refs/heads/b2", output)
        mock_objs.assert_called_once_with(["sha_b1", "sha_b2"], [])
        mock_pack.assert_called_once()
        self.assertEqual(mock_pack.call_args[0][0], ["objA", "objB"])
        self.assertIn("staged_dir", mock_pack.call_args[1])
        uploaded_files = [call.args[2] for call in h.client.upload_file.call_args_list]
        self.assertEqual(uploaded_files.count("pack-batch1234.pack"), 1)
        self.assertEqual(uploaded_files.count("pack-batch1234.idx"), 1)
        self.assertIn("b1", uploaded_files)
        self.assertIn("b2", uploaded_files)

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha_main")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_excludes_the_refs_the_remote_has_under_the_lock(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        # The pre-lock listing is stale on purpose: it still names a branch that
        # has since been deleted.  What may be left out of the pack is what the
        # remote holds *now*.
        h._refs_cache = {
            "refs/heads/main": "stale_sha_main",
            "refs/heads/deleted": "stale_sha_deleted",
        }
        tree = {
            "/git-repo/refs": [{"type": "dir", "name": "heads"}],
            "/git-repo/refs/heads": [
                {"type": "file", "name": "main"},
                {"type": "file", "name": "dev"},
            ],
        }
        shas = {"/git-repo/refs/heads/main": "live_sha_main", "/git-repo/refs/heads/dev": "live_sha_dev"}
        h.client.list_dir.side_effect = lambda rid, path: tree.get(path.rstrip("/"), [])
        h.client.get_file_text.side_effect = lambda rid, path: shas.get(path)

        with patch("sys.stdout", io.StringIO()), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/feature:refs/heads/feature"])

        mock_objs.assert_called_once()
        _, exclude = mock_objs.call_args[0]
        self.assertIn("live_sha_main", exclude)
        self.assertIn("live_sha_dev", exclude)
        self.assertNotIn("stale_sha_deleted", exclude)
        self.assertNotIn("stale_sha_main", exclude)

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha_main")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_sends_everything_when_the_refs_cannot_be_re_read(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/other": "stale_sha"}
        h.client.list_dir.side_effect = RuntimeError("boom")
        h.client.get_file_text.return_value = None

        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as err, \
                patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/feature:refs/heads/feature"])

        if mock_objs.call_args is not None:
            _, exclude = mock_objs.call_args[0]
            self.assertNotIn("stale_sha", exclude)
        self.assertIn("could not re-read the remote refs", err.getvalue())

    def test_a_password_in_the_url_is_masked_in_every_message_that_names_it(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.library_name = "lib"
        h.raw_url = "seafile://https://user:SECRETPW@seafile.example.com/lib"
        h.path_defaulted = True
        h._refs_cache = {}
        err = io.StringIO()
        with patch("sys.stderr", err):
            h._note_defaulted_path()
            h._warn_if_head_dangles("refs/heads/main")
        self.assertNotIn("SECRETPW", err.getvalue())
        self.assertIn("seafile.example.com", err.getvalue())
        self.assertIn("so this repository lives at", err.getvalue())

    # -- a ref is published only for packs proven to be on the remote ---------
    def _push_one(self, listing, **kw):
        """Push main with one 8-byte pack; *listing* answers the pack directory."""
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.client.list_dir.side_effect = lambda rid, path: listing(h.client) if "objects/pack" in path else []
        for key, value in kw.items():
            setattr(h.client, key, value)
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), \
                patch("git_remote_seafile.helper.RemoteLock"), \
                patch("git_remote_seafile.helper.rev_parse", return_value="sha1"), \
                patch("git_remote_seafile.helper.is_ancestor", return_value=True), \
                patch("git_remote_seafile.helper.get_objects_to_push", return_value=["o"]), \
                patch("git_remote_seafile.helper.create_packfile",
                      return_value=("abc", b"12345678", b"IDX")):
            h.cmd_push(["refs/heads/main:refs/heads/main"])
        ref_writes = [c for c in h.client.upload_file.call_args_list if c.args[2] == "main"]
        return h, out.getvalue(), err.getvalue(), ref_writes

    def test_a_truncated_pack_upload_publishes_no_ref(self):
        _h, out, err, ref_writes = self._push_one(
            lambda client: [{"name": "pack-abc.pack", "size": 3}, {"name": "pack-abc.idx", "size": 3}]
        )
        self.assertEqual(ref_writes, [], "a ref was written for a pack that landed short")
        self.assertIn("error refs/heads/main", out)
        self.assertIn("is 3 bytes on the remote but 8 locally", out)

    def test_a_pack_missing_from_the_listing_publishes_no_ref(self):
        _h, out, _err, ref_writes = self._push_one(lambda client: [{"name": "pack-abc.idx", "size": 3}])
        self.assertEqual(ref_writes, [])
        self.assertIn("pack-abc.pack is not listed on the remote", out)

    def test_a_pack_that_landed_whole_publishes_the_ref(self):
        _h, out, _err, ref_writes = self._push_one(
            lambda client: [{"name": "pack-abc.pack", "size": 8}, {"name": "pack-abc.idx", "size": 3}]
        )
        self.assertEqual(len(ref_writes), 1)
        self.assertIn("ok refs/heads/main", out)

    # -- every ref is fenced, not just the first -------------------------------
    @patch("git_remote_seafile.helper.rev_parse", side_effect=lambda ref: f"sha_{ref.split('/')[-1]}")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_the_lease_is_re_checked_before_each_ref(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        lock = MagicMock()
        lock.__enter__.return_value = lock
        # before the loop, before b1, then the lease is gone before b2
        lock.verify_ownership.side_effect = [True, True, False]
        lock.ownership_state.return_value = LEASE_LOST
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), \
                patch("git_remote_seafile.helper.RemoteLock", return_value=lock):
            h.cmd_push(["refs/heads/b1:refs/heads/b1", "refs/heads/b2:refs/heads/b2"])
        written = [c.args[2] for c in h.client.upload_file.call_args_list]
        self.assertIn("b1", written)
        self.assertNotIn("b2", written, "a ref was written after the lease was lost")
        self.assertIn("ok refs/heads/b1", out.getvalue())
        self.assertIn("error refs/heads/b2 could not confirm lock ownership", out.getvalue())

    # -- a landed push is not reported rejected --------------------------------
    @patch("git_remote_seafile.helper.rev_parse", return_value="sha_main")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_a_head_bootstrap_failure_does_not_reject_a_landed_push(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://lib/repo"
        h._refs_cache = {}

        def get_text(rid, path):
            if path.endswith("/HEAD"):
                raise RuntimeError("HTTP 500")
            if path.endswith("refs/heads/main"):
                return "sha_main" if h.client.upload_file.called else None
            return None

        h.client.get_file_text.side_effect = get_text
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), \
                patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])
        self.assertIn("ok refs/heads/main", out.getvalue())
        self.assertNotIn("error refs/heads/main", out.getvalue())
        self.assertIn("default branch (HEAD) could not be set", err.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha_main")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_aborts_ref_updates_if_lock_ownership_lost(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        mock_lock = MagicMock()
        mock_lock.__enter__.return_value = mock_lock
        # Ownership verification fails right before ref writes, because another
        # client took over the lease.
        mock_lock.verify_ownership.return_value = False
        mock_lock.ownership_state.return_value = LEASE_LOST

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), patch("git_remote_seafile.helper.RemoteLock", return_value=mock_lock):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        expected = "could not confirm lock ownership before updating refs (another client took over the lock)"
        self.assertIn(f"error refs/heads/main {expected}", out.getvalue())
        self.assertIn(f"Push error: {expected}", err.getvalue())
        # Ref was never uploaded to remote
        h.client.upload_file.assert_not_called()

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_reports_unproven_ownership_distinctly(self, mock_objs, mock_ancestor, mock_rev):
        """A transient read/write failure must not be reported as a takeover.

        Both outcomes fence the ref write, but telling the user "another client
        took over the lock" when the real cause was an unreadable ticket sends
        them hunting for a competing push that never happened (D7).
        """
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        mock_lock = MagicMock()
        mock_lock.__enter__.return_value = mock_lock
        mock_lock.verify_ownership.return_value = False
        mock_lock.ownership_state.return_value = LEASE_UNKNOWN

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), patch("git_remote_seafile.helper.RemoteLock", return_value=mock_lock):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("could not confirm lock ownership before updating refs", output)
        self.assertIn("ownership is unproven", output)
        self.assertNotIn("another client took over", output)
        h.client.upload_file.assert_not_called()

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_auto_gc_notification_when_disabled(self, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://repo/path"
        h._refs_cache = {}

        # 22 small remote packfiles -> threshold is 20, auto-gc disabled
        h.client.list_dir.return_value = [{"name": f"pack-{i}.pack"} for i in range(22)]
        cfg = RemoteConfig(lock_timeout=15, lock_lease=60, lock_settle=0, auto_gc=False, gc_threshold=20)

        err = io.StringIO()
        with patch("sys.stderr", err), \
             patch("git_remote_seafile.helper.RemoteLock"), \
             patch("git_remote_seafile.helper._notice_due", return_value=True), \
             patch("git_remote_seafile.helper.RemoteConfig.load", return_value=cfg):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("Notice: Remote repository has 22 small packfiles", err.getvalue())
        self.assertIn("Tip: Run 'git-remote-seafile gc", err.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    @patch("git_remote_seafile.gc.tail_merge")
    def test_auto_gc_triggered_when_enabled(self, mock_merge, mock_objs, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://repo/path"
        h._refs_cache = {}

        # 22 small remote packfiles -> threshold is 20, auto-gc enabled
        h.client.list_dir.return_value = [{"name": f"pack-{i}.pack"} for i in range(22)]
        cfg = RemoteConfig(lock_timeout=15, lock_lease=60, lock_settle=0, auto_gc=True, gc_threshold=20)

        with patch("git_remote_seafile.helper.RemoteLock"), \
             patch("git_remote_seafile.helper.RemoteConfig.load", return_value=cfg):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        mock_merge.assert_called_once_with(
            h.client, "repo1", "/git-repo", config=cfg, verbose=True,
        )

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    @patch("git_remote_seafile.gc.tail_merge")
    def test_auto_gc_counts_only_small_packs_toward_the_threshold(self, mock_merge, mock_objs, mock_ancestor, mock_rev):
        # Frozen packs (at or above seafile.gcmaxpackmib) are part of the
        # bounded floor: they must not re-arm the threshold on their own.
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://repo/path"
        h._refs_cache = {}

        # 2 small packs plus 30 frozen ones: the total count crosses the
        # threshold, but the *movable* count does not.
        mi_b = 1024**2
        packs = (
            [{"name": f"pack-small-{i}.pack", "size": mi_b // 2} for i in range(2)]
            + [{"name": f"pack-big-{i}.pack", "size": 2 * mi_b} for i in range(30)]
        )
        h.client.list_dir.side_effect = lambda rid, path: packs if "objects/pack" in path else []
        cfg = RemoteConfig(lock_timeout=15, lock_lease=60, lock_settle=0, auto_gc=True, gc_threshold=20,
                           gc_max_pack_mib=1)

        err = io.StringIO()
        with patch("sys.stderr", err), \
             patch("git_remote_seafile.helper.RemoteLock"), \
             patch("git_remote_seafile.helper.RemoteConfig.load", return_value=cfg):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertEqual(err.getvalue(), "")
        mock_merge.assert_not_called()

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    @patch("git_remote_seafile.gc.tail_merge")
    def test_auto_gc_defers_when_the_movable_set_exceeds_the_budget(self, mock_merge, mock_objs, mock_ancestor, mock_rev):
        # Enough *small* packs to cross the threshold, but their total exceeds
        # the download budget: auto-gc must notice and defer instead of
        # round-tripping that much mid-push.
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://repo/path"
        h._refs_cache = {}

        # 9 packs of ~1 MiB each: every one is movable (under the 2 MiB freeze
        # threshold) and together they exceed the 8 MiB budget.
        mi_b = 1024**2
        h.client.list_dir.return_value = [
            {"name": f"pack-{i}.pack", "size": mi_b} for i in range(9)
        ]
        cfg = RemoteConfig(lock_timeout=15, lock_lease=60, lock_settle=0, auto_gc=True, gc_threshold=3,
                           gc_max_mib=8, gc_max_pack_mib=4)

        err = io.StringIO()
        with patch("sys.stderr", err), \
             patch("git_remote_seafile.helper.RemoteLock"), \
             patch("git_remote_seafile.helper.RemoteConfig.load", return_value=cfg):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("Auto tail-merge was deferred", err.getvalue())
        self.assertIn("seafile.gcmaxpackmib", err.getvalue())
        mock_merge.assert_not_called()

    @patch("git_remote_seafile.helper.install_packfile")
    @patch("git_remote_seafile.helper.get_git_dir")
    def test_cmd_fetch(self, mock_get_git_dir, mock_install_pack):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"

        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)
            mock_get_git_dir.return_value = local_git

            # Remote has pack-1.pack and pack-2.pack
            h.client.list_dir.return_value = [
                {"name": "pack-1.pack"},
                {"name": "pack-2.pack"},
            ]
            h.client.get_file_bytes.return_value = b"PACKBYTES"

            out = io.StringIO()
            with patch("sys.stdout", out):
                h.cmd_fetch(["fetch 123 refs/heads/main"])

            # Verify packfiles were downloaded and installed
            self.assertEqual(mock_install_pack.call_count, 2)

    def test_cmd_list_empty_repository(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/empty-repo"
        h._refs_cache = {}

        # Completely empty repository on Seafile
        h.client.list_dir.return_value = []
        h.client.get_file_text.return_value = None

        out = io.StringIO()
        with patch("sys.stdout", out):
            h.cmd_list()

        output = out.getvalue()
        self.assertEqual(output.strip(), "")
        self.assertTrue(output.endswith("\n"))

    @patch("git_remote_seafile.helper.rev_parse", return_value="tag_sha_123")
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=[])
    def test_cmd_push_tag_creation_and_deletion(self, mock_objs, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        # The deletion path reads the ref before removing it, and that read is
        # load-bearing: it is what proves the target is a *file* rather than the
        # refs/tags directory `delete_entry` would otherwise happily remove, and
        # what turns deleting a ref that is not there into git's "remote ref
        # does not exist" instead of an `ok` for a no-op.  So the mock has to
        # model the ref the push just created -- a blanket None describes a
        # repository where the tag was never pushed, and the delete then
        # reported `ok` for removing something that did not exist.
        created = {"tag": False}

        def fake_get_file_text(repo_id, path):
            if path.endswith("refs/tags/v1.0.0") and created["tag"]:
                return "tag_sha_123"
            return None

        h.client.get_file_text.side_effect = fake_get_file_text
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/tags/v1.0.0:refs/tags/v1.0.0"])

        output = out.getvalue()
        self.assertIn("ok refs/tags/v1.0.0", output)
        uploaded = [c.args[2] for c in h.client.upload_file.call_args_list]
        self.assertIn("v1.0.0", uploaded)

        # Now delete the tag -- which the push above has just created.
        created["tag"] = True
        # ...and it is gone once the delete is acknowledged (the helper reads
        # the ref back, so a double that keeps serving it models a server that
        # lied).
        h.client.delete_entry.side_effect = lambda repo, path: (
            created.__setitem__("tag", False) or True
        )
        out_del = io.StringIO()
        with patch("sys.stdout", out_del), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/tags/v1.0.0"])

        self.assertIn("ok refs/tags/v1.0.0", out_del.getvalue())
        h.client.delete_entry.assert_called_with("repo1", "/git-repo/refs/tags/v1.0.0")

    def test_cmd_push_delete_failure(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.client.delete_entry.return_value = False

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/protected-branch"])

        output = out.getvalue()
        self.assertIn("error refs/heads/protected-branch failed to delete ref on remote", output)

    def test_cmd_push_delete_prunes_empty_nested_directories(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/feature/auth": "sha123"}
        h.client.delete_entry.return_value = True

        def fake_list(repo_id, path):
            if path.endswith("/feature"):
                return []
            return [{"name": "main"}]
        h.client.list_dir.side_effect = fake_list

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/feature/auth"])

        self.assertIn("ok refs/heads/feature/auth", out.getvalue())
        h.client.delete_entry.assert_any_call("repo1", "/git-repo/refs/heads/feature")
        # The directory cache must forget the deleted path (public API), or a
        # future D/F ref conflict can never be recreated.
        h.client.evict_known_dir.assert_called_once_with("repo1", "/git-repo/refs/heads/feature")

    def test_a_delete_the_server_acknowledged_but_did_not_perform_is_not_ok(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/gone": "a" * 40}
        h.client.get_file_text.return_value = "a" * 40  # before and after the delete
        h.client.delete_entry.return_value = True

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/gone"])

        self.assertIn("error refs/heads/gone", out.getvalue())
        self.assertIn("still there", out.getvalue())
        self.assertNotIn("ok refs/heads/gone", out.getvalue())

    def test_a_sibling_created_before_the_prune_keeps_its_directory(self):
        """The directory delete is recursive; the last listing before it
        decides.  A peer's `feature/beta` appearing in between must survive."""
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/feature/auth": "sha123"}
        h.client.delete_entry.return_value = True
        h.client.dir_exists.return_value = True
        listings = iter([[], [{"name": "beta", "type": "file"}]])
        h.client.list_dir.side_effect = lambda repo_id, path: next(listings)

        with patch("sys.stdout", io.StringIO()), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/feature/auth"])

        deleted = [c.args[1] for c in h.client.delete_entry.call_args_list]
        self.assertNotIn("/git-repo/refs/heads/feature", deleted)

    @patch("git_remote_seafile.helper.rev_parse", return_value="a" * 40)
    @patch("git_remote_seafile.helper.is_ancestor")
    def test_a_remote_ref_that_looks_like_a_git_option_never_reaches_git(self, mock_ancestor, _rev):
        h = make_helper()
        h.client = MagicMock()
        h.client.get_file_text.return_value = "--stdin"
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        self.assertEqual(
            h._decide_update("refs/heads/main", "refs/heads/main", force=False), "fetch first"
        )
        mock_ancestor.assert_not_called()

    def test_cmd_push_delete_does_not_prune_on_an_unconfirmed_listing(self):
        # list_dir answers [] for both "empty" and "missing/unreachable"; a
        # transient failure used to read as "prune it" -- a destructive call
        # keyed on an answer the server never actually gave.  dir_exists()
        # re-asks and only an explicit 200 confirms.
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/feature/auth": "sha123"}
        h.client.delete_entry.return_value = True
        h.client.list_dir.side_effect = Exception("transient listing failure")
        h.client.dir_exists.return_value = False

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/feature/auth"])

        # the ref delete itself succeeds; only the prune's listing is broken,
        # and no parent directory was touched on that broken answer
        self.assertIn("ok refs/heads/feature/auth", out.getvalue())
        pruned = [
            c for c in h.client.delete_entry.call_args_list
            if c.args[1] == "/git-repo/refs/heads/feature"
        ]
        self.assertEqual(pruned, [])

    def test_cmd_push_reports_a_malformed_refspec_instead_of_dying(self):
        # git never sends a spec without the src:dst colon, but a hand-driven
        # protocol can; the split used to raise ValueError and kill the helper
        # mid-protocol with no error line for git to read.
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        with patch("git_remote_seafile.helper.check_preflight_safety", return_value=[]):
            out = io.StringIO()
            with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
                h.cmd_push(["+refs/heads/main"])

        self.assertIn("invalid refspec", out.getvalue())
        self.assertIn("missing ':'", out.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="sha123")
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=["obj1"])
    @patch("git_remote_seafile.helper.create_packfile", return_value=("pack1", b"PACK", b"IDX"))
    def test_cmd_push_upload_failure_reports_error(self, mock_pack, mock_objs, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        h.client.upload_file.side_effect = RuntimeError("HTTP 500 Seafile server error")

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output = out.getvalue()
        self.assertIn("error refs/heads/main HTTP 500 Seafile server error", output)

    def test_cmd_push_lock_timeout_aborts_all_specs(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        mock_lock_cls = MagicMock()
        mock_lock_instance = MagicMock()
        mock_lock_instance.__enter__.side_effect = RepositoryLockedError("locked by user@otherhost")
        mock_lock_cls.return_value = mock_lock_instance

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock", mock_lock_cls):
            h.cmd_push(["refs/heads/main:refs/heads/main", "refs/heads/dev:refs/heads/dev"])

        output = out.getvalue()
        self.assertIn("error refs/heads/main locked by user@otherhost", output)
        self.assertIn("error refs/heads/dev locked by user@otherhost", output)

    def test_a_head_read_failure_is_raised_before_any_ref_line_is_written(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.client.get_file_text.side_effect = SeafileAPIError("HEAD read: HTTP 503")
        out = io.StringIO()
        with patch("git_remote_seafile.helper.iter_refs",
                   side_effect=lambda c, r, p, ns, **kw: iter([(f"{ns}/main", "a" * 40)] if ns == "refs/heads" else [])),              patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SeafileAPIError):
                h.cmd_list()
        self.assertEqual(out.getvalue(), "", "git must not receive a partial advertisement")

    def test_listing_retries_once_when_a_refs_directory_vanishes_and_then_succeeds(self):
        h = make_helper()
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        calls = {"n": 0}

        def flaky(c, r, p, ns, **kw):
            if ns == "refs/heads":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise SeafileAPIError("/git-repo/refs/heads/feature was listed by its parent but is now missing")
                return iter([(f"{ns}/main", "a" * 40)])
            return iter([])

        out, err = io.StringIO(), io.StringIO()
        with patch("git_remote_seafile.helper.iter_refs", side_effect=flaky),                 patch("git_remote_seafile.helper.time.sleep"),                 patch("sys.stdout", out), patch("sys.stderr", err):
            h.cmd_list()
        self.assertIn(f"{'a' * 40} refs/heads/main", out.getvalue())
        self.assertIn("retrying once", err.getvalue())

    def test_listing_fails_rather_than_advertise_part_of_the_repository(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        out = io.StringIO()
        with patch("git_remote_seafile.helper.iter_refs",
                   side_effect=SeafileAPIError("a refs directory is now missing")),                 patch("git_remote_seafile.helper.time.sleep"),                 patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SeafileAPIError):
                h.cmd_list()
        self.assertEqual(out.getvalue(), "")

    def test_deleting_the_branch_head_points_at_says_so(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.raw_url = "seafile://lib/git-repo"
        h._refs_cache = {}

        deleted: set[str] = set()

        def text(repo, path):
            if path.endswith("/HEAD"):
                return "ref: refs/heads/main"
            return None if path in deleted else "a" * 40

        h.client.get_file_text.side_effect = text
        h.client.delete_entry.side_effect = lambda repo, path: deleted.add(path) or True
        lock = MagicMock()
        lock.verify_ownership.return_value = True
        err, out = io.StringIO(), io.StringIO()
        with patch("sys.stderr", err), patch("sys.stdout", out):
            h._delete_remote_ref(lock, "refs/heads/main")
        self.assertIn("ok refs/heads/main", out.getvalue())
        self.assertIn("set-head seafile://lib/git-repo", err.getvalue())

        err2 = io.StringIO()
        with patch("sys.stderr", err2), patch("sys.stdout", io.StringIO()):
            h._delete_remote_ref(lock, "refs/heads/other")
        self.assertNotIn("set-head", err2.getvalue())

    def test_a_command_that_merely_starts_like_a_known_one_is_unsupported(self):
        h = make_helper()
        h.cmd_list = MagicMock()
        h.cmd_push = MagicMock()
        for line in ("listing", "pushed refs/heads/a:refs/heads/a", "fetching abc"):
            with patch("sys.stdin", io.StringIO(line + "\n")), \
                 patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(GitError):
                    h.run()
        h.cmd_list.assert_not_called()
        h.cmd_push.assert_not_called()

    def test_helper_run_protocol_loop(self):
        h = make_helper()
        h.cmd_capabilities = MagicMock()
        h.cmd_list = MagicMock()
        h.cmd_push = MagicMock()
        h.cmd_fetch = MagicMock()

        # Simulate git sending capabilities, list, push, fetch, unknown command, and EOF
        commands = (
            "capabilities\n"
            "list\n"
            "list for-push\n"
            "push refs/heads/main:refs/heads/main\n"
            "push refs/heads/dev:refs/heads/dev\n"
            "\n"
            "fetch sha1\n"
            "fetch sha2\n"
            "\n"
            "unknown-cmd\n"
        )
        # The unknown command ends the run loudly now:
        # the helper must not go on reading an undefined protocol state.
        with patch("sys.stdin", io.StringIO(commands)), patch("sys.stdout", io.StringIO()):
            with self.assertRaises(GitError):
                h.run()

        h.cmd_capabilities.assert_called_once()
        self.assertEqual(h.cmd_list.call_count, 2)
        h.cmd_list.assert_any_call(for_push=False)
        h.cmd_list.assert_any_call(for_push=True)
        h.cmd_push.assert_called_once_with([
            "refs/heads/main:refs/heads/main",
            "refs/heads/dev:refs/heads/dev"
        ])
        h.cmd_fetch.assert_called_once_with(["sha1", "sha2"])

    @patch("git_remote_seafile.helper.rev_parse", return_value=None)
    def test_cmd_push_local_ref_not_found(self, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/missing:refs/heads/missing"])

        self.assertIn("error refs/heads/missing local ref does not exist", out.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=False)
    def test_cmd_push_non_fast_forward_rejected(self, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "remote_sha"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("error refs/heads/main non-fast-forward", out.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha")
    @patch("git_remote_seafile.helper.is_ancestor", side_effect=GitError("fatal: Not a valid object name remote_sha"))
    def test_cmd_push_unknown_remote_tip_reports_fetch_first(self, mock_ancestor, mock_rev):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {"refs/heads/main": "remote_sha"}

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("error refs/heads/main fetch first", out.getvalue())

    @patch("git_remote_seafile.helper.rev_parse", return_value="local_sha")
    @patch("git_remote_seafile.helper.is_ancestor", return_value=True)
    @patch("git_remote_seafile.helper.get_objects_to_push", return_value=["obj1"])
    @patch("git_remote_seafile.helper.create_packfile", return_value=("pack1", b"PACK", b"IDX"))
    def test_cmd_push_cas_recheck_catches_a_moved_ref(self, mock_pack, mock_objs, mock_anc, mock_rev):
        # N-1's second half: after the fast-forward decision, the ref is read
        # again immediately before the write.  A concurrent push that landed
        # between the two reads must be caught by this compare-and-swap and
        # reported as "fetch first" -- not clobbered by the ref upload.
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        # read 1: the fast-forward check; read 2: the CAS re-check
        h.client.get_file_text.side_effect = ["remote_sha", "moved_sha"]
        h.client.list_dir.side_effect = _pack_dir_shows_uploads(h.client)

        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertIn("error refs/heads/main fetch first", out.getvalue())
        # no ref file was written: the only uploads were the pack artefacts
        ref_writes = [
            c for c in h.client.upload_file.call_args_list
            if len(c.args) >= 3 and c.args[1].startswith("/git-repo/refs/heads")
        ]
        self.assertEqual(ref_writes, [])

    def test_cmd_push_branch_deletion(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        # Success case
        h.client.delete_entry.return_value = True
        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/feature"])
        self.assertIn("ok refs/heads/feature", out.getvalue())

        # Exception case
        h.client.delete_entry.side_effect = RuntimeError("Delete failed")
        out = io.StringIO()
        with patch("sys.stdout", out), patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push([":refs/heads/feature"])
        self.assertIn("error refs/heads/feature Delete failed", out.getvalue())


    def test_cmd_push_multiline_error_sanitized(self):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h.library_name = "test-repo"
        h._refs_cache = {}

        # Simulate multi-line exception during upload
        h.client.upload_file.side_effect = Exception("line 1 error\nline 2 fatal error\nline 3 details")

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), \
             patch("git_remote_seafile.helper.rev_parse", return_value="sha123"), \
             patch("git_remote_seafile.helper.is_ancestor", return_value=True), \
             patch("git_remote_seafile.helper.get_objects_to_push", return_value=[]), \
             patch("git_remote_seafile.helper.RemoteLock"):
            h.cmd_push(["refs/heads/main:refs/heads/main"])

        output_lines = out.getvalue().splitlines()
        # Verify stdout lines obey git protocol: single error line followed by terminator newline
        non_empty = [line for line in output_lines if line]
        self.assertEqual(len(non_empty), 1)
        self.assertTrue(non_empty[0].startswith("error refs/heads/main "))
        self.assertIn("line 1 error", non_empty[0])
        self.assertIn("line 2 fatal error", non_empty[0])
        # Ensure raw unhandled newlines were replaced so git protocol does not break
        self.assertNotIn("\n", non_empty[0])


class TestPushDestinationValidation(unittest.TestCase):
    """A push destination is validated, not assumed.

    The helper applied ``is_valid_ref_name`` to every ref it *listed* but never
    to the destination of a push, so only a prefix test stood in the way.  That
    test accepts ``refs/heads/`` -- and ``_full_path`` strips the trailing slash
    to ``refs/heads``, which is a *directory*, which ``delete_entry`` removes as
    happily as a file.  So ``git push origin :refs/heads/`` deleted every branch
    in the repository.
    """

    def _helper(self, refs_cache=None):
        h = make_helper()
        h.client = MagicMock()
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = dict(refs_cache or {})
        return h

    def _push(self, h, spec, lock=None):
        out = io.StringIO()
        with patch("sys.stdout", out), patch(
            "git_remote_seafile.helper.RemoteLock", return_value=lock if lock else MagicMock()
        ):
            h.cmd_push([spec])
        return out.getvalue()

    def test_the_validator_agrees_with_git_on_the_boundary_names(self):
        from git_remote_seafile.helper import _destination_error

        for ok in ("refs/heads/main", "refs/heads/feature/auth", "refs/tags/v1.0"):
            with self.subTest(dst=ok):
                self.assertIsNone(_destination_error(ok))
        for bad in ("refs/heads/", "refs/heads/a/", "refs/tags/", "refs/heads",
                    "refs/notes/x", "HEAD", "refs/heads/a.lock", "refs/heads/.hidden"):
            with self.subTest(dst=bad):
                self.assertIsNotNone(_destination_error(bad))

    def test_a_destination_naming_a_namespace_is_refused(self):
        for dst in ("refs/heads/", "refs/heads/a/", "refs/tags/"):
            with self.subTest(dst=dst):
                h = self._helper()
                output = self._push(h, ":" + dst)
                self.assertIn("trailing component", output)
                h.client.delete_entry.assert_not_called()

    def test_a_destination_outside_the_namespaces_is_refused(self):
        h = self._helper()
        output = self._push(h, "refs/notes/x:refs/notes/x")
        self.assertIn("refusing to push outside", output)

    def test_a_destination_git_would_reject_is_refused(self):
        h = self._helper()
        output = self._push(h, "refs/heads/a..b:refs/heads/a..b")
        self.assertIn("git-check-ref-format", output)

    def test_deleting_a_ref_that_is_not_there_is_an_error(self):
        """Not a silent `ok` -- and never a blind directory removal."""
        h = self._helper({"refs/heads/gone": "stale-sha"})
        h.client.get_file_text.return_value = None

        output = self._push(h, ":refs/heads/gone")

        self.assertIn("remote ref does not exist", output)
        h.client.delete_entry.assert_not_called()
        # The cached SHA is only usable while the ref exists; a no-op delete
        # that reported `ok` left it behind as a push exclusion.
        self.assertIn("refs/heads/gone", h._refs_cache)

    def test_the_delete_path_is_fenced_by_the_lease(self):
        h = self._helper()
        h.client.get_file_text.return_value = "sha123"
        lock = MagicMock()
        lock.verify_ownership.return_value = False
        lock.ownership_state.return_value = LEASE_LOST
        lock.__enter__.return_value = lock

        output = self._push(h, ":refs/heads/old", lock=lock)

        self.assertIn("could not confirm lock ownership", output)
        h.client.delete_entry.assert_not_called()


class TestFetchSafety(unittest.TestCase):
    """The guardrails must run on fetch/clone, not only on push (#13).

    ``git clone seafile://Documents/code/myproject`` run from inside the synced
    ``Documents/`` library used to sail through: the clone downloaded packfiles
    straight into the synced tree and started the churn/reflection cycle the
    guardrails exist to prevent.  Fetch is read-only against the *server*, but
    it writes a whole repository into the local directory, and that directory is
    what the collision check is about.

    These tests drive the real ``check_preflight_safety`` -- only the desktop
    client's repo.db discovery is stubbed -- so the path logic under test is the
    shipping code.
    """

    @staticmethod
    def _helper(library_name="Documents", repo_path="/code/myproject", repo_id="r1"):
        h = make_helper()
        h.client = MagicMock()
        h.client.get_repo_id.return_value = repo_id
        h.repo_id = repo_id
        h.repo_path = repo_path
        h.library_name = library_name
        h._refs_cache = {}
        return h

    def test_fetch_runs_the_guardrails_in_fetch_mode(self):
        """cmd_fetch must consult the guardrails, and with push_mode=False."""
        seen = {}

        def fake_check(client, library, path, push_mode=True, **kwargs):
            seen["push_mode"] = push_mode
            seen["library"] = library
            seen["path"] = path
            return []

        h = self._helper()
        with patch(
            "git_remote_seafile.helper.check_preflight_safety", side_effect=fake_check
        ), patch(
            "git_remote_seafile.helper.get_git_dir",
            side_effect=RuntimeError("reached the fetch body"),
        ):
            with patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(RuntimeError):
                    h.cmd_fetch([f"{'a' * 40} refs/heads/main"])

        self.assertIs(
            seen["push_mode"], False,
            "a fetch must be checked in fetch mode -- the push-only reflection "
            "gate (Trap 2) would otherwise block legitimate fetches",
        )
        self.assertEqual(seen["library"], "Documents")
        self.assertEqual(seen["path"], "/code/myproject")

    def test_clone_inside_the_synced_library_is_blocked(self):
        """The reviewer's exact scenario: clone into the tree it warns about.

        ``git clone`` runs the helper with cwd = the parent of the directory
        being created, which is not a repository yet -- so git cannot name a
        work tree and the cwd fallback is what catches this.
        """
        h = self._helper()

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            inside = doc_dir / "code" / "myproject"
            inside.mkdir(parents=True)

            libs = [
                {"repo_id": "r1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            original_cwd = os.getcwd()
            os.chdir(inside)
            try:
                with patch(
                    "git_remote_seafile.safety.discover_local_synced_libraries",
                    return_value=libs,
                ), patch(
                    "git_remote_seafile.safety.get_local_work_tree",
                    return_value=None,
                ), patch(
                    "git_remote_seafile.safety.get_trusted_git_config_bool",
                    return_value=False,
                ), patch(
                    "git_remote_seafile.helper.get_git_dir",
                    side_effect=AssertionError(
                        "the fetch body ran despite the Trap 1 collision"
                    ),
                ):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SafetyError) as ctx:
                            h.cmd_fetch([f"{'a' * 40} refs/heads/main"])
            finally:
                os.chdir(original_cwd)

        self.assertIn("Trap 1", str(ctx.exception))

    def test_the_reflection_gate_still_blocks_a_push(self):
        """Fetch skips Trap 2; the identical push is still blocked by it.

        Without the paired push assertion this test would also pass if Trap 2
        had simply been broken for everyone.
        """
        h = self._helper(repo_path="/elsewhere/repo")

        with tempfile.TemporaryDirectory() as tmpdir:
            doc_dir = Path(tmpdir) / "Documents"
            doc_dir.mkdir()
            outside = Path(tmpdir) / "outside"
            outside.mkdir()

            libs = [
                {"repo_id": "r1", "name": "Documents", "worktree": doc_dir,
                 "server_url": "https://seafile.example.com"}
            ]

            with patch(
                "git_remote_seafile.safety.get_trusted_git_config_bool", return_value=False
            ), patch.dict(os.environ, {"SEAFILE_SKIP_SAFETY_CHECKS": ""}):
                # push: the un-ignored path in a synced library is blocked
                with self.assertRaises(SafetyError) as ctx:
                    check_preflight_safety(
                        h.client, "Documents", "/elsewhere/repo",
                        local_worktree=outside, push_mode=True, synced_libs=libs,
                    )
                self.assertIn("Trap 2", str(ctx.exception))

                # fetch: the same inputs must pass, and reach the fetch body
                # -- and the spy proves the guardrails actually ran, in fetch
                # mode, rather than never being consulted at all.
                modes_seen = []
                real_check = check_preflight_safety

                def spy(client, library, path, **kwargs):
                    modes_seen.append(kwargs.get("push_mode"))
                    return real_check(client, library, path, **kwargs)

                with patch(
                    "git_remote_seafile.helper.check_preflight_safety", side_effect=spy
                ), patch(
                    "git_remote_seafile.safety.discover_local_synced_libraries",
                    return_value=libs,
                ), patch(
                    "git_remote_seafile.helper.get_git_dir",
                    side_effect=RuntimeError("reached the fetch body"),
                ):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(RuntimeError):
                            h.cmd_fetch([f"{'a' * 40} refs/heads/main"])

                self.assertEqual(
                    modes_seen, [False],
                    "the fetch must be checked exactly once, in fetch mode",
                )


class TestFetchFailureIsLoud(unittest.TestCase):
    """A failed fetch must never look like a successful one.

    cmd_fetch caught every exception, wrote the protocol terminator and
    returned normally, so the helper exited 0.  Git then reported a successful
    fetch of nothing: `git clone` produced an *empty* repository and exited 0,
    and the user only found out later.  This is the fetch half of the same
    silent-success class as the refs-listing bug.
    """

    @staticmethod
    def _helper(client):
        h = make_helper()
        h.client = client
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        return h

    def test_a_listing_error_propagates_and_emits_no_terminator(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.side_effect = SeafileAPIError("HTTP 500 server error")
            h = self._helper(client)

            out = io.StringIO()
            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SeafileAPIError):
                        h.cmd_fetch([f"{'a' * 40} refs/heads/main"])

            self.assertEqual(
                out.getvalue(), "",
                "a failed fetch must not write the success terminator -- that is "
                "what made git report an empty repository as a successful clone",
            )

    def test_an_advertised_pack_that_cannot_be_downloaded_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [
                {"name": "pack-ffffffffffffffffffffffffffffffffffffffff.pack"},
            ]
            client.get_file_bytes.return_value = None  # 404 on the download link
            h = self._helper(client)

            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("sys.stdout", new_callable=io.StringIO):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        with self.assertRaises(SeafileAPIError) as ctx:
                            h.cmd_fetch([f"{'a' * 40} refs/heads/main"])

            self.assertIn("pack-ffffffffffffffffffffffffffffffffffffffff.pack", str(ctx.exception))

    def test_a_successful_fetch_still_emits_the_terminator(self):
        # The guard against false failure: a healthy fetch must stay quiet and
        # terminate the protocol normally.
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack"}]
            client.get_file_bytes.return_value = b"PACKBYTES"
            h = self._helper(client)

            out = io.StringIO()
            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("git_remote_seafile.helper.install_packfile") as install:
                    with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                        h.cmd_fetch([f"{'a' * 40} refs/heads/main"])

            install.assert_called_once()
            self.assertEqual(out.getvalue(), "\n")

    def test_invalid_pack_name_ignored_in_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [
                {"name": "../evil.pack"},
                {"name": "malformed.pack"},
            ]
            h = self._helper(client)

            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO) as err:
                    h.cmd_fetch([f"{'a' * 40} refs/heads/main"])

            self.assertEqual(out.getvalue(), "\n")
            self.assertIn("ignoring invalid remote packfile name", err.getvalue())
            client.get_file_bytes.assert_not_called()

    def test_malformed_ref_name_ignored_in_cmd_list(self):
        client = MagicMock()
        client.list_dir.side_effect = lambda repo_id, path: [
            {"name": "main", "type": "file"},
            {"name": "bad..ref", "type": "file"},
            {"name": "bad space", "type": "file"},
        ] if "refs/heads" in path else []
        client.get_file_text.return_value = "0123456789012345678901234567890123456789"
        h = self._helper(client)

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            h.cmd_list()

        output = out.getvalue()
        self.assertIn("refs/heads/main", output)
        self.assertNotIn("bad..ref", output)
        self.assertNotIn("bad space", output)
        self.assertIn("ignoring malformed ref name", err.getvalue())

    def test_truncated_pack_download_fails_with_size_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack", "size": 1024}]
            client.get_file_bytes.return_value = b"SHORT_BYTES"
            del client.download_file_to  # ensure fallback to get_file_bytes
            h = self._helper(client)

            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SeafileAPIError) as ctx:
                        h.cmd_fetch([f"{'a' * 40} refs/heads/main"])

            self.assertIn("truncated", str(ctx.exception))

    def test_streaming_pack_download_via_download_file_to(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack", "size": 10}]
            def fake_download_file_to(repo_id, path, dest, progress_callback=None):
                Path(dest).write_bytes(b"0123456789")
                return True
            client.download_file_to.side_effect = fake_download_file_to
            h = self._helper(client)

            out = io.StringIO()
            with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                with patch("git_remote_seafile.helper.install_packfile") as install:
                    with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                        h.cmd_fetch([f"{'a' * 40} refs/heads/main"])

            client.download_file_to.assert_called()
            install.assert_called_once()
            self.assertEqual(out.getvalue(), "\n")


class TestInstalledPackState(unittest.TestCase):
    """A9 — a local repack must not make the next fetch re-download everything.

    The skip that keeps a fetch from downloading every remote pack was keyed on
    the *local* pack filenames under ``objects/pack``.  A local ``git gc``
    repacks and renames them, so the names stopped matching while the objects
    were still present, and the whole remote was fetched again.
    """

    @staticmethod
    def _helper(client):
        h = make_helper()
        h.client = client
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        return h

    @staticmethod
    def _client(pack_name="pack-1.pack", size=9):
        client = MagicMock()
        client.list_dir.return_value = [{"name": pack_name, "size": size}]
        client.get_file_bytes.return_value = b"PACKBYTES"
        return client

    def _fetch(self, local_git, client, install_side_effect=None, spec=None):
        h = self._helper(client)
        with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
            with patch("git_remote_seafile.helper.install_packfile") as install:
                if install_side_effect is not None:
                    install.side_effect = install_side_effect
                with patch("sys.stdout", new_callable=io.StringIO):
                    with patch("sys.stderr", new_callable=io.StringIO):
                        h.cmd_fetch(spec if spec is not None else ["refs/heads/main"])
        return install

    def _state(self, local_git):
        path = local_git / "seafile" / "repo1" / "packs.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_a_fetch_records_what_it_installed(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            self._fetch(local_git, self._client()).assert_called_once()
            self.assertEqual(self._state(local_git)["remotes"]["/git-repo"]["pack-1.pack"], 9)

    def test_a_repack_does_not_cause_a_second_download(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            pack_dir = local_git / "objects" / "pack"
            pack_dir.mkdir(parents=True)

            def install_writes(name, pack, idx=None, **kwargs):
                (pack_dir / name).write_bytes(b"INSTALLED")

            self._fetch(
                local_git, self._client(), install_side_effect=install_writes
            ).assert_called_once()
            self.assertTrue((pack_dir / "pack-1.pack").exists())

            # A local `git gc`: the object store is repacked under new names, so
            # the pack this clone downloaded is no longer a file of that name.
            # The objects are still there, which is why a second fetch must not
            # download them again.
            (pack_dir / "pack-1.pack").unlink()
            (pack_dir / "pack-repacked.pack").write_bytes(b"REPACKED")

            self._fetch(
                local_git, self._client(), install_side_effect=install_writes
            ).assert_not_called()

    def test_a_destination_with_no_state_still_downloads(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            self._fetch(local_git, self._client()).assert_called_once()

    def test_state_recorded_for_another_destination_is_not_used(self):
        """The same library holds a different set of packs per destination."""
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)
            state_dir = local_git / "seafile" / "repo1"
            state_dir.mkdir(parents=True)
            (state_dir / "packs.json").write_text(
                json.dumps({"version": 1, "remotes": {"/other": {"pack-1.pack": 9}}}),
                encoding="utf-8",
            )

            self._fetch(local_git, self._client()).assert_called_once()

    def test_a_truncated_state_file_costs_a_download_not_a_failure(self):
        """The one way this file could cause work to be skipped is reading a
        half-written one as valid, so a damaged file must read as 'nothing'."""
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)
            state_dir = local_git / "seafile" / "repo1"
            state_dir.mkdir(parents=True)
            (state_dir / "packs.json").write_text(
                '{"version": 1, "remotes": {"/git-repo": {"pack-1.pack": ',
                encoding="utf-8",
            )

            self._fetch(local_git, self._client()).assert_called_once()

    def test_an_unknown_state_version_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)
            state_dir = local_git / "seafile" / "repo1"
            state_dir.mkdir(parents=True)
            (state_dir / "packs.json").write_text(
                json.dumps({"version": 99, "remotes": {"/git-repo": {"pack-1.pack": 9}}}),
                encoding="utf-8",
            )

            self._fetch(local_git, self._client()).assert_called_once()

    def test_a_pruned_pack_is_not_skipped_on_the_strength_of_the_state_file(self):
        """The recorded list is a hint, and a hint must not outrank evidence.

        The helper installs *every* remote pack, whatever git asked for, so a
        single-branch clone holds objects no local ref reaches -- and a later
        ``git gc --prune`` drops them legitimately.  The state file still says
        "fetched", so trusting it skips the one download that is needed and git
        dies with "did not send all necessary objects" and no explanation.
        """
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)
            state_dir = local_git / "seafile" / "repo1"
            state_dir.mkdir(parents=True)
            (state_dir / "packs.json").write_text(
                json.dumps({"version": 1, "remotes": {"/git-repo": {"pack-1.pack": 9}}}),
                encoding="utf-8",
            )
            sha = "a" * 40
            with patch("git_remote_seafile.helper.objects_may_be_incomplete", return_value=True):
                with patch("git_remote_seafile.helper.filter_existing_objects", return_value=[]):
                    install = self._fetch(local_git, self._client(), spec=[sha])

            install.assert_called_once()

    def test_a_healthy_object_store_still_honours_the_recorded_list(self):
        """The retry is a fallback, not the new default: when the objects are
        present the recorded list keeps its A9 saving."""
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)
            state_dir = local_git / "seafile" / "repo1"
            state_dir.mkdir(parents=True)
            (state_dir / "packs.json").write_text(
                json.dumps({"version": 1, "remotes": {"/git-repo": {"pack-1.pack": 9}}}),
                encoding="utf-8",
            )
            sha = "b" * 40
            with patch("git_remote_seafile.helper.objects_may_be_incomplete", return_value=True):
                with patch(
                    "git_remote_seafile.helper.filter_existing_objects", return_value=[sha]
                ):
                    install = self._fetch(local_git, self._client(), spec=[sha])

            install.assert_not_called()


class TestSmartPackFetchFiltering(unittest.TestCase):
    """Smart Pack Fetch Filtering (M-9):

    If all requested commit objects in fetch_specs are already present in
    the local object store, cmd_fetch terminates cleanly without downloading
    redundant remote packfiles.
    """

    @staticmethod
    def _helper(client):
        h = make_helper()
        h.client = client
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        return h

    def test_fetch_skips_pack_download_when_all_objects_exist_locally(self):
        client = MagicMock()
        h = self._helper(client)
        sha1 = "1111111111111111111111111111111111111111"
        sha2 = "2222222222222222222222222222222222222222"
        out = io.StringIO()

        # Pin the clone-completeness probes: they read the *ambient* repository,
        # and CI checks out with --depth 1, so leaving them real would make this
        # test assert different things locally and on CI.  The object-closure
        # probe (tips_are_locally_complete) is pinned for the same reason --
        # its own behaviour, including the interrupted-fetch wedge it exists to
        # catch, is tested against real git in
        # tests.test_git_util.TestTipsAreLocallyComplete.
        with patch("git_remote_seafile.helper.objects_may_be_incomplete", return_value=False), \
                patch("git_remote_seafile.helper.tips_are_locally_complete", return_value=True), \
                patch("git_remote_seafile.helper.filter_existing_objects", return_value=[sha1, sha2]):
            with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                h.cmd_fetch([f"{sha1} refs/heads/main", f"{sha2} refs/heads/feature"])

        self.assertEqual(out.getvalue(), "\n")
        # client.list_dir should NOT even be called because packs are bypassed
        client.list_dir.assert_not_called()

    def test_fetch_downloads_packs_when_objects_missing_locally(self):
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack"}]
            client.get_file_bytes.return_value = b"PACKBYTES"
            h = self._helper(client)
            sha1 = "1111111111111111111111111111111111111111"
            sha2 = "2222222222222222222222222222222222222222"
            out = io.StringIO()

            # Only sha1 exists locally, sha2 is missing
            with patch("git_remote_seafile.helper.objects_may_be_incomplete", return_value=False), \
                    patch("git_remote_seafile.helper.filter_existing_objects", return_value=[sha1]):
                with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                    with patch("git_remote_seafile.helper.install_packfile") as install:
                        with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                            h.cmd_fetch([f"{sha1} refs/heads/main", f"{sha2} refs/heads/feature"])

            self.assertEqual(out.getvalue(), "\n")
            client.list_dir.assert_called_once()
            install.assert_called_once()

    def test_fetch_does_not_skip_when_the_clone_may_be_incomplete(self):
        """A shallow or partial clone must not take the tip-presence shortcut.

        The tip commit can be present while its trees and blobs are not, so
        skipping the download would leave `git checkout` reporting missing
        objects -- the exact case DESIGN 7.5 documents as the heuristic's
        limitation, now closed rather than merely described.
        """
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = [{"name": "pack-1.pack"}]
            client.get_file_bytes.return_value = b"PACKBYTES"
            h = self._helper(client)
            sha1 = "1111111111111111111111111111111111111111"
            sha2 = "2222222222222222222222222222222222222222"
            out = io.StringIO()

            # Both objects "exist", so the old heuristic would have skipped --
            # the guard is the only thing that can withhold the skip.
            with patch("git_remote_seafile.helper.objects_may_be_incomplete", return_value=True), \
                    patch("git_remote_seafile.helper.filter_existing_objects", return_value=[sha1, sha2]):
                with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                    with patch("git_remote_seafile.helper.install_packfile") as install:
                        with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                            h.cmd_fetch([f"{sha1} refs/heads/main", f"{sha2} refs/heads/feature"])

            client.list_dir.assert_called_once()
            install.assert_called_once()

    def test_fetch_does_not_skip_when_completeness_cannot_be_determined(self):
        """Fails safe: an unanswerable probe fetches rather than skips.

        The cost of a redundant download is bandwidth; the cost of a wrong skip
        is a checkout that cannot resolve its own objects.
        """
        with tempfile.TemporaryDirectory() as td:
            local_git = Path(td) / ".git"
            (local_git / "objects" / "pack").mkdir(parents=True)

            client = MagicMock()
            client.list_dir.return_value = []
            h = self._helper(client)
            sha1 = "1111111111111111111111111111111111111111"
            out = io.StringIO()

            with patch("git_remote_seafile.helper.objects_may_be_incomplete", return_value=True), \
                    patch("git_remote_seafile.helper.filter_existing_objects", return_value=[sha1]):
                with patch("git_remote_seafile.helper.get_git_dir", return_value=local_git):
                    with patch("sys.stdout", out), patch("sys.stderr", new_callable=io.StringIO):
                        h.cmd_fetch([f"{sha1} refs/heads/main"])

            client.list_dir.assert_called_once()


class TestPushStagingCleanup(unittest.TestCase):
    """Push Staging Cleanup (H-1):

    Ensure temporary push staging directory is cleaned up after upload.
    """

    def test_cmd_push_cleans_up_staging_directory(self):
        h = make_helper()
        h.client = MagicMock()
        h.client.get_file_text.return_value = None
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}

        created_staging_dir = []
        original_mkdtemp = tempfile.mkdtemp

        def track_mkdtemp(*args, **kwargs):
            path = original_mkdtemp(*args, **kwargs)
            created_staging_dir.append(Path(path))
            return path

        with patch("git_remote_seafile.helper.rev_parse", return_value="sha_main"):
            with patch("git_remote_seafile.helper.is_ancestor", return_value=True):
                with patch("git_remote_seafile.helper.get_objects_to_push", return_value=["obj1"]):
                    with patch("tempfile.mkdtemp", side_effect=track_mkdtemp):
                        with patch("git_remote_seafile.helper.create_packfile", return_value=("pack1", b"PACK", b"IDX")):
                            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                                with patch("git_remote_seafile.helper.RemoteLock"):
                                    h.cmd_push(["refs/heads/main:refs/heads/main"])

        self.assertEqual(len(created_staging_dir), 1)
        self.assertFalse(created_staging_dir[0].exists(), "Push staging directory must be cleaned up")


class _FakeRemote:
    """A Seafile-shaped remote for the push path: what you PUT is what you GET.

    The push reads a ref, writes it, then reads it *again* to detect a lost
    update, because Seafile exposes no atomic compare-and-swap.  A client whose
    ``get_file_text`` returns one fixed value cannot model that sequence -- the
    pre-write read and the post-write read have to be able to differ -- so the
    earlier push tests could not express it.  This keeps a path -> content map,
    and ``clobber`` lets a test inject a competing writer that lands
    immediately after ours.
    """

    def __init__(self, clobber: dict[str, str] | None = None):
        self.files: dict[str, str] = {}
        self.clobber = dict(clobber or {})

    def upload_file(self, repo_id, parent_dir, filename, content, **kwargs):
        path = f"{parent_dir}/{filename}"
        self.files[path] = content.decode("utf-8").strip()
        if path in self.clobber:
            self.files[path] = self.clobber[path]
        return True

    def get_file_text(self, repo_id, path):
        return self.files.get(path)

    def list_dir(self, repo_id, path):
        return []


class TestPushRefReadBack(unittest.TestCase):
    """The push re-reads every ref it wrote (N-1, extended).

    The CAS check and the write are separate round trips, so a peer that
    ignores the advisory lock can land between them.  Reporting ``ok`` without
    looking back would make that lost update silent: the push that lost would
    simply vanish from the remote while the client believed it had succeeded.
    """

    @staticmethod
    def _helper(remote):
        h = make_helper()
        h.client = remote
        h.repo_id = "repo1"
        h.repo_path = "/git-repo"
        h._refs_cache = {}
        return h

    def _push(self, remote, out, err):
        h = self._helper(remote)
        with patch("git_remote_seafile.helper.rev_parse", return_value="localsha1"), \
                patch("git_remote_seafile.helper.is_ancestor", return_value=True), \
                patch("git_remote_seafile.helper.get_objects_to_push", return_value=[]), \
                patch("git_remote_seafile.helper.RemoteLock"):
            with patch("sys.stdout", out), patch("sys.stderr", err):
                h.cmd_push(["refs/heads/main:refs/heads/main"])

    def test_a_verified_write_reports_ok(self):
        remote = _FakeRemote()
        out, err = io.StringIO(), io.StringIO()
        self._push(remote, out, err)

        self.assertIn("ok refs/heads/main", out.getvalue())
        self.assertNotIn("error", out.getvalue())
        self.assertNotIn("unverified", err.getvalue())
        # And the ref really did land in the fake remote.
        self.assertEqual(remote.files["/git-repo/refs/heads/main"], "localsha1")

    def test_a_ref_clobbered_after_the_write_is_reported_as_an_error(self):
        """The point of the read-back: a lost update must not read as ok."""
        remote = _FakeRemote(clobber={"/git-repo/refs/heads/main": "rivalsha9"})
        out, err = io.StringIO(), io.StringIO()
        self._push(remote, out, err)

        self.assertIn("error refs/heads/main fetch first", out.getvalue())
        self.assertNotIn("ok refs/heads/main", out.getvalue())
        self.assertIn("another client updated it concurrently", err.getvalue())

    def test_a_conflict_copy_beside_the_ref_is_reported_not_swallowed(self):
        """Simultaneous writers each report success and leave a copy *beside* the ref.

        Measured on a real Seafile CE 13.0.28: twelve concurrent replace=1
        uploads of one path all returned success and left the path plus six
        ``<name> (SFConflict ...)`` files.  The read-back reads our path, which
        holds one of the two values, so only a look at the directory sees it.
        """
        remote = _FakeRemote()
        copy = "main (SFConflict someone 2026-Oct-13-05-44-44)"
        remote.list_dir = lambda repo_id, path: [
            {"name": "main", "type": "file"},
            {"name": copy, "type": "file"},
            {"name": "other", "type": "file"},
        ]
        out, err = io.StringIO(), io.StringIO()
        self._push(remote, out, err)

        self.assertIn("ok refs/heads/main", out.getvalue())
        self.assertIn("another client wrote refs/heads/main at the same time", err.getvalue())
        self.assertIn(copy, err.getvalue())

    def test_a_clean_directory_produces_no_conflict_warning(self):
        remote = _FakeRemote()
        remote.list_dir = lambda repo_id, path: [{"name": "main", "type": "file"}]
        out, err = io.StringIO(), io.StringIO()
        self._push(remote, out, err)
        self.assertNotIn("SFConflict", err.getvalue())

    def test_an_unreadable_ref_is_a_warning_not_an_error(self):
        """The upload already succeeded, so a read we cannot finish proves nothing.

        Calling a good push failed is its own bug -- the same reasoning that
        separates LEASE_LOST from LEASE_UNKNOWN in the lock.
        """
        remote = _FakeRemote()

        def explode(repo_id, path):
            # Only the post-write read-back fails; the earlier reads must work
            # or the push never reaches the write at all.
            if path.endswith("refs/heads/main") and path in remote.files:
                raise SeafileAPIError("network blip")
            return remote.files.get(path)

        remote.get_file_text = explode
        out, err = io.StringIO(), io.StringIO()
        self._push(remote, out, err)

        self.assertIn("ok refs/heads/main", out.getvalue())
        self.assertNotIn("error", out.getvalue())
        self.assertIn("unverified", err.getvalue())

    def test_a_ref_not_visible_after_the_write_is_a_warning_not_an_error(self):
        remote = _FakeRemote()

        def vanish(repo_id, path):
            if path.endswith("refs/heads/main") and path in remote.files:
                return None
            return remote.files.get(path)

        remote.get_file_text = vanish
        out, err = io.StringIO(), io.StringIO()
        self._push(remote, out, err)

        self.assertIn("ok refs/heads/main", out.getvalue())
        self.assertNotIn("error", out.getvalue())
        self.assertIn("was not visible immediately after", err.getvalue())


class ProtocolErrorDisciplineTests(unittest.TestCase):
    """Every ``error`` line the helper writes goes through ``_proto_error``.

    Git reads the helper's stdout one line at a time, so an exception message
    carrying a newline becomes a second, invented protocol line.  Six call sites
    used to open-code the flattening and the delete path wrote the raw exception,
    which made the property a convention rather than a guarantee.

    This is a source-level check on purpose: a runtime test can only cover the
    sites someone remembered to write a case for, and a *missed* site is exactly
    the failure being guarded against.
    """

    HELPER = (
        Path(__file__).resolve().parent.parent
        / "git_remote_seafile"
        / "helper.py"
    )

    @staticmethod
    def _is_stdout_write(node: ast.AST) -> bool:
        func = getattr(node, "func", None)
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "write"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "stdout"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        )

    @staticmethod
    def _literal_prefix(node: ast.AST) -> str | None:
        """The leading constant of the first argument, for ``f"error {dst} ..."``."""
        args = getattr(node, "args", None)
        if not args:
            return None
        first = args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        if (
            isinstance(first, ast.JoinedStr)
            and first.values
            and isinstance(first.values[0], ast.Constant)
            and isinstance(first.values[0].value, str)
        ):
            return first.values[0].value
        return None

    def _error_writers(self) -> dict[str, list[int]]:
        """``{function name: [lines]}`` for every direct ``error ...`` write."""
        tree = ast.parse(self.HELPER.read_text(encoding="utf-8", errors="replace"))
        writers: dict[str, list[int]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call) or not self._is_stdout_write(sub):
                    continue
                prefix = self._literal_prefix(sub)
                if prefix and prefix.startswith("error "):
                    writers.setdefault(node.name, []).append(sub.lineno)
        return writers

    def test_the_only_function_writing_error_lines_is_proto_error(self):
        writers = self._error_writers()
        self.assertIn(
            "_proto_error", writers,
            "expected _proto_error to be the writer of 'error' lines; found "
            f"{sorted(writers)}",
        )
        bypassing = {name: lines for name, lines in writers.items() if name != "_proto_error"}
        self.assertEqual(
            bypassing, {},
            "these functions write an 'error' line directly instead of going "
            f"through _proto_error: {bypassing}",
        )

    def test_the_helper_actually_uses_proto_error(self):
        # A guard on the guard: if _proto_error were dead code the test above
        # would still pass, so pin that the call sites exist.
        source = self.HELPER.read_text(encoding="utf-8", errors="replace")
        self.assertGreater(source.count("_proto_error("), 10)


if __name__ == "__main__":
    unittest.main()
