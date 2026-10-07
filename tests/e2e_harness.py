"""e2e_harness.py - offline end-to-end harness for git-remote-seafile.

Everything here runs on 127.0.0.1 with no network, no Seafile server and no
real credentials.  It provides three things:

  SeafileStub      An in-memory stand-in for the Seafile Web API v2.1, served on
                   an ephemeral port.  Implements only the endpoints the helper
                   actually calls, backed by a dict.  Supports fault injection
                   (``stub.faults``) so tests can exercise the failure paths a
                   healthy server will never produce on demand.

  make_helper_env  Builds an environment in which a *real* ``git`` binary can
                   drive the *real* helper against the stub: a ``git-remote-seafile``
                   shim on PATH plus SEAFILE_SERVER/SEAFILE_TOKEN pointing at the
                   stub.

  run_git          Runs a git command with that environment.

Why this exists: the unit suite mocks at the HTTP boundary, so it cannot catch
bugs that only appear when real git speaks the remote-helper protocol to the real
helper code over a real socket.  This harness closes that gap.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

REPO_ID = "testlib-id"
REPO_NAME = "testlib"


def _norm(p: str) -> str:
    return "/" + p.strip("/")


def _parse_multipart(body: bytes, boundary: bytes):
    """Minimal multipart/form-data parser.

    Returns (fields, filename, filedata).  Byte-exact: we slice the CRLF that
    precedes the boundary rather than rstrip()ing, so a binary payload ending in
    a '-' or newline byte is not corrupted.
    """
    fields: dict[str, str] = {}
    filename = None
    filedata = None
    for chunk in body.split(b"--" + boundary):
        chunk = chunk.lstrip(b"\r\n")
        if chunk.rstrip(b"\r\n") in (b"", b"--"):
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        head, data = chunk.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        m_file = re.search(rb'filename="([^"]*)"', head)
        m_name = re.search(rb'name="([^"]*)"', head)
        if m_file:
            filename = m_file.group(1).decode("utf-8", "replace")
            filedata = data
        elif m_name:
            fields[m_name.group(1).decode("utf-8", "replace")] = data.decode("utf-8", "replace")
    return fields, filename, filedata


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, *args):  # silence request logging
        pass

    @property
    def stub(self) -> "SeafileStub":
        return self.server.stub  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _query_p(self) -> str:
        return parse_qs(urlparse(self.path).query).get("p", ["/"])[0]

    def _rid(self):
        # Parse first: a client with proxy env vars set sends an absolute-form
        # request target ("http://host/api2/...") rather than an origin-form one
        # ("/api2/..."), and a leading-scheme regex match would return None.
        m = re.match(r"/api2/repos/([^/]+)/", urlparse(self.path).path)
        return m.group(1) if m else None

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _drain(self) -> None:
        """Consume any request body.

        With HTTP/1.1 keep-alive the client reuses the socket, so an unread body
        is parsed as the next request line.  Every handler must drain.
        """
        self._read_body()

    # -- endpoints --------------------------------------------------------
    def do_GET(self):
        self._drain()
        path = urlparse(self.path).path
        f = self.stub.faults

        if path == "/api2/repos/":
            if f.get("repos"):
                return self._send(f["repos"], b"injected failure")
            return self._json([{"id": REPO_ID, "name": REPO_NAME}])

        if path == "/api2/account/info/":
            return self._json({"email": "e2e@example.com"})

        if path.startswith("/api2/repos/") and path.endswith("/dir/"):
            if f.get("dir"):
                return self._send(f["dir"], b"injected failure")
            rid, p = self._rid(), self._query_p()
            # Simulate a transient 404 on a scoped subset of listings (e.g. the
            # refs namespace) while the rest of the repo stays readable.  This
            # is how a healthy-looking server can make a populated repo look
            # empty.
            if f.get("dir_404") and f["dir_404"] in p:
                return self._send(404, b"[]")
            if p != "/" and not self.stub.has_under(rid, p):
                return self._send(404, b"[]")
            return self._json(self.stub.list_dir(rid, p))

        if path.startswith("/api2/repos/") and path.endswith("/file/"):
            if f.get("file"):
                return self._send(f["file"], b"injected failure")
            rid, p = self._rid(), _norm(self._query_p())
            if p not in self.stub.files.get(rid, {}):
                return self._send(404, b"null")
            link = f"http://127.0.0.1:{self.stub.port}/raw/{rid}?p={p}"
            return self._send(200, json.dumps(link).encode("utf-8"))

        if path.startswith("/api2/repos/") and path.endswith("/upload-link/"):
            if f.get("upload_link"):
                return self._send(f["upload_link"], b"injected failure")
            rid = self._rid()
            link = f"http://127.0.0.1:{self.stub.port}/upload/{rid}"
            return self._send(200, json.dumps(link).encode("utf-8"))

        if path.startswith("/raw/"):
            rid, p = path.split("/")[2], _norm(self._query_p())
            self.stub.raw_hits.append(p)
            if f.get("raw"):
                return self._send(f["raw"], b"injected failure")
            # Fail only the pack download, so the ref listing still succeeds.
            # This isolates the fetch stage: git has been told which objects to
            # expect, and then the transfer of those objects fails.
            if f.get("raw_pack") and "/objects/pack/" in p:
                return self._send(f["raw_pack"], b"injected failure")
            return self._send(200, self.stub.files.get(rid, {}).get(p, b""), "application/octet-stream")

        return self._send(404, b"{}")

    def do_POST(self):
        body = self._read_body()
        path = urlparse(self.path).path
        if path.startswith("/api2/repos/") and path.endswith("/dir/"):
            return self._send(200, b"{}")

        if path.startswith("/upload/"):
            if self.stub.faults.get("upload"):
                return self._send(self.stub.faults["upload"], b"injected failure")
            rid = path.split("/")[2]
            ctype = self.headers.get("Content-Type", "")
            m = re.search(r"boundary=([^\s;]+)", ctype)
            if not m:
                return self._send(400, b'{"error":"no boundary"}')
            fields, filename, filedata = _parse_multipart(body, m.group(1).encode())
            parent = fields.get("parent_dir", "/")
            if filename is None or filedata is None:
                return self._send(400, b'{"error":"no file part"}')
            self.stub.files.setdefault(rid, {})[_norm(parent + "/" + filename)] = filedata
            return self._send(200, b'{"id":"uploaded"}')

        return self._send(404, b"{}")

    def do_DELETE(self):
        self._drain()
        rid, p = self._rid(), _norm(self._query_p())
        if p in self.stub.files.get(rid, {}):
            del self.stub.files[rid][p]
            return self._send(200, b'{"success":true}')
        return self._send(404, b"{}")


class SeafileStub:
    """In-memory Seafile Web API v2.1 stand-in."""

    def __init__(self):
        self.files: dict[str, dict[str, bytes]] = {}
        self.faults: dict[str, int] = {}
        self.raw_hits: list[str] = []
        self.port = 0
        self._httpd = None
        self._thread = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "SeafileStub":
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.stub = self  # type: ignore[attr-defined]
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reset(self) -> None:
        self.files.clear()
        self.faults.clear()
        self.raw_hits.clear()

    # -- store helpers ----------------------------------------------------
    def has_under(self, repo_id: str, path: str) -> bool:
        prefix = _norm(path).rstrip("/") + "/"
        return any(k.startswith(prefix) for k in self.files.get(repo_id, {}))

    def list_dir(self, repo_id: str, path: str) -> list[dict]:
        prefix = _norm(path).rstrip("/") + "/"
        out, seen = [], set()
        for key in self.files.get(repo_id, {}):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if "/" in rest:
                d = rest.split("/")[0]
                if d not in seen:
                    seen.add(d)
                    out.append({"type": "dir", "name": d})
            else:
                out.append({"type": "file", "name": rest})
        return out

    def packs(self, repo_path: str) -> list[str]:
        d = _norm(repo_path + "/objects/pack")
        return sorted(n.rsplit("/", 1)[-1] for n in self.files.get(REPO_ID, {}) if n.startswith(d + "/"))


def _sh_quote(s: str) -> str:
    return '"' + s.replace("\\", "/") + '"'


def make_helper_env(bin_dir: Path, stub_url: str, repo_root: Path) -> dict:
    """Create a PATH shim that runs the real helper, and return an env dict.

    The shim is a POSIX shell script (Git for Windows executes these through its
    bundled sh).  It launches the same interpreter running the tests, with the
    repository root injected on sys.path, so no install step is required.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "_grs_launcher.py"
    launcher.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from git_remote_seafile.cli import main\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )
    shim = bin_dir / "git-remote-seafile"
    shim.write_text(
        "#!/bin/sh\n" f"exec {_sh_quote(sys.executable)} {_sh_quote(str(launcher))} \"$@\"\n",
        encoding="utf-8",
    )
    try:
        os.chmod(shim, 0o755)
    except OSError:
        pass

    env = dict(os.environ)
    # The host may export http_proxy/https_proxy (this machine does).  If the
    # helper's HTTP client honours them, it will send every request to the
    # corporate proxy and the loopback stub never sees it.  Tests are offline,
    # so drop them outright rather than relying on no_proxy matching rules.
    for var in list(env):
        if var.lower() in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy"):
            env.pop(var, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["SEAFILE_SERVER"] = stub_url
    env["SEAFILE_TOKEN"] = "e2e-token"
    # Transport-level tests: safety guardrails have their own unit tests, and
    # this keeps the run deterministic regardless of the host's Seafile client.
    env["SEAFILE_SKIP_SAFETY_CHECKS"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_git(args, cwd, env, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command, capturing output as text."""
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed (rc={proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def run_helper(args, cwd, env, check: bool = True) -> subprocess.CompletedProcess:
    """Run the helper's own CLI (``git-remote-seafile <args>``).

    The PATH shim is a POSIX shell script, which is awkward to exec directly on
    Windows; invoking the launcher with the same interpreter is equivalent and
    portable.
    """
    bin_dir = Path(env["PATH"].split(os.pathsep)[0])
    launcher = bin_dir / "_grs_launcher.py"
    proc = subprocess.run(
        [sys.executable, str(launcher), *list(args)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git-remote-seafile {' '.join(args)} failed (rc={proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def init_repo(path: Path, env: dict) -> Path:
    """Create an empty repo with a deterministic identity."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q", "-b", "main", "."], path, env)
    run_git(["config", "user.email", "e2e@example.com"], path, env)
    run_git(["config", "user.name", "E2E Tester"], path, env)
    run_git(["config", "core.autocrlf", "false"], path, env)
    return path


def commit_file(repo: Path, name: str, content: str, message: str, env: dict) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    run_git(["add", "--", name], repo, env)
    # Pass the identity explicitly instead of relying on ambient config.  A
    # clone does not inherit the local user.name/user.email that init_repo()
    # sets, and CI runners have no global identity -- GitHub's macOS images do,
    # which is why this failed only on Linux and Windows.
    run_git(
        [
            "-c", "user.email=e2e@example.com",
            "-c", "user.name=E2E Tester",
            "commit", "-q", "-m", message,
        ],
        repo,
        env,
    )


__all__ = [
    "SeafileStub",
    "make_helper_env",
    "run_git",
    "run_helper",
    "init_repo",
    "commit_file",
    "REPO_ID",
    "REPO_NAME",
]
