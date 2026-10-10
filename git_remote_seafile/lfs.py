"""lfs.py - Git LFS Custom Transfer Agent protocol implementation for Seafile."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import SeafileClient


class LFSTransferAgent:
    """Implements Git LFS Custom Transfer protocol over Seafile Web API."""

    def __init__(
        self,
        client: SeafileClient,
        repo_id: str,
        repo_path: str,
        git_dir: Path | str | None = None,
    ):
        self.client = client
        self.repo_id = repo_id
        self.repo_path = repo_path.rstrip("/")
        self.lfs_dir = f"{self.repo_path}/lfs"
        tmp_parent: Path | None = None
        if git_dir is not None:
            candidate = Path(git_dir) / "lfs" / "tmp"
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                tmp_parent = candidate
            except Exception:
                tmp_parent = None
        self._temp_dir = tempfile.TemporaryDirectory(
            prefix="git-seaf-lfs-",
            dir=str(tmp_parent) if tmp_parent else None,
        )

    def _object_subpath(self, oid: str) -> tuple[str, str]:
        """Convert SHA256 OID into (parent_dir, filename)."""
        parent = f"{self.lfs_dir}/{oid[:2]}/{oid[2:4]}"
        return parent, oid

    def _is_valid_oid(self, oid: Any) -> bool:
        """Validate OID is a safe hex/alphanumeric string with no path traversal."""
        if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-zA-Z_-]{4,64}", oid):
            return False
        return ".." not in oid and "/" not in oid and "\\" not in oid

    def _send_progress(self, oid: str, bytes_so_far: int, bytes_since_last: int) -> None:
        """Emit Git LFS progress event."""
        self._send_json({
            "event": "progress",
            "oid": oid,
            "bytesSoFar": bytes_so_far,
            "bytesSinceLast": bytes_since_last,
        })

    def handle_init(self, msg: dict[str, Any]) -> None:
        """Handle LFS init handshake."""
        # Acknowledge initialization with an empty JSON object
        self._send_json({})

    def handle_upload(self, msg: dict[str, Any]) -> None:
        """Upload a local file to Seafile LFS object store."""
        oid = msg.get("oid")
        if not self._is_valid_oid(oid):
            self._send_json({
                "event": "complete",
                "oid": oid or "",
                "error": {"code": 400, "message": f"Invalid OID: {oid}"},
            })
            return

        local_path = msg.get("path")
        if not local_path or not os.path.isfile(local_path):
            self._send_json({
                "event": "complete",
                "oid": oid,
                "error": {"code": 400, "message": f"Local file not found: {local_path}"},
            })
            return

        parent_dir, filename = self._object_subpath(oid)
        local_size = os.path.getsize(local_path)

        # Check if remote object already exists with the identical byte size to skip duplicate upload.
        # Name + size is the whole check, deliberately: the remote path is
        # derived from the content hash (OID), so a file already stored under
        # that name at that size is the same object for practical purposes.
        # Verifying content would cost a full download -- exactly what this
        # skip exists to avoid.  A corrupted same-size leftover is accepted
        # collateral; delete it server-side to force a re-upload.
        already_exists = False
        try:
            entries = self.client.list_dir(self.repo_id, parent_dir)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("name") == filename and entry.get("size") == local_size:
                        already_exists = True
                        break
        except Exception:
            already_exists = False

        if already_exists:
            self._send_progress(oid, local_size, local_size)
            self._send_json({"event": "complete", "oid": oid})
            return

        try:
            # Hand the client the *path*, not the bytes.  An LFS object is
            # routinely far larger than RAM, and reading it in first is exactly
            # what LFS exists to avoid; the client streams it from disk.
            last_bytes = 0

            def on_upload_progress(transferred: int, total: int) -> None:
                nonlocal last_bytes
                delta = transferred - last_bytes
                if delta > 0:
                    self._send_progress(oid, transferred, delta)
                    last_bytes = transferred

            self.client.upload_file(
                self.repo_id, parent_dir, filename, Path(local_path), replace=True, progress_callback=on_upload_progress
            )
            final_delta = local_size - last_bytes
            if final_delta > 0:
                self._send_progress(oid, local_size, final_delta)
            elif last_bytes == 0 and local_size == 0:
                self._send_progress(oid, 0, 0)
            self._send_json({"event": "complete", "oid": oid})
        except Exception as ex:
            self._send_json({
                "event": "complete",
                "oid": oid,
                "error": {"code": 500, "message": f"Upload failed: {ex}"},
            })

    def handle_download(self, msg: dict[str, Any]) -> None:
        """Download an LFS object from Seafile into a local temp file."""
        oid = msg.get("oid")
        if not self._is_valid_oid(oid):
            self._send_json({
                "event": "complete",
                "oid": oid or "",
                "error": {"code": 400, "message": f"Invalid OID: {oid}"},
            })
            return

        parent_dir, filename = self._object_subpath(oid)
        file_path = f"{parent_dir}/{filename}"

        try:
            temp_dest = Path(self._temp_dir.name) / oid
            # Streamed straight to disk: `get_file_bytes` would hold the whole
            # object in memory before writing it.
            last_bytes = 0

            def on_download_progress(transferred: int, total: int) -> None:
                nonlocal last_bytes
                delta = transferred - last_bytes
                if delta > 0:
                    self._send_progress(oid, transferred, delta)
                    last_bytes = transferred

            download_ok = self.client.download_file_to(
                self.repo_id, file_path, temp_dest, progress_callback=on_download_progress
            )

            if not download_ok:
                self._send_json({
                    "event": "complete",
                    "oid": oid,
                    "error": {"code": 404, "message": f"Object {oid} not found on Seafile"},
                })
                return

            size = temp_dest.stat().st_size if temp_dest.is_file() else 0
            # git-lfs sends the expected size with the download request; a
            # mismatch means the stored object is truncated or corrupt.  LFS
            # would catch it on the final hash, but an early, explicit error
            # beats a confusing hash failure later.
            expected = msg.get("size")
            if isinstance(expected, int) and not isinstance(expected, bool) and expected >= 0 and size != expected:
                self._send_json({
                    "event": "complete",
                    "oid": oid,
                    "error": {
                        "code": 502,
                        "message": (
                            f"Object {oid} downloaded {size} bytes but the "
                            f"request expected {expected}"
                        ),
                    },
                })
                return
            final_delta = size - last_bytes
            if final_delta > 0:
                self._send_progress(oid, size, final_delta)
            elif last_bytes == 0 and size == 0:
                self._send_progress(oid, 0, 0)
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
                # A silent skip here would look exactly like a hung transfer if
                # the protocol ever desyncs; name the line so it is diagnosable.
                sys.stderr.write(f"git-remote-seafile lfs: ignoring malformed JSON line: {line[:200]}\n")
                sys.stderr.flush()
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
