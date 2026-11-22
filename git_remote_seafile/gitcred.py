"""gitcred.py - the ``git credential`` fallback (specified in CREDENTIAL.md).

A machine with no ``SEAFILE_TOKEN``, no ``~/.git-seafile.json`` and no desktop
client can keep its token in whatever store git already uses (the Windows
Credential Manager, the macOS keychain, libsecret) instead of a plaintext file.

Three rules shape everything here:

* **Never prompt, never hang.**  ``GIT_TERMINAL_PROMPT=0`` stops only git's own
  terminal prompt; an askpass program (an editor's integrated terminal sets one)
  and Git Credential Manager have switches of their own, so all of them are set
  and the child runs in its own process group, which is killed as a whole at the
  limit so a helper's own child cannot outlive the call or hold a pipe.
* **Only ever our own entries.**  The username is always ``seafile-token`` or
  ``seafile-token:<account>``, never empty: with none, a helper returns whatever
  it has for the host, which on a shared host is the user's Git password.  A
  credential that comes back under another username is ignored, and ``reject``
  is only ever sent for an entry we asked for.
* **No secret outside standard input.**  The token travels on the child's
  standard input; the environment switches are not secrets.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

#: The username of every credential this module stores or asks for.
USERNAME = "seafile-token"

#: Seconds before the whole process group is killed.
TIMEOUT = 20.0

#: The switches that close every way git or a helper can ask the user.  None of
#: them is a secret.  ``GCM_ALLOW_WINDOWSAUTH=false`` is the one that stops Git
#: Credential Manager probing the Seafile host (an HTTP HEAD, for Windows
#: integrated authentication) on a lookup that finds nothing; the generic-provider
#: switch alone does not (measured, Windows, GCM 2.9.1).
_QUIET_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
    "GCM_PROVIDER": "generic",
    "GCM_ALLOW_WINDOWSAUTH": "false",
}

_QUIET_ARGS = [
    "-c", "core.askPass=",
    "-c", "credential.interactive=never",
    "-c", "credential.useHttpPath=true",
]

_DEFAULT_PORTS = {"https": 443, "http": 80}
_BAD = re.compile(r"[\x00-\x1f\x7f]")


class CredentialRequestError(ValueError):
    """The server address cannot be turned into a safe credential request."""


@dataclass
class Found:
    """What ``fill`` returned for one of our usernames."""

    token: str
    username: str


@dataclass
class Outcome:
    """The result of a lookup: a credential, or why there is none."""

    found: Found | None = None
    tried: list[str] = field(default_factory=list)
    note: str = ""


def usernames(account: str | None) -> list[str]:
    """The names to try, most specific first."""
    if account:
        return [f"{USERNAME}:{account}", USERNAME]
    return [USERNAME]


def _clean(name: str, value: str) -> str:
    if not value or _BAD.search(value) or any(ch in value for ch in " \t"):
        raise CredentialRequestError(f"the {name} {value!r} is not safe to hand to git credential")
    return value


def build_request(server_url: str, username: str) -> dict[str, str]:
    """The ``key=value`` fields of one credential request, validated.

    The host is lower-cased, converted to ASCII (punycode) and loses a default
    port; the path loses its leading and trailing slashes (a leading slash is
    stored as ``//path`` by git's own ``credential-store`` and then cannot be read
    back); a URL with userinfo is refused, and so is any field with a control
    character, which is how a newline would smuggle in a second field.
    """
    parts = urlsplit(server_url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise CredentialRequestError(f"the server address {server_url!r} is not an http or https URL")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise CredentialRequestError("the server address carries a user name or password; remove it")
    if _BAD.search(server_url):
        raise CredentialRequestError("the server address has a control character")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as ex:
        raise CredentialRequestError(f"the server address {server_url!r} is not valid: {ex}") from ex
    if not host:
        raise CredentialRequestError(f"the server address {server_url!r} has no host")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as ex:
        raise CredentialRequestError(f"the host {host!r} is not a valid name") from ex
    host = _clean("host", host.lower())
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    request = {"protocol": scheme, "host": host}
    path = parts.path.strip("/")
    if path:
        request["path"] = _clean("path", path)
    request["username"] = _clean("username", username)
    return request


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_QUIET_ENV)
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _run(action: str, fields: dict[str, str], timeout: float) -> tuple[int | None, str]:
    """Run ``git credential <action>`` with ``fields`` on standard input.

    Returns ``(returncode, stdout)``; ``returncode`` is ``None`` when git is
    missing or the limit was hit (the process group is killed).
    """
    payload = "".join(f"{k}={v}\n" for k, v in fields.items()) + "\n"
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            ["git", *_QUIET_ARGS, "credential", action],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_env(), text=True, **kwargs,
        )
    except OSError:
        return None, ""
    try:
        out, _ = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError):
            pass
        return None, ""
    return proc.returncode, out or ""


def _parse(out: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    return fields


def fill(server_url: str, account: str | None = None, timeout: float = TIMEOUT) -> Outcome:
    """Ask git for our token for ``server_url``.

    Tries ``seafile-token:<account>`` then ``seafile-token``.  An answer whose
    username is not the one sent, or whose path differs from the one sent, is
    ignored: a helper must not be able to hand us someone else's credential.
    """
    outcome = Outcome()
    for name in usernames(account):
        outcome.tried.append(name)
        request = build_request(server_url, name)
        code, out = _run("fill", request, timeout)
        if code is None:
            outcome.note = "git credential did not answer in time (or git is missing)"
            continue
        if code != 0:
            continue
        answer = _parse(out)
        token = answer.get("password", "")
        if not token or answer.get("username", name) != name:
            continue
        if "path" in answer and answer["path"].strip("/") != request.get("path", ""):
            continue
        outcome.found = Found(token=token, username=name)
        return outcome
    return outcome


def approve(server_url: str, username: str, token: str, timeout: float = TIMEOUT) -> bool:
    """Store ``token`` under our name.  Call only after it authenticated."""
    request = build_request(server_url, username)
    request["password"] = token
    code, _ = _run("approve", request, timeout)
    return code == 0


def reject(server_url: str, username: str, timeout: float = TIMEOUT) -> bool:
    """Erase our stale entry, then check it is gone with one more ``fill``.

    Returns True when a following lookup finds nothing under ``username``.  Only
    ever called for an entry ``fill`` returned, after the server said 401.
    """
    request = build_request(server_url, username)
    _run("reject", request, timeout)
    code, out = _run("fill", request, timeout)
    if code != 0:
        return True
    answer = _parse(out)
    return not answer.get("password")


def have_git() -> bool:
    try:
        return subprocess.run(
            ["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


__all__ = [
    "CredentialRequestError", "Found", "Outcome", "USERNAME", "approve", "build_request", "fill",
    "have_git", "reject", "usernames",
]
