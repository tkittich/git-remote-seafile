"""lock.py - Cooperative mutex locking for git-remote-seafile."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from typing import Any
from .client import SeafileClient


class RepositoryLockedError(Exception):
    pass


class RemoteLock:
    """Cooperative distributed lease lock stored on Seafile.
    
    Prevents race conditions if multiple machines attempt to push to the
    same repository concurrently. Includes automatic lease expiration so
    stale locks from crashed processes do not permanently block pushes.
    """

    def __init__(
        self,
        client: SeafileClient,
        repo_id: str,
        repo_path: str,
        timeout: int = 15,
        lease: int = 60,
    ):
        self.client = client
        self.repo_id = repo_id
        self.repo_path = repo_path.rstrip("/")
        self.lock_file_path = f"{self.repo_path}/.git-lock.json"
        self.timeout = timeout
        self.lease = lease
        self.acquired = False

    def _get_lock_info(self) -> dict[str, Any] | None:
        raw = self.client.get_file_text(self.repo_id, self.lock_file_path)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def acquire(self) -> None:
        start_time = time.time()
        hostname = socket.gethostname()
        tok = getattr(self.client, "token", None)
        owner = str(tok)[:8] if tok is not None else "user"

        while True:
            info = self._get_lock_info()
            now = time.time()

            if info is not None:
                lock_time = info.get("timestamp", 0)
                lock_lease = info.get("lease", self.lease)
                expires_at = lock_time + lock_lease

                if now < expires_at:
                    # Lock is actively held
                    lock_owner = info.get("owner", "another user")
                    lock_machine = info.get("machine", "another machine")
                    if time.time() - start_time >= self.timeout:
                        raise RepositoryLockedError(
                            f"Repository is locked by '{lock_owner}' on '{lock_machine}'. "
                            f"Lock expires in {int(expires_at - now)}s."
                        )
                    sys.stderr.write(f"Repository locked by {lock_machine}; waiting for lock...\n")
                    sys.stderr.flush()
                    time.sleep(2)
                    continue
                else:
                    sys.stderr.write("Overriding expired lock from previous session.\n")
                    sys.stderr.flush()

            # Attempt to write lock
            lock_payload = {
                "owner": owner,
                "machine": hostname,
                "timestamp": now,
                "lease": self.lease,
            }
            try:
                self.client.upload_file(
                    self.repo_id,
                    self.repo_path,
                    ".git-lock.json",
                    json.dumps(lock_payload).encode("utf-8"),
                    replace=True,
                )
                self.acquired = True
                return
            except Exception as ex:
                if time.time() - start_time >= self.timeout:
                    raise RepositoryLockedError(f"Failed to acquire lock: {ex}")
                time.sleep(2)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.client.delete_entry(self.repo_id, self.lock_file_path)
        except Exception:
            pass
        finally:
            self.acquired = False

    def __enter__(self) -> RemoteLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
