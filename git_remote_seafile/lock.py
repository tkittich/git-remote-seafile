"""lock.py - Cooperative mutex locking for git-remote-seafile."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
import uuid
from typing import Any
from .client import SeafileClient
from .git_util import get_git_config_int


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
        timeout: int | None = None,
        lease: int | None = None,
    ):
        self.client = client
        self.repo_id = repo_id
        self.repo_path = repo_path.rstrip("/")
        self.lock_file_path = f"{self.repo_path}/.git-lock.json"
        if timeout is None:
            timeout = get_git_config_int("seafile.locktimeout", 15)
        if lease is None:
            lease = get_git_config_int("seafile.locklease", 60)
        self.timeout = timeout
        self.lease = lease
        self.acquired = False
        # (owner, machine) of the lease we wrote, so that release() can tell our
        # own lock apart from one another machine has since taken over.
        self._identity: tuple[str, str] | None = None
        self._nonce: str | None = None

    def _get_lock_info(self) -> dict[str, Any] | None:
        raw = self.client.get_file_text(self.repo_id, self.lock_file_path)
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _owner_id(self) -> str:
        """A stable, non-secret identifier for the credentials in use.

        The lock payload is stored inside the repository, where anyone with
        access can read it, so it must not carry anything derived from the API
        token in the clear -- the previous implementation wrote the token's first
        eight characters there.  A truncated hash still identifies one client
        consistently without disclosing the secret.
        """
        tok = getattr(self.client, "token", None)
        if not tok:
            return "user"
        return hashlib.sha256(str(tok).encode("utf-8")).hexdigest()[:8]

    def acquire(self) -> None:
        start_time = time.monotonic()
        hostname = socket.gethostname()
        owner = self._owner_id()
        nonce = uuid.uuid4().hex
        self._identity = (owner, hostname)
        self._nonce = nonce

        while True:
            info = self._get_lock_info()
            now = time.time()

            if isinstance(info, dict):
                try:
                    lock_time = float(info.get("timestamp", 0))
                    lock_lease = float(info.get("lease", self.lease))
                except (ValueError, TypeError):
                    lock_time = 0.0
                    lock_lease = 0.0
                expires_at = lock_time + lock_lease

                if now < expires_at:
                    # Lock is actively held
                    lock_owner = info.get("owner", "another user")
                    lock_machine = info.get("machine", "another machine")
                    if time.monotonic() - start_time >= self.timeout:
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
                "nonce": nonce,
                "timestamp": now,
                "lease": self.lease,
                "pid": os.getpid(),
            }
            try:
                self.client.upload_file(
                    self.repo_id,
                    self.repo_path,
                    ".git-lock.json",
                    json.dumps(lock_payload).encode("utf-8"),
                    replace=True,
                )
                # Verify that our write was not overwritten by a concurrent writer
                verify = self._get_lock_info()
                if isinstance(verify, dict) and verify.get("nonce") and verify.get("nonce") != nonce:
                    if time.monotonic() - start_time >= self.timeout:
                        raise RepositoryLockedError("Lock acquisition collided with another client.")
                    sys.stderr.write("Lock acquisition collided; retrying...\n")
                    sys.stderr.flush()
                    time.sleep(1)
                    continue

                self.acquired = True
                return
            except Exception as ex:
                if isinstance(ex, RepositoryLockedError):
                    raise
                if time.monotonic() - start_time >= self.timeout:
                    raise RepositoryLockedError(f"Failed to acquire lock: {ex}") from ex
                time.sleep(2)

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False

        # Only remove a lock that is still ours.  If our lease expired while we
        # were working, another machine or process may have taken it in the meantime,
        # and deleting then would revoke a lock we no longer hold -- letting two
        # writers into the repository at once.  A missing or unreadable lock
        # file tells us nothing, so fall through to the delete in that case.
        info = self._get_lock_info()
        if info is not None:
            remote_nonce = info.get("nonce")
            if remote_nonce is not None:
                if remote_nonce != self._nonce:
                    sys.stderr.write(
                        "Not releasing the remote lock: it is now held by "
                        f"'{info.get('owner')}' on '{info.get('machine')}'.\n"
                    )
                    sys.stderr.flush()
                    return
            elif (info.get("owner"), info.get("machine")) != self._identity:
                sys.stderr.write(
                    "Not releasing the remote lock: it is now held by "
                    f"'{info.get('owner')}' on '{info.get('machine')}'.\n"
                )
                sys.stderr.flush()
                return

        try:
            self.client.delete_entry(self.repo_id, self.lock_file_path)
        except Exception:
            pass

    def __enter__(self) -> RemoteLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
