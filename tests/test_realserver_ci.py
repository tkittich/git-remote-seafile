"""test_realserver_ci.py - The container-workflow driver's logic, without a container.

``tools/realserver_ci.py`` is CI glue whose failure modes (readiness polling,
token and library lifecycle) only otherwise surface on a real runner, which is
exactly where this project's history says things fail first.  These tests pin
the parts that need no server.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("realserver_ci", _ROOT / "tools" / "realserver_ci.py")
assert _spec is not None and _spec.loader is not None
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


class _Resp:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Session:
    def __init__(self, get_resp=None, post_resp=None):
        self._get = get_resp
        self._post = post_resp

    def get(self, url, headers=None, timeout=None):
        return self._get

    def post(self, url, data=None, headers=None, timeout=None):
        return self._post

    def delete(self, url, headers=None, timeout=None):
        return _Resp(200)


class TestRealserverCI(unittest.TestCase):
    def test_the_ping_loop_accepts_a_pong(self):
        with patch.object(ci.requests, "get", return_value=_Resp(200, "pong")):
            ci.wait_for_seahub(deadline=ci.time.monotonic() + 10)

    def test_the_ping_loop_fails_with_the_last_answer_after_the_budget(self):
        err = ci.requests.ConnectionError("connection refused")
        with patch.object(ci.requests, "get", side_effect=err):
            with patch.object(ci.time, "sleep"):
                with self.assertRaises(SystemExit) as ctx:
                    ci.wait_for_seahub(deadline=ci.time.monotonic() + 0.05)
        self.assertIn("did not become ready", str(ctx.exception))
        self.assertIn("connection refused", str(ctx.exception))

    def test_a_bad_auth_answer_exits_naming_the_failure(self):
        with self.assertRaises(SystemExit) as ctx:
            ci.auth_token(_Session(post_resp=_Resp(400, "invalid credentials")))
        self.assertIn("auth-token failed", str(ctx.exception))
        self.assertIn("invalid credentials", str(ctx.exception))

    def test_the_scratch_library_is_found_by_name_and_absent_is_none(self):
        session = _Session(get_resp=_Resp(payload=[
            {"name": "other", "id": "x"},
            {"name": "grs-ci-scratch", "id": "rid1"},
        ]))
        self.assertEqual(ci.find_library(session, "tok", "grs-ci-scratch"), "rid1")
        self.assertIsNone(ci.find_library(session, "tok", "absent"))

    def test_creating_a_library_returns_the_repo_id(self):
        session = _Session(post_resp=_Resp(200, payload={"repo_id": "rid2"}))
        self.assertEqual(ci.create_library(session, "tok", "grs-ci-scratch"), "rid2")

    def test_the_server_and_header_helpers_honor_their_inputs(self):
        self.assertEqual(ci._headers("t0k"), {"Authorization": "Token t0k"})
        with patch.dict("os.environ", {"GRS_CI_SEAFILE_SERVER": "http://box:1234/"}):
            self.assertEqual(ci._server(), "http://box:1234")
        self.assertEqual(ci._server(), "http://127.0.0.1")

    def test_the_helper_home_carries_the_server_and_token_the_suite_strips_from_env(self):
        with tempfile.TemporaryDirectory() as parent:
            home = ci.helper_home(parent, "t0k")
            cfg = json.loads((Path(home) / ".git-seafile.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg, {"server": "http://127.0.0.1", "token": "t0k"})


if __name__ == "__main__":
    unittest.main()
