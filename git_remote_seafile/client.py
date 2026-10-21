"""client.py - Seafile Web API v2.1 client for git-remote-seafile."""

from __future__ import annotations

from collections import Counter
import difflib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
import uuid
from urllib.parse import quote, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url as _urllib3_parse_url
from urllib3.util.retry import Retry

from .git_util import get_git_config_bool
from .seafile_paths import get_candidate_db_paths
from .url import normalize_netloc
from .sqlite_read import open_live_sqlite_ro


def _transfer_authority(url: str) -> tuple[str, int | None] | None:
    """``(host, port)`` of *url* as the HTTP stack will connect to it, or ``None``.

    Whether the account token rides along is decided by comparing this with the
    API server's.  It must be the parser the *connection* uses: ``urlparse`` and
    urllib3 read ``http://other\\@api-host/x`` differently (the first sees host
    ``api-host``, the second connects to ``other``), and a check made with one
    parser on a request sent by the other hands the token to ``other``.  The
    port is dropped when it is the scheme's default, so ``https://h`` and
    ``https://h:443`` -- and a proxy's mistaken ``http://h`` -- are one place.
    ``None`` (unparseable) never equals anything, so it never gets the token.
    """
    try:
        parsed = _urllib3_parse_url(url if "://" in url else f"http://{url}")
    except Exception:
        return None
    host = (parsed.host or "").lower()
    if not host:
        return None
    port = parsed.port
    if port in (80, 443):
        port = None
    return host, port


def _link_authority_is_sane(url: str) -> bool:
    """False for a transfer link whose authority cannot be read the same way twice.

    A real file-server link is ``scheme://host[:port]/path``.  Backslashes,
    userinfo and control characters in the authority exist only to make two
    parsers disagree, so a link carrying them is refused rather than
    interpreted.
    """
    if "://" not in url:
        return True  # relative: completed against the server
    authority = url.split("://", 1)[1]
    for stop in ("/", "?", "#"):
        authority = authority.split(stop, 1)[0]
    return not any(ch in authority for ch in ("\\", "@")) and all(
        32 < ord(ch) < 127 for ch in authority
    )


_LINK_IN_ERROR = re.compile(r"url: \S+")


def _scrub_transfer_error(ex: BaseException, url: str) -> str:
    """The text of a request error without the transfer link it names.

    ``requests`` puts the request path in its messages (``Max retries exceeded
    with url: /seafhttp/files/<token>/...``), and the path of a download or
    upload link carries a short-lived bearer token.  ``_error_origin`` keeps it
    out of the messages this module writes; this keeps it out of the ones
    ``requests`` writes.
    """
    text = str(ex)
    parsed = urlparse(url)
    for secret in (
        f"{parsed.path}?{parsed.query}" if parsed.query else "",
        parsed.path,
        parsed.query,
    ):
        if secret and len(secret) > 1:
            text = text.replace(secret, "<transfer link>")
    return _LINK_IN_ERROR.sub("url: <transfer link>", text)


#: What a text file the tool reads from a library (ref, HEAD, lock ticket, a
#: small state file) may be.  Generous for the largest of those, and far below
#: what a hostile library could plant to exhaust memory.
MAX_TEXT_FILE_BYTES = 16 * 1024 * 1024


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
        self.filename = filename  # named in read()'s short-source error
        self.progress_callback = progress_callback
        self._uploaded_file_bytes = 0
        try:
            self._file_start_pos = self.file_obj.tell() if hasattr(self.file_obj, "tell") else 0
        except Exception:
            self._file_start_pos = 0

        prefix = []
        for name, val in fields.items():
            # A field *value* sits between the header block and the next
            # boundary unescaped.  The boundary is random, so a value cannot
            # realistically forge one, but a CR/LF would still let it start a
            # header of its own -- refuse rather than send a form the server
            # reads differently from what was meant.
            if "\r" in val or "\n" in val or self.boundary in val:
                raise ValueError(f"multipart field {name!r} contains a line break or the boundary")
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
            if not chunk and needed > 0:
                # The source ended before the size we measured from it (a file
                # truncated mid-upload, or a mis-measured stream).  Returning
                # b"" from here reads as EOF to urllib3, which sends the
                # declared Content-Length with a *shorter* body and no error
                # anywhere -- the server then hangs on the shortfall or
                # rejects a corrupt multipart.  Fail the upload instead.
                raise SeafileAPIError(
                    f"upload source for '{self.filename}' ended after "
                    f"{self._uploaded_file_bytes} of {self.file_size} bytes; "
                    "the file changed size while it was being uploaded"
                )
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


#: A file at least this large is uploaded in ``Content-Range`` chunks rather than
#: as one request.  Measured against a real server: a single request of 2 GiB is
#: reset while 1.75 GiB is accepted, but the same server assembled a 5 GiB file
#: from 256 MiB chunks and served it back byte-identical.  Below the threshold
#: the one-request form stays, because it is what every server version has done.
CHUNKED_UPLOAD_MIN_BYTES = 1024 * 1024 * 1024

