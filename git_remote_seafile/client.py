"""client.py - Seafile Web API v2.1 client for git-remote-seafile."""

from __future__ import annotations

from collections import Counter
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from collections.abc import Iterator
from typing import Any, Callable
import uuid
from urllib.parse import quote, unquote, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url as _urllib3_parse_url
from urllib3.util.retry import Retry

from . import gitcred
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

#: Folders of a blob store (``<store>/blobs/<xx>``, ``<store>/pins/<name>``), recognised only when the client says it is
#: working in a store root, so a repository that happens to hold a ``blobs`` or ``pins`` folder is unaffected.
_STORE_LAYOUT = re.compile(r"blobs|pins|blobs/[0-9a-f]{2}|pins/[^/]+")

#: Where a fan-out folder may sit, relative to the repository's own path: ``lfs/xx`` and
#: ``lfs/xx/yy`` (the LFS store), ``payload/objects/xx`` and ``objects/xx``.  A branch
#: namespace such as ``refs/heads/ab/cd`` is not one of them (review dfd2269, gemini C2).
_FAN_OUT = re.compile(r"(?:lfs/[0-9a-f]{2}(?:/[0-9a-f]{2})?|(?:payload/)?objects/[0-9a-f]{2})")


def _is_layout_dir(parent: str, name: str, root: str | None = None, store: bool = False) -> bool:
    """True for a folder the helper or the snapshot tool lays out itself.

    The fixed names, and the two-hex fan-out folders under ``lfs/`` (two levels)
    and the payload's ``objects/`` (one).  With *root* (the repository's own path),
    only a folder strictly inside it: a repository a person named ``lfs`` or
    ``db/ca`` is theirs, and a copy of it is named, not removed (review 06cc623:
    opus G3.1, glm G3.1, gemini G3.1a).
    """
    if root is not None:
        path = f"{parent.rstrip('/')}/{name}"
        prefix = ("/" + root.strip("/")).rstrip("/") + "/"          # "/" for a root at the library's top
        if not path.startswith(prefix):
            return False
    if name in _LAYOUT_DIR_NAMES:
        return True
    if root is not None:
        rel = path[len(prefix):]
        if store and _STORE_LAYOUT.fullmatch(rel) is not None:
            return True          # the blob store's own folders, and only under a store root (STORE.md D2, R9)
        return _FAN_OUT.fullmatch(rel) is not None
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


def _inside_scope(url: str, scope: str) -> bool:
    """True when *url* is *scope* or under it, read the way a server reads a path.

    A string prefix is not enough: ``requests`` normalises a relative ``Location`` but not an
    absolute one, so ``https://host/seafile/../other/x`` starts with the scope and is routed to
    another application by a front end that resolves dot segments (fable-C1).  The path is
    percent-decoded first and refused when it holds a dot segment, an encoded slash or a
    ``;`` parameter, however they are spelled.
    """
    from urllib.parse import unquote, urlsplit

    try:
        parts = urlsplit(url)
        base = urlsplit(scope)
    except ValueError:
        return False
    if parts.scheme != base.scheme or _transfer_authority(url) != _transfer_authority(scope):
        return False
    raw = parts.path
    decoded = raw
    for _ in range(3):                       # a doubly encoded dot segment
        decoded = unquote(decoded)
    if ";" in decoded or "\\" in decoded or any(seg in ("..", ".") for seg in decoded.split("/")):
        return False
    if "%2f" in raw.lower() or "%5c" in raw.lower():
        return False
    prefix = base.path.rstrip("/")
    return decoded == prefix or decoded.startswith(prefix + "/")


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
    # The link named after "url:" first: replacing the path first left "url: <transfer" for the
    # pattern to match and a stray "link>" behind it (gemini-4-hydrogen-G0.2).
    text = _LINK_IN_ERROR.sub("url: <transfer link>", str(ex))
    parsed = urlparse(url)
    for secret in (
        f"{parsed.path}?{parsed.query}" if parsed.query else "",
        parsed.path,
        parsed.query,
    ):
        if secret and len(secret) > 1:
            text = text.replace(secret, "<transfer link>")
    return text


#: What a text file the tool reads from a library (ref, HEAD, lock ticket, a
#: small state file) may be.  Generous for the largest of those, and far below
#: what a hostile library could plant to exhaust memory.
MAX_TEXT_FILE_BYTES = 16 * 1024 * 1024


class SeafileAuthError(Exception):
    pass


class SeafileAPIError(Exception):
    pass


class UploadCorruptedError(SeafileAPIError):
    """The server stored a chunk of an upload one byte too long (its multipart parser, measured on CE 13.0).

    The temporary cannot be repaired or restarted under the same name (the server keeps the partial by name and
    answers ``400 Upload offset is invalid`` to a restart), so the *caller*, which chose the name, starts the object
    again under a new one.
    """


class SeafileConnectionError(SeafileAPIError, requests.RequestException):
    """The connection dropped, timed out or was refused before the server answered.

    Both a ``SeafileAPIError`` (so every ``except SeafileAPIError`` sees it) and a
    ``requests.RequestException`` (so the handlers written for that keep working).
    """


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


class UploadSourceChangedError(Exception):
    """The bytes a chunk was sent with were not the same on every attempt: the file changed while uploading.

    Deliberately **not** a ``SeafileAPIError``: ``_upload`` retries those, and this must reach the caller,
    which routes it to "changed while uploading" and not to "the server failed" (RESUME.md D1 8(d)).
    """


class HashingReader:
    """A read-only file object that hashes the bytes handed to the uploader (RESUME.md D1 step 2).

    Only a *forward-contiguous* first read is hashed, so the digest is that of the bytes the server received,
    in order.  A chunked upload also drives the attempt protocol: ``begin_attempt`` restores the running hash to
    the copy taken at the last accepted boundary, and ``accept`` -- called when the server is known to hold the
    chunk -- requires that **every attempt which read the whole span read the same bytes** (the server may have
    kept the first attempt although the second is the one that was answered).  The digest then describes what
    the server holds.  A reader that is rewound some other way keeps the old ``rewound`` flag.
    """

    def __init__(self, fh: Any, size: int, meter: Any = None) -> None:
        self._fh = fh
        self._size = size
        self._meter = meter
        self._hash = hashlib.sha256()
        self._hashed = 0
        #: True once any byte was read a second time outside the attempt protocol.
        self.rewound = False
        self._boundary: "tuple[int, Any]" = (0, self._hash.copy())
        self._span_end: "int | None" = None
        self._pending_end = 0
        self._attempts: list[Any] = []

    def read(self, size: int = -1) -> bytes:
        at = self._fh.tell()
        data = self._fh.read(size)
        if data and self._meter is not None:
            self._meter.add(len(data))
        if data and at < self._hashed:
            self.rewound = True
        if data and at == self._hashed:
            self._hash.update(data)
            self._hashed += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return int(self._fh.seek(offset, whence))

    def tell(self) -> int:
        return int(self._fh.tell())

    @property
    def complete(self) -> bool:
        return self._hashed == self._size

    def hexdigest(self) -> str:
        return self._hash.hexdigest()

    def _close_attempt(self) -> None:
        if self._span_end is not None and self._hashed == self._span_end:
            self._attempts.append(self._hash.copy())
        self._span_end = None

    def begin_attempt(self, offset: int, length: int) -> None:
        """A send of ``[offset, offset+length)`` is about to start (the first, or a retry)."""
        self._close_attempt()
        at, saved = self._boundary
        if at != offset:
            self.rewound = True      # not a position this reader can restore: the caller re-reads, as before
            return
        self._hash = saved.copy()
        self._hashed = at
        self._span_end = self._pending_end = offset + length

    def accept(self) -> None:
        """The server holds the span: every attempt that read all of it must have read the same bytes."""
        self._close_attempt()
        attempts, self._attempts = self._attempts, []
        if len({h.hexdigest() for h in attempts}) != 1:
            # none read the whole span, or two read different bytes: the digest cannot vouch for what is stored
            raise UploadSourceChangedError("the file changed while it was being uploaded")
        end = self._pending_end
        self._boundary = (end, attempts[0].copy())    # the boundary moves to the end of the accepted span
        self._hash = attempts[0].copy()
        self._hashed = end


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


