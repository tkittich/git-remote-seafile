"""client.py - Seafile Web API v2.1 client for git-remote-seafile."""

from __future__ import annotations

from collections import Counter
import io
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid
from urllib.parse import quote, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .git_util import get_git_config_bool
from .seafile_paths import get_candidate_db_paths
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


def _sanitize_header_param(val: str) -> str:
    """Sanitize parameter values in Content-Disposition headers to prevent header injection."""
    cleaned = str(val).replace("\r", "").replace("\n", "")
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


class StreamingMultipartFile:
    """Streams multipart/form-data with Content-Length without buffering file contents in memory.

    Exposes read(size), seek(offset, whence), and __len__ so that `requests` sets
    Content-Length and streams chunks over the socket.
    """

    def __init__(
        self,
        fields: dict[str, str],
        file_field: str,
        filename: str,
        file_obj: Any,
        file_size: int,
        boundary: str | None = None,
        progress_callback: Any = None,
    ):
        self.boundary = boundary or f"----GRSBoundary{uuid.uuid4().hex}"
        self.file_obj = file_obj
        self.file_size = file_size
        self.progress_callback = progress_callback
        self._uploaded_file_bytes = 0
        try:
            self._file_start_pos = self.file_obj.tell() if hasattr(self.file_obj, "tell") else 0
        except Exception:
            self._file_start_pos = 0

        prefix = []
        for name, val in fields.items():
            safe_name = _sanitize_header_param(name)
            prefix.append(
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'
                f"{val}\r\n".encode("utf-8")
            )
        safe_field = _sanitize_header_param(file_field)
        safe_filename = _sanitize_header_param(filename)
        prefix.append(
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{safe_field}"; filename="{safe_filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        )
        self.header = b"".join(prefix)
        self.footer = f"\r\n--{self.boundary}--\r\n".encode("utf-8")
        self.total_len = len(self.header) + file_size + len(self.footer)
        self._pos = 0

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self) -> int:
        return self.total_len

    def read(self, size: int = -1) -> bytes:
        if size == -1 or size is None:
            size = self.total_len - self._pos
        if size <= 0 or self._pos >= self.total_len:
            return b""

        chunks: list[bytes] = []
        needed = size

        # 1. Header prefix
        if self._pos < len(self.header):
            avail = len(self.header) - self._pos
            take = min(needed, avail)
            chunks.append(self.header[self._pos : self._pos + take])
            self._pos += take
            needed -= take

        # 2. File body
        file_start = len(self.header)
        file_end = file_start + self.file_size
        if needed > 0 and file_start <= self._pos < file_end:
            rel_pos = self._pos - file_start
            if hasattr(self.file_obj, "seek"):
                self.file_obj.seek(self._file_start_pos + rel_pos)
            take = min(needed, file_end - self._pos)
            chunk = self.file_obj.read(take)
            chunks.append(chunk)
            self._pos += len(chunk)
            needed -= len(chunk)
            file_transferred = min(self.file_size, max(0, self._pos - file_start))
            self._uploaded_file_bytes = file_transferred
            if self.progress_callback:
                try:
                    self.progress_callback(file_transferred, self.file_size)
                except Exception:
                    pass

        # 3. Footer suffix
        if needed > 0 and self._pos >= file_end:
            foot_pos = self._pos - file_end
            avail = len(self.footer) - foot_pos
            take = min(needed, avail)
            chunks.append(self.footer[foot_pos : foot_pos + take])
            self._pos += take

        return b"".join(chunks)

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self.total_len + offset
        else:
            raise ValueError(f"Invalid whence: {whence}")
        return self._pos

    def tell(self) -> int:
        return self._pos


