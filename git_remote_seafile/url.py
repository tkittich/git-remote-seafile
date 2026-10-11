"""url.py - Seafile URL parsing and normalization for git-remote-seafile."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

# One DNS label: alphanumeric, may carry inner hyphens, 1-63 characters.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"

# A conservative hostname: dot-separated labels whose *final* label is
# alphabetic, which is what a real hostname's TLD always is.  The point is to
# leave version-suffixed names alone -- a library called `notes.v2` or
# `proj.2026` ends in a numeric label, which no public hostname can have, so it
# stays a library.  `fullmatch` rather than `$`: `$` also matches before a
# trailing newline, so `"host.com\n"` would pass a `$`-anchored check.
_HOSTNAME_RE = re.compile(rf"(?:{_LABEL}\.)+[A-Za-z]{{2,63}}")


def _port_or_none(parsed, url: str) -> int | None:
    """Read ``parsed.port`` without letting urllib's raw error escape.

    ``urlparse().port`` raises a bare ``ValueError`` -- "Port could not be cast
    to integer value as 'abc'" -- for a malformed or out-of-range port.  That
    reaches the user with no mention of the URL they typed and nothing to say
    the fault is a *URL* fault, which sends them looking at the server.  Reading
    it here turns it into this tool's own message.
    """
    try:
        return parsed.port
    except ValueError:
        raise ValueError(
            f"Invalid Seafile URL format: invalid port in '{parsed.netloc}': {url}"
        ) from None


def normalize_netloc(url_or_netloc: str) -> str:
    """Normalize a server URL or bare netloc for consistent comparisons.

    Lowercases the hostname and strips default ports (443 for HTTPS, 80 for
    HTTP), so credential lookups and URL rewriting compare authorities rather
    than spellings: `HTTPS://Host:443` and `https://host` are the same place.

    A malformed port is a clean error, not a raw `ValueError`: this runs on a
    server URL a user typed into `SEAFILE_SERVER`, where "invalid port" is the
    only useful thing to say.
    """
    if not url_or_netloc:
        return ""
    original = url_or_netloc
    if "://" not in url_or_netloc:
        url_or_netloc = f"http://{url_or_netloc}"
    parsed = urlparse(url_or_netloc)
    host = (parsed.hostname or "").lower()
    port = _port_or_none(parsed, original)
    if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
        return f"{host}:{port}"
    return host


def mask_url(url: str) -> str:
    """Return *url* with any ``user:password@`` userinfo redacted.

    A URL a user typed may embed credentials (`seafile://user:pass@host/...`),
    and URLs end up echoed -- in the gc tip that names the raw URL, in lock
    wait messages, in CLI status lines.  Anything interpolated into output
    goes through this first, so the token a user keeps in the URL never lands
    in a terminal scrollback or a pasted log.  URLs without userinfo are
    returned unchanged.

    Nested schemes are the common spelling here (`seafile://https://host/...`
    is how parse_seafile_url accepts an explicit server), so the seafile://
    prefix is stripped before parsing and re-added afterwards: urlparse sees
    the *inner* scheme's authority, which is where the credentials live.
    """
    prefix = ""
    body = url
    if url[:10].lower() == "seafile://":
        prefix = url[:10]
        body = url[10:]
    if "@" not in body:
        return url
    parsed = urlparse(body if "://" in body else f"//{body}")
    if not parsed.username:
        return url
    host = (parsed.hostname or "").lower()
    netloc = host
    try:
        port = parsed.port
    except ValueError:
        # This runs on the *error* path -- a URL is masked precisely when it is
        # about to be echoed -- so it must never raise.  A malformed port is
        # dropped rather than reported: the redaction is what matters here, and
        # the parser is about to tell whoever typed it.
        port = None
    if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
        netloc = f"{netloc}:{port}"
    rest = parsed.path or ""
    if parsed.query:
        rest = f"{rest}?{parsed.query}"
    if parsed.fragment:
        rest = f"{rest}#{parsed.fragment}"
    scheme = f"{parsed.scheme}://" if parsed.scheme else ""
    return f"{prefix}{scheme}{netloc}{rest}"


def _looks_like_host(segment: str) -> bool:
    """Heuristic: does this URL segment name a server rather than a library?

    Two signals are treated as instructions rather than hints: a port
    ("host:8443") and userinfo ("user@host").  Both are always honoured.

    A dot is only a hint.  It counts when the name has the shape of a real
    hostname -- dot-separated labels ending in an *alphabetic* label.  That
    keeps version-suffixed library names usable: `notes.v2` and `proj.2026` end
    in a numeric label, which no hostname does, so they stay libraries even with
    a sub-path after them.

    The heuristic cannot decide every case, and does not pretend to.  A
    two-label name with an alphabetic suffix -- `my.lib`, `seafile.local` -- is
    genuinely ambiguous, and is read as a server.  The explicit-scheme form is
    how to say otherwise: `seafile://https://<server>/my.lib/<path>` names the
    server outright, which is what makes `my.lib` a library there.

    Only the bare form consults this, and only when a library *and* a path
    follow, so a two-segment URL never reaches it.
    """
    if segment in (".", ".."):
        return False
    host_part = segment.split("@")[-1].split(":")[0]
    if host_part in (".", "..") or not host_part:
        return False
    if ":" in segment or "@" in segment:
        return True
    return _HOSTNAME_RE.fullmatch(host_part) is not None


def _path_components(raw: str, url: str) -> list[str]:
    """Unquote *raw*, split it on ``/``, and validate every component.

    Unquoting happens **before** splitting.  The other order -- split, then
    unquote each part -- leaves an encoded separator intact, so
    ``a%2F..%2F..%2Fb`` survives as the single component ``a/../../b`` and slips
    past a ``.``/``..`` check that only ever sees whole components.  Unquoting
    first makes ``%2F`` a real separator, and every component it produces is then
    held to the same rule.

    A component that is ``.``, ``..`` or carries a NUL is refused.  The first
    two traverse; a NUL cannot appear in a git path at all, so accepting one
    only defers the failure to a layer that cannot explain it.
    """
    components = [p for p in unquote(raw).strip("/").split("/") if p]
    for part in components:
        if part in (".", ".."):
            raise ValueError(
                f"Invalid Seafile URL format: path segments cannot contain '.' or '..': {url}"
            )
        if "\x00" in part:
            raise ValueError(
                f"Invalid Seafile URL format: path segments cannot contain a NUL byte: {url}"
            )
    return components


def _server_url_from_bare_host(segment: str) -> str:
    """Build a server URL from a bare ``host[:port]`` segment.

    The bare form carries no scheme, so one must be chosen.  A port, when given,
    is an explicit instruction and is honoured -- including the scheme it
    implies, so ``host:80`` stays plain HTTP rather than being forced onto HTTPS
    with its port silently dropped (which redirected the request to :443).  Only
    a portless host falls back to the documented HTTPS default.

    A bracketed IPv6 literal (``[::1]``, ``[2001:db8::1]:8443``) is parsed as
    one address: the brackets are what make its colons unambiguous.  An
    *unbracketed* colon-bearing segment is refused with the spelling that
    works -- guessing would produce a nonsense authority that fails far from
    where the URL was written.
    """
    host = segment.split("@")[-1]
    if host.startswith("["):
        close = host.find("]")
        if close == -1:
            raise ValueError(
                f"Invalid Seafile URL format: unterminated '[' in host '{segment}'"
            )
        v6 = host[1:close]
        if not v6:
            raise ValueError(f"Invalid Seafile URL format: empty IPv6 address in '{segment}'")
        rest = host[close + 1 :]
        if not rest:
            return f"https://[{v6.lower()}]"
        if not rest.startswith(":") or not rest[1:].isdigit() or not (
            0 < int(rest[1:]) < 65536
        ):
            raise ValueError(
                f"Invalid Seafile URL format: invalid port '{rest}' in host '{segment}'"
            )
        port = int(rest[1:])
        scheme = "http" if port == 80 else "https"
        if port == (80 if scheme == "http" else 443):
            return f"{scheme}://[{v6.lower()}]"
        return f"{scheme}://[{v6.lower()}]:{port}"

    if ":" not in host:
        return f"https://{host.lower()}"

    hostname, _, port_s = host.rpartition(":")
    if not hostname:
        raise ValueError(f"Invalid Seafile URL format: missing host in '{segment}'")
    if ":" in hostname:
        raise ValueError(
            f"Invalid Seafile URL format: '{segment}' looks like an unbracketed "
            f"IPv6 address. Write it with brackets -- "
            f"seafile://[{host}]/<library>/<path> -- or with an explicit scheme: "
            f"seafile://https://[{host}]/<library>/<path>"
        )
    if not port_s.isdigit() or not (0 < int(port_s) < 65536):
        raise ValueError(
            f"Invalid Seafile URL format: invalid port '{port_s}' in host '{segment}'"
        )
    port = int(port_s)
    # :80 is HTTP, everything else is assumed HTTPS (the tool's default).
    scheme = "http" if port == 80 else "https"
    if port == (80 if scheme == "http" else 443):
        return f"{scheme}://{hostname.lower()}"
    return f"{scheme}://{hostname.lower()}:{port}"


@dataclass(frozen=True)
class SeafileURL:
    """Parsed representation of a seafile:// URL."""

    server_url: str | None
    library_name: str
    repo_path: str
    raw_url: str