def _with_ret_json(url: str) -> str:
    """The upload link with ``ret-json=1``: the reply then names each stored file and its size, which the client checks."""
    return url + ("&" if "?" in url else "?") + "ret-json=1"


def _is_our_landing(landed: str, filename: str) -> bool:
    """Whether *landed*, the name an upload reply gives, is the file this upload wrote: ``name`` or ``name (N)``."""
    return bool(landed) and (landed == filename or re.fullmatch(re.escape(filename) + r" \(\d+\)", landed) is not None)


#: The upload endpoint refuses a body whose last network read is only the final CRLF; a body this residue modulo this number can
#: end that way (see StreamingMultipartFile).
REFUSED_BODY_MODULUS = 512
REFUSED_BODY_RESIDUE = 2


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
        boundary_pad: int = 0,
    ):
        base_boundary = boundary or f"----GRSBoundary{uuid.uuid4().hex}"
        self.boundary = base_boundary
        self.file_obj = file_obj
        self.file_size = file_size
        self.filename = filename  # named in read()'s short-source error
        self.progress_callback = progress_callback
        self._uploaded_file_bytes = 0
        try:
            self._file_start_pos = self.file_obj.tell() if hasattr(self.file_obj, "tell") else 0
        except Exception:
            self._file_start_pos = 0

        # The server's upload parser answers ``400 Bad request.`` when the very last read of the body is the 2-byte CRLF that
        # follows the closing boundary (upload-file.c: the closing line is consumed without its CRLF, and the CRLF then
        # reads as a blank header line).  Where reads end is the network's doing; behind the usual nginx they are 4096
        # bytes, so a body of 4096 * k + 2 bytes fails every time and for any content (measured on CE 13.0: of 4,200
        # consecutive body lengths only those failed; sending the last byte alone is fine).  A retry with the same length
        # fails again, so the boundary is lengthened until the body is not 2 past a multiple of 512 (which covers every read
        # size that is a multiple of 512; about 1 upload in 512 is lengthened, by a few bytes).  A boundary the caller chose
        # is kept: it is then the caller's to get right.
        for pad in range(boundary_pad, boundary_pad + 16):
            self.boundary = base_boundary if boundary is not None else base_boundary + "0" * pad
            self.header, self.footer = self._frame(fields, file_field, filename)
            self.total_len = len(self.header) + file_size + len(self.footer)
            if boundary is not None or self.total_len % REFUSED_BODY_MODULUS != REFUSED_BODY_RESIDUE:
                break
        self.pad_used = pad          # a retry starts above it: the same length would repeat the same read splits
        self._pos = 0

    def _frame(self, fields: dict[str, str], file_field: str, filename: str) -> "tuple[bytes, bytes]":
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
        return b"".join(prefix), f"\r\n--{self.boundary}--\r\n".encode("utf-8")

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

#: The rate the wait after a last chunk assumes the server assembles at (see ``_chunk_verdict``); an assumption.
ASSEMBLY_BYTES_PER_SECOND = 100 * 1024 * 1024

#: Attempts per chunk (the first plus retries) before the upload is abandoned.
UPLOAD_CHUNK_ATTEMPTS = 3
#: How many times a caller starts an object again under a new temporary name after the server stored a chunk wrongly.
UPLOAD_RESTARTS = 2
#: Fresh upload links one upload may ask for after a failed chunk (a link lives an hour; 8 covers a week-long upload).
UPLOAD_LINK_REFRESHES = 8
#: A link older than this is replaced before the next chunk is sent.  The server's default lifetime is 3600 s and an
#: expired link does not always answer ``403``: **a large body sent to one is not read, and the request hangs until
#: the transfer times out** (measured on CE 13.0 with the lifetime set to 60 s), so waiting for the refusal costs a
#: whole timeout per chunk.  A server set to a shorter lifetime is caught by the failure path instead.
UPLOAD_LINK_MAX_AGE = 2700.0

#: How long a ref-sized write (a 41-byte file under the lock, behind a fence that
#: proved ownership moments before) waits for the server's reply.  The 600 s above
#: is for a multi-gigabyte upload; applied to a ref it widened the gap between the
#: fence and the write to minutes, and a resend after a timeout could land after
#: a competitor's write.  A server that has not answered a 41-byte write in this
#: long is not going to be waited on; the caller reports the ref as unknown.
REF_WRITE_READ_TIMEOUT_SECONDS = 20


def _leave_the_request_alone(request: Any) -> Any:
    """An ``auth`` that changes nothing: its being set keeps requests from reading ``.netrc``."""
    return request


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

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        """Every API call: a dropped connection or timeout surfaces as ``SeafileConnectionError``.

        A handler written ``except SeafileAPIError`` used to miss the raw ``requests``
        exception (fable H2.5: ``cmd_list``'s retry, the lease renewal, the cleanups).
        The error is both a ``SeafileAPIError`` and a ``RequestException``, so a handler
        written for either keeps working; its text names the method and path, never the
        query (a link's token) or the headers.
        """
        try:
            return super().request(method, url, *args, **kwargs)
        except SeafileConnectionError:
            raise
        except requests.RequestException as ex:
            from urllib.parse import urlsplit
            # The cause stays (a certificate, DNS or refused-connection reason is what lets
            # someone act on it, fable2-B1); only the url's path and query are scrubbed.
            raise SeafileConnectionError(
                f"{type(ex).__name__} during {str(method).upper()} {urlsplit(str(url)).path}: "
                f"{_scrub_transfer_error(ex, str(url))}"
            ) from None

    def rebuild_auth(self, prepared_request: Any, response: Any) -> None:
        super().rebuild_auth(prepared_request, response)
        url = str(prepared_request.url or "")
        scope = self.scope().rstrip("/")
        if not scope or not _inside_scope(url, scope):
            prepared_request.headers.pop("Authorization", None)


