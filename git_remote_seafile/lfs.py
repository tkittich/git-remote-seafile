"""lfs.py - Git LFS Custom Transfer Agent protocol implementation for Seafile."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import SeafileClient


class LFSTransferAgent:
    """Implements Git LFS Custom Transfer protocol over Seafile Web API."""

    def __init__(self, client: SeafileClient, repo_id: str, repo_path: str):
        self.client = client
        self.repo_id = repo_id
        self.repo_path = repo_path.rstrip("/")
        self.lfs_dir = f"{self.repo_path}/lfs"
        self._temp_dir = tempfile.TemporaryDirectory(prefix="git-seaf-lfs-")

    def _object_subpath(self, oid: str) -> tuple[str, str]:
        """Convert SHA256 OID into (parent_dir, filename)."""
        parent = f"{self.lfs_dir}/{oid[:2]}/{oid[2:4]}"
        return parent, oid

    def handle_init(self, msg: dict[str, Any]) -> None:
        """Handle LFS init handshake."""
        # Acknowledge initialization with an empty JSON object
        self._send_json({})

    def handle_upload(self, msg: dict[str, Any]) -> None:
        """Upload a local file to Seafile LFS object store."""
        oid = msg["oid"]
        local_path = msg.get("path")
        if not local_path or not os.path.isfile(local_path):
            self._send_json({
                "event": "complete",
                "oid": oid,
                "error": {"code": 400, "message": f"Local file not found: {local_path}"},
            })
            return

        parent_dir, filename = self._object_subpath(oid)
        try:
            # Hand the client the *path*, not the bytes.  An LFS object is
            # routinely far larger than RAM, and reading it in first is exactly
            # what LFS exists to avoid; the client streams it from disk.
            self.client.upload_file(
                self.repo_id, parent_dir, filename, Path(local_path), replace=True
            )
            self._send_json({"event": "complete", "oid": oid})
        except Exception as ex:
            self._send_json({
                "event": "complete",
                "oid": oid,
                "error": {"code": 500, "message": f"Upload failed: {ex}"},
            })

    def handle_download(self, msg: dict[str, Any]) -> None:
        """Download an LFS object from Seafile into a local temp file."""
        oid = msg["oid"]
        parent_dir, filename = self._object_subpath(oid)
        file_path = f"{parent_dir}/{filename}"

        try:
            temp_dest = Path(self._temp_dir.name) / oid
            # Streamed straight to disk: `get_file_bytes` would hold the whole
            # object in memory before writing it.
            if not self.client.download_file_to(self.repo_id, file_path, temp_dest):
                self._send_json({
                    "event": "complete",
                    "oid": oid,
                    "error": {"code": 404, "message": f"Object {oid} not found on Seafile"},
                })
                return

            self._send_json({
                "event": "complete",
                "oid": oid,
                "path": str(temp_dest),
            })
        except Exception as ex:
            self._send_json({
                "event": "complete",
                "oid": oid,
                "error": {"code": 500, "message": f"Download failed: {ex}"},
            })

    def _send_json(self, data: dict[str, Any]) -> None:
        line = json.dumps(data) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()

    def run(self) -> None:
        """Main protocol loop reading JSON messages from git-lfs via stdin."""
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except Exception:
                continue

            event = msg.get("event")
            if event == "init":
                self.handle_init(msg)
            elif event == "upload":
                self.handle_upload(msg)
            elif event == "download":
                self.handle_download(msg)
            elif event == "terminate":
                break
            else:
                self._send_json({"error": {"code": 400, "message": f"Unknown event: {event}"}})
