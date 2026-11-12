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
from typing import Any, Callable
import uuid
from urllib.parse import quote, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url as _urllib3_parse_url
from urllib3.util.retry import Retry

from .git_util import get_git_config_bool
from .seafile_paths import get_candidate_db_paths
from .url import is_loopback_host, normalize_netloc


def _denied_hint(resp: requests.Response, verb: str) -> str:
    """What a 403 on a folder means, for the end of the error message; empty for other statuses.

    A Pro folder permission (read-only, preview, cloud-edit, invisible) refuses with a bare
    ``Permission denied.``, and the message it surfaces in starts with whatever the helper was
    doing at the time -- "Failed to acquire lock: ..." on a push -- which points the reader at
    the lock rather than at the folder's permissions.
    """
    if resp.status_code != 403:
        return ""
    return (
        f". This account may not {verb} here: check the permissions on the library and on "
        "this folder (a folder permission applies to everything below it)"
    )


#: The folders the helper and the snapshot tool lay out themselves, by name: the
#: repository's lock directory and git layout, the LFS store and the payload store.
#: Only a copy of one of these is removed by ``mkdir_p``.  Every other folder it
#: creates is one a person named -- the repository's own path, a branch namespace
#: (``refs/heads/feature/``), a payload view -- and may sit among their own files,
#: so a copy of it is named and left (the maintainer, after review 4f1cdd4).
_LAYOUT_DIR_NAMES = frozenset({
    ".git-lock.d", "refs", "heads", "tags", "objects", "pack", "lfs", "payload",
})

_HEX2 = re.compile(r"[0-9a-f]{2}")


def _is_layout_dir(parent: str, name: str, root: str | None = None) -> bool:
    """True for a folder the helper or the snapshot tool lays out itself.

    The fixed names, and the two-hex fan-out folders under ``lfs/`` (two levels)
    and the payload's ``objects/`` (one).  With *root* (the repository's own path),
    only a folder strictly inside it: a repository a person named ``lfs`` or
    ``db/ca`` is theirs, and a copy of it is named, not removed (review 06cc623:
    opus G3.1, glm G3.1, gemini G3.1a).
    """
    if root is not None:
        path = f"{parent.rstrip('/')}/{name}"
        top = "/" + root.strip("/")
        if not path.startswith(top.rstrip("/") + "/"):
            return False
    if name in _LAYOUT_DIR_NAMES:
        return True
    above = parent.rstrip("/").rpartition("/")[2]
    return _HEX2.fullmatch(name) is not None and (
        above in ("lfs", "objects") or _HEX2.fullmatch(above) is not None
    )


def _is_seafile_copy_of(name: str, candidate: str) -> bool:
    """True when *candidate* is how Seafile names a second ``mkdir`` of *name*.

    The number goes before the last extension: ``.git-lock.d`` -> ``.git-lock (1).d``
    (measured, Pro 13.0.29), ``pack`` -> ``pack (1)``.  Both shapes are accepted, so a
    deployment that appends the number instead is caught too.
    """
    shapes = [(name, "")]
    stem, dot, ext = name.rpartition(".")
    if dot and stem:
        shapes.append((stem, "." + ext))
    return any(
        re.fullmatch(re.escape(s) + r" \(\d+\)" + re.escape(e), candidate) is not None
        for s, e in shapes
    )
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


class ListedButUnreadableError(SeafileAPIError):
    """A file its folder lists could not be read (a 404 or an empty body from the file host)."""


class SeafilePermanentError(SeafileAPIError):
    """An API failure that asking again cannot change.

    A missing library, a refused token and an encrypted library are the same
    on the second try; retrying them only prints a Warning: that makes a plain
    mistake look like a flaky server.  Subclass of :class:`SeafileAPIError`, so
    every existing handler keeps working.
    """


class TransferAborted(Exception):
    """Raised by a progress callback to stop the transfer it observes.

    The streaming upload swallows ordinary callback errors (a monitoring
    hiccup must not kill a multi-gigabyte push), so a caller that *means* to
    abort -- the helper, when the lock lease is provably lost and the rest of
    the upload is doomed -- raises this instead, and the two swallow sites let
    it through.
    """


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
                except TransferAborted:
                    raise
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