def _normalize_netloc(url_or_netloc: str) -> str:
    """Normalize a server URL or netloc for consistent credential matching.

    Converts hostname to lowercase and strips default ports (443 for HTTPS, 80 for HTTP).
    """
    if not url_or_netloc:
        return ""
    if "://" not in url_or_netloc:
        url_or_netloc = f"http://{url_or_netloc}"
    parsed = urlparse(url_or_netloc)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
        return f"{host}:{port}"
    return host


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
        self._server_time_offset: float | None = None
        self._server_time_offsets: list[float] = []
        self.session = requests.Session()
        # File transfers go through a session as well, so they get connection
        # reuse, the `verify`/proxy settings and the retry adapter.  This second
        # one carries no Authorization header: download and upload links embed
        # their own short-lived token, and a clustered deployment may serve them
        # from a different host, where the account token has no business going.
        self._file_session = requests.Session()
        self._mount_retries(self.session)
        self._mount_retries(self._file_session)
        self.session.hooks["response"].append(self._record_server_date_header)
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

    def _record_server_date_header(self, resp: requests.Response, *args: Any, **kwargs: Any) -> None:
        date_hdr = resp.headers.get("Date") if hasattr(resp, "headers") else None
        if date_hdr:
            try:
                from email.utils import parsedate_to_datetime
                server_ts = parsedate_to_datetime(date_hdr).timestamp()
                offset = server_ts - time.time()
                self._server_time_offsets.append(offset)
                if len(self._server_time_offsets) > 7:
                    self._server_time_offsets.pop(0)
                sorted_offsets = sorted(self._server_time_offsets)
                self._server_time_offset = sorted_offsets[len(sorted_offsets) // 2]
            except Exception:
                pass

    def get_server_time(self) -> float:
        """Return estimated server timestamp synchronized with HTTP Date response header."""
        if self._server_time_offset is not None:
            return time.time() + self._server_time_offset
        return time.time()

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
            if not self.server_url or _normalize_netloc(env_server) == _normalize_netloc(self.server_url):
                self.server_url = env_server.rstrip("/")
                self.token = env_token
                return
        elif env_token and self.server_url:
            self.token = env_token
            return

        # 2. Config file (~/.git-seafile.json)
        config_path = Path.home() / ".git-seafile.json"
        if config_path.is_file():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                cfg_server = cfg.get("server")
                cfg_token = cfg.get("token")
                if cfg_server and (cfg_token or not require_token):
                    if not self.server_url or _normalize_netloc(cfg_server) == _normalize_netloc(self.server_url):
                        self.server_url = cfg_server.rstrip("/")
                        self.token = cfg_token
                        return
                elif cfg_token and self.server_url and not cfg_server:
                    self.token = cfg_token
                    return
            except Exception:
                pass

        # 3. Read directly from local Seafile client accounts.db (Windows, Linux, macOS, seaf-cli)
        candidates = get_candidate_db_paths("accounts.db")
        for db_path in candidates:
            row = None
            with open_live_sqlite_ro(db_path) as con:
                if con is None:
                    continue
                try:
                    if self.server_url:
                        target_netloc = _normalize_netloc(self.server_url)
                        rows = con.execute(
                            "SELECT url, token FROM Accounts ORDER BY lastVisited DESC"
                        ).fetchall()
                        for r_url, r_token in rows:
                            if r_url and _normalize_netloc(r_url) == target_netloc:
                                row = (r_url, r_token)
                                break
                    else:
                        row = con.execute("SELECT url, token FROM Accounts ORDER BY lastVisited DESC LIMIT 1").fetchone()
                except Exception:
                    continue
            if row and row[0] and (row[1] or not require_token):
                if not self.server_url:
                    self.server_url = row[0].rstrip("/")
                self.token = row[1]
                return

        if not self.server_url or (require_token and not self.token):
            if self.server_url:
                raise SeafileAuthError(
                    f"Could not find Seafile credentials for '{self.server_url}'. "
                    "Set SEAFILE_TOKEN or configure ~/.git-seafile.json"
                )
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
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

    def _transfer_session(self, url: str) -> requests.Session:
        """Pick the session to send a file transfer through.

        Both sessions share pooling, TLS/proxy settings and the retry adapter;
        they differ only in whether the Seafile account token rides along.
        """
        if _normalize_netloc(url) == _normalize_netloc(self.server_url):
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

        Three jobs live here, and they are not the same job:

        * Completing a *relative* link (``/seafhttp/files/...``) against the
          server.  That is completion, not rewriting, and it is always done --
          `requests` cannot fetch a bare path.
        * Correcting the *scheme* when the link names the very same authority as
          the server.  A reverse proxy that answers the API over https will
          often report the file endpoint as plain http; that is the same
          host:port with a scheme the proxy got wrong, not a second server.
          This must be fixed unconditionally, because a redirected POST is
          re-issued by `requests` as a *GET*, and the upload endpoint answers
          that with HTTP 400 -- which killed every push at lock acquisition.
          Only ever upgraded, never downgraded.
        * Forcing an *absolute* link's host onto the server.  That fixes a proxy
          reporting an internal host, but it also breaks split/clustered
          deployments, where the file server genuinely lives elsewhere and the
          rewritten URL 404s.  It stays opt-in (``seafile.forcefilehost`` /
          ``SEAFILE_FORCE_FILE_HOST``).

        The authority comparison is exact, port included: a file server on
        ``host:8082`` is a different endpoint, not a mistyped scheme.
        """
        srv = urlparse(self.server_url)
        tgt = urlparse(raw_url)

        if not tgt.netloc or self.force_file_host:
            return urlunparse(
                (srv.scheme, srv.netloc, tgt.path, tgt.params, tgt.query, tgt.fragment)
            )

        if tgt.netloc == srv.netloc and tgt.scheme == "http" and srv.scheme == "https":
            return urlunparse(
                ("https", tgt.netloc, tgt.path, tgt.params, tgt.query, tgt.fragment)
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

        repos = resp.json()
        if not isinstance(repos, list):
            raise SeafileAPIError(f"Unexpected response from {url}: {resp.text}")

        # Check for direct ID match first (UUIDs are globally unique)
        for repo in repos:
            rid = repo.get("id", "")
            if rid:
                self._repos_cache[rid] = rid
                if rid == name_or_id:
                    return rid

        # Collect matches by name
        matches = [r for r in repos if r.get("name") == name_or_id]

        # Cache unambiguous unique names to accelerate subsequent lookups
        name_counts = Counter(r.get("name", "") for r in repos if r.get("name"))
        for r in repos:
            rname = r.get("name", "")
            rid = r.get("id", "")
            if rname and rid and name_counts[rname] == 1:
                self._repos_cache[rname] = rid

        if not matches:
            if name_or_id in self._repos_cache:
                return self._repos_cache[name_or_id]
            raise SeafileAPIError(f"Seafile library not found: '{name_or_id}'")

        if len(matches) == 1:
            rid = matches[0].get("id", "")
            self._repos_cache[name_or_id] = rid
            return rid

        # Disambiguate duplicate library names: owned libraries take precedence over shared/group libraries.
        # In Seafile Web API v2.1, owned libraries report type "repo" (or "mine"), while shared
        # and group libraries report "srepo" or "grepo". When available, owner matching username
        # also identifies owned libraries.
        owned = [
            r for r in matches
            if r.get("type") in ("repo", "mine")
            or (getattr(self, "username", None) and r.get("owner") == getattr(self, "username", None))
        ]
        if len(owned) == 1:
            rid = owned[0].get("id", "")
            sys.stderr.write(
                f"Warning: multiple Seafile libraries match '{name_or_id}'. "
                f"Selecting owned library {rid} over shared. "
                f"To avoid ambiguity, use the repository UUID: seafile://{rid}/...\n"
            )
            self._repos_cache[name_or_id] = rid
            return rid

        # Ambiguous tie: multiple owned libraries or multiple shared libraries with no owned library
        matching_ids = [r.get("id", "") for r in matches if r.get("id")]
        raise SeafileAPIError(
            f"Multiple Seafile libraries match '{name_or_id}' ({', '.join(matching_ids)}). "
            f"Please specify the library UUID in the remote URL (seafile://<uuid>/...)."
        )

    def list_dir(self, repo_id: str, path: str = "/") -> list[dict[str, Any]]:
        """List entries in a directory. Returns empty list if directory does not exist."""
        clean_path = ("/" + path.strip("/")).rstrip("/") or "/"
        url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={quote(clean_path, safe='/')}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to list dir {clean_path}: HTTP {resp.status_code} {resp.text}")
        return resp.json()

    @property
    def _known_dirs(self) -> set[tuple[str, str]]:
        # Lazily initialized so unit test instances created via __new__ without __init__ remain functional
        if not hasattr(self, "_known_dirs_cache"):
            self._known_dirs_cache: set[tuple[str, str]] = set()
        return self._known_dirs_cache

    @_known_dirs.setter
    def _known_dirs(self, val: set[tuple[str, str]]) -> None:
        self._known_dirs_cache = val

    def dir_exists(self, repo_id: str, dir_path: str) -> bool:
        """Check if directory exists on Seafile."""
        clean_path = ("/" + dir_path.strip("/")).rstrip("/") or "/"
        if clean_path == "/":
            return True
        if (repo_id, clean_path) in self._known_dirs:
            return True
        url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={quote(clean_path, safe='/')}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 200:
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
        for part in parts:
            current = f"{current}/{part}"
            if (repo_id, current) in self._known_dirs:
                continue
            if self.dir_exists(repo_id, current):
                self._known_dirs.add((repo_id, current))
                continue

            url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={quote(current, safe='/')}"
            resp = self.session.post(url, data={"operation": "mkdir"}, timeout=self.timeout)
            if resp.status_code in (200, 201):
                self._known_dirs.add((repo_id, current))
                continue
            if resp.status_code == 400 and "already exists" in resp.text.lower():
                self._known_dirs.add((repo_id, current))
                continue

            # The mkdir did not report success.  Before giving up, check whether
            # the directory is there anyway -- it may have been created
            # concurrently, or the server may answer with an unexpected status.
            # Note that list_dir returns [] (never None) for a missing
            # directory, so testing a listing for emptiness silently reports
            # success for a directory that was never created, and caches it as
            # known so it is never retried.
            if self.dir_exists(repo_id, current):
                self._known_dirs.add((repo_id, current))
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
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to get download link for {clean_path}: HTTP {resp.status_code}")

        dl_url = resp.text.strip().strip('"')
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
        content: Any,
        replace: bool = True,
        progress_callback: Any = None,
    ) -> bool:
        """Upload file content to a parent directory.

        ``content`` may be ``bytes``, an open binary file object, or a path.
        A file object or path is *streamed* to the server rather than read into
        memory first -- that is what makes the multi-gigabyte LFS objects the
        docs advertise possible, and because a file object supports seek/tell,
        ``requests`` still sends a Content-Length instead of chunked encoding.
        Passing ``bytes`` stays supported for small payloads (packfiles, refs).
        """
        if isinstance(content, (str, os.PathLike)):
            with open(content, "rb") as fh:
                return self._upload(repo_id, parent_dir, filename, fh, replace, progress_callback)
        return self._upload(repo_id, parent_dir, filename, content, replace, progress_callback)

    def _upload(
        self,
        repo_id: str,
        parent_dir: str,
        filename: str,
        payload: Any,
        replace: bool,
        progress_callback: Any = None,
    ) -> bool:
        clean_parent = ("/" + parent_dir.strip("/")).rstrip("/") or "/"
        self.mkdir_p(repo_id, clean_parent)

        # 1. Get upload link
        link_url = f"{self.server_url}/api2/repos/{repo_id}/upload-link/?p={quote(clean_parent, safe='/')}"
        resp = self.session.get(link_url, timeout=self.timeout)
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to get upload link for {clean_parent}: HTTP {resp.status_code} {resp.text}")

        upload_url = self._adjust_url(resp.text.strip().strip('"'))

        # 2. Post file.
        # If payload is a file object with .read and .seek, stream it via StreamingMultipartFile
        # so requests streams chunks directly without buffering the whole file in RAM.
        if hasattr(payload, "read") and hasattr(payload, "seek"):
            cur = payload.tell()
            payload.seek(0, 2)
            file_size = payload.tell() - cur
            payload.seek(cur)

            mp = StreamingMultipartFile(
                fields={"parent_dir": clean_parent, "replace": "1" if replace else "0"},
                file_field="file",
                filename=filename,
                file_obj=payload,
                file_size=file_size,
                progress_callback=progress_callback,
            )
            headers = {
                "Content-Type": mp.content_type,
                "Content-Length": str(len(mp)),
            }
            up_resp = self._transfer_session(upload_url).post(
                upload_url, data=mp, headers=headers, timeout=self.timeout
            )
        elif isinstance(payload, (bytes, bytearray)) and progress_callback is not None:
            bio = io.BytesIO(payload)
            mp = StreamingMultipartFile(
                fields={"parent_dir": clean_parent, "replace": "1" if replace else "0"},
                file_field="file",
                filename=filename,
                file_obj=bio,
                file_size=len(payload),
                progress_callback=progress_callback,
            )
            headers = {
                "Content-Type": mp.content_type,
                "Content-Length": str(len(mp)),
            }
            up_resp = self._transfer_session(upload_url).post(
                upload_url, data=mp, headers=headers, timeout=self.timeout
            )
        else:
            files = {"file": (filename, payload)}
            data = {"parent_dir": clean_parent, "replace": "1" if replace else "0"}
            up_resp = self._transfer_session(upload_url).post(
                upload_url, files=files, data=data, timeout=self.timeout
            )
            if progress_callback:
                try:
                    payload_len = len(payload) if hasattr(payload, "__len__") else 0
                    progress_callback(payload_len, payload_len)
                except Exception:
                    pass
        if up_resp.status_code not in (200, 201):
            # Name the endpoint that was actually hit.  A wrong scheme or host
            # here is the difference between a working push and an opaque
            # "HTTP 400", and the origin is enough to see it -- the path is
            # deliberately omitted because it carries the upload token.
            origin = urlparse(upload_url)
            raise SeafileAPIError(
                f"Failed to upload {filename} to {clean_parent}: "
                f"HTTP {up_resp.status_code} "
                f"(POST {origin.scheme}://{origin.netloc}) {up_resp.text}"
            )
        return True

    def download_file_to(self, repo_id: str, file_path: str, dest: Any, progress_callback: Any = None) -> bool:
        """Stream a remote file to *dest* without holding it in memory.

        Returns False when the object does not exist.  ``get_file_bytes`` stays
        for small payloads (packfiles, refs); this is the path for LFS objects,
        where the whole point is that they may not fit in RAM.  Without
        ``stream=True`` `requests` reads the entire body into memory before the
        first chunk is written, which is the bug this exists to avoid.
        """
        clean_path = "/" + file_path.strip("/")
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to get download link for {clean_path}: HTTP {resp.status_code}")

        dl_url = self._adjust_url(resp.text.strip().strip('"'))
        file_resp = self._transfer_session(dl_url).get(
            dl_url, timeout=self.timeout, stream=True
        )
        try:
            if file_resp.status_code == 404:
                return False
            if file_resp.status_code != 200:
                raise SeafileAPIError(f"Failed to download file from {dl_url}: HTTP {file_resp.status_code}")

            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            total_size = 0
            if "Content-Length" in file_resp.headers:
                try:
                    total_size = int(file_resp.headers["Content-Length"])
                except Exception:
                    total_size = 0
            transferred = 0
            with open(dest_path, "wb") as fh:
                for chunk in file_resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
                        transferred += len(chunk)
                        if progress_callback:
                            try:
                                progress_callback(transferred, total_size or transferred)
                            except Exception:
                                pass
            return True
        finally:
            file_resp.close()

    def delete_entry(self, repo_id: str, path: str) -> bool:
        """Delete file or directory at path."""
        clean_path = "/" + path.strip("/")
        # Seafile's /file/ endpoint deletes both files and directories,
        # while /dir/ returns 404 when deleting a file. Try /file/ first.
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self.session.delete(url, timeout=self.timeout)
        if resp.status_code == 200:
            return True
        dir_url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={quote(clean_path, safe='/')}"
        resp_dir = self.session.delete(dir_url, timeout=self.timeout)
        return resp_dir.status_code == 200