def parse_seafile_url(url: str) -> SeafileURL:
    """Parse a seafile:// URL into a structured SeafileURL dataclass.

    Accepted forms:

      seafile://<library>/<path>                  server from credentials
      seafile://<library>/<sub>/<path>            ditto, nested path
      seafile://<host>/<library>/<path>           server from the first segment
      seafile://https://<host>/<library>/<path>   server stated explicitly

    The third form is the only ambiguous one, so it is kept deliberately
    narrow: the first segment is read as a host only when a library *and* a
    path follow it, and only when it has the shape of a real hostname --
    dot-separated labels ending in an alphabetic one. A two-segment URL is
    therefore always <library>/<path>, and a version-suffixed library
    (`notes.v2`, `proj.2026`) stays a library, because a numeric final label is
    not something a hostname can have.

    Some names read either way -- `my.lib`, `seafile.local` -- and are taken as
    servers. The explicit-scheme form is how to say otherwise::

        seafile://my.lib/team/repo                  -> server my.lib
        seafile://https://server.example/my.lib/team/repo
                                                    -> library my.lib
    """
    # Schemes are case-insensitive per RFC 3986 and git passes user-typed URLs
    # through verbatim, so SEAFILE://... must match the prefix too; a
    # case-sensitive compare would let it fall into the bare-path branch below
    # and be silently mis-parsed (e.g. "SEAFILE:" looking host-like).
    stripped = url[10:] if url[:10].lower() == "seafile://" else url
    server_url = None

    # Scheme comparison is case-insensitive per RFC 3986; urlparse normalizes the
    # scheme to lowercase while leaving host/path casing untouched.
    if stripped.lower().startswith(("http://", "https://")):
        parsed = urlparse(stripped)
        scheme = parsed.scheme
        host = (parsed.hostname or "").lower()
        if not host or host in (".", ".."):
            raise ValueError(f"Invalid Seafile URL format: invalid host '{host}': {url}")
        port = _port_or_none(parsed, url)
        if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
            server_url = f"{scheme}://{host}:{port}"
        else:
            server_url = f"{scheme}://{host}"
        path_parts = _path_components(parsed.path, url)
    else:
        parts = _path_components(stripped, url)
        if len(parts) >= 3 and _looks_like_host(parts[0]):
            server_url = _server_url_from_bare_host(parts[0])
            path_parts = parts[1:]
        else:
            path_parts = parts

    if not path_parts:
        raise ValueError(f"Invalid Seafile URL format: {url}")

    # A lone hostname-shaped segment is a server with no library named.
    if server_url is None and len(path_parts) == 1 and _looks_like_host(path_parts[0]):
        raise ValueError(
            f"'{url}' names a server but no library. Write "
            f"seafile://{path_parts[0]}/<library>/<path>, or "
            f"seafile://https://{path_parts[0]}/<library>/<path> to be explicit."
        )

    library_name = path_parts[0]
    repo_path = "/" + "/".join(path_parts[1:]) if len(path_parts) > 1 else "/git-repo"
    return SeafileURL(
        server_url=server_url,
        library_name=library_name,
        repo_path=repo_path,
        raw_url=url,
    )