#: How long a ref-sized write (a 41-byte file under the lock, behind a fence that
#: proved ownership moments before) waits for the server's reply.  The 600 s above
#: is for a multi-gigabyte upload; applied to a ref it widened the gap between the
#: fence and the write to minutes, and a resend after a timeout could land after
#: a competitor's write.  A server that has not answered a 41-byte write in this
#: long is not going to be waited on; the caller reports the ref as unknown.
REF_WRITE_READ_TIMEOUT_SECONDS = 20


class _ScopedSession(requests.Session):
    """A session whose ``Authorization`` header never leaves the Seafile server's URL.

    ``requests`` drops the header on a redirect to another host, scheme or port, but
    keeps it on the same host: a server at ``https://host/seafile`` that redirects
    (an SSO middleware, an open redirect) to ``https://host/other-app`` handed that
    app the account token (review 06cc623, gemini C1).  *scope* is the server URL;
    a redirect outside it goes on without the header.
    """

    def __init__(self, scope: Callable[[], str]) -> None:
        super().__init__()
        self.scope = scope   # read at redirect time: the server URL may be loaded later

    def rebuild_auth(self, prepared_request: Any, response: Any) -> None:
        super().rebuild_auth(prepared_request, response)
        url = str(prepared_request.url or "")
        scope = self.scope().rstrip("/")
        if not scope or (url != scope and not url.startswith(scope + "/")):
            prepared_request.headers.pop("Authorization", None)


#: The longest one retry sleeps for a server's ``Retry-After`` (fable-C1).  urllib3 honours the
#: header for 429, 500 and 503 with no bound of its own the client sets (one ``list_dir`` against
#: ``Retry-After: 4`` took 12 s, a ``delete_entry`` 24 s), and the thread that sleeps is the one
#: that renews the push lease.  A throttle answers "one second" in practice (measured, AA23).
RETRY_AFTER_CAP_SECONDS = 5.0
_RETRY_AFTER_NOTED = False


