"""client.py - Seafile Web API v2.1 client for git-remote-seafile."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests


class SeafileAuthError(Exception):
    pass


class SeafileAPIError(Exception):
    pass


class SeafileClient:
    """Lightweight HTTP client for Seafile Web API v2.1."""

    def __init__(
        self,
        server_url: str | None = None,
        token: str | None = None,
        timeout: int = 30,
    ):
        self.server_url = (server_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self._repos_cache: dict[str, str] = {}  # name_or_id -> id
        self._known_dirs: set[tuple[str, str]] = set()  # (repo_id, clean_path)

        if not self.server_url or not self.token:
            self._load_credentials()

        if self.token:
            self.session.headers.update({"Authorization": f"Token {self.token}"})

    def _load_credentials(self) -> None:
        """Load credentials from env vars, config file, or local Seafile client."""
        # 1. Environment variables
        env_server = os.environ.get("SEAFILE_SERVER")
        env_token = os.environ.get("SEAFILE_TOKEN")
        if env_server and env_token:
            self.server_url = env_server.rstrip("/")
            self.token = env_token
            return

        # 2. Config file (~/.git-seafile.json)
        config_path = Path.home() / ".git-seafile.json"
        if config_path.is_file():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                if cfg.get("server") and cfg.get("token"):
                    self.server_url = cfg["server"].rstrip("/")
                    self.token = cfg["token"]
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
            if db_path.is_file():
                try:
                    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
                    row = None
                    if self.server_url:
                        host = urlparse(self.server_url).netloc
                        row = con.execute(
                            "SELECT url, token FROM Accounts WHERE url LIKE ? ORDER BY lastVisited DESC LIMIT 1",
                            (f"%{host}%",),
                        ).fetchone()
                    if not row:
                        row = con.execute("SELECT url, token FROM Accounts ORDER BY lastVisited DESC LIMIT 1").fetchone()
                    con.close()
                    if row and row[0] and row[1]:
                        self.server_url = row[0].rstrip("/")
                        self.token = row[1]
                        return
                except Exception:
                    continue

        if not self.server_url or not self.token:
            raise SeafileAuthError(
                "Could not find Seafile credentials. Set SEAFILE_SERVER and SEAFILE_TOKEN "
                "or configure ~/.git-seafile.json"
            )

    def _adjust_url(self, raw_url: str) -> str:
        """Ensure scheme and host match server_url if internal redirect / HTTP was returned."""
        srv = urlparse(self.server_url)
        tgt = urlparse(raw_url)
        return urlunparse((srv.scheme, srv.netloc, tgt.path, tgt.params, tgt.query, tgt.fragment))

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
            entries = self.list_dir(repo_id, current)
            if entries is not None:
                if known is not None:
                    known.add((repo_id, current))
                continue
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
        file_resp = requests.get(dl_url, timeout=self.timeout)
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
        up_resp = requests.post(upload_url, files=files, data=data, timeout=self.timeout)
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
