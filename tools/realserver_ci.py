"""realserver_ci.py - Wait for a Seafile container, then run the real-server suite.

The container workflow (`.github/workflows/realserver.yml`) calls this rather
than inlining the steps in YAML: readiness, token, scratch-library lifecycle
and cleanup are logic with failure modes, and YAML is where those failure
modes go unseen.  Everything the suite needs -- a reachable server, a token,
a scratch library named ``grs-ci-scratch`` -- is set up here, torn down here,
and nothing outside that library is touched.

Against a manually started container (the compose file in `.github/`), the
same script runs locally: the server, admin credentials and library name are
overridable through ``GRS_CI_SEAFILE_SERVER``, ``GRS_CI_ADMIN_EMAIL`` and
``GRS_CI_ADMIN_PASSWORD``.

Exit code is the suite's exit code; a readiness or setup failure exits
non-zero with the reason on stderr.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = "grs-ci-scratch"
POLL_SECONDS = 5.0
#: First boot initializes MariaDB, then seahub, and the image pull itself is
#: part of this budget on a cold runner.  Ten minutes was the observed cost
#: with margin; this is the one knob that should be generous.
READINESS_BUDGET_SECONDS = 900.0


def _server() -> str:
    return os.environ.get("GRS_CI_SEAFILE_SERVER", "http://localhost:8080").rstrip("/")


def _admin() -> tuple[str, str]:
    return (
        os.environ.get("GRS_CI_ADMIN_EMAIL", "ci@grs.invalid"),
        os.environ.get("GRS_CI_ADMIN_PASSWORD", "grs-ci-admin"),
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Token {token}"}


def wait_for_seahub(deadline: float) -> None:
    """Poll ``/api2/ping/`` until seahub answers, or fail with the last answer.

    A first boot answers 502 from nginx while seahub initializes and refuses
    connections before nginx is up; both are "not yet", not "broken".
    """
    server = _server()
    last = "never polled"
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{server}/api2/ping/", timeout=10)
            if resp.status_code == 200 and "pong" in resp.text:
                return
            last = f"HTTP {resp.status_code}: {resp.text[:120]}"
        except requests.RequestException as exc:
            last = str(exc)[:160]
        time.sleep(POLL_SECONDS)
    raise SystemExit(
        f"the Seafile container did not become ready within "
        f"{READINESS_BUDGET_SECONDS:.0f}s; last answer from {server}: {last}"
    )


def auth_token(session: requests.Session) -> str:
    email, password = _admin()
    resp = session.post(
        f"{_server()}/api2/auth-token/",
        data={"username": email, "password": password},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"auth-token failed: HTTP {resp.status_code}: {resp.text[:200]}")
    token = str(resp.json().get("token", ""))
    if not token:
        raise SystemExit("auth-token answered 200 but carried no token")
    return token


def find_library(session: requests.Session, token: str, name: str) -> str | None:
    resp = session.get(f"{_server()}/api2/repos/", headers=_headers(token), timeout=30)
    if resp.status_code != 200:
        raise SystemExit(f"listing libraries failed: HTTP {resp.status_code}: {resp.text[:200]}")
    for entry in resp.json():
        if isinstance(entry, dict) and entry.get("name") == name:
            return str(entry.get("id", "")) or None
    return None


def create_library(session: requests.Session, token: str, name: str) -> str:
    resp = session.post(
        f"{_server()}/api2/repos/",
        headers=_headers(token),
        data={"name": name, "desc": "git-remote-seafile CI scratch"},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise SystemExit(f"creating the scratch library failed: HTTP {resp.status_code}: {resp.text[:200]}")
    repo_id = str(resp.json().get("repo_id", ""))
    if not repo_id:
        raise SystemExit("creating the scratch library answered without a repo_id")
    return repo_id


def delete_library(session: requests.Session, token: str, repo_id: str) -> None:
    resp = session.delete(f"{_server()}/api2/repos/{repo_id}/", headers=_headers(token), timeout=120)
    if resp.status_code not in (200, 404):
        sys.stderr.write(
            f"Warning: deleting the scratch library {repo_id} answered "
            f"HTTP {resp.status_code}; it may be left behind on the container "
            "(which is discarded with the runner).\n"
        )
        sys.stderr.flush()


def main() -> int:
    deadline = time.monotonic() + READINESS_BUDGET_SECONDS
    print(f"Waiting for {_server()} to come up (first boot takes minutes)...")
    sys.stdout.flush()
    wait_for_seahub(deadline)
    print("Seahub is up.")
    sys.stdout.flush()

    session = requests.Session()
    token = auth_token(session)

    # Residue from an earlier failed run would make get_repo_id ambiguous;
    # the scratch library is the one thing this script owns, so it is
    # recreated rather than reused.
    stale = find_library(session, token, LIBRARY)
    if stale is not None:
        print(f"Removing scratch library {LIBRARY} left by an earlier run...")
        delete_library(session, token, stale)

    repo_id = create_library(session, token, LIBRARY)
    print(f"Scratch library {LIBRARY} is {repo_id}.")
    sys.stdout.flush()

    status = 1
    try:
        env = dict(os.environ)
        env["SEAFILE_SERVER"] = _server()
        env["SEAFILE_TOKEN"] = token
        env["SEAFILE_TEST_LIBRARY"] = LIBRARY
        # The suite is the same one a developer runs by hand; a failure here
        # is a finding about the server, not about this script.
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_realserver", "-v"],
            cwd=str(ROOT),
            env=env,
        )
        status = proc.returncode
    finally:
        delete_library(session, token, repo_id)
    if status == 0:
        print("Real-server suite passed against the container.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