class _CappedRetry(Retry):
    """``Retry`` that never sleeps longer than ``RETRY_AFTER_CAP_SECONDS`` for ``Retry-After``."""

    def get_retry_after(self, response: Any) -> float | None:
        global _RETRY_AFTER_NOTED
        wait = super().get_retry_after(response)
        if wait is None:
            return None
        if wait > RETRY_AFTER_CAP_SECONDS:
            wait = RETRY_AFTER_CAP_SECONDS
        if wait > 0 and not _RETRY_AFTER_NOTED:
            _RETRY_AFTER_NOTED = True
            sys.stderr.write(
                "Note: the server asked this client to slow down (Retry-After); each request "
                f"waits at most {RETRY_AFTER_CAP_SECONDS:g} s and is tried again.\n"
            )
            sys.stderr.flush()
        return wait


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
        self.session: requests.Session = _ScopedSession(lambda: self.server_url)
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
        #: The repository path whose inside the caller lays out (the helper and the
        #: LFS agent set it); ``None`` judges a folder by its name alone.
        self.layout_root: str | None = None

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

    @property
    def server_time_measured(self) -> bool:
        """True once a ``Date`` header has been seen, so ``get_server_time`` is the server's clock."""
        return self._server_time_offset is not None

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
        if is_loopback_host(host):
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
                "Set SEAFILE_TOKEN or configure ~/.git-seafile.json "
                "(or run 'git-remote-seafile login')"
            )
        if require_token:
            return (
                "Could not find Seafile credentials. Set SEAFILE_SERVER and SEAFILE_TOKEN "
                "or configure ~/.git-seafile.json (or run 'git-remote-seafile login')"
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
        retry = _CappedRetry(
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

        The authority comparison goes through :func:`_transfer_authority` -- the
        same parser the token decision uses -- so two spellings of one server
        (``HTTP://Host:443`` vs ``https://host``) are the same place *and* the
        two decisions cannot disagree.  They used to: the token compared
        ``http://host:443`` to ``https://host`` as equal (443 folded away) while
        the upgrade compared them as different (443 kept), so a link the proxy
        mis-spelled with the port was fetched in cleartext *with* the account
        token attached -- the one place the "token only to the host it names,
        over the scheme it was configured for" rule was not the rule the code
        applied (REVIEW.iq3_s C1, reproduced).  A genuinely different endpoint
        still compares different: a file server on ``host:8082`` is not
        ``host``, and ``host:8082`` over http stays ``host:8082``.
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
            _transfer_authority(raw_url) == _transfer_authority(self.server_url)
            and tgt.scheme == "http"
            and srv.scheme == "https"
        ):
            return urlunparse(
                ("https", tgt.netloc, tgt.path, tgt.params, tgt.query, tgt.fragment)
            )

        return raw_url

    def _json_or_none(self, path: str) -> Any:
        """``GET <server><path>`` as JSON, or ``None`` for any failure (a question
        with a safe default, never a reason to stop on its own)."""
        try:
            resp = self.session.get(f"{self.server_url}{path}", timeout=self.timeout)
            return resp.json() if resp.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return None

    def hidden_history_risk(self, repo_id: str) -> str | None:
        """Why this account may not see the library's whole history; ``None`` when it does.

        On Seafile Professional a folder permission can hide part of a library from
        an account that is not its owner: an ``invisible`` folder is left out of its
        parent's listing (measured on Pro 13.0.29, ROADMAP B2).  A ref under such a
        folder reads as absent, and a ``gc`` or ``--payload-delete`` run by that
        account would delete what only that ref reaches.  Community Edition has no
        folder permissions, and the owner is never restricted by them.  On Pro, an
        account or owner the server does not report is a risk too: cannot tell is
        not "safe".
        """
        # Only an answer that names the edition settles it: a server-info that failed
        # or came back without a feature list is "cannot tell", which on this path is a
        # risk, not CE -- a proxy blocking that one URL switched the guard off (review
        # 06cc623, deepseek G2.2 and glm G2.2).  Asked twice, since a refusal costs a
        # rerun on CE.
        info = self._json_or_none("/api2/server-info/")
        if not isinstance(info, dict) or not isinstance(info.get("features"), list):
            info = self._json_or_none("/api2/server-info/")
        features = info.get("features") if isinstance(info, dict) else None
        if not isinstance(features, list):
            return ("the server did not say which edition it runs (/api2/server-info/ gave no "
                    "feature list), and on Seafile Professional a folder permission can hide "
                    "refs from an account that is not the owner")
        if "seafile-pro" not in features:
            return None
        account = self._json_or_none("/api2/account/info/")
        library = self._json_or_none(f"/api2/repos/{repo_id}/")
        email = account.get("email") if isinstance(account, dict) else None
        owner = library.get("owner") if isinstance(library, dict) else None
        # For its owner the library's info answers ``"owner": "self"``, not the email (measured on Pro
        # 13.0.29: the first version compared emails and refused the owner).
        if owner == "self" or (isinstance(email, str) and email and email == owner):
            return None
        # ``/api2/repos/<id>/`` answers ``"share"`` to *every* account that is not the owner, so
        # it cannot say who does own the library or that a group does (measured on Pro 13.0.29,
        # ROADMAP Z22).  The v2.1 detail names the owner: a real email, or ``<id>@seafile_group``
        # for a group (department) library, the same to a member and to the admin who made it.
        detail = self._json_or_none(f"/api/v2.1/repos/{repo_id}/")
        named = detail.get("owner_email") if isinstance(detail, dict) else None
        if isinstance(named, str) and named:
            owner = named
            # The detail can name this very account (the first check saw only ``share``): refusing
            # the owner is the failure Z22's first version had (review 8eb416b, gemini G2.1).
            if isinstance(email, str) and email and email == owner:
                return None
        if isinstance(owner, str) and is_group_owner(owner):
            return (f"this library is owned by a group ({owner}), so no account is its owner, "
                    "and on Seafile Professional a folder permission can hide refs from any "
                    "member")
        if not (isinstance(email, str) and email and isinstance(owner, str) and owner):
            return ("the server did not say which account owns this library, and on Seafile "
                    "Professional a folder permission can hide refs from an account that is not "
                    "the owner")
        return (f"this account ({email}) is not the library's owner ({owner}), and on Seafile "
                "Professional a folder permission can hide refs from it (an 'invisible' folder "
                "is left out of its listing)")

    def get_repo_id(self, name_or_id: str) -> str:
        """Resolve a library name or id to its repo UUID.

        Refuses an encrypted library: the web API serves its files only after a
        per-library password has been supplied, which this tool has no way to
        take, so every later call would fail with a status code that does not
        say why.
        """
        rid = self._resolve_repo_id(name_or_id)
        if rid in self._encrypted_repo_ids:
            raise SeafilePermanentError(
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
                raise SeafilePermanentError(
                    f"Failed to list repos: HTTP {resp.status_code} ({detail}).\n"
                    "Hint: check or refresh your token with 'git-remote-seafile check-auth' "
                    "or log in again with 'git-remote-seafile login' (or the Seafile desktop client)."
                )
            raise SeafileAPIError(f"Failed to list repos: HTTP {resp.status_code} {self._said(resp.text)}")

        repos = resp.json()
        if not isinstance(repos, list):
            raise SeafileAPIError(f"Unexpected response from {url}: {resp.text}")
        # An element that is not an object cannot be a library; a hostile or
        # broken answer used to escape as AttributeError from `.get` below,
        # shown to the user as "'str' object has no attribute 'get'"
        # (REVIEW.sonnet-5.5 C5, reproduced).
        repos = [repo for repo in repos if isinstance(repo, dict)]

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
            # No close match usually means the library was never made: nothing
            # creates one on first push, and the listing alone does not say so.
            create = (
                "" if suggestions
                else " If it does not exist yet, create it in the Seafile web UI, or run "
                     "'git-remote-seafile doctor <seafile://url> --create-library'."
            )
            raise SeafilePermanentError(
                f"Seafile library not found: '{name_or_id}'.{hint}{available}{create}"
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
            raise SeafileAPIError(
                f"Failed to list dir {clean_path}: HTTP {resp.status_code} {self._said(resp.text)}"
                + _denied_hint(resp, "see")
            )
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
            f"Failed to check dir {clean_path}: HTTP {resp.status_code} {self._said(resp.text)}"
            + _denied_hint(resp, "see")
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

            # What the parent held before: a `<name> (N)` folder already there is not a
            # copy this mkdir made -- a person's own, or another repository named that
            # way mid first push -- and was deleted as one (review 4f1cdd4: opus-5.5,
            # deepseek-v4.1-flash and glm-5.3 G2.1).  A listing that fails leaves the
            # set unknown, and then nothing is removed.
            parent = current.rpartition("/")[0] or "/"
            try:
                before: set[str] | None = {
                    str(e.get("name")) for e in self.list_dir(repo_id, parent)
                }
            except (SeafileAPIError, requests.RequestException):
                before = None
            url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={quote(current, safe='/')}"
            resp = self.session.post(url, data={"operation": "mkdir"}, timeout=self.timeout)
            if resp.status_code in (200, 201):
                # Seafile's mkdir is create-or-duplicate: on a name a concurrent
                # client just took it answers success and creates a copy beside
                # it (measured on CE 13.0.28: six simultaneous creates made three
                # directories, no errors).  The copy is named before the last
                # extension, "<stem> (1)<ext>": two first pushes at once on Pro
                # 13.0.29 left ".git-lock (1).d" beside ".git-lock.d".  Nothing
                # ever addresses the copy -- every later request names `current`
                # by path, so both clients use the one directory (the same run:
                # each waited on the other's lock ticket) -- and the copy is
                # only litter.  An empty copy of one of the helper's own layout
                # folders is removed; a copy of a folder a person named, or one
                # that holds anything, is not ours to judge and is named instead.  One listing per
                # *created* directory (two, with the one before), so an established
                # repository pays nothing.
                try:
                    copies = [
                        str(e.get("name")) for e in self.list_dir(repo_id, parent)
                        if e.get("type") == "dir" and str(e.get("name")) not in (before or ())
                        and _is_seafile_copy_of(part, str(e.get("name")))
                    ]
                except (SeafileAPIError, requests.RequestException):
                    copies = []
                for copy in copies:
                    copy_path = f"{parent.rstrip('/')}/{copy}"
                    if before is None:
                        # The listing before the mkdir failed, so whether this copy is
                        # new cannot be told: never removed, but no longer unmentioned
                        # either (review 06cc623, opus G3.1).
                        sys.stderr.write(
                            f"Warning: {copy_path} is beside {current}; it may be a numbered "
                            "copy two clients made by creating the folder at once, or a "
                            "folder that was there before. Nothing uses it; check it in the "
                            "web UI\n"
                        )
                        sys.stderr.flush()
                        continue
                    if not _is_layout_dir(parent, part, self.layout_root):
                        sys.stderr.write(
                            f"Warning: {copy_path} appeared beside {current} as it was created "
                            "(Seafile makes a numbered copy when two clients create a folder at "
                            "once); nothing uses it. It is not one of the helper's own folders, "
                            "so it is left for you to delete in the web UI\n"
                        )
                        sys.stderr.flush()
                        continue
                    try:
                        removed = (
                            self.list_dir(repo_id, copy_path) == []
                            and self.delete_entry(repo_id, copy_path)
                        )
                    except (SeafileAPIError, requests.RequestException):
                        removed = False
                    if not removed:
                        # The other client of the pair saw the same copy and may have
                        # removed it first: gone is what was wanted, not a warning.
                        try:
                            removed = not self.dir_exists(repo_id, copy_path)
                        except (SeafileAPIError, requests.RequestException):
                            removed = False
                    if not removed:
                        sys.stderr.write(
                            f"Warning: {copy_path} was created beside {current} by two clients "
                            "creating it at once and could not be removed; nothing uses it, "
                            "and it can be deleted in the web UI\n"
                        )
                        sys.stderr.flush()
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
                f"Failed to create directory {current}: HTTP {resp.status_code} {self._said(resp.text)}"
                + _denied_hint(resp, "write")
            )
        return True

    def _said(self, text: object, *secrets: str) -> str:
        """A server's reply made safe to put in a message: one line, printable, short, no secrets.

        Replies went into messages whole: a proxy's HTML error page flooded the line, and a
        hostile or broken server could put terminal escape sequences (clear screen, set the
        title) on the person's terminal.  Whitespace is folded, any other control character
        becomes ``?``, the result is cut at 200 characters, and the account token and any
        string in *secrets* (a transfer link's path and query, which are bearer tokens) are
        replaced if the server echoed them back.
        """
        raw = text.decode("utf-8", "replace") if isinstance(text, (bytes, bytearray)) else str(text or "")
        for secret in (self.token, *secrets):
            if secret and len(secret) >= 8:
                raw = raw.replace(secret, "<removed>")
        clean = "".join(ch if ch.isprintable() else (" " if ch.isspace() else "?") for ch in raw)
        return " ".join(clean.split())[:200]

    @staticmethod
    def _body_sample(resp: requests.Response) -> bytes:
        """Up to 1 KiB of a response body, without reading a streamed one to its end."""
        try:
            if getattr(resp, "_content_consumed", False):
                return bytes(resp.content[:1024])
            return bytes(resp.raw.read(1024, decode_content=True))
        except Exception:
            return b""

    @staticmethod
    def _link_secrets(url: str) -> tuple[str, ...]:
        """The parts of a transfer link that are bearer tokens: its path and its query."""
        parts = urlparse(url)
        return tuple(x for x in (parts.path, parts.query) if x)

    def _file_is_under_a_missing_folder(self, repo_id: str, clean_path: str, resp: requests.Response) -> bool:
        """True when a failed file read only means that the file's folder does not exist.

        Seafile Professional 13.0.29 answers a file read under a missing folder with 403 (one
        level missing) or a 500 page (two or more), where Community Edition answers 404
        (issue 6, measured on both; the owner and a shared user alike, under every
        sub-folder permission).  The first push to a new path reads ``HEAD`` and the ref
        files before any folder exists, so it stopped on the first of them.

        The 403 body is the same for "no permission" and "no folder", so the file read
        cannot say which.  The directory listing can: a missing folder lists as 404 for
        anyone, and a folder that exists lists as 200, or 403 to a person who may not see it.
        Only a clean 404 for the file's own folder counts.  Anything else -- another status
        on the file (a proxy's 502 says nothing about folders), a listing that fails, a
        file at the top of the library -- leaves the original error, so a real permission
        problem or a flaky server is never read as "no such file": a ref wrongly read as
        absent is a ref a push may overwrite.
        """
        if resp.status_code not in (403, 500):
            return False
        folder = clean_path.rpartition("/")[0]
        if not folder:
            return False
        try:
            return not self.dir_exists(repo_id, folder)
        except (SeafileAPIError, requests.RequestException):
            return False

    def _download_link_error(self, clean_path: str, resp: requests.Response) -> SeafileAPIError:
        """The error for a file read the server answered with something other than 200 or 404.

        The message carries what the server said, and for a 403 where the folder is known
        to exist (or is unknown) it names the permissions, because a bare ``HTTP 403``
        reads as a permission problem and the person needs to know where to look.
        """
        said = self._said(resp.text)
        message = f"Failed to get download link for {clean_path}: HTTP {resp.status_code}"
        if said:
            message += f" {said}"
        if resp.status_code == 403:
            message += (
                ". This account may not read here: check the permissions on the library and "
                "on this folder. If they allow it, run 'git-remote-seafile doctor <url>' and "
                "send its output to the issue tracker"
            )
        return SeafileAPIError(message)

    def get_file_text(self, repo_id: str, file_path: str) -> str | None:
        """Download file and return as string, or None if not found.

        ``None`` means only that the download said so: for a state file whose
        absence decides something, use :func:`read_listed_text`.
        """
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
            if self._file_is_under_a_missing_folder(repo_id, clean_path, resp):
                return None
            raise self._download_link_error(clean_path, resp)

        dl_url = resp.text.strip().strip('"')
        dl_url = self._adjust_url(dl_url)
        file_resp = self._xfer("get", dl_url, timeout=self.timeout, stream=max_bytes is not None)
        try:
            if file_resp.status_code == 404:
                return None
            if file_resp.status_code != 200:
                said = self._said(self._body_sample(file_resp), *self._link_secrets(dl_url))
                raise SeafileAPIError(
                    f"Failed to download {clean_path}: HTTP {file_resp.status_code} "
                    f"(GET {self._error_origin(dl_url)})" + (f" {said}" if said else "")
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
        # An upload is never re-sent to wherever a 3xx points: requests would POST the
        # whole pack, payload object or ref there, past ``force_file_host`` and the
        # scheme check every transfer link goes through (review 06cc623, gemini C1).
        if method != "get":
            kwargs.setdefault("allow_redirects", False)
        try:
            resp = getattr(self._transfer_session(url), method)(url, **kwargs)
        except requests.RequestException as ex:
            raise SeafileAPIError(
                f"{type(ex).__name__} during {method.upper()} {self._error_origin(url)}: "
                f"{_scrub_transfer_error(ex, url)}"
            ) from None
        if method != "get" and 300 <= resp.status_code < 400:
            target = resp.headers.get("Location", "")
            raise SeafileAPIError(
                f"{method.upper()} {self._error_origin(url)} answered HTTP {resp.status_code}, "
                f"a redirect to {self._error_origin(target) if target else 'nowhere'}; an "
                "upload is not re-sent elsewhere. Check the server's SERVICE_URL / FILE_SERVER_ROOT"
            )
        return resp

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
        *,
        attempts: int | None = None,
        read_timeout: float | None = None,
    ) -> bool:
        """Upload file content to a parent directory.

        ``attempts`` and ``read_timeout`` override the defaults (three attempts
        for a rewindable payload, ``UPLOAD_READ_TIMEOUT_SECONDS``): a caller
        writing a ref wants one attempt and a short wait, because a resend after
        a timeout may land after somebody else's write.

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
        # Passed on only when set, so the default call keeps its old shape.
        options: dict[str, Any] = {}
        if attempts is not None:
            options["attempts"] = attempts
        if read_timeout is not None:
            options["read_timeout"] = read_timeout
        if isinstance(content, os.PathLike):
            with open(content, "rb") as fh:
                return self._upload(
                    repo_id, parent_dir, filename, fh, replace, progress_callback, **options
                )
        if isinstance(content, str):
            content = content.encode("utf-8")
        return self._upload(
            repo_id, parent_dir, filename, content, replace, progress_callback, **options
        )

    def _upload(
        self,
        repo_id: str,
        parent_dir: str,
        filename: str,
        payload: Any,
        replace: bool,
        progress_callback: Any = None,
        *,
        attempts: int | None = None,
        read_timeout: float | None = None,
    ) -> bool:
        clean_parent = ("/" + parent_dir.strip("/")).rstrip("/") or "/"
        self.mkdir_p(repo_id, clean_parent)

        # 1. Get upload link
        def _get_upload_url() -> str:
            link_url = f"{self.server_url}/api2/repos/{repo_id}/upload-link/?p={quote(clean_parent, safe='/')}"
            resp = self.session.get(link_url, timeout=self.timeout)
            if resp.status_code != 200:
                raise SeafileAPIError(
                    f"Failed to get upload link for {clean_parent}: HTTP {resp.status_code} "
                    f"{self._said(resp.text)}" + _denied_hint(resp, "write")
                )
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
        max_attempts = (
            attempts if attempts is not None
            else UPLOAD_CHUNK_ATTEMPTS if is_rewindable else 1
        )
        upload_timeout = self._upload_timeout(read_timeout)
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
                        "post", upload_url, data=mp, headers=headers, timeout=upload_timeout
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
                        "post", upload_url, data=mp, headers=headers, timeout=upload_timeout
                    )
                else:
                    files = {"file": (filename, payload)}
                    data = {"parent_dir": clean_parent, "replace": "1" if replace else "0"}
                    up_resp = self._xfer(
                        "post", upload_url, files=files, data=data, timeout=upload_timeout
                    )
                    if progress_callback:
                        try:
                            payload_len = len(payload) if hasattr(payload, "__len__") else 0
                            progress_callback(payload_len, payload_len)
                        except TransferAborted:
                            raise
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
                    f"(POST {self._error_origin(upload_url)}) "
                    f"{self._said(up_resp.text, *self._link_secrets(upload_url))}"
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
                    f"(POST {self._error_origin(upload_url)}) "
                    f"{self._said(up_resp.text, *self._link_secrets(upload_url))}"
                )
            raise SeafileAPIError(
                f"Failed to upload {filename} to {clean_parent}: {last_error} "
                f"(POST {self._error_origin(upload_url)})"
            )
        return True

    def _upload_timeout(self, read_timeout: float | None = None) -> tuple[float, float]:
        """``(connect, read)`` for an upload request; see the module constant.

        An explicit *read_timeout* is taken as given (a ref write is meant to be
        shorter than the 30 s request timeout, not floored to it).
        """
        if read_timeout is not None:
            return (self.timeout, read_timeout)
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
                        except TransferAborted:
                            raise
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
                last_error = f"HTTP {resp.status_code} {self._said(resp.text, *self._link_secrets(upload_url))}"
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
            if self._file_is_under_a_missing_folder(repo_id, clean_path, resp):
                return False
            raise self._download_link_error(clean_path, resp)

        dl_url = self._adjust_url(resp.text.strip().strip('"'))
        file_resp = self._xfer("get", dl_url, timeout=self.timeout, stream=True)
        try:
            if file_resp.status_code == 404:
                return False
            if file_resp.status_code != 200:
                said = self._said(self._body_sample(file_resp), *self._link_secrets(dl_url))
                raise SeafileAPIError(
                    f"Failed to download {clean_path}: HTTP {file_resp.status_code} "
                    f"(GET {self._error_origin(dl_url)})" + (f" {said}" if said else "")
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

    def create_library(self, name: str) -> str:
        """Create an unencrypted library called *name* and return its id."""
        resp = self.session.post(
            f"{self.server_url}/api2/repos/", data={"name": name}, timeout=self.timeout
        )
        if resp.status_code not in (200, 201):
            raise SeafileAPIError(
                f"Failed to create library '{name}': HTTP {resp.status_code} {self._said(resp.text)}"
            )
        try:
            body = resp.json()
        except ValueError:
            body = None
        rid = (body.get("repo_id") or body.get("id")) if isinstance(body, dict) else None
        if not isinstance(rid, str) or not rid:
            raise SeafileAPIError(
                f"The server created '{name}' but did not return its id; check the Seafile web UI"
            )
        self._repos_cache[name] = rid
        return rid

    def get_history_limit(self, repo_id: str) -> int | None:
        """How many days of history the library keeps (-1 all, 0 none), or None if unreadable."""
        try:
            resp = self.session.get(
                f"{self.server_url}/api2/repos/{repo_id}/history-limit/", timeout=self.timeout
            )
            days = resp.json().get("keep_days") if resp.status_code == 200 else None
        except (requests.RequestException, ValueError, AttributeError):
            return None
        return days if isinstance(days, int) and not isinstance(days, bool) else None

    def set_history_limit(self, repo_id: str, days: int) -> bool:
        """Keep *days* days of history (-1 all, 0 none); True when the server accepted it."""
        try:
            resp = self.session.put(
                f"{self.server_url}/api2/repos/{repo_id}/history-limit/",
                data={"keep_days": str(days)},
                timeout=self.timeout,
            )
        except requests.RequestException:
            return False
        return resp.status_code == 200

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


# -- state files: "listed" decides, not a download ------------------------------------
#
# Functions rather than methods so that any object with ``list_dir`` and
# ``get_file_text``/``get_file_bytes`` -- the tests' fakes included -- answers the same.


def is_group_owner(owner: str) -> bool:
    """True for the owner a Seafile Professional group (department) library reports.

    Such a library has no owning account: its owner reads as ``<id>@seafile_group``
    (reviewers report ``$group:...`` as well; both are taken, and the container
    measurement is ROADMAP B13).  Asking for "the owner" there names nobody.
    """
    return owner.endswith("@seafile_group") or owner.startswith("$group")


def owner_advice(risk: str, flag: str) -> str:
    """What to do about *risk*: run as the owner -- or, for a group library, check and pass *flag*."""
    if "owned by a group" in risk:
        return (f"no account can run it as the owner: check that no folder permission applies "
                f"to this account, then pass {flag}")
    return f"run it as the library's owner, or pass {flag} if no folder permission applies to this account"


def is_listed_file(client: Any, repo_id: str, file_path: str) -> bool:
    """True when *file_path*'s folder lists it as a file (a missing folder lists as empty).

    The listing is asked twice before giving up, and one that fails both times
    raises: "could not tell" is the caller's to decide, never "not there".
    """
    parent, _, leaf = ("/" + file_path.strip("/")).rpartition("/")
    try:
        entries = client.list_dir(repo_id, parent or "/")
    except Exception:
        entries = client.list_dir(repo_id, parent or "/")
    return any(
        isinstance(entry, dict) and entry.get("name") == leaf and entry.get("type") == "file"
        for entry in entries
    )


def _unreadable(file_path: str, got: object) -> ListedButUnreadableError:
    how = "the download answered not found" if got is None else "an empty body"
    return ListedButUnreadableError(f"{file_path} is listed but its content could not be read ({how})")


def read_listed_text(client: Any, repo_id: str, file_path: str) -> str | None:
    """A state file's text, stripped; ``None`` only when its folder does not list it.

    The tool's state files (refs, ``HEAD``, lock tickets, the payload markers) are
    never empty.  A download that comes back absent or empty while the folder still
    lists the file -- the file host answering 404 for one file after the API gave a
    link, or an empty body -- raises :class:`ListedButUnreadableError`: reading it as
    "not there" overwrote a branch (review 4f1cdd4) and erased the payload's
    history-rewrite fence (review 06cc623).
    """
    text = client.get_file_text(repo_id, file_path)
    if text:
        return text
    if is_listed_file(client, repo_id, file_path):
        raise _unreadable(file_path, text)
    return None


def read_listed_bytes(
    client: Any, repo_id: str, file_path: str, max_bytes: int | None = None
) -> bytes | None:
    """:func:`read_listed_text` for a file read as bytes (the payload's mark list)."""
    data = client.get_file_bytes(repo_id, file_path, max_bytes=max_bytes)
    if data and data.strip():
        return data
    if is_listed_file(client, repo_id, file_path):
        raise _unreadable(file_path, data)
    return None
