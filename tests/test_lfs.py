"""test_lfs.py - Git LFS custom transfer agent: protocol, streaming, validation."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from git_remote_seafile.lfs import LFSTransferAgent

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

        def fake_download(repo_id, file_path, dest, progress_callback=None):
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
            # Line 2: unknown event -> 400 error
            self.assertEqual(output_lines[1]["error"]["code"], 400)
            self.assertIn("Unknown event: unknown_event_type", output_lines[1]["error"]["message"])
        finally:
            agent._temp_dir.cleanup()