#: Size of one chunk of a chunked upload.
UPLOAD_CHUNK_BYTES = 128 * 1024 * 1024

#: How long to wait for the server's *reply* to an upload.  The 30 s request
#: timeout suits a directory listing; after the last byte of a large upload the
#: server still has to assemble and commit the file, and that phase grows with
#: the size.  The connect timeout stays short.
UPLOAD_READ_TIMEOUT_SECONDS = 600

#: Attempts per chunk (the first plus retries) before the upload is abandoned.
UPLOAD_CHUNK_ATTEMPTS = 3


class SeafileClient:
    """Lightweight HTTP client for Seafile Web API v2.1."""

    def __init__(
        self,
        server_url: str | None = None,
        token: str | None = None,
        timeout: int = 30,
        require_credentials: bool = True,
        username: str | None = None,
    ):
        self.server_url = (server_url or "").rstrip("/")
        self.token = token
        self.username = username
        self.timeout = timeout
        self._force_file_host: bool | None = None  # resolved lazily; see below
        self._server_time_offset: float | None = None
        self._server_time_offsets: list[float] = []
        self._warned_no_server_time: bool = False
        # One Session is shared by every thread (ref listing, uploads).  That is
        # safe only while the read paths leave it alone: nothing after __init__
        # changes its headers, cookies or adapters, and the one shared mutation,
        # the Date hook's offset list, is a GIL-atomic append/pop.  Keep it so,
        # or give each worker thread its own session.
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
        self._encrypted_repo_ids: set[str] = set()
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

    def close(self) -> None:
        """Release both sessions' connection pools.

        A one-shot CLI run lets process exit do this; the doctor, the test
        suite and any tool that builds several clients in one process do not,
        and every unclosed session pins its sockets until the cyclic
        collector gets to them.
        """
        self.session.close()
        self._file_session.close()

    def __enter__(self) -> "SeafileClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _record_server_date_header(self, resp: requests.Response, *args: Any, **kwargs: Any) -> None:
        date_hdr = resp.headers.get("Date") if hasattr(resp, "headers") else None
        if date_hdr:
            try:
                import re
                from datetime import timezone
                from email.utils import parsedate_to_datetime
                # Extract first standard RFC 2822 GMT date if multiple headers were combined
                m = re.search(r"[A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}\s+GMT", date_hdr)
                raw_hdr = m.group(0) if m else date_hdr
                dt = parsedate_to_datetime(raw_hdr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                server_ts = dt.timestamp()
                offset = server_ts - time.time()
                self._server_time_offsets.append(offset)
                if len(self._server_time_offsets) > 7:
                    self._server_time_offsets.pop(0)
                sorted_offsets = sorted(self._server_time_offsets)
                self._server_time_offset = sorted_offsets[len(sorted_offsets) // 2]
            except Exception:
                pass

    def get_server_time(self) -> float:
        """Estimated server timestamp, from the offset learned off ``Date`` headers.

        The offset is the median of the last few observations, so it is stable
        across a burst of requests.  When no ``Date`` header has been seen at
        all -- the first call, or a server that omits it -- this falls back to
        the *local* clock, which the protocol does not vouch for.  That is worth
        saying once on stderr: the lock derives ticket ordering from this value,
        and a queue that came out mis-ordered is otherwise unexplainable.
        """
        if self._server_time_offset is not None:
            return time.time() + self._server_time_offset
        if not self._warned_no_server_time:
            self._warned_no_server_time = True
            sys.stderr.write(
                "Warning: no server Date header seen yet; using the local clock for "
                "lock ordering.\n"
            )
            sys.stderr.flush()
        return time.time()

    def _adopt_token(self, token: str | None, source: str) -> None:
        """Take a token, warning if it is about to travel in the clear.

        A Seafile token is a bearer credential: sent over plain HTTP, anything
        on the network path can read it and use it.  The scheme is the user's
        choice in the URL, so this warns rather than refuses -- but it warns on
        every adoption, because the alternative is a credential silently
        leaving the machine readable.

        Loopback is exempt.  A Seafile desktop client on `http://localhost` is
        not on a network path, and warning on every `git push` for those users
        would train them to ignore the message.
        """
        self.token = token
        if not token or not self.server_url.startswith("http://"):
            return
        host = (urlparse(self.server_url).hostname or "").lower()
        if host == "localhost" or host == "::1" or host.startswith("127."):
            return
        sys.stderr.write(
            f"Warning: {source} is being sent to {self.server_url} over plain HTTP, "
            "where anything on the network path can read it. Use https:// instead.\n"
        )
        sys.stderr.flush()

    def _load_credentials(self, require_token: bool = True) -> None:
        """Find a server and token: environment, then config file, then the
        Seafile desktop client's accounts.db.

        ``require_token=False`` accepts a server URL on its own.  With the
        default (``True``) the search keeps looking until it finds a server
        *and* a token, and raises if it cannot.  Each source is its own method;
        the first to adopt wins.
        """
        if self._credentials_from_env(require_token):
            return
        config_path = Path.home() / ".git-seafile.json"
        found, cfg_server, cfg_token = self._credentials_from_config_file(config_path, require_token)
        if found:
            return
        if self._credentials_from_accounts_db(require_token):
            return
        if not self.server_url or (require_token and not self.token):
            raise SeafileAuthError(
                self._missing_credentials_message(require_token, config_path, cfg_server, cfg_token)
            )

    def _credentials_from_env(self, require_token: bool) -> bool:
        """SEAFILE_SERVER / SEAFILE_TOKEN.  True when adopted.

        A token is only ever sent to the server it was *declared* for.  A bare
        SEAFILE_TOKEN used to be adopted for whatever server the URL happened to
        name -- so `git clone seafile://somewhere.example/lib/path`, a command a
        user is far more likely to have been handed than to have written,
        silently posted the user's Seafile token to that host.  That is a
        confused deputy: the URL is attacker-chosen, the credential is not.
        Scoping it costs an explicit SEAFILE_SERVER, and the failure message
        names the omission rather than reporting "no credentials".
        """
        env_server = os.environ.get("SEAFILE_SERVER")
        env_token = os.environ.get("SEAFILE_TOKEN")
        if env_server and (env_token or not require_token):
            if not self.server_url or normalize_netloc(env_server) == normalize_netloc(self.server_url):
                self.server_url = env_server.rstrip("/")
                self._adopt_token(env_token, "SEAFILE_TOKEN")
                return True
        return False

    def _credentials_from_config_file(
        self, config_path: Path, require_token: bool
    ) -> tuple[bool, str | None, str | None]:
        """``~/.git-seafile.json``.  Returns ``(adopted, server, token)``.

        The server and token are returned even when not adopted, so the failure
        message can name a token that arrived without a server beside it rather
        than treating the whole file as absent.
        """
        cfg_server: str | None = None
        cfg_token: str | None = None
        if not config_path.is_file():
            return False, None, None
        if os.name != "nt":
            try:
                mode = config_path.stat().st_mode
                if mode & 0o077:
                    sys.stderr.write(
                        f"Warning: {config_path} is accessible by others (mode {oct(mode & 0o777)}). "
                        f"Permissions should be set to 0600 (chmod 600 {config_path}) to protect credentials.\n"
                    )
            except Exception:
                pass
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            cfg_server = cfg.get("server")
            cfg_token = cfg.get("token")
            cfg_user = cfg.get("username") or cfg.get("user")
            if cfg_user and not self.username:
                self.username = cfg_user
            if cfg_server and (cfg_token or not require_token):
                if not self.server_url or normalize_netloc(cfg_server) == normalize_netloc(self.server_url):
                    self.server_url = cfg_server.rstrip("/")
                    self._adopt_token(cfg_token, f"the token in {config_path}")
                    return True, cfg_server, cfg_token
        except Exception as ex:
            # Not fatal -- accounts.db may still authenticate -- but silence
            # here means a user who mistyped a token in this file gets a bare
            # 401 with no hint that their config was rejected.
            sys.stderr.write(
                f"Warning: {config_path} could not be used ({ex}); "
                f"continuing with other credential sources.\n"
            )
            sys.stderr.flush()
        return False, cfg_server, cfg_token

    def _account_row(self, con: Any) -> tuple[str | None, str | None, str | None] | None:
        """The ``(url, token, username)`` row to use from an open accounts.db.

        With a server URL known, the most recently visited account on that
        server; without one, the most recently visited account.  Older clients'
        schema has no ``username`` column, so a failing three-column query falls
        back to two.  ``LookupError`` means neither query ran.
        """
        target = normalize_netloc(self.server_url) if self.server_url else None
        for cols in ("url, token, username", "url, token"):
            try:
                if target:
                    rows = con.execute(
                        f"SELECT {cols} FROM Accounts ORDER BY lastVisited DESC"
                    ).fetchall()
                    row = next((r for r in rows if r[0] and normalize_netloc(r[0]) == target), None)
                else:
                    row = con.execute(
                        f"SELECT {cols} FROM Accounts ORDER BY lastVisited DESC LIMIT 1"
                    ).fetchone()
            except Exception:
                continue
            if row is None:
                return None
            padded = tuple(row) + (None,) * (3 - len(row))
            return padded[0], padded[1], padded[2]
        raise LookupError("accounts.db could not be queried")

    def _credentials_from_accounts_db(self, require_token: bool) -> bool:
        """The desktop client's accounts.db (Windows, Linux, macOS, seaf-cli)."""
        for db_path in get_candidate_db_paths("accounts.db"):
            row = None
            with open_live_sqlite_ro(db_path) as con:
                if con is None:
                    continue
                try:
                    row = self._account_row(con)
                except LookupError:
                    continue
            if row and row[0] and (row[1] or not require_token):
                # The account's URL is the one the desktop client works with,
                # path included when the server sits under one; the URL on the
                # command line can only name host and port.  Same authority, so
                # the token's scope is unchanged -- only the path is learned.
                if not self.server_url or normalize_netloc(row[0]) == normalize_netloc(
                    self.server_url
                ):
                    self.server_url = row[0].rstrip("/")
                self._adopt_token(row[1], "the token in the Seafile client's accounts.db")
                if row[2] and not self.username:
                    self.username = row[2]
                return True
        return False

    def _missing_credentials_message(
        self,
        require_token: bool,
        config_path: Path,
        cfg_server: str | None,
        cfg_token: str | None,
    ) -> str:
        """Why no credentials were found, naming every source that looked usable.

        A skipped token that is not mentioned reads as "no credentials exist",
        and the user goes hunting for one that is already set.
        """
        if self.server_url:
            env_server = os.environ.get("SEAFILE_SERVER")
            env_token = os.environ.get("SEAFILE_TOKEN")
            skipped: list[str] = []
            if env_token and not env_server:
                skipped.append(
                    "SEAFILE_TOKEN was set without SEAFILE_SERVER, so it was not used: "
                    "a token is only sent to the server it names, and a URL may name "
                    "any server. Set SEAFILE_SERVER to the server the token belongs to"
                )
            elif env_server and env_token and normalize_netloc(env_server) != normalize_netloc(self.server_url):
                skipped.append(
                    f"SEAFILE_SERVER names {normalize_netloc(env_server)}, so "
                    "SEAFILE_SERVER/SEAFILE_TOKEN were not used for this server; "
                    "they are only sent to the server they name"
                )
            if cfg_token and not cfg_server:
                skipped.append(
                    f"{config_path} holds a token but no \"server\", so the token was "
                    "not used: a token is only sent to the server it names"
                )
            detail = " (" + "; ".join(skipped) + ")" if skipped else ""
            return (
                f"Could not find Seafile credentials for '{self.server_url}'{detail}. "
                "Set SEAFILE_TOKEN or configure ~/.git-seafile.json"
            )
        if require_token:
            return (
                "Could not find Seafile credentials. Set SEAFILE_SERVER and SEAFILE_TOKEN "
                "or configure ~/.git-seafile.json"
            )
        return (
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
        adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

    def _transfer_session(self, url: str) -> requests.Session:
        """Pick the session to send a file transfer through.

        Both sessions share pooling, TLS/proxy settings and the retry adapter;
        they differ only in whether the Seafile account token rides along.
        """
        if _transfer_authority(url) == _transfer_authority(self.server_url):
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

        The authority comparison goes through :func:`normalize_netloc`, so two
        spellings of one server (``HTTP://Host:443`` vs ``https://host``) are
        the same place -- a user-typed ``SEAFILE_SERVER`` keeps its case, and
        the link the server returns need not match it character for character.
        A genuinely different endpoint still compares different: a file server
        on ``host:8082`` is not ``host``, and ``host:8082`` over http stays
        ``host:8082``.
        """
        if not _link_authority_is_sane(raw_url):
            raise SeafileAPIError(
                "the server returned a transfer link with an unreadable host "
                f"({self._error_origin(raw_url)}); refusing to send anything to it"
            )
        srv = urlparse(self.server_url)
        tgt = urlparse(raw_url)

        if not tgt.netloc or self.force_file_host:
            path = tgt.path
            # A server served under a path (``https://host/seafile``) completes
            # a relative link under that path too: the proxy only routes the
            # prefixed form.  A link that already carries the prefix is left.
            prefix = srv.path.rstrip("/")
            if prefix and not (path == prefix or path.startswith(prefix + "/")):
                path = prefix + (path if path.startswith("/") else "/" + path)
            return urlunparse(
                (srv.scheme, srv.netloc, path, tgt.params, tgt.query, tgt.fragment)
            )

        if (
            normalize_netloc(tgt.netloc) == normalize_netloc(srv.netloc)
            and tgt.scheme == "http"
            and srv.scheme == "https"
        ):
            return urlunparse(
                ("https", tgt.netloc, tgt.path, tgt.params, tgt.query, tgt.fragment)
            )

        return raw_url

    def get_repo_id(self, name_or_id: str) -> str:
        """Resolve a library name or id to its repo UUID.

        Refuses an encrypted library: the web API serves its files only after a
        per-library password has been supplied, which this tool has no way to
        take, so every later call would fail with a status code that does not
        say why.
        """
        rid = self._resolve_repo_id(name_or_id)
        if rid in self._encrypted_repo_ids:
            raise SeafileAPIError(
                f"Seafile library '{name_or_id}' is encrypted. Encrypted libraries are not "
                "supported: the server will not serve their files over the web API without "
                "the library password. Use an unencrypted library for the remote."
            )
        return rid

    def _resolve_repo_id(self, name_or_id: str) -> str:
        if name_or_id in self._repos_cache:
            return self._repos_cache[name_or_id]

        url = f"{self.server_url}/api2/repos/"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                detail = resp.text.strip() or "Unauthorized"
                raise SeafileAPIError(
                    f"Failed to list repos: HTTP {resp.status_code} ({detail}).\n"
                    "Hint: check or refresh your token with 'git-remote-seafile check-auth' "
                    "or log in via the Seafile desktop client."
                )
            raise SeafileAPIError(f"Failed to list repos: HTTP {resp.status_code} {resp.text}")

        repos = resp.json()
        if not isinstance(repos, list):
            raise SeafileAPIError(f"Unexpected response from {url}: {resp.text}")

        # Check for direct ID match first (UUIDs are globally unique)
        for repo in repos:
            rid = repo.get("id", "")
            if rid:
                self._repos_cache[rid] = rid
                if repo.get("encrypted") is True:
                    self._encrypted_repo_ids.add(rid)
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
            # Reaching here means name_or_id is not cached (a hit at the top of
            # this method returns immediately), so there is no stale entry to
            # revalidate; the pop is defensive only.  The typo suggestion lives
            # here, at the one place the miss is detected, because every caller
            # -- RemoteHelper.__init__ included -- sees this message.  Building
            # it from the listing we already fetched avoids a second request.
            self._repos_cache.pop(name_or_id, None)
            names = [r.get("name") for r in repos if r.get("name")]
            suggestions = difflib.get_close_matches(name_or_id, names, n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            available = f" Available libraries: {', '.join(sorted(names))}." if names else ""
            raise SeafileAPIError(
                f"Seafile library not found: '{name_or_id}'.{hint}{available}"
            )

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
        data = resp.json()
        # Normalise here, once, so no caller has to guard: a non-array response
        # is a protocol error worth naming rather than an AttributeError three
        # frames later, and a non-object element cannot be a directory entry.
        if not isinstance(data, list):
            raise SeafileAPIError(
                f"Failed to list dir {clean_path}: expected a JSON array, got {type(data).__name__}"
            )
        # An entry without a string name cannot be addressed or compared, and
        # every caller indexes ``name``; drop it here once instead of crashing
        # in a caller with a bare KeyError.
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("name"), str)]


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
        if resp.status_code == 404:
            return False
        # Anything else is "cannot tell", and answering "cannot tell" as "does
        # not exist" is how a transient 5xx becomes a duplicate mkdir or a
        # skipped path.  list_dir() beside this raises for exactly these states,
        # so the two now agree: 200 is yes, 404 is no, anything else is an error
        # the caller can decide about.
        raise SeafileAPIError(
            f"Failed to check dir {clean_path}: HTTP {resp.status_code} {resp.text}"
        )

    def evict_known_dir(self, repo_id: str, dir_path: str) -> None:
        """Forget a cached directory (the caller deleted it on the server)."""
        clean_path = ("/" + dir_path.strip("/")).rstrip("/")
        self._known_dirs.discard((repo_id, clean_path))

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
        data = self.get_file_bytes(repo_id, file_path, max_bytes=MAX_TEXT_FILE_BYTES)
        if data is None:
            return None
        return data.decode("utf-8", errors="replace").strip()

    def get_file_bytes(
        self, repo_id: str, file_path: str, max_bytes: int | None = None
    ) -> bytes | None:
        """Download file content, or None if 404.

        With ``max_bytes`` a body the server *declares* larger than that is
        refused before it is read: refs, HEAD and lock tickets are a few dozen
        bytes, and a planted 200 MiB one was otherwise buffered whole.
        """
        clean_path = "/" + file_path.strip("/")
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to get download link for {clean_path}: HTTP {resp.status_code}")

        dl_url = resp.text.strip().strip('"')
        dl_url = self._adjust_url(dl_url)
        file_resp = self._xfer("get", dl_url, timeout=self.timeout, stream=max_bytes is not None)
        try:
            if file_resp.status_code == 404:
                return None
            if file_resp.status_code != 200:
                raise SeafileAPIError(
                    f"Failed to download {clean_path}: HTTP {file_resp.status_code} "
                    f"(GET {self._error_origin(dl_url)})"
                )
            if max_bytes is not None:
                try:
                    declared = int(file_resp.headers.get("Content-Length", ""))
                except (TypeError, ValueError):
                    declared = None
                if declared is not None and declared > max_bytes:
                    raise SeafileAPIError(
                        f"{clean_path} is {declared} bytes, more than the {max_bytes} this "
                        "file may hold; refusing to read it"
                    )
            if max_bytes is None:
                return file_resp.content
            # The declared length is the server's word.  A chunked body has
            # none, and a lying one is the server's to tell: bound what is
            # actually read, one byte past the cap being proof enough.
            body = bytearray()
            for chunk in file_resp.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise SeafileAPIError(
                        f"{clean_path} is larger than the {max_bytes} bytes this "
                        "file may hold; refusing to read it"
                    )
            return bytes(body)
        finally:
            if max_bytes is not None:
                try:
                    file_resp.close()
                except Exception:
                    pass

    def _xfer(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """One request to a transfer link, with its failures stripped of the link.

        The exception ``requests`` raises names the full request path, token
        included, and callers print exceptions.  ``from None`` also keeps the
        original out of an uncaught traceback.
        """
        try:
            return getattr(self._transfer_session(url), method)(url, **kwargs)
        except requests.RequestException as ex:
            raise SeafileAPIError(
                f"{type(ex).__name__} during {method.upper()} {self._error_origin(url)}: "
                f"{_scrub_transfer_error(ex, url)}"
            ) from None

    @staticmethod
    def _error_origin(url: str) -> str:
        """``scheme://netloc`` of a transfer URL, for error messages.

        The path of a download/upload link carries its own short-lived bearer
        token, so -- exactly as the upload error below already does -- only the
        origin is named: enough to see a wrong scheme or host, not enough to
        reuse the token that ended up in a CI log or a pasted issue.
        """
        origin = urlparse(url)
        return f"{origin.scheme}://{origin.netloc}"

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

        A bare ``str`` is *content* (encoded UTF-8), never a filename -- pass a
        ``pathlib.Path`` to upload a file from disk.  A path spelled as ``str``
        would otherwise be uploaded as a file whose body is its own name.
        """
        if isinstance(content, os.PathLike):
            with open(content, "rb") as fh:
                return self._upload(repo_id, parent_dir, filename, fh, replace, progress_callback)
        if isinstance(content, str):
            content = content.encode("utf-8")
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
        def _get_upload_url() -> str:
            link_url = f"{self.server_url}/api2/repos/{repo_id}/upload-link/?p={quote(clean_parent, safe='/')}"
            resp = self.session.get(link_url, timeout=self.timeout)
            if resp.status_code != 200:
                raise SeafileAPIError(f"Failed to get upload link for {clean_parent}: HTTP {resp.status_code} {resp.text}")
            return self._adjust_url(resp.text.strip().strip('"'))

        upload_url = _get_upload_url()

        # 2. Post file.
        # If payload is a file object with .read and .seek, stream it via StreamingMultipartFile
        # so requests streams chunks directly without buffering the whole file in RAM.
        if hasattr(payload, "read") and hasattr(payload, "seek"):
            cur = payload.tell()
            payload.seek(0, 2)
            file_size = payload.tell() - cur
            payload.seek(cur)

            if file_size >= CHUNKED_UPLOAD_MIN_BYTES:
                return self._upload_chunked(
                    repo_id, clean_parent, filename, payload, file_size, replace,
                    upload_url, progress_callback,
                )

        is_rewindable = (hasattr(payload, "read") and hasattr(payload, "seek")) or isinstance(
            payload, (bytes, bytearray)
        )
        max_attempts = UPLOAD_CHUNK_ATTEMPTS if is_rewindable else 1
        start_cur = payload.tell() if hasattr(payload, "tell") else 0

        up_resp = None
        last_error = ""
        for attempt in range(max_attempts):
            if attempt:
                time.sleep(0.3 * attempt)
                try:
                    upload_url = _get_upload_url()
                except Exception as ex:
                    last_error = f"Failed to refresh upload link: {ex}"
                    continue
            if hasattr(payload, "seek"):
                payload.seek(start_cur)

            try:
                if hasattr(payload, "read") and hasattr(payload, "seek"):
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
                    up_resp = self._xfer(
                        "post", upload_url, data=mp, headers=headers, timeout=self._upload_timeout()
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
                    up_resp = self._xfer(
                        "post", upload_url, data=mp, headers=headers, timeout=self._upload_timeout()
                    )
                else:
                    files = {"file": (filename, payload)}
                    data = {"parent_dir": clean_parent, "replace": "1" if replace else "0"}
                    up_resp = self._xfer(
                        "post", upload_url, files=files, data=data, timeout=self._upload_timeout()
                    )
                    if progress_callback:
                        try:
                            payload_len = len(payload) if hasattr(payload, "__len__") else 0
                            progress_callback(payload_len, payload_len)
                        except Exception:
                            pass
            except SeafileAPIError as ex:
                last_error = str(ex)
                continue

            if up_resp is not None:
                if up_resp.status_code in (200, 201):
                    return True
                last_error = (
                    f"HTTP {up_resp.status_code} "
                    f"(POST {self._error_origin(upload_url)}) {up_resp.text}"
                )
                if up_resp.status_code < 500:
                    break

        if up_resp is None or up_resp.status_code not in (200, 201):
            if up_resp is not None:
                # Name the endpoint that was actually hit.  A wrong scheme or host
                # here is the difference between a working push and an opaque
                # "HTTP 400", and the origin is enough to see it -- the path is
                # deliberately omitted because it carries the upload token.
                raise SeafileAPIError(
                    f"Failed to upload {filename} to {clean_parent}: "
                    f"HTTP {up_resp.status_code} "
                    f"(POST {self._error_origin(upload_url)}) {up_resp.text}"
                )
            raise SeafileAPIError(
                f"Failed to upload {filename} to {clean_parent}: {last_error} "
                f"(POST {self._error_origin(upload_url)})"
            )
        return True

    def _upload_timeout(self) -> tuple[float, float]:
        """``(connect, read)`` for an upload request; see the module constant."""
        return (self.timeout, max(self.timeout, UPLOAD_READ_TIMEOUT_SECONDS))

    def _upload_chunked(
        self,
        repo_id: str,
        parent_dir: str,
        filename: str,
        fh: Any,
        file_size: int,
        replace: bool,
        upload_url: str,
        progress_callback: Any = None,
    ) -> bool:
        """Upload *fh* as ``Content-Range`` chunks and prove the result is whole.

        The server assembles the chunks under *filename* and the file appears
        when the last one lands.  A server (or proxy) that ignores
        ``Content-Range`` would instead *replace* the file with each chunk in
        turn, ending with only the last piece -- so the size is checked against
        the listing before this returns True, and a mismatch is an error rather
        than a quietly truncated pack.
        """
        start0 = fh.tell()
        url = upload_url + ("&" if "?" in upload_url else "?") + "ret-json=1"
        session = self._transfer_session(upload_url)
        safe_name = _sanitize_header_param(filename)
        offset = 0
        while offset < file_size:
            length = min(UPLOAD_CHUNK_BYTES, file_size - offset)
            last_error = ""
            for _attempt in range(UPLOAD_CHUNK_ATTEMPTS):
                fh.seek(start0 + offset)

                def chunk_progress(done: int, total: int, _base: int = offset) -> None:
                    if progress_callback is not None:
                        try:
                            progress_callback(_base + done, file_size)
                        except Exception:
                            pass

                mp = StreamingMultipartFile(
                    fields={"parent_dir": parent_dir, "replace": "1" if replace else "0"},
                    file_field="file",
                    filename=filename,
                    file_obj=fh,
                    file_size=length,
                    progress_callback=chunk_progress,
                )
                headers = {
                    "Content-Type": mp.content_type,
                    "Content-Length": str(len(mp)),
                    "Content-Range": f"bytes {offset}-{offset + length - 1}/{file_size}",
                    "Content-Disposition": f'attachment; filename="{safe_name}"',
                }
                try:
                    resp = session.post(url, data=mp, headers=headers, timeout=self._upload_timeout())
                except requests.RequestException as ex:
                    last_error = f"{type(ex).__name__}: {_scrub_transfer_error(ex, upload_url)}"
                    continue
                if resp.status_code == 200:
                    last_error = ""
                    break
                last_error = f"HTTP {resp.status_code} {resp.text[:200]}"
                if resp.status_code < 500:
                    break
            if last_error:
                raise SeafileAPIError(
                    f"Failed to upload {filename} to {parent_dir}: chunk bytes "
                    f"{offset}-{offset + length - 1} of {file_size}: {last_error} "
                    f"(POST {self._error_origin(upload_url)})"
                )
            offset += length

        entries = self.list_dir(repo_id, parent_dir)
        landed = next((e for e in entries if e.get("name") == filename), None)
        got = landed.get("size") if landed else None
        if got != file_size:
            raise SeafileAPIError(
                f"Chunked upload of {filename} to {parent_dir} finished but the server holds "
                f"{got} bytes instead of {file_size}; the server may not support Content-Range."
            )
        return True

    def download_file_to(
        self,
        repo_id: str,
        file_path: str,
        dest: Any,
        progress_callback: Any = None,
        max_bytes: int | None = None,
    ) -> bool:
        """Stream a remote file to *dest* without holding it in memory.

        Returns False when the object does not exist.  ``get_file_bytes`` stays
        for small payloads (packfiles, refs); this is the path for LFS objects,
        where the whole point is that they may not fit in RAM.  Without
        ``stream=True`` `requests` reads the entire body into memory before the
        first chunk is written, which is the bug this exists to avoid.

        A body shorter than the declared ``Content-Length`` is a failure, not a
        small file, and raises rather than returning True -- a truncated object
        written into the LFS store is indistinguishable from a complete one
        afterwards.  Any other mid-stream failure leaves a partial file at
        *dest*: the method does not unlink it, because *dest* may be a
        caller-managed path.  Callers must therefore treat *dest* as untrusted
        after an exception -- every current caller stages into a temp/scratch
        location and verifies the byte count before use.

        ``max_bytes`` is for a caller that already knows how big the object must
        be (a restore from a manifest): the stream is cut off the moment it goes
        past that, so a hostile manifest that declares a small size for a huge
        object cannot fill the disk before the size is compared.
        """
        clean_path = "/" + file_path.strip("/")
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise SeafileAPIError(f"Failed to get download link for {clean_path}: HTTP {resp.status_code}")

        dl_url = self._adjust_url(resp.text.strip().strip('"'))
        file_resp = self._xfer("get", dl_url, timeout=self.timeout, stream=True)
        try:
            if file_resp.status_code == 404:
                return False
            if file_resp.status_code != 200:
                raise SeafileAPIError(
                    f"Failed to download {clean_path}: HTTP {file_resp.status_code} "
                    f"(GET {self._error_origin(dl_url)})"
                )

            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            total_size = 0
            if "Content-Length" in file_resp.headers:
                try:
                    total_size = int(file_resp.headers["Content-Length"])
                except Exception:
                    total_size = 0
            if max_bytes is not None and total_size > max_bytes:
                raise SeafileAPIError(
                    f"{clean_path} is {total_size} bytes on the server, more than the "
                    f"{max_bytes} expected; not downloading it"
                )
            transferred = 0
            with open(dest_path, "wb") as fh:
                for chunk in file_resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        if max_bytes is not None and transferred + len(chunk) > max_bytes:
                            raise SeafileAPIError(
                                f"{clean_path} is larger than the {max_bytes} bytes expected; "
                                "download stopped"
                            )
                        fh.write(chunk)
                        transferred += len(chunk)
                        if progress_callback:
                            try:
                                progress_callback(transferred, total_size or transferred)
                            except Exception:
                                pass
            # A short body is a *failed* download, not a small one.  The server
            # declares the length up front, so a stream that ends early is a
            # truncated transfer -- a dropped connection, a proxy that cut the
            # response -- and returning True would hand the caller a partial
            # object with nothing to notice it by.  The LFS path is why this
            # matters: a truncated object is written straight into the store.
            # Checked only when the server declared a length (a chunked response
            # has none, and there is then nothing to compare against).
            if total_size and transferred != total_size:
                raise SeafileAPIError(
                    f"Truncated download of {clean_path}: got {transferred} of "
                    f"{total_size} bytes"
                )
            return True
        finally:
            file_resp.close()

    def rename_entry(self, repo_id: str, path: str, new_name: str) -> bool:
        """Rename a file or directory in place (same parent).  False when refused,
        which includes a name already taken."""
        clean_path = "/" + path.strip("/")
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self.session.post(
            url, data={"operation": "rename", "newname": new_name}, timeout=self.timeout
        )
        return resp.status_code in (200, 201)

    def copy_entries(self, repo_id: str, src_parent: str, names: list[str], dst_parent: str) -> bool:
        """Copy files or directories server-side, within one library.

        Moves metadata, not bytes: the copies share the originals' blocks (measured
        on CE 13.0: 20-30 ms for a 1,500-entry tree, 0 bytes added).  The
        destination directory must already exist and keeps the source names.
        """
        url = f"{self.server_url}/api/v2.1/repos/sync-batch-copy-item/"
        resp = self.session.post(
            url,
            json={
                "src_repo_id": repo_id,
                "src_parent_dir": "/" + src_parent.strip("/"),
                "src_dirents": list(names),
                "dst_repo_id": repo_id,
                "dst_parent_dir": "/" + dst_parent.strip("/"),
            },
            timeout=self.timeout,
        )
        return resp.status_code == 200

    def delete_entry(self, repo_id: str, path: str) -> bool:
        """Delete file or directory at path."""
        clean_path = "/" + path.strip("/")
        # Seafile's /file/ endpoint deletes both files and directories,
        # while /dir/ returns 404 when deleting a file. Try /file/ first.
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self.session.delete(url, timeout=self.timeout)
        if resp.status_code == 200:
            self._forget_dirs_under(repo_id, clean_path)
            return True
        dir_url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={quote(clean_path, safe='/')}"
        resp_dir = self.session.delete(dir_url, timeout=self.timeout)
        if resp_dir.status_code == 200:
            self._forget_dirs_under(repo_id, clean_path)
            return True
        return False

    def _forget_dirs_under(self, repo_id: str, path: str) -> None:
        """Drop the "this directory exists" memory for *path* and everything below it.

        ``mkdir_p`` trusts that memory; after a delete it would otherwise skip
        re-creating a directory the server no longer has (and the next copy or
        upload into it would be refused).
        """
        prefix = path.rstrip("/") + "/"
        self._known_dirs = {
            (rid, known) for rid, known in self._known_dirs
            if not (rid == repo_id and (known == path.rstrip("/") or known.startswith(prefix)))
        }
