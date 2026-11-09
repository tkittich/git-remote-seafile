"""test_lfs.py - Git LFS custom transfer agent: protocol, streaming, validation."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys

from git_remote_seafile.client import SeafileClient
from git_remote_seafile.lfs import PART_SUFFIX, LFSTransferAgent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_harness import REPO_ID, SeafileStub  # noqa: E402


class TestLFSUploadAgainstTheStub(unittest.TestCase):
    """ROADMAP Z30: an object is uploaded under a name of its own and renamed onto its oid.

    Two machines uploading one object at once each wrote the final name and the server kept
    the loser as a conflict copy.  The stub's rename answers success and leaves a "(1)" copy
    on a taken name, as the server does, so these runs see what a race leaves.
    """

    OID = "ab" + "cd" + "e" * 60
    DATA = b"LFS-OBJECT-BYTES" * 100

    @classmethod
    def setUpClass(cls):
        cls.stub = SeafileStub().start()
        cls.client = SeafileClient(server_url=cls.stub.base_url, token="t")

    @classmethod
    def tearDownClass(cls):
        cls.stub.stop()

    def setUp(self):
        self.stub.reset()
        self.agent = LFSTransferAgent(self.client, REPO_ID, "/git-test")
        self.addCleanup(self.agent._temp_dir.cleanup)
        handle, self.local = tempfile.mkstemp()
        Path(self.local).write_bytes(self.DATA)
        import os
        os.close(handle)
        self.addCleanup(Path(self.local).unlink, missing_ok=True)
        self.folder, self.name = self.agent._object_subpath(self.OID)
        self.path = f"{self.folder}/{self.name}"
        self.uploaded: list[str] = []
        real_upload = self.client.upload_file

        def recording_upload(repo_id, parent_dir, filename, *args, **kwargs):
            self.uploaded.append(filename)
            return real_upload(repo_id, parent_dir, filename, *args, **kwargs)

        patcher = patch.object(self.client, "upload_file", recording_upload)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stored(self) -> dict[str, bytes]:
        return {k: v for k, v in self.stub.files.get(REPO_ID, {}).items() if k.startswith(self.folder)}

    def upload(self) -> dict:
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.agent.handle_upload({"event": "upload", "oid": self.OID, "path": self.local})
        return [json.loads(x) for x in out.getvalue().splitlines() if x][-1]

    def test_an_object_goes_up_under_a_part_name_and_is_left_alone(self):
        resp = self.upload()
        self.assertNotIn("error", resp)
        self.assertEqual(len(self.uploaded), 1)
        self.assertTrue(self.uploaded[0].startswith(self.name + PART_SUFFIX), self.uploaded)
        self.assertEqual(self.stored(), {self.path: self.DATA})

    def test_a_peer_that_stored_the_same_bytes_first_leaves_no_conflict_copy(self):
        real = self.client.upload_file

        def peer_wins(repo_id, parent_dir, filename, *args, **kwargs):
            real(repo_id, parent_dir, filename, *args, **kwargs)
            self.stub.files[REPO_ID][self.path] = self.DATA  # another machine's rename landed first

        with patch.object(self.client, "upload_file", peer_wins):
            resp = self.upload()
        self.assertNotIn("error", resp)
        self.assertEqual(self.stored(), {self.path: self.DATA})

    def test_a_damaged_object_under_the_oid_is_replaced(self):
        self.stub.files.setdefault(REPO_ID, {})[self.path] = b"cut short"
        resp = self.upload()
        self.assertNotIn("error", resp)
        self.assertEqual(self.stored(), {self.path: self.DATA})

    def test_a_rename_the_server_does_not_perform_is_an_error_and_leaves_no_part_file(self):
        with patch.object(self.client, "rename_entry", return_value=False):
            resp = self.upload()
        self.assertIn("the server accepted the upload but lists", resp["error"]["message"])
        self.assertEqual(self.stored(), {})

class TestLFSTransferAgent(unittest.TestCase):
    def test_lfs_init(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_init({"event": "init", "operation": "upload"})
            self.assertEqual(stdout_buf.getvalue().strip(), "{}")
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_upload_success(self):
        mock_client = MagicMock()
        # Before the upload the object is absent; afterwards the listing shows it whole.
        mock_client.list_dir.side_effect = [
            [], [{"type": "file", "name": "1234567890abcdef", "size": len(b"SAMPLE-LFS-CONTENT")}],
        ]
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"SAMPLE-LFS-CONTENT")
                temp_path = tf.name

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "1234567890abcdef", "path": temp_path})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], "1234567890abcdef")
            progress_events = [m for m in msgs if m.get("event") == "progress"]
            self.assertEqual(len(progress_events), 1)
            self.assertEqual(progress_events[0]["oid"], "1234567890abcdef")
            self.assertEqual(progress_events[0]["bytesSoFar"], len(b"SAMPLE-LFS-CONTENT"))
            mock_client.upload_file.assert_called_once()
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_upload_progress_reports_incremental_deltas(self):
        mock_client = MagicMock()
        def fake_upload(repo_id, parent_dir, filename, path, replace=True, progress_callback=None):
            if progress_callback:
                progress_callback(100, 300)
                progress_callback(250, 300)
                progress_callback(300, 300)
            return True

        mock_client.upload_file.side_effect = fake_upload
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"X" * 300)
                temp_path = tf.name

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "oid123", "path": temp_path})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            progress = [m for m in msgs if m.get("event") == "progress"]
            # Verify incremental deltas: 100-0=100, 250-100=150, 300-250=50
            self.assertEqual([(p["bytesSoFar"], p["bytesSinceLast"]) for p in progress], [
                (100, 100),
                (250, 150),
                (300, 50),
            ])
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_upload_skips_when_object_already_exists_with_same_size(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"SAMPLE-LFS-CONTENT")
                temp_path = tf.name

            local_size = len(b"SAMPLE-LFS-CONTENT")
            oid = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
            _, filename = agent._object_subpath(oid)
            mock_client.list_dir.return_value = [
                {"type": "file", "name": filename, "size": local_size}
            ]

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": oid, "path": temp_path})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], oid)
            mock_client.upload_file.assert_not_called()
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_upload_streams_from_disk(self):
        """An LFS object can be far larger than RAM, so it must not be read in."""
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"SAMPLE-LFS-CONTENT")
                temp_path = tf.name

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "abcdef01", "path": temp_path})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")

            args, _ = mock_client.upload_file.call_args
            self.assertNotIsInstance(
                args[3], (bytes, bytearray),
                "the LFS payload must be a path or file object, not the file's bytes",
            )
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_upload_file_not_found(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "missingoid", "path": "/nonexistent/path"})

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertIn("error", resp)
            self.assertEqual(resp["error"]["code"], 400)
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_download_success(self):
        mock_client = MagicMock()

        def fake_download(repo_id, file_path, dest, progress_callback=None, max_bytes=None):
            Path(dest).write_bytes(b"BINARY-OBJECT-BYTES")
            return True

        mock_client.download_file_to = MagicMock(side_effect=fake_download)
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_download({"event": "download", "oid": "abcdef0123456789"})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], "abcdef0123456789")
            self.assertTrue(Path(resp["path"]).is_file())
            self.assertEqual(Path(resp["path"]).read_bytes(), b"BINARY-OBJECT-BYTES")
            progress_events = [m for m in msgs if m.get("event") == "progress"]
            self.assertEqual(len(progress_events), 1)
            mock_client.get_file_bytes.assert_not_called()
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_upload_skips_duplicate_existing_file(self):
        mock_client = MagicMock()
        mock_client.list_dir.return_value = [
            {"name": "1234567890abcdef", "size": len(b"SAMPLE-LFS-CONTENT"), "type": "file"}
        ]
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"SAMPLE-LFS-CONTENT")
                temp_path = tf.name

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "1234567890abcdef", "path": temp_path})

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["oid"], "1234567890abcdef")
            # Upload should be skipped
            mock_client.upload_file.assert_not_called()
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_rejects_invalid_oid_path_traversal(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            # Traversal in upload
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "../../etc/passwd", "path": "/some/path"})
            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["error"]["code"], 400)
            self.assertIn("Invalid OID", resp["error"]["message"])

            # Traversal in download
            stdout_buf_dl = io.StringIO()
            with patch("sys.stdout", stdout_buf_dl):
                agent.handle_download({"event": "download", "oid": "../../etc/shadow"})
            resp_dl = json.loads(stdout_buf_dl.getvalue().strip())
            self.assertEqual(resp_dl["event"], "complete")
            self.assertEqual(resp_dl["error"]["code"], 400)
            self.assertIn("Invalid OID", resp_dl["error"]["message"])
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_download_not_found_404(self):
        mock_client = MagicMock()
        mock_client.download_file_to.return_value = False
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_download({"event": "download", "oid": "missingoid"})

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["error"]["code"], 404)
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_download_flags_a_size_mismatch(self):
        # git-lfs sends the expected size with the request; a truncated or
        # corrupted remote object used to be handed over silently and only
        # fail later on the final hash.
        mock_client = MagicMock()

        def fake_download(repo_id, file_path, dest, progress_callback=None, max_bytes=None):
            Path(dest).write_bytes(b"SHORT")
            return True

        mock_client.download_file_to = MagicMock(side_effect=fake_download)
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_download(
                    {"event": "download", "oid": "abcdef0123456789", "size": 1024}
                )

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["error"]["code"], 502)
            self.assertIn("expected 1024", resp["error"]["message"])
            self.assertIn("downloaded 5 bytes", resp["error"]["message"])
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_download_is_cut_off_at_the_size_git_lfs_announced(self):
        """A server that streams without end must not fill the disk before the
        size is compared: the announced size is the cap, and no size means no cap."""
        for msg_size, want in ((5, 5), (0, 0), (None, None), (True, None), (-1, None), ("5", None)):
            mock_client = MagicMock()
            mock_client.download_file_to = MagicMock(return_value=False)
            agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
            try:
                msg = {"event": "download", "oid": "abcdef0123456789"}
                if msg_size is not None:
                    msg["size"] = msg_size
                with patch("sys.stdout", io.StringIO()):
                    agent.handle_download(msg)
                self.assertEqual(
                    mock_client.download_file_to.call_args.kwargs.get("max_bytes"), want, msg_size
                )
            finally:
                agent._temp_dir.cleanup()

    def test_lfs_download_accepts_a_matching_size(self):
        mock_client = MagicMock()

        def fake_download(repo_id, file_path, dest, progress_callback=None, max_bytes=None):
            Path(dest).write_bytes(b"EXACT")
            return True

        mock_client.download_file_to = MagicMock(side_effect=fake_download)
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_download(
                    {"event": "download", "oid": "abcdef0123456789", "size": 5}
                )

            msgs = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines() if line]
            resp = msgs[-1]
            self.assertEqual(resp["event"], "complete")
            self.assertNotIn("error", resp)
        finally:
            agent._temp_dir.cleanup()

    def test_lfs_upload_internal_server_error(self):
        mock_client = MagicMock()
        mock_client.upload_file.side_effect = RuntimeError("Network timeout")
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        temp_path = tempfile.mktemp()
        Path(temp_path).write_bytes(b"DATA")
        try:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                agent.handle_upload({"event": "upload", "oid": "oid123", "path": temp_path})

            resp = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(resp["event"], "complete")
            self.assertEqual(resp["error"]["code"], 500)
            self.assertIn("Network timeout", resp["error"]["message"])
        finally:
            agent._temp_dir.cleanup()
            Path(temp_path).unlink(missing_ok=True)

    def test_lfs_agent_protocol_loop_resilience(self):
        mock_client = MagicMock()
        agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
        try:
            input_stream = io.StringIO(
                "INVALID_CORRUPT_JSON_LINE\n"
                '{"event": "init"}\n'
                '{"event": "unknown_event_type"}\n'
                '{"event": "terminate"}\n'
            )
            stdout_buf = io.StringIO()
            with patch("sys.stdin", input_stream), patch("sys.stdout", stdout_buf):
                agent.run()

            output_lines = [json.loads(line) for line in stdout_buf.getvalue().strip().splitlines()]
            # Line 1: init response -> {}
            self.assertEqual(output_lines[0], {})
            # Line 2: unknown event -> 400 error.  The reply must carry the
            # "event" key -- git-lfs dispatches on it, and a bare {"error": ...}
            # matches no state of its wait loop.
            self.assertEqual(output_lines[1]["event"], "complete")
            self.assertEqual(output_lines[1]["error"]["code"], 400)
            self.assertIn("Unknown event: unknown_event_type", output_lines[1]["error"]["message"])
        finally:
            agent._temp_dir.cleanup()

    def test_an_upload_the_server_acknowledges_but_does_not_hold_is_an_error(self):
        """Review 4f1cdd4 (glm-5.3 G6.1): a 2xx was the whole proof, so a proxy that
        answered success for a failed upload made git-lfs record the object as pushed.
        The listing must show the object at its size, or the upload is reported failed."""
        for listed in ([], [{"type": "file", "name": "oid123", "size": 3}]):
            with self.subTest(listed=listed):
                mock_client = MagicMock()
                mock_client.list_dir.side_effect = [[], listed]
                agent = LFSTransferAgent(mock_client, "repo1", "/git-test")
                temp_path = tempfile.mktemp()
                Path(temp_path).write_bytes(b"DATA")
                try:
                    stdout_buf = io.StringIO()
                    with patch("sys.stdout", stdout_buf):
                        agent.handle_upload({"event": "upload", "oid": "oid123", "path": temp_path})
                    resp = [json.loads(x) for x in stdout_buf.getvalue().splitlines() if x][-1]
                    self.assertEqual(resp["event"], "complete")
                    self.assertIn("error", resp)
                    self.assertIn("the server accepted the upload but lists", resp["error"]["message"])
                finally:
                    agent._temp_dir.cleanup()
                    Path(temp_path).unlink(missing_ok=True)

    def test_a_json_line_that_is_not_an_object_is_skipped_not_fatal(self):
        """Review 4f1cdd4 (deepseek-v4.1-flash G6.1): ``[1]`` is valid JSON with no event;
        ``msg.get`` raised and the agent died with a traceback."""
        agent = LFSTransferAgent(MagicMock(), "repo1", "/git-test")
        try:
            stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
            lines = ['[1, 2]', '"x"', '3', '{"event": "init"}', '{"event": "terminate"}']
            stdin = io.StringIO("".join(line + "\n" for line in lines))
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                agent.run()
            self.assertEqual([json.loads(x) for x in stdout_buf.getvalue().splitlines() if x], [{}])
            self.assertIn("a JSON line that is not an object", stderr_buf.getvalue())
        finally:
            agent._temp_dir.cleanup()