#: The longest one retry sleeps for a server's ``Retry-After`` (fable-C1).  urllib3 honours the
#: header for 429, 500 and 503 with no bound of its own the client sets (one ``list_dir`` against
#: ``Retry-After: 4`` took 12 s, a ``delete_entry`` 24 s), and the thread that sleeps is the one
#: that renews the push lease.  Measured on the CE container (AA23): a throttle answered 10 s the
#: first time and 1 s after; a cap below 10 s would retry three times inside the first window.
#: Sent on every transfer-link download.  ``Content-Length`` is the encoded length and ``requests``
#: counts decoded bytes, so a file host or proxy that gzipped a body made every complete download
#: read as "Truncated" (review 9fe723f, fable-C1); the bytes of a pack or object are the bytes.
_IDENTITY_ENCODING = {"Accept-Encoding": "identity"}

RETRY_AFTER_CAP_SECONDS = 10.0
_RETRY_AFTER_NOTED = False


_THROTTLE_ANSWERS = 0


def throttle_answers() -> int:
    """How many HTTP 429 answers this process has seen (AA23).

    A caller that runs requests in parallel reads it before and after a batch: a rise means the
    server's per-user request rate was crossed and the next batch should use fewer workers.
    """
    return _THROTTLE_ANSWERS


class _CappedRetry(Retry):
    """``Retry`` that never sleeps longer than ``RETRY_AFTER_CAP_SECONDS`` for ``Retry-After``."""

    def is_retry(self, method: str, status_code: int, has_retry_after: bool = False) -> bool:
        global _THROTTLE_ANSWERS
        if status_code == 429:
            _THROTTLE_ANSWERS += 1   # a GIL-atomic enough counter: only its rise is read
        return super().is_retry(method, status_code, has_retry_after)

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

    #: Ask for one upload link per folder and reuse it (a caller that sends many small files into one folder).
    reuse_upload_links = False

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
        self._git_credential_note = ""
        self._config_wants_git = False
        self._git_credential_username: str | None = None  # set only when the token came from git's store
        self._git_credential_confirmed = False
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
        # A ``.netrc`` entry for the host would make requests replace the Token header with Basic
        # auth, and send the netrc password on the file session that is meant to carry no
        # credential; the answer was "HTTP 401 ... refresh your token" for a good token (review
        # dfd2269, fable-C1).  An explicit auth object is what stops requests from consulting it.
        self.session.auth = _leave_the_request_alone
        self._file_session.auth = _leave_the_request_alone
        self._mount_retries(self.session)
        self._mount_retries(self._file_session)
        self.session.hooks["response"].append(self._record_server_date_header)
        self._repos_cache: dict[str, str] = {}  # name_or_id -> id
        self._encrypted_repo_ids: set[str] = set()
        self._known_dirs: set[tuple[str, str]] = set()  # (repo_id, clean_path)
        #: The repository path whose inside the caller lays out (the helper and the
        #: LFS agent set it); ``None`` judges a folder by its name alone.
        self.layout_root: str | None = None
        #: True when ``layout_root`` is a blob store's root (the blob command sets it).
        self.store_layout = False

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
        if not token or not self.server_url.lower().startswith("http://"):
            return
        host = (urlparse(self.server_url).hostname or "").lower()
        if is_loopback_host(host):
            return
        sys.stderr.write(
            f"Warning: {source} is being sent to {self.server_url} over plain HTTP, "
            "where anything on the network path can read it. Use https:// instead.\n"
        )
        sys.stderr.flush()

    def _note_plaintext_token(self, config_path: Path) -> None:
        """Say once that a token kept in the config file is deprecated in favour of git's credential store.

        A ``Notice:``, not a ``Warning:``: the token still works and a job run with ``--exit-warnings``
        must not start failing for it.  Deprecation policy: announced in 0.11, the file token is kept
        working until a later release says otherwise.
        """
        sys.stderr.write(
            f"Notice: the token in {config_path} is stored as plain text, which is deprecated. "
            "Run 'git-remote-seafile login --store credential' to keep it in git's credential store "
            "instead; the file token still works in 0.11.\n"
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
        # A file that says "credential_store": "git" for this server asks git first, so the desktop
        # client's account for the same server cannot outrank what `login --store credential` wrote.
        asked_git = self._config_wants_git
        if asked_git and self._credentials_from_git(require_token):
            return
        if self._credentials_from_accounts_db(require_token):
            return
        if not asked_git and self._credentials_from_git(require_token):          # not twice: each is a `git credential fill`
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
            if self._may_replace_server_url(env_server):
                self.server_url = env_server.rstrip("/")
                self._adopt_token(env_token, "SEAFILE_TOKEN")
                return True
        return False

    def _may_replace_server_url(self, source_server: str) -> bool:
        """True when a server named by a credential source may stand in for ``self.server_url``.

        The same host and port, and never a step from ``https`` down to ``http``: the netloc
        comparison ignores the scheme, so an ``http://`` entry in ``SEAFILE_SERVER``, the config
        file or accounts.db replaced an explicit ``https://`` URL and the token went out in the
        clear (gemini-4-hydrogen-C1).
        """
        if not self.server_url:
            return True
        if normalize_netloc(source_server) != normalize_netloc(self.server_url):
            return False
        return not (
            self.server_url.lower().startswith("https://") and source_server.lower().startswith("http://")
        )

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
            # The account name belongs to the server the file names (CREDENTIAL.md D2): a file
            # written for server A must not make a lookup for server B ask under A's account.
            if cfg_user and not self.username and (not cfg_server or self._may_replace_server_url(cfg_server)):
                self.username = cfg_user
            if cfg_server and (cfg_token or not require_token):
                if self._may_replace_server_url(cfg_server):
                    self.server_url = cfg_server.rstrip("/")
                    self._adopt_token(cfg_token, f"the token in {config_path}")
                    if cfg_token:
                        self._note_plaintext_token(config_path)
                    return True, cfg_server, cfg_token
            elif cfg_server and not cfg_token and cfg.get("credential_store") == "git":
                # `login --store credential` wrote this: the host is known, the token is in git's store.
                if self._may_replace_server_url(cfg_server):
                    self.server_url = cfg_server.rstrip("/")
                    self._config_wants_git = True
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

    def _credentials_from_git(self, require_token: bool) -> bool:
        """The last source: ``git credential fill`` (CREDENTIAL.md).

        Only when a server is known (a git answer names no server of its own: the
        host and path of the request are the server, and git matched them) and a
        token is wanted.  Never prompts (see :mod:`gitcred`); any failure is a
        silent "nothing found" with a note for the error message.
        """
        self._git_credential_note = ""
        if not (self.server_url and require_token and not self.token):
            return False
        try:
            outcome = gitcred.fill(self.server_url, self.username)
        except gitcred.CredentialRequestError as ex:
            self._git_credential_note = f"git credential helper not asked: {ex}"
            return False
        if outcome.found is None:
            tried = ", ".join(outcome.tried)
            self._git_credential_note = f"git credential helper had nothing under {tried}" + (
                f" ({outcome.note})" if outcome.note else ""
            )
            return False
        self._adopt_token(outcome.found.token, "the token from the git credential helper")
        self._git_credential_username = outcome.found.username
        return True

    def _git_credential_worked(self) -> None:
        """Tell git's store the token it gave us authenticated (``approve``, once per client).

        Only for a token that came from git under our own name (R5): a token from the
        environment or a file is not git's to store."""
        if self._git_credential_username is None or self._git_credential_confirmed or not self.token:
            return
        self._git_credential_confirmed = True
        try:
            gitcred.approve(self.server_url, self._git_credential_username, self.token)
        except gitcred.CredentialRequestError:
            pass

    def _git_credential_failed(self) -> None:
        """The server answered ``401`` for a token git gave us: erase that entry (``reject``).

        Never for any other source, and never for a ``403`` (a valid token without permission)."""
        username = self._git_credential_username
        if username is None:
            return
        self._git_credential_username = None
        try:
            if not gitcred.reject(self.server_url, username):
                sys.stderr.write(
                    f"Warning: could not erase the stale credential '{username}' from git's store; "
                    "remove it with your credential manager.\n"
                )
        except gitcred.CredentialRequestError:
            pass

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
                if self._may_replace_server_url(row[0]):
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
            if getattr(self, "_git_credential_note", ""):
                skipped.append(self._git_credential_note)
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
        later.  Only methods that change nothing are re-sent once the request may
        have reached the server (GET, HEAD, OPTIONS): an upload, a delete or a
        rename sent twice does work we cannot undo.

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
            # Only a request that changes nothing is sent again after it may have reached the
            # server.  urllib3's default also re-sends DELETE and PUT (fable2-B2: one cleanup
            # delete went out four times and removed a ref another client had written meanwhile),
            # and an SSL error after the request went out counts as "other", retried for every
            # method (fable2-C2: a ref write with ``attempts=1`` arrived four times).  A refused
            # connection (nothing sent) is still retried for every method, by ``connect``.
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            other=0,
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
            # The account token goes only to a link that is inside the server's own scope: an
            # API answer that came out of another application on the same host names a link
            # there, and the first request to it would carry the token (fable-C2).
            if _inside_scope(url, (self.server_url or "").rstrip("/")):
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

    def _json_body(self, resp: Any, what: str) -> Any:
        """``resp.json()``, with a body that is not JSON (an SSO login page answered 200) named.

        ``requests`` raises its own ``JSONDecodeError``, which is no ``SeafileAPIError``: the
        listing's retry and the cleanups missed it and the person read "Expecting value: line 1
        column 1" (review 134c8af, fable-C4).
        """
        try:
            return resp.json()
        except ValueError:
            raise SeafileAPIError(
                f"The server answered 200 for {what} but the body is not JSON (a login page or a "
                f"proxy's error page?): {self._said(getattr(resp, 'text', ''))}"
            ) from None

    def _json_or_none(self, path: str) -> Any:
        """``GET <server><path>`` as JSON, or ``None`` for any failure (a question
        with a safe default, never a reason to stop on its own)."""
        try:
            resp = self.session.get(f"{self.server_url}{path}", timeout=self.timeout)
            return resp.json() if resp.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return None

    def hidden_history_risk(self, repo_id: str, noun: str = "refs") -> str | None:
        """Why this account may not see the library's whole history; ``None`` when it does.

        On Seafile Professional a folder permission can hide part of a library from
        an account that is not its owner: an ``invisible`` folder is left out of its
        parent's listing (measured on Pro 13.0.29, ROADMAP B2).  A ref under such a
        folder reads as absent, and a ``gc`` or ``--payload-delete`` run by that
        account would delete what only that ref reaches.  Community Edition has no
        folder permissions, and the owner is never restricted by them.  On Pro, an
        account or owner the server does not report is a risk too: cannot tell is
        not "safe".  *noun* is what the warning says is hidden ("refs" for a Git remote, "pins" for the blob store).
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
                    f"{noun} from an account that is not the owner")
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
                    f"and on Seafile Professional a folder permission can hide {noun} from any "
                    "member")
        if not (isinstance(email, str) and email and isinstance(owner, str) and owner):
            return ("the server did not say which account owns this library, and on Seafile "
                    f"Professional a folder permission can hide {noun} from an account that is not "
                    "the owner")
        return (f"this account ({email}) is not the library's owner ({owner}), and on Seafile "
                f"Professional a folder permission can hide {noun} from it (an 'invisible' folder "
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
        if resp.status_code == 200:
            self._git_credential_worked()
        if resp.status_code != 200:
            if resp.status_code == 401:
                self._git_credential_failed()
            if resp.status_code in (401, 403):
                detail = self._said(resp.text) or "Unauthorized"
                raise SeafilePermanentError(
                    f"Failed to list repos: HTTP {resp.status_code} ({detail}).\n"
                    "Hint: check or refresh your token with 'git-remote-seafile check-auth' "
                    "or log in again with 'git-remote-seafile login' (or the Seafile desktop client)."
                )
            raise SeafileAPIError(f"Failed to list repos: HTTP {resp.status_code} {self._said(resp.text)}")

        repos = self._json_body(resp, "the library list")
        if not isinstance(repos, list):
            raise SeafileAPIError(f"Unexpected response from {url}: {self._said(resp.text)}")
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
        data = self._json_body(resp, f"the listing of {clean_path}")
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
            # A 429 refuses before acting, so it is waited out and the create sent again; a
            # timeout may have landed and is not (AA23: the AA18 listing run died here).
            def _create(u: str = url) -> requests.Response:
                return self.session.post(u, data={"operation": "mkdir"}, timeout=self.timeout)

            resp = self._send_waiting_out_a_throttle(_create)
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
                    if not _is_layout_dir(parent, part, self.layout_root, self.store_layout):
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
        file_resp = self._xfer("get", dl_url, timeout=self.timeout, stream=max_bytes is not None,
                                 headers=_IDENTITY_ENCODING)
        try:
            if file_resp.status_code == 404:
                return None
            if file_resp.status_code != 200:
                said = self._said(self._body_sample(file_resp), *self._link_secrets(dl_url))
                raise SeafileAPIError(
                    f"Failed to download {clean_path}: HTTP {file_resp.status_code} "
                    f"(GET {self._error_origin(dl_url)})" + (f" {said}" if said else "")
                )
            if max_bytes is not None:     # the streamed read counts bytes; a whole body is just decoded
                self._refuse_encoded_body(file_resp, clean_path, dl_url)
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
            for chunk in self._stream_chunks(file_resp, dl_url, 65536):
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

    def _refuse_encoded_body(self, resp: requests.Response, clean_path: str, url: str) -> None:
        """A transfer body that arrives compressed is refused, by name, not read as "truncated".

        ``Accept-Encoding: identity`` is sent, so this is a file host or proxy ignoring it.
        ``Content-Length`` would then be the encoded size while the cap and the byte count see
        decoded bytes: every complete download read as truncated, and a small body could expand
        past the cap before it was counted (review 9fe723f, fable-C1).
        """
        raw = resp.headers.get("Content-Encoding", "")
        encoding = raw.strip().lower() if isinstance(raw, str) else ""
        if encoding and encoding != "identity":
            raise SeafileAPIError(
                f"Failed to download {clean_path}: the file host answered with Content-Encoding: "
                f"{self._said(encoding)} although identity was asked for (GET "
                f"{self._error_origin(url)}); a proxy in front of it is compressing the body. "
                "Exempt the file host's /seafhttp path from compression."
            )

    def _stream_chunks(self, resp: requests.Response, url: str, chunk_size: int) -> Iterator[bytes]:
        """``resp.iter_content`` with a body that dies mid-stream raised as ``SeafileConnectionError``.

        ``Session.request`` wraps the call, but a streamed body is read afterwards, so a
        connection reset or a chunked body cut short raised a bare ``ChunkedEncodingError`` that
        ``except SeafileAPIError`` (``cmd_list``'s retry, the pack download) never saw
        (fable2-C1).  The text names the method and the link's origin, never its token.
        """
        try:
            yield from resp.iter_content(chunk_size=chunk_size)
        except SeafileConnectionError:
            raise
        except requests.RequestException as ex:
            raise SeafileConnectionError(
                f"{type(ex).__name__} during GET {self._error_origin(url)} (the body): "
                f"{_scrub_transfer_error(ex, url)}"
            ) from None

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
            # A dropped connection is a SeafileConnectionError here as everywhere (AA27); the
            # transfer paths raised the base class and missed every handler of it
            # (gemini-4-hydrogen-G0.2).
            kind = (
                SeafileConnectionError
                if isinstance(ex, (SeafileConnectionError, requests.ConnectionError, requests.Timeout))
                else SeafileAPIError
            )
            raise kind(
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

        # 1. Get upload link.  An upload token is reusable (read from the server's source), so a caller that sends
        # many small files into one folder (the blob store) asks for it once per folder and reuses it until it is
        # older than UPLOAD_LINK_MAX_AGE; a refused or failed upload asks again (*force*).
        link_cache: "dict[tuple[str, str], tuple[str, float]]" = self.__dict__.setdefault("_upload_links", {})

        def _get_upload_url(force: bool = False) -> str:
            key = (repo_id, clean_parent)
            if self.reuse_upload_links and not force:
                hit = link_cache.get(key)
                if hit is not None and time.monotonic() - hit[1] < UPLOAD_LINK_MAX_AGE:
                    return hit[0]
            link_url = f"{self.server_url}/api2/repos/{repo_id}/upload-link/?p={quote(clean_parent, safe='/')}"
            resp = self.session.get(link_url, timeout=self.timeout)
            if resp.status_code != 200:
                raise SeafileAPIError(
                    f"Failed to get upload link for {clean_parent}: HTTP {resp.status_code} "
                    f"{self._said(resp.text)}" + _denied_hint(resp, "write")
                )
            fresh = self._adjust_url(resp.text.strip().strip('"'))
            if self.reuse_upload_links:
                link_cache[key] = (fresh, time.monotonic())
            return fresh

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
                    upload_url, progress_callback, refresh_url=lambda: _get_upload_url(True),
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
        expected_size: int | None = (
            file_size if (hasattr(payload, "read") and hasattr(payload, "seek"))
            else len(payload) if isinstance(payload, (bytes, bytearray)) else None
        )

        up_resp = None
        last_error = ""
        fresh_link_retry = 1
        pad_floor = 0
        wrong_last = False          # the last attempt was answered 200 with a stored size other than the one sent
        for attempt in range(max_attempts):
            wrong_last = False
            if attempt:
                time.sleep(0.3 * attempt)
                try:
                    upload_url = _get_upload_url(True)
                except Exception as ex:
                    last_error = f"Failed to refresh upload link: {ex}"
                    continue
            if hasattr(payload, "seek"):
                payload.seek(start_cur)
            begin_attempt = getattr(payload, "begin_attempt", None)
            if begin_attempt is not None and hasattr(payload, "read") and hasattr(payload, "seek"):
                begin_attempt(start_cur, file_size)     # one digest per attempt, as for a chunk (RESUME.md D1 step 2)
            else:
                begin_attempt = None

            try:
                if hasattr(payload, "read") and hasattr(payload, "seek"):
                    mp = StreamingMultipartFile(
                        fields={"parent_dir": clean_parent, "replace": "1" if replace else "0"},
                        file_field="file",
                        filename=filename,
                        file_obj=payload,
                        file_size=file_size,
                        progress_callback=progress_callback,
                        boundary_pad=pad_floor,          # a retry is framed with a longer boundary: other read splits
                    )
                    pad_floor = mp.pad_used + 1
                    headers = {
                        "Content-Type": mp.content_type,
                        "Content-Length": str(len(mp)),
                    }
                    up_resp = self._xfer(
                        "post", _with_ret_json(upload_url), data=mp, headers=headers, timeout=upload_timeout
                    )
                elif isinstance(payload, (bytes, bytearray)):          # also without a progress callback: one framing, one length rule
                    bio = io.BytesIO(payload)
                    mp = StreamingMultipartFile(
                        fields={"parent_dir": clean_parent, "replace": "1" if replace else "0"},
                        file_field="file",
                        filename=filename,
                        file_obj=bio,
                        file_size=len(payload),
                        progress_callback=progress_callback,
                        boundary_pad=pad_floor,
                    )
                    pad_floor = mp.pad_used + 1
                    headers = {
                        "Content-Type": mp.content_type,
                        "Content-Length": str(len(mp)),
                    }
                    up_resp = self._xfer(
                        "post", _with_ret_json(upload_url), data=mp, headers=headers, timeout=upload_timeout
                    )
                else:
                    files = {"file": (filename, payload)}
                    data = {"parent_dir": clean_parent, "replace": "1" if replace else "0"}
                    up_resp = self._xfer(
                        "post", _with_ret_json(upload_url), files=files, data=data, timeout=upload_timeout
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
                    wrong = self._wrong_stored_size(up_resp, expected_size)
                    if wrong is not None:
                        # The server's upload parser can store a body one byte longer than it was sent, and answer 200
                        # (measured on CE 13.0, same bug as the 400 in StreamingMultipartFile: it depends on where the
                        # network ends a read); its reply names the size it stored, so a wrong one is caught here, the
                        # copy is removed when a retry would not replace it, and the retry is framed differently.
                        landed, got = wrong
                        wrong_last = True
                        last_error = f"the server stored {got} bytes of {filename}, not {expected_size}"
                        if not replace and _is_our_landing(landed, filename):
                            # Only a name that is ours: a reply entry with another or no name must never reach
                            # ``delete_entry`` (an empty name is the parent folder, and that call removes folders).
                            try:
                                self.delete_entry(repo_id, f"{clean_parent}/{landed}")
                            except SeafileAPIError:
                                pass
                        up_resp = None
                        continue
                    if begin_attempt is not None:
                        payload.accept()    # every attempt that read the whole file must have read the same bytes
                    return True
                last_error = (
                    f"HTTP {up_resp.status_code} "
                    f"(POST {self._error_origin(upload_url)}) "
                    f"{self._said(up_resp.text, *self._link_secrets(upload_url))}"
                )
                if up_resp.status_code == 403 and fresh_link_retry and attempt + 1 < max_attempts:
                    # A refused upload is tried once more on a new link: a reused link may have expired (a 403 for a
                    # small body; a large one hangs instead).  A 400 is retried below, framed with a longer boundary.
                    fresh_link_retry -= 1
                    continue
                if up_resp.status_code == 400 and attempt + 1 < max_attempts:
                    continue          # the next attempt is framed with a different length (see StreamingMultipartFile)
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
            if wrong_last:
                # Not a transport failure: the server answered 200 and holds other bytes under the name.  A caller that
                # uploaded straight onto a final name must remove it; one that chose a temporary name starts again.
                raise UploadCorruptedError(
                    f"Failed to upload {filename} to {clean_parent}: {last_error} "
                    f"(POST {self._error_origin(upload_url)})"
                )
            raise SeafileAPIError(
                f"Failed to upload {filename} to {clean_parent}: {last_error} "
                f"(POST {self._error_origin(upload_url)})"
            )
        return True

    @staticmethod
    def _wrong_stored_size(resp: Any, expected: "int | None") -> "tuple[str, int] | None":
        """``(landed name, stored size)`` when the upload reply (``ret-json=1``) names a size other than *expected*."""
        if expected is None:
            return None
        try:
            entries = resp.json()
        except Exception:
            return None
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if isinstance(entry, dict):
                size = entry.get("size")
                if isinstance(size, int) and not isinstance(size, bool) and size != expected:
                    return str(entry.get("name", "")), size
        return None

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
        refresh_url: "Callable[[], str] | None" = None,
    ) -> bool:
        """Upload *fh* as ``Content-Range`` chunks and prove the result is whole.

        An upload link **expires an hour after it was issued, mid-upload included** (the server looks the token up on
        every chunk and never extends it; read from its source and measured on CE 13.0).  An expired link answers
        ``403 Access token not found`` to a small body, but a large one is **not read and the request hangs** until
        the transfer times out.  The partial is kept by path, not by token, so a fresh link continues it.  With
        *refresh_url* the client replaces a link older than ``UPLOAD_LINK_MAX_AGE`` before sending the next chunk, and
        after a chunk failed by timeout, reset or ``403``, before sending it again.
        The probe has said a chunk is not held before it is sent again.

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
        refreshes = 0
        issued = time.monotonic()

        def renew() -> None:
            nonlocal upload_url, url, session, issued
            try:
                fresh = refresh_url() if refresh_url is not None else upload_url
            except SeafileAPIError:
                return          # no new link now: the old one is tried again, and the attempt budget decides
            upload_url = fresh
            url = upload_url + ("&" if "?" in upload_url else "?") + "ret-json=1"
            session = self._transfer_session(upload_url)
            issued = time.monotonic()

        while offset < file_size:
            length = min(UPLOAD_CHUNK_BYTES, file_size - offset)
            last_error = ""
            if refresh_url is not None and time.monotonic() - issued > UPLOAD_LINK_MAX_AGE:
                renew()
            pad_floor = 0
            for _attempt in range(UPLOAD_CHUNK_ATTEMPTS):
                fh.seek(start0 + offset)
                begin_attempt = getattr(fh, "begin_attempt", None)
                if begin_attempt is not None:
                    begin_attempt(start0 + offset, length)    # one digest per attempt (RESUME.md D1 step 2)

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
                    boundary_pad=pad_floor,          # a chunk that was refused is framed with a longer boundary
                )
                pad_floor = mp.pad_used + 1
                headers = {
                    "Content-Type": mp.content_type,
                    "Content-Length": str(len(mp)),
                    "Content-Range": f"bytes {offset}-{offset + length - 1}/{file_size}",
                    "Content-Disposition": f'attachment; filename="{safe_name}"',
                }
                resp = None
                try:
                    # No redirect is followed: a chunk is not re-sent where a 3xx points (as in _xfer), and
                    # the 3xx falls to the status check below as the failure it is (review dfd2269,
                    # gemini-4-hydrogen-2-C3).
                    resp = session.post(
                        url, data=mp, headers=headers, timeout=self._upload_timeout(),
                        allow_redirects=False,
                    )
                except requests.RequestException as ex:
                    last_error = f"{type(ex).__name__}: {_scrub_transfer_error(ex, upload_url)}"
                if resp is not None:
                    if resp.status_code == 200:
                        last_error = ""
                        if begin_attempt is not None:
                            fh.accept()
                        if offset + length < file_size:
                            # A chunk's reply carries no size, and the server can store a chunk one byte too long and still
                            # answer 200: ask how much it holds now (one request per chunk) and stop at once, not at the end.
                            held = self.uploaded_bytes(repo_id, parent_dir, filename)
                            if held is not None and held > offset + length:
                                raise UploadCorruptedError(
                                    f"Failed to upload {filename} to {parent_dir}: after the chunk ending at byte "
                                    f"{offset + length} of {file_size} the server holds {held} bytes (it stored the chunk "
                                    f"one byte too long); the upload must start again under a new name "
                                    f"(POST {self._error_origin(upload_url)})"
                                )
                        break
                    last_error = f"HTTP {resp.status_code} {self._said(resp.text, *self._link_secrets(upload_url))}"
                # The chunk failed or went unanswered, and the server may hold it all the same: a resend of a
                # chunk it holds answers 400 (measured on CE 13.0.29, RESUME.md section 1).  Ask before any
                # resend, and act on the answer, never on a guess.
                offset_refused = resp is not None and resp.status_code == 400 and "offset" in (resp.text or "").lower()
                verdict = self._chunk_verdict(repo_id, parent_dir, filename, offset, length, file_size,
                                              offset_refused=offset_refused)
                if verdict == "stored":
                    last_error = ""
                    if begin_attempt is not None:
                        fh.accept()
                    break
                if verdict == "corrupt":
                    raise UploadCorruptedError(
                        f"Failed to upload {filename} to {parent_dir}: the server holds more than the chunk ending at byte "
                        f"{offset + length} of {file_size} (it stored the chunk one byte too long); the upload must start again "
                        f"under a new name (POST {self._error_origin(upload_url)})"
                    )
                if verdict == "unknown":
                    raise SeafileAPIError(
                        f"Failed to upload {filename} to {parent_dir}: chunk bytes "
                        f"{offset}-{offset + length - 1} of {file_size}: {last_error}; and the server could not say "
                        "whether it holds the chunk, so it was not sent again (the half-sent temporary is left "
                        f"for the collector) (POST {self._error_origin(upload_url)})"
                    )
                if (refresh_url is not None and verdict == "absent" and refreshes < UPLOAD_LINK_REFRESHES
                        and (resp is None or resp.status_code == 403)):
                    refreshes += 1
                    renew()          # the link expired (a large body to one is not read: a timeout, or a 403)
                    continue
                if resp is not None and resp.status_code == 400 and verdict == "absent" and _attempt + 1 < UPLOAD_CHUNK_ATTEMPTS:
                    continue          # refused and nothing stored (the body-length case): framed with a longer boundary
                if resp is not None and resp.status_code < 500:
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
        if isinstance(got, int) and got > file_size:
            raise UploadCorruptedError(
                f"Chunked upload of {filename} to {parent_dir} finished but the server holds {got} bytes instead of "
                f"{file_size} (a chunk was stored one byte too long); the upload must start again under a new name."
            )
        if got != file_size:
            raise SeafileAPIError(
                f"Chunked upload of {filename} to {parent_dir} finished but the server holds "
                f"{got} bytes instead of {file_size}; the server may not support Content-Range."
            )
        return True

    def _chunk_verdict(
        self, repo_id: str, parent_dir: str, filename: str, offset: int, length: int, total: int,
        offset_refused: bool = False,
    ) -> str:
        """After a chunk failed or went unanswered: ``stored``, ``absent``, ``corrupt`` or ``unknown`` (RESUME.md D1 step 2).

        The answers are measured (CE 13.0.29): in the middle of an upload the count advances by exactly the chunk
        when it was stored; after the **last** chunk it is 0 and the file is already listed at the total, or the
        total while the server assembles and the name is not yet listed.  A probe that cannot answer after one
        retry is ``unknown`` -- not "short" -- and the caller does not resend.  ``corrupt`` is a count above what was
        sent (the server stored a chunk one byte too long); so is a count that is neither the chunk's offset nor its end
        after the server refused the offset (*offset_refused*): the bytes it holds are not the ones sent, and waiting
        does not change that.
        """
        end = offset + length
        last = end >= total
        # After the last chunk the server assembles the file, and that takes longer the bigger the file is: the wait grows
        # with *total* (5 s more per 500 MiB, at most the upload reply timeout), so a big object is not called ``unknown``
        # only because the server was still working.  The rate is an assumption, not a measurement: nobody could run a
        # 50 GiB upload here (PLAN_0.12), and a wait that is too long costs time only after a failed last chunk.
        extra_rounds = int(min(total / ASSEMBLY_BYTES_PER_SECOND, UPLOAD_READ_TIMEOUT_SECONDS) / 5) if last else 0
        for tries in range(6 + extra_rounds):
            held = self.uploaded_bytes(repo_id, parent_dir, filename)
            if held is None:
                held = self.uploaded_bytes(repo_id, parent_dir, filename)      # one retry
                if held is None:
                    return "unknown"
            if last and held in (0, total):
                # 0 is also what a completed upload answers: only the listing tells the two apart
                listed = next((e for e in self.list_dir(repo_id, parent_dir)
                               if isinstance(e, dict) and e.get("name") == filename), None)
                if listed is not None and listed.get("size") == total:
                    return "stored"
                size = listed.get("size") if listed is not None else None
                if isinstance(size, int) and not isinstance(size, bool) and size > total:
                    return "corrupt"          # the last chunk was stored one byte too long
            if (not last and held > end) or (last and held > total):
                return "corrupt"          # more than was sent: the server stored a chunk one byte too long
            if held == offset:
                return "absent"
            if not last and held == end:
                return "stored"
            if offset_refused and not last and held != offset and held != end:
                return "corrupt"          # the server refused our offset and holds neither it nor the chunk's end
            time.sleep(min(0.5 * (tries + 1), 5.0))        # in flight or assembling: wait and ask again
        return "unknown"

    def download_file_to(
        self,
        repo_id: str,
        file_path: str,
        dest: Any,
        progress_callback: Any = None,
        max_bytes: int | None = None,
        resume_from: int = 0,
        hasher: Any = None,
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

        ``resume_from`` continues a partial *dest* of exactly that many bytes
        (RESUME.md D2): a ``Range: bytes=N-`` request whose answer must be ``206``
        with a ``Content-Range`` of ``N-<size-1>/<size>``.  A server that ignores
        the header (``200``) or answers another range makes the download start
        again from byte 0, never a guess.  The partial is proved only by the
        caller's own hash at the end.  No ``Range`` is sent for 0, and none when
        the partial is already as large as the declared object (``max_bytes``):
        that answers ``416`` for a complete object, which would throw the file
        away, so the call returns at once and the caller's hash decides.  The
        declared size bounds the whole object, as ``resume_from + transferred``.

        ``hasher`` (anything with ``update(bytes)``) is fed every byte as it arrives, so a caller that wants only the
        digest can pass ``os.devnull`` as *dest* and need no scratch space.  It sees the whole body or nothing it can
        trust: with ``resume_from`` it would miss the partial's bytes, so that combination is refused.
        """
        if hasher is not None and resume_from:
            raise ValueError("hasher cannot be combined with resume_from: it would not see the bytes already held")
        clean_path = "/" + file_path.strip("/")
        dest_path = Path(dest)
        resume = max(0, int(resume_from or 0))
        if resume:
            try:
                held = dest_path.stat().st_size
            except OSError:
                held = -1
            if held != resume or (max_bytes is not None and resume > max_bytes):
                resume = 0   # the partial is not what the caller says: start again, never guess
            elif max_bytes is not None and resume == max_bytes:
                return True  # complete already: the caller's hash decides, no request
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            if self._file_is_under_a_missing_folder(repo_id, clean_path, resp):
                return False
            raise self._download_link_error(clean_path, resp)

        dl_url = self._adjust_url(resp.text.strip().strip('"'))
        headers = dict(_IDENTITY_ENCODING)
        if resume:
            headers["Range"] = f"bytes={resume}-"
        file_resp = self._xfer("get", dl_url, timeout=self.timeout, stream=True, headers=headers)
        try:
            if file_resp.status_code == 404:
                return False
            append = False
            expected_total = 0
            if resume and file_resp.status_code == 206:
                said_range = str(file_resp.headers.get("Content-Range", "")).strip()
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", said_range, re.IGNORECASE)   # RFC 9110: the unit is case-insensitive
                body_says = file_resp.headers.get("Content-Length")
                if match and int(match.group(1)) == resume and int(match.group(2)) == int(match.group(3)) - 1 >= resume and (
                    body_says is None or str(body_says).strip() == str(int(match.group(3)) - resume)
                ):
                    append = True
                    expected_total = int(match.group(3))
                else:
                    file_resp.close()   # another range than the one asked for: never appended
                    return self.download_file_to(repo_id, file_path, dest, progress_callback, max_bytes, 0, hasher=hasher)
            elif resume and file_resp.status_code == 416:
                file_resp.close()
                return self.download_file_to(repo_id, file_path, dest, progress_callback, max_bytes, 0, hasher=hasher)
            elif file_resp.status_code != 200:
                said = self._said(self._body_sample(file_resp), *self._link_secrets(dl_url))
                raise SeafileAPIError(
                    f"Failed to download {clean_path}: HTTP {file_resp.status_code} "
                    f"(GET {self._error_origin(dl_url)})" + (f" {said}" if said else "")
                )

            self._refuse_encoded_body(file_resp, clean_path, dl_url)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            total_size = expected_total
            if not append and "Content-Length" in file_resp.headers:
                try:
                    total_size = int(file_resp.headers["Content-Length"])
                except Exception:
                    total_size = 0
            if max_bytes is not None and total_size > max_bytes:
                raise SeafileAPIError(
                    f"{clean_path} is {total_size} bytes on the server, more than the "
                    f"{max_bytes} expected; not downloading it"
                )
            transferred = resume if append else 0
            if append:
                try:
                    unchanged = dest_path.stat().st_size == resume   # the partial moved under us while the request ran
                except OSError:
                    unchanged = False
                if not unchanged:
                    file_resp.close()
                    return self.download_file_to(repo_id, file_path, dest, progress_callback, max_bytes, 0, hasher=hasher)
            # The declared size bounds the stream; when the caller gave none, the server's own total does.
            cap = max_bytes if max_bytes is not None else (expected_total if append else None)
            with open(dest_path, "ab" if append else "wb") as fh:
                for chunk in self._stream_chunks(file_resp, dl_url, 1024 * 1024):
                    if chunk:
                        if cap is not None and transferred + len(chunk) > cap:
                            raise SeafileAPIError(
                                f"{clean_path} is larger than the {cap} bytes expected; "
                                "download stopped"
                            )
                        fh.write(chunk)
                        if hasher is not None:
                            hasher.update(chunk)
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
        return self.rename_entry_result(repo_id, path, new_name)[0] in (200, 201)

    def rename_entry_result(self, repo_id: str, path: str, new_name: str) -> "tuple[int, str | None]":
        """Rename, and say what the server answered: ``(status, landing name)``.

        The landing name is where the bytes now are: *new_name*, or ``new_name (1)`` when that name was
        taken (measured on CE 13.0.29: the reply is a download URL whose last segment, percent-encoded, is
        the name).  ``None`` when the reply names nothing parseable, which a caller treats as a failure of
        the object, never as the name being free (RESUME.md D1 step 4, round 4).
        """
        clean_path = "/" + path.strip("/")
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        # A 429 is waited out, as a DELETE's is: the rename that publishes a payload or LFS object
        # answered a bare False for it, and the caller read that as "a name already taken"
        # (review 134c8af, fable-E2).
        # Not followed: Seafile answers a rename with ``301`` whose ``Location`` names the landing (measured on CE 13.0.29, review
        # R7 Opus P4 and Gemini).  Following it made a second request, minted a download token, and behind a proxy that
        # emits an ``http://`` Location dropped the Authorization header and answered 401 *after the rename had succeeded*.
        resp = self._send_waiting_out_a_throttle(lambda: self.session.post(
            url, data={"operation": "rename", "newname": new_name}, timeout=self.timeout, allow_redirects=False
        ))
        if resp.status_code in (301, 302):
            try:
                # ``unquote``, not ``parse_qs``: a literal ``+`` in a file name is not a space (review R8 Gemini P10), and the
                # landing is a *name*: an empty one, ``.``, ``..`` or one holding a slash is refused, because a caller deletes by it
                # (DeepSeek P2: ``p=/blobs/ab/..`` would have named the folder above).
                query = urlparse(resp.headers.get("Location", "")).query
                where = unquote(next((part[2:] for part in query.split("&") if part.startswith("p=")), ""))
                leaf = where.rstrip("/").rpartition("/")[2]
                folder = "/".join(x for x in where.rstrip("/").rpartition("/")[0].split("/") if x)
                asked = "/".join(x for x in clean_path.rstrip("/").rpartition("/")[0].split("/") if x)
                # a rename keeps the folder: a landing in another one is not an answer to this rename
                if leaf in ("", ".", "..") or "/" in leaf or "\\" in leaf or folder != asked:
                    leaf = ""
            except Exception:
                leaf = ""
            # A redirect with a landing name is the success answer: reported as 200, as the callers expect.
            return (200, leaf) if leaf else (resp.status_code, None)
        if resp.status_code not in (200, 201):
            return resp.status_code, None
        try:
            said = resp.text.strip().strip('"')
            leaf = unquote(urlparse(said).path.rpartition("/")[2]) if said.startswith(("http://", "https://")) else ""
            if leaf in (".", "..") or "/" in leaf or "\\" in leaf:
                leaf = ""
        except Exception:
            leaf = ""
        return resp.status_code, (leaf or None)

    def uploaded_bytes(self, repo_id: str, parent_dir: str, file_name: str) -> "int | None":
        """Bytes the server holds of a chunked upload in progress under *file_name*; ``None`` when unanswered.

        Measured on CE 13.0.29: the count while the upload is in progress, **0 once it is complete**, and
        0 for a name that never existed -- so 0 is never proof that nothing is stored (RESUME.md section 1).
        A failed or malformed answer is ``None``, never 0: "unknown" and "nothing" lead to different actions.
        """
        url = f"{self.server_url}/api/v2.1/repos/{repo_id}/file-uploaded-bytes/"
        try:
            resp = self.session.get(
                url, params={"parent_dir": "/" + parent_dir.strip("/"), "file_name": file_name}, timeout=self.timeout
            )
            if resp.status_code != 200:
                return None
            held = resp.json().get("uploadedBytes")
        except (requests.RequestException, ValueError, AttributeError):
            return None
        return held if isinstance(held, int) and not isinstance(held, bool) and held >= 0 else None

    def copy_entries(self, repo_id: str, src_parent: str, names: list[str], dst_parent: str) -> bool:
        """Copy files or directories server-side, within one library.

        Moves metadata, not bytes: the copies share the originals' blocks (measured
        on CE 13.0: 20-30 ms for a 1,500-entry tree, 0 bytes added).  The
        destination directory must already exist and keeps the source names.
        """
        url = f"{self.server_url}/api/v2.1/repos/sync-batch-copy-item/"
        body = {
            "src_repo_id": repo_id,
            "src_parent_dir": "/" + src_parent.strip("/"),
            "src_dirents": list(names),
            "dst_repo_id": repo_id,
            "dst_parent_dir": "/" + dst_parent.strip("/"),
        }
        resp = self._send_waiting_out_a_throttle(
            lambda: self.session.post(url, json=body, timeout=self.timeout)
        )
        return resp.status_code == 200

    def _delete_waiting_out_a_throttle(self, url: str) -> requests.Response:
        """One DELETE, again after a 429 (at most three times, each wait capped).

        The session never re-sends a DELETE: after a timeout it may have landed.  A 429 is the
        rate limiter refusing the request before it acted, so waiting ``Retry-After`` and
        sending it again is safe, and it is the difference between ``--payload-delete`` ending
        as "the server refused to delete" after the history fetch and going on (review 9fe723f,
        fable-E1; AA23's last pool).
        """
        return self._send_waiting_out_a_throttle(lambda: self.session.delete(url, timeout=self.timeout))

    def _send_waiting_out_a_throttle(self, send: Callable[[], requests.Response]) -> requests.Response:
        """*send*, again after a 429 (at most three times, each wait capped).

        For a request the session does not re-send on its own (a DELETE or a POST: after a
        timeout it may have landed).  A 429 refuses before acting, so the wait is safe.
        """
        resp = send()
        for _ in range(3):
            if resp.status_code != 429:
                break
            try:
                wait = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                wait = 1.0
            if wait != wait:      # "nan" parses; time.sleep would refuse it
                wait = 1.0
            time.sleep(min(max(wait, 0.0), RETRY_AFTER_CAP_SECONDS))
            resp = send()
        return resp

    def delete_entry(self, repo_id: str, path: str) -> bool:
        """Delete file or directory at path."""
        clean_path = "/" + path.strip("/")
        # Seafile's /file/ endpoint deletes both files and directories,
        # while /dir/ returns 404 when deleting a file. Try /file/ first.
        url = f"{self.server_url}/api2/repos/{repo_id}/file/?p={quote(clean_path, safe='/')}"
        resp = self._delete_waiting_out_a_throttle(url)
        if resp.status_code == 200:
            self._forget_dirs_under(repo_id, clean_path)
            return True
        dir_url = f"{self.server_url}/api2/repos/{repo_id}/dir/?p={quote(clean_path, safe='/')}"
        resp_dir = self._delete_waiting_out_a_throttle(dir_url)
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
