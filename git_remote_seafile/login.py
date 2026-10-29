"""login.py - ``git-remote-seafile login``: trade a password for an API token and keep the token.

Without the Seafile desktop client the only way to authenticate was to fetch a
token by hand (``curl -d username=... /api2/auth-token/``) and write
``~/.git-seafile.json`` yourself.  This does that, once, and never stores the
password: it is read from the terminal, sent once to the server's own token
endpoint, and dropped.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import requests

from .url import normalize_netloc

OTP_HEADER = "X-SEAFILE-OTP"
CONFIG_NAME = ".git-seafile.json"
TIMEOUT_SECONDS = 30


class LoginError(Exception):
    """A login that cannot go on; the message is what the user is told."""


def _is_loopback(server: str) -> bool:
    host = normalize_netloc(server).rsplit(":", 1)[0].strip("[]").lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")


def normalise_server(raw: str) -> str:
    """``seafile.example.com`` -> ``https://seafile.example.com``; a path is kept."""
    server = raw.strip().rstrip("/")
    if not server:
        raise LoginError("no server was given")
    if "://" not in server:
        server = "https://" + server
    if not server.lower().startswith(("https://", "http://")):
        raise LoginError(f"'{raw}' is not an http(s) server address")
    if server.lower().startswith("http://") and not _is_loopback(server):
        # The password would cross the network readable by anything on the path.
        raise LoginError(
            f"{server} is plain HTTP, and a password sent over it can be read on the way; "
            "use the https:// address"
        )
    return server


def request_token(
    server: str,
    username: str,
    password: str,
    otp: str | None = None,
    *,
    post: Callable[..., Any] | None = None,
) -> Any:
    """POST the credentials to the server's token endpoint and return the response."""
    headers = {OTP_HEADER: otp} if otp else {}
    do_post = post or requests.post
    try:
        return do_post(
            f"{server}/api2/auth-token/",
            data={"username": username, "password": password},
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as ex:
        raise LoginError(f"cannot reach {server}: {type(ex).__name__}") from None


def _needs_otp(resp: Any) -> bool:
    return resp.status_code == 400 and str(resp.headers.get("X-Seafile-OTP", "")).lower() == "required"


def obtain_token(
    server: str,
    username: str,
    password: str,
    *,
    ask: Callable[[str], str],
    post: Callable[..., Any] | None = None,
) -> str:
    """The API token for these credentials, asking for a two-factor code if the server wants one."""
    resp = request_token(server, username, password, post=post)
    asked_otp = _needs_otp(resp)
    if asked_otp:
        resp = request_token(server, username, password, ask("Two-factor code: ").strip(), post=post)
    if resp.status_code == 200:
        try:
            token = resp.json().get("token")
        except (ValueError, AttributeError):
            token = None
        if isinstance(token, str) and token:
            return token
        raise LoginError(
            f"{server} answered with HTTP 200 but no token; check the server address "
            "(a proxy or login page?)"
        )
    if resp.status_code == 400:
        raise LoginError(
            f"{server} refused that username or password"
            + (" or two-factor code" if asked_otp else "")
        )
    if resp.status_code == 404:
        raise LoginError(f"{server} has no Seafile API at /api2/auth-token/; check the server address")
    raise LoginError(f"{server} answered the login with HTTP {resp.status_code}")


def save_credentials(config_path: Path, server: str, token: str, username: str) -> str | None:
    """Write the server and token to *config_path*, owner-only; return a note if one replaced another server.

    Other keys in an existing file are kept.  The file is replaced atomically so
    an interruption cannot leave half a token.
    """
    existing: dict[str, Any] = {}
    note = None
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
        old = existing.get("server")
        if isinstance(old, str) and old and normalize_netloc(old) != normalize_netloc(server):
            note = f"the file held an account for {old}; it now holds {server} (one account per file)"
    existing.update({"server": server, "token": token, "username": username})
    fd, tmp_name = tempfile.mkstemp(prefix=".git-seafile-", suffix=".tmp", dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=2)
            handle.write("\n")
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, config_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return note


def run_login(
    args: list[str],
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    post: Callable[..., Any] | None = None,
    config_path: Path | None = None,
    known_server: str | None = None,
    interactive: bool | None = None,
) -> int:
    """``login [<server>] [--user NAME]``.  Returns the exit code."""
    server_arg: str | None = None
    user_arg: str | None = None
    it = iter(args)
    for arg in it:
        if arg == "--user":
            user_arg = next(it, None)
            if not user_arg:
                sys.stderr.write("Usage: git-remote-seafile login [<server>] [--user NAME]\n")
                return 2
        elif arg.startswith("-") or server_arg is not None:
            sys.stderr.write("Usage: git-remote-seafile login [<server>] [--user NAME]\n")
            return 2
        else:
            server_arg = arg
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        sys.stderr.write(
            "Error: login asks for your password, which needs a terminal. For a script or CI set "
            "SEAFILE_SERVER and SEAFILE_TOKEN instead (User Guide section 6).\n"
        )
        return 1
    try:
        default = known_server or ""
        raw = server_arg or ask(f"Seafile server{f' [{default}]' if default else ''}: ").strip() or default
        server = normalise_server(raw)
        username = user_arg or ask("Username (your email): ").strip()
        if not username:
            raise LoginError("no username was given")
        password = ask_secret("Password: ")
        if not password:
            raise LoginError("no password was given")
        token = obtain_token(server, username, password, ask=ask, post=post)
    except LoginError as ex:
        sys.stderr.write(f"Error: {ex}\n")
        return 1
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\nError: login cancelled; nothing was saved.\n")
        return 1
    path = config_path or Path.home() / CONFIG_NAME
    try:
        note = save_credentials(path, server, token, username)
    except OSError as ex:
        sys.stderr.write(f"Error: the token could not be saved to {path}: {ex}\n")
        return 1
    if note:
        sys.stderr.write(f"Note: {note}.\n")
    print(f"Logged in to {server} as {username}; the token is saved in {path} (the password is not).")
    print("Next: git-remote-seafile doctor <seafile://library/path> to check a destination.")
    return 0
