"""client.py - Seafile Web API v2.1 client for git-remote-seafile."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .git_util import get_git_config_bool
from .sqlite_read import open_live_sqlite_ro


class SeafileAuthError(Exception):
    pass


class SeafileAPIError(Exception):
    pass


def _force_file_host_enabled() -> bool:
    """Should returned download/upload links be forced onto the API server?

    Off by default.  Rewriting the host fixes a reverse proxy that reports an
    unreachable internal host, but it also breaks split/clustered deployments,
    where the file server genuinely lives elsewhere and the rewritten URL 404s.
    Since the returned host is the server's own answer, it is trusted unless the
    user says otherwise.

    ``SEAFILE_FORCE_FILE_HOST`` accepts an explicit off as well, so it can
    override a ``seafile.forcefilehost`` that is set to true.
    """
    env_val = os.environ.get("SEAFILE_FORCE_FILE_HOST", "").strip().lower()
    if env_val in ("1", "true", "yes", "on"):
        return True
    if env_val in ("0", "false", "no", "off"):
        return False
    return get_git_config_bool("seafile.forcefilehost", False)


class SeafileClient:
    """Lightweight HTTP client for Seafile Web API v2.1."""

    def __init__(
        self,
        server_url: str | None = None,
        token: str | None = None,
        timeout: int = 30,
        require_credentials: bool = True,
    ):
        self.server_url = (server_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        self._force_file_host: bool | None = None  # resolved lazily; see below
        self.session = requests.Session()
        # File transfers go through a session as well, so they get connection
        # reuse, the `verify`/proxy settings and the retry adapter.  This second
        # one carries no Authorization header: download and upload links embed
        # their own short-lived token, and a clustered deployment may serve them
        # from a different host, where the account token has no business going.
        self._file_session = requests.Session()
        self._mount_retries(self.session)
        self._mount_retries(self._file_session)
        self._repos_cache: dict[str, str] = {}  # name_or_id -> id
        self._known_dirs: set[tuple[str, str]] = set()  # (repo_id, clean_path)

        # `require_credentials=False` is for commands that need to know which
        # server they are talking about but never authenticate -- `desktop-url`
        # only builds a URL from it.  Such a command still needs the server;
        # demanding a token as well made it fail for users who have the desktop
        # client installed but no reachable token.
        if not self.server_url or (require_credentials and not self.token):
            self._load_credentials(require_token=require_credentials)

        if self.token:
            self.session.headers.update({"Authorization": f"Token {self.token}"})

    def _load_credentials(self, require_token: bool = True) -> None:
        """Load credentials from env vars, config file, or local Seafile client.

        ``require_token=False`` accepts a server URL on its own.  With the
        default (``True``) the search is unchanged: it keeps looking until it
        finds a server *and* a token, and raises if it cannot.
        """
        # 1. Environment variables
        env_server = os.environ.get("SEAFILE_SERVER")
        env_token = os.environ.get("SEAFILE_TOKEN")
        if env_server and (env_token or not require_token):
            self.server_url = env_server.rstrip("/")
            self.token = env_token
            return

        # 2. Config file (~/.git-seafile.json)
        config_path = Path.home() / ".git-seafile.json"
        if config_path.is_file():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                if cfg.get("server") and (cfg.get("token") or not require_token):
                    self.server_url = cfg["server"].rstrip("/")
                    self.token = cfg.get("token")
                    return
            except Exception:
                pass

        # 3. Read directly from local Seafile client accounts.db (Windows, Linux, macOS, seaf-cli)
        candidates = []
        for ini_path in [Path.home() / "ccnet" / "seafile.ini", Path.home() / ".ccnet" / "seafile.ini"]:
            if ini_path.is_file():
                try:
                    data_dir = Path(ini_path.read_text(encoding="utf-8").strip())
                    candidates.append(data_dir / "accounts.db")
                except Exception:
                    pass
        candidates.extend([
            Path.home() / "ccnet" / "accounts.db",
            Path.home() / ".ccnet" / "accounts.db",
            Path.home() / "Seafile" / "seafile-data" / "accounts.db",
            Path.home() / ".seafile-data" / "accounts.db",
            Path.home() / "Seafile" / ".seafile-data" / "accounts.db",
            Path.home() / "Library" / "Application Support" / "Seafile" / "accounts.db",
            Path.home() / ".config" / "seafile" / "accounts.db",
        ])
        for db_path in candidates:
            row = None
            with open_live_sqlite_ro(db_path) as con:
                if con is None:
                    continue
                try:
                    if self.server_url:
                        host = urlparse(self.server_url).netloc
                        row = con.execute(
                            "SELECT url, token FROM Accounts WHERE url LIKE ? ORDER BY lastVisited DESC LIMIT 1",
                            (f"%{host}%",),
                        ).fetchone()
                    if not row:
                        row = con.execute("SELECT url, token FROM Accounts ORDER BY lastVisited DESC LIMIT 1").fetchone()
                except Exception:
                    continue
            if row and row[0] and (row[1] or not require_token):
                self.server_url = row[0].rstrip("/")
                self.token = row[1]
                return

        if not self.server_url or (require_token and not self.token):
            if require_token:
                raise SeafileAuthError(
                    "Could not find Seafile credentials. Set SEAFILE_SERVER and SEAFILE_TOKEN "
                    "or configure ~/.git-seafile.json"
                )
            raise SeafileAuthError(
                "Could not determine the Seafile server URL. Set SEAFILE_SERVER or "
                "configure ~/.git-seafile.json"
            )

    @staticmethod
    def _mount_retries(session: requests.Session) -> None:
        """Retry transient failures instead of aborting a whole transfer.

        A dropped connection or a 502 part-way through a packfile download used
        to fail the fetch outright, on a link that was usually fine a second
        later.  Only idempotent methods are retried: urllib3's default
        ``allowed_methods`` excludes POST, so an upload is never re-sent -- a
        retried upload can duplicate work we then cannot undo.

        ``raise_on_status=False`` matters: without it, exhausting the retries on
        a 500 raises a urllib3 error instead of returning the response, which
        would change what the callers' status-code handling sees.
        """
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.3,
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

    def _transfer_session(self, url: str) -> requests.Session:
        """Pick the session to send a file transfer through.

        Both sessions share pooling, TLS/proxy settings and the retry adapter;
        they differ only in whether the Seafile account token rides along.
        """
        if urlparse(url).netloc == urlparse(self.server_url).netloc:
            return self.session
        return self._file_session

    @property
    def force_file_host(self) -> bool:
        """Whether returned link hosts should be forced onto the API server.

        Resolved on first use and cached, deliberately: reading the setting
        shells out to ``git config``, and constructing a client must not require
        a working ``git`` or even a sane ``PATH`` -- a cleared environment is
        exactly what the credential tests use, and a missing binary there turned
        client construction into a ``FileNotFoundError``.
        """
        if self._force_file_host is None:
            self._force_file_host = _force_file_host_enabled()
        return self._force_file_host

    @force_file_host.setter
    def force_file_host(self, value: bool) -> None:
        self._force_file_host = value

    def _adjust_url(self, raw_url: str) -> str:
        """Return an absolute, reachable URL for a returned download/upload link.

        Two jobs were conflated here:

        * Completing a *relative* link (``/seafhttp/files/...``) against the
          server.  That is completion, not rewriting, and it is always done --
          `requests` cannot fetch a bare path.
        * Forcing an *absolute* link's host onto the server.  That fixes a
          reverse proxy reporting an internal host, but it also breaks
          split/clustered deployments, where the file server genuinely lives
          elsewhere and the rewritten URL 404s.  It is now opt-in
          (``seafile.forcefilehost`` / ``SEAFILE_FORCE_FILE_HOST``).
        """
        srv = urlparse(self.server_url)
        tgt = urlparse(raw_url)

        if not tgt.netloc or self.force_file_host:
            return urlunparse(
                (srv.scheme, srv.netloc, tgt.path, tgt.params, tgt.query, tgt.fragment)
            )
        return raw_url

    def get_repo_id(self, name_or_id: str) -> str:
        """Resolve a library name or id to its repo UUID."""
        if name_or_id in self._repos_cache:
            return self._repos_cache[name_or_id]

        url = f"{self.server_url}/api2/repos/"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to list repos: HTTP {resp.status_code} {resp.text}")

        for repo in resp.json():
            rid = repo.get("id", "")
            rname = repo.get("name", "")
            self._repos_cache[rid] = rid
            self._repos_cache[rname] = rid

        if name_or_id in self._repos_cache:
            return self._repos_cache[name_or_id]
        raise SeafileAPIError(f"Seafile library not found: '{name_or_id}'")

    def list_dir(self, repo_id: str, path: str = "/") -> list[dict[str, Any]]:
        """List entries in a directory. Returns empty list if directory does not exist."""
        clean_path = ("/" + path.strip("/")).rstrip("/") or "/"
        url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={clean_path}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to list dir {clean_path}: HTTP {resp.status_code} {resp.text}")
        return resp.json()

    def dir_exists(self, repo_id: str, dir_path: str) -> bool:
        """Check if directory exists on Seafile."""
        clean_path = ("/" + dir_path.strip("/")).rstrip("/") or "/"
        if clean_path == "/":
            return True
        if (repo_id, clean_path) in getattr(self, "_known_dirs", set()):
            return True
        url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={clean_path}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 200:
            if hasattr(self, "_known_dirs"):
                self._known_dirs.add((repo_id, clean_path))
            return True
        return False

    def mkdir_p(self, repo_id: str, dir_path: str) -> bool:
        """Recursively ensure a directory path exists without duplicate creation."""
        clean_dir = ("/" + dir_path.strip("/")).rstrip("/")
        if not clean_dir or clean_dir == "/":
            return True

        parts = [p for p in clean_dir.strip("/").split("/") if p]
        current = ""
        known = getattr(self, "_known_dirs", None)
        for part in parts:
            current = f"{current}/{part}"
            if known is not None and (repo_id, current) in known:
                continue
            if self.dir_exists(repo_id, current):
                if known is not None:
                    known.add((repo_id, current))
                continue

            url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={current}"
            resp = self.session.post(url, data={"operation": "mkdir"}, timeout=self.timeout)
            if resp.status_code in (200, 201):
                if known is not None:
                    known.add((repo_id, current))
                continue
            if resp.status_code == 400 and "already exists" in resp.text.lower():
                if known is not None:
                    known.add((repo_id, current))
                continue

            # The mkdir did not report success.  Before giving up, check whether
            # the directory is there anyway -- it may have been created
            # concurrently, or the server may answer with an unexpected status.
            # Note that list_dir returns [] (never None) for a missing
            # directory, so testing a listing for emptiness silently reports
            # success for a directory that was never created, and caches it as
            # known so it is never retried.
            if self.dir_exists(repo_id, current):
                if known is not None:
                    known.add((repo_id, current))
                continue

            raise SeafileAPIError(
                f"Failed to create directory {current}: HTTP {resp.status_code} {resp.text}"
            )
        return True

    def get_file_text(self, repo_id: str, file_path: str) -> str | None:
        """Download file and return as string, or None if not found."""
        data = self.get_file_bytes(repo_id, file_path)
        if data is None:
            return None
        return data.decode("utf-8", errors="replace").strip()

    def get_file_bytes(self, repo_id: str, file_path: str) -> bytes | None:
        """Download file content, or None if 404."""
        clean_path = "/" + file_path.strip("/")
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={clean_path}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to get download link for {clean_path}: HTTP {resp.status_code}")

        dl_url = resp.text.strip('"')
        dl_url = self._adjust_url(dl_url)
        file_resp = self._transfer_session(dl_url).get(dl_url, timeout=self.timeout)
        if file_resp.status_code == 404:
            return None
        if file_resp.status_code != 200:
            raise SeafileAPIError(f"Failed to download file from {dl_url}: HTTP {file_resp.status_code}")
        return file_resp.content

    def upload_file(
        self,
        repo_id: str,
        parent_dir: str,
        filename: str,
        content: bytes,
        replace: bool = True,
    ) -> bool:
        """Upload file content to a parent directory."""
        clean_parent = ("/" + parent_dir.strip("/")).rstrip("/") or "/"
        self.mkdir_p(repo_id, clean_parent)

        # 1. Get upload link
        link_url = f"{self.server_url}/api2/repos/{repo_id}/upload-link/?p={clean_parent}"
        resp = self.session.get(link_url, timeout=self.timeout)
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to get upload link for {clean_parent}: HTTP {resp.status_code} {resp.text}")

        upload_url = self._adjust_url(resp.text.strip('"'))

        # 2. Post file
        files = {"file": (filename, content)}
        data = {"parent_dir": clean_parent, "replace": "1" if replace else "0"}
        up_resp = self._transfer_session(upload_url).post(
            upload_url, files=files, data=data, timeout=self.timeout
        )
        if up_resp.status_code not in (200, 201):
            raise SeafileAPIError(f"Failed to upload {filename} to {clean_parent}: HTTP {up_resp.status_code} {up_resp.text}")
        return True

    def delete_entry(self, repo_id: str, path: str) -> bool:
        """Delete file or directory at path."""
        clean_path = "/" + path.strip("/")
        # Seafile's /file/ endpoint deletes both files and directories,
        # while /dir/ returns 404 when deleting a file. Try /file/ first.
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={clean_path}"
        resp = self.session.delete(url, timeout=self.timeout)
        if resp.status_code == 200:
            return True
        dir_url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={clean_path}"
        resp_dir = self.session.delete(dir_url, timeout=self.timeout)
        return resp_dir.status_code == 200
