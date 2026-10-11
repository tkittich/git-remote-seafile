"""url.py - Seafile URL parsing and normalization for git-remote-seafile."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlparse


def normalize_netloc(url_or_netloc: str) -> str:
    """Normalize a server URL or bare netloc for consistent comparisons.

    Lowercases the hostname and strips default ports (443 for HTTPS, 80 for
    HTTP), so credential lookups and URL rewriting compare authorities rather
    than spellings: `HTTPS://Host:443` and `https://host` are the same place.
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


def _looks_like_host(segment: str) -> bool:
    """Heuristic: does this URL segment name a server rather than a library?

    A port ("host:8443") or a dot ("seafile.example.com") is a strong hint.
    This is only consulted for the bare form, and only when a library and a
    path follow, so a library whose name merely contains a dot is never caught
    by it.
    """
    if segment in (".", ".."):
        return False
    host_part = segment.split("@")[-1].split(":")[0]
    if host_part in (".", "..") or not host_part:
        return False
    if ":" in segment:
        return True
    return "." in host_part


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
    path follow it, and it actually looks like a hostname. A two-segment
    URL is therefore always <library>/<path>. When a name really is ambiguous,
    the explicit-scheme form settles it.
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
        port = parsed.port
        if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
            server_url = f"{scheme}://{host}:{port}"
        else:
            server_url = f"{scheme}://{host}"
        path_parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
    else:
        parts = [unquote(p) for p in stripped.strip("/").split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise ValueError(f"Invalid Seafile URL format: path segments cannot contain '.' or '..': {url}")
        if len(parts) >= 3 and _looks_like_host(parts[0]):
            server_url = _server_url_from_bare_host(parts[0])
            path_parts = parts[1:]
        else:
            path_parts = parts

    if not path_parts:
        raise ValueError(f"Invalid Seafile URL format: {url}")

    if any(p in (".", "..") for p in path_parts):
        raise ValueError(f"Invalid Seafile URL format: path segments cannot contain '.' or '..': {url}")

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
