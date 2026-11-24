"""lfs.py - Git LFS Custom Transfer Agent protocol implementation for Seafile."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeGuard

from .client import HashingReader, SeafileClient

#: An upload lands under ``<oid>.part-<token>`` and is renamed onto ``<oid>``.
PART_SUFFIX = ".part-"


def _sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class _ProgressThrottle:
    """Forward transport progress to git-lfs without one line per chunk.

    A transfer reports on every 64 KiB chunk -- tens of thousands of JSON lines
    per GiB, each parsed by git-lfs.  An update is passed on when at least one
    percent of the object has moved or 250 ms have passed since the last one;
    ``last_bytes`` is what was actually reported, so the caller's final "the
    rest" event still adds up to the whole object.
    """

    _MIN_INTERVAL = 0.25

    def __init__(self, emit: Callable[[int, int], None]) -> None:
        self._emit = emit
        self.last_bytes = 0
        self._last_time: float | None = None

    def callback(self, transferred: int, total: int) -> None:
        delta = transferred - self.last_bytes
        if delta <= 0:
            return
        now = time.monotonic()
        due = (
            self._last_time is None
            or delta >= max(total // 100, 1)
            or now - self._last_time >= self._MIN_INTERVAL
        )
        if not due:
            return
        self._emit(transferred, delta)
        self.last_bytes = transferred
        self._last_time = now


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
        if isinstance(client, SeafileClient):
            client.layout_root = self.repo_path
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

    def _is_valid_oid(self, oid: Any) -> TypeGuard[str]:
        """Validate OID is a safe hex/alphanumeric string with no path traversal.

        Declared as a ``TypeGuard`` so the ``if not self._is_valid_oid(oid):
        return`` guard at every call site also narrows ``oid`` from the
        ``Any | None`` that a JSON lookup yields to a plain ``str``.  The check
        and the type then say the same thing, and the calls below it -- which
        all want a ``str`` -- need no separate assertion.
        """
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

    def _drop_part(self, parent_dir: str, temp: str) -> None:
        """Remove a half-published upload's temporary file; a leftover is harmless, so best effort."""
        try:
            entries = self.client.list_dir(self.repo_id, parent_dir)
            if any(isinstance(e, dict) and e.get("name") == temp for e in entries):
                self.client.delete_entry(self.repo_id, f"{parent_dir}/{temp}")
        except Exception:
            pass

    def _settle_object_name(self, parent_dir: str, filename: str, size: int, ours: "str | None" = None) -> dict | None:
        """List the folder after the rename and leave exactly one object, at *size*, if it can.

        Seafile's rename onto a taken name answers success and leaves a "<name> (1)" copy
        (measured on CE 13.0.28, DESIGN §6): a second uploader of the same bytes finds the
        object already there, and its copy is redundant; a same-named object of the wrong
        size is damage, replaced by the copy.  Returns the entry now holding the name.
        """
        marker = f"{filename} ("
        entries = self.client.list_dir(self.repo_id, parent_dir)
        listed = [e for e in entries if isinstance(e, dict)]
        held = next((e for e in listed if e.get("name") == filename), None)
        copies = [str(e.get("name")) for e in listed if str(e.get("name", "")).startswith(marker)]
        if ours is not None:
            # The rename said where our bytes landed: another writer's "(N)" copy is not ours to judge or
            # delete -- it may be the only whole object (RESUME.md D1 revision 3).
            copies = [name for name in copies if name == ours]
        if not copies:
            return held
        if held is not None and held.get("size") == size:
            for name in copies:
                self.client.delete_entry(self.repo_id, f"{parent_dir}/{name}")
            return held
        right = next((str(e["name"]) for e in listed
                      if str(e.get("name", "")).startswith(marker) and e.get("size") == size), None)
        if right is None:
            return held
        if held is not None and held.get("type") == "dir":
            return None                          # a folder is never an object and never ours to delete (N4)
        if held is not None:
            # Read it again right before it goes: another uploader that settled the same damage
            # a moment ago has put the right object here, and the listing above is stale (the
            # race is narrowed, not closed: the server has no compare-and-delete).
            again = next((e for e in self.client.list_dir(self.repo_id, parent_dir)
                          if isinstance(e, dict) and e.get("name") == filename), None)
            if again is not None and again.get("size") == size:
                for name in copies:
                    self.client.delete_entry(self.repo_id, f"{parent_dir}/{name}")
                return again
            self.client.delete_entry(self.repo_id, f"{parent_dir}/{filename}")
        if not self.client.rename_entry(self.repo_id, f"{parent_dir}/{right}", filename):
            # The damaged object is already gone.  Nothing may be deleted now: the right copy
            # under its numbered name is the only whole one, and the caller reports the object
            # as not stored, so git-lfs sends it again (review dfd2269, fable-G0.1 and
            # gemini-4-hydrogen-2-C1; the payload copy, `_commit_object`, already returns False).
            return None
        for name in copies:
            if name != right:
                self.client.delete_entry(self.repo_id, f"{parent_dir}/{name}")
        return next((e for e in self.client.list_dir(self.repo_id, parent_dir)
                     if isinstance(e, dict) and e.get("name") == filename), None)

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
        claimed = msg.get("size")
        if isinstance(claimed, int) and not isinstance(claimed, bool) and claimed != local_size:
            # git-lfs hands over the file it found, which can be a short copy; storing it under
            # the oid would replace a good remote object with it (fable-C3).
            self._send_json({
                "event": "complete",
                "oid": oid,
                "error": {"code": 400, "message": (
                    f"The local object is {local_size} bytes but git-lfs expects {claimed}; "
                    "not uploaded")},
            })
            return

        # Check if remote object already exists with the identical byte size to skip duplicate upload.
        # Name + size is the whole check, deliberately: the remote path is
        # derived from the content hash (OID), so a file already stored under
        # that name at that size is the same object for practical purposes.
        # Verifying content would cost a full download -- exactly what this
        # skip exists to avoid.  A corrupted same-size leftover is accepted
        # collateral; delete it server-side to force a re-upload.
        already_exists = False
        held_at_another_size = False
        try:
            entries = self.client.list_dir(self.repo_id, parent_dir)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("name") == filename:
                        if entry.get("size") == local_size:
                            already_exists = True
                        else:
                            held_at_another_size = True
                        break
        except Exception:
            already_exists = False

        if already_exists:
            self._send_progress(oid, local_size, local_size)
            self._send_json({"event": "complete", "oid": oid})
            return

        if held_at_another_size and _sha256_of(local_path) != oid:
            # The remote object is replaced by this one below; only bytes that hash to the oid
            # may replace it (fable-C3: the replaced copy can be the only whole one).
            self._send_json({
                "event": "complete",
                "oid": oid,
                "error": {"code": 400, "message": (
                    "The remote object is at another size, but the local file does not hash to "
                    f"{oid}; nothing was replaced")},
            })
            return

        try:
            # Hand the client the *path*, not the bytes.  An LFS object is
            # routinely far larger than RAM, and reading it in first is exactly
            # what LFS exists to avoid; the client streams it from disk.
            throttle = _ProgressThrottle(lambda so_far, delta: self._send_progress(oid, so_far, delta))

            # Under a name of its own, then renamed onto the object's name: two machines
            # uploading one object at once each wrote the final name, and the server kept
            # the loser as "<oid> (SFConflict ...)" beside it (ROADMAP Z30, gemini A6).
            # The payload does the same (PAYLOAD.md D5).
            temp = f"{filename}{PART_SUFFIX}{os.urandom(4).hex()}"
            # A real git-lfs oid is the SHA-256 of the object: hash what is sent and refuse to publish bytes
            # that are not the oid (RESUME.md D1 step 2).  Other oids (a test's, a hand-made one) are not checked.
            checked = re.fullmatch(r"[0-9a-f]{64}", oid) is not None
            try:
                with open(local_path, "rb") as source:
                    reader = HashingReader(source, local_size) if checked else None
                    self.client.upload_file(
                        self.repo_id, parent_dir, temp, reader if reader is not None else Path(local_path),
                        replace=True, progress_callback=throttle.callback,
                    )
                if reader is not None:
                    vouched = (_sha256_of(local_path) == oid) if reader.rewound else (
                        reader.complete and reader.hexdigest() == oid)
                    if not vouched:
                        raise RuntimeError("the file changed while it was being uploaded; nothing was stored, push again")
                ours = None
                landed = getattr(self.client, "rename_entry_result", None)
                outcome = landed(self.repo_id, f"{parent_dir}/{temp}", filename) if callable(landed) else None
                if isinstance(outcome, tuple) and len(outcome) == 2:
                    status, landing = outcome
                    if status in (200, 201) and landing is None:
                        raise RuntimeError("the server accepted the rename but did not say where the object landed; "
                                           "nothing was stored, push again")
                    ours = landing if landing and landing != filename else None
                else:
                    self.client.rename_entry(self.repo_id, f"{parent_dir}/{temp}", filename)
                # A 2xx is an acknowledgement, not a proof: a proxy that answers success for a
                # failed upload made git-lfs record the object as pushed, and `git lfs prune`
                # may then remove the only copy.  Every other transfer reads back; so does
                # this one, with the listing the skip above already trusts (review 4f1cdd4,
                # glm-5.3 G6.1, first reported at v0.9.12).
                listed = self._settle_object_name(parent_dir, filename, local_size, ours)
            except Exception:
                self._drop_part(parent_dir, temp)
                raise
            held = listed.get("size") if listed else None
            if held != local_size:
                self._drop_part(parent_dir, temp)
                raise RuntimeError(
                    f"the server accepted the upload but lists {filename} "
                    + ("not at all" if listed is None else f"at {held} bytes")
                    + f" instead of {local_size}; nothing was stored, push again"
                )
            last_bytes = throttle.last_bytes
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
            throttle = _ProgressThrottle(lambda so_far, delta: self._send_progress(oid, so_far, delta))

            # git-lfs sends the object's size with the request.  Cutting the
            # stream off at it keeps a server that streams without end (or
            # lies about its length) from filling the disk before the size is
            # compared below.
            expected = msg.get("size")
            cap = (
                expected
                if isinstance(expected, int) and not isinstance(expected, bool) and expected >= 0
                else None
            )
            download_ok = self.client.download_file_to(
                self.repo_id, file_path, temp_dest, progress_callback=throttle.callback,
                max_bytes=cap,
            )
            last_bytes = throttle.last_bytes

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
            if cap is not None and size != expected:
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
            if not isinstance(msg, dict):
                # Valid JSON that is not an object ("[1]", "3") has no event to read;
                # `msg.get` raised and the agent died with a traceback (review 4f1cdd4,
                # deepseek-v4.1-flash G6.1).
                sys.stderr.write(f"git-remote-seafile lfs: ignoring a JSON line that is not an object: {line[:200]}\n")
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
                # git-lfs dispatches on the "event" key; a bare {"error": ...}
                # matches no state of its wait loop, which can hang the
                # transfer.  The well-formed complete
                # event with an error payload is what a caller can react to.
                self._send_json({
                    "event": "complete",
                    "oid": msg.get("oid") or "",
                    "error": {"code": 400, "message": f"Unknown event: {event}"},
                })
