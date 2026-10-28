"""test_diagnose.py - the doctor's server-side checks (ROADMAP S14, S15).

The upload-limit search is tested on its own with a fake server, and end to end
through ``doctor --upload-limit`` against the in-process stub, whose
``max_upload`` fault answers 413 to a body over a size, as nginx does with
``client_max_body_size``.  Bodies here are a few MiB: the real default ceiling
(1023 MiB) is exercised by ``tests/test_realserver.py`` and the container
workflow.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import pathlib
import sys
import threading
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, SeafileStub  # noqa: E402
from git_remote_seafile import cli, diagnose  # noqa: E402
from git_remote_seafile.client import SeafileClient  # noqa: E402

MIB = diagnose.MIB


class MeasureUploadLimitTests(unittest.TestCase):
    @staticmethod
    def server(limit: int):
        calls: list[int] = []

        def attempt(size: int) -> str | None:
            calls.append(size)
            return None if size <= limit else "HTTP 413"

        return attempt, calls

    def test_a_server_with_no_limit_below_the_ceiling_costs_one_upload(self):
        attempt, calls = self.server(10**12)
        result = diagnose.measure_upload_limit(attempt, floor=MIB, ceiling=64 * MIB)
        self.assertEqual(calls, [64 * MIB])
        self.assertEqual((result.accepted, result.rejected), (64 * MIB, None))
        self.assertTrue(result.unlimited_below_ceiling)

    def test_a_limit_is_found_to_within_the_resolution(self):
        for limit_mib in (2, 5, 37, 100, 1000):
            attempt, calls = self.server(limit_mib * MIB)
            result = diagnose.measure_upload_limit(attempt, floor=MIB, ceiling=1023 * MIB)
            self.assertLessEqual(result.accepted, limit_mib * MIB)
            self.assertGreater(result.rejected, limit_mib * MIB)
            self.assertLessEqual(
                result.rejected - result.accepted, max(MIB, result.accepted // 8) + MIB
            )
            self.assertLessEqual(len(calls), 14, calls)
            self.assertEqual(result.reason, "HTTP 413")

    def test_a_server_that_refuses_even_the_floor_has_no_size_limit_to_report(self):
        result = diagnose.measure_upload_limit(lambda size: "HTTP 403", floor=MIB, ceiling=64 * MIB)
        self.assertEqual(result.accepted, 0)
        self.assertEqual(result.reason, "HTTP 403")

    def test_the_recommended_cap_leaves_headroom_under_the_limit(self):
        self.assertEqual(diagnose.recommended_pack_mib(100 * MIB), 80)
        self.assertEqual(diagnose.recommended_pack_mib(MIB // 2), 1)

    def test_zero_stream_behaves_like_a_file_of_that_size(self):
        stream = diagnose.ZeroStream(10)
        stream.seek(0, 2)
        self.assertEqual(stream.tell(), 10)
        stream.seek(3)
        self.assertEqual(stream.read(100), bytes(7))
        self.assertEqual(stream.read(5), b"")


class DoctorServerChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = SeafileStub().start()
        cls.client = SeafileClient(server_url=cls.stub.base_url, token="t")

    @classmethod
    def tearDownClass(cls):
        cls.stub.stop()

    def setUp(self):
        self.stub.reset()
        self.helper = SimpleNamespace(
            client=self.client, repo_id=REPO_ID, repo_path="/repo", library_name="lib",
        )

    def put(self, path: str, data: bytes = b"x") -> None:
        self.stub.files.setdefault(REPO_ID, {})[path] = data

    def upload_limit(self, max_mib: int):
        out = io.StringIO()
        configured = SimpleNamespace(gc_max_pack_mib=1024)
        with contextlib.redirect_stdout(out), \
                patch("git_remote_seafile.config.RemoteConfig.load", return_value=configured):
            code = cli._doctor_upload_limit(self.helper, max_mib)
        return code, out.getvalue()

    # -- S14 -------------------------------------------------------------------
    def test_conflict_copies_are_found_anywhere_under_the_repository_path(self):
        self.put("/repo/refs/heads/main")
        self.put("/repo/refs/heads/main (SFConflict me 2026-10-12-10-00-00)")
        self.put("/repo/packs/pack-1.pack")
        self.put("/repo/packs/pack-1 (SFConflict me 2026-10-12-10-00-00).pack")
        self.put("/elsewhere/refs/heads/x (SFConflict me 2026-10-12-10-00-00)")
        found, capped = diagnose.find_conflict_copies(self.client, REPO_ID, "/repo")
        self.assertEqual(found, [
            "/repo/packs/pack-1 (SFConflict me 2026-10-12-10-00-00).pack",
            "/repo/refs/heads/main (SFConflict me 2026-10-12-10-00-00)",
        ])
        self.assertFalse(capped)

    def test_a_walk_that_hits_the_directory_cap_says_so(self):
        for n in range(5):
            self.put(f"/repo/d{n}/f")
        found, capped = diagnose.find_conflict_copies(self.client, REPO_ID, "/repo", max_dirs=3)
        self.assertTrue(capped)

    # -- S15 -------------------------------------------------------------------
    def test_a_proxy_limit_is_measured_and_a_smaller_cap_is_recommended(self):
        self.stub.faults["max_upload"] = 6 * MIB
        code, out = self.upload_limit(16)
        self.assertEqual(code, 1, out)
        self.assertIn("refused: HTTP 413", out)
        self.assertIn("Hint: git config seafile.gcmaxpackmib 4", out)
        self.assertIn("fails after the lock is taken", out)

    def test_a_probe_removes_what_it_uploaded(self):
        self.stub.faults["max_upload"] = 6 * MIB
        self.put("/repo/refs/heads/main", b"a" * 41)
        self.upload_limit(16)
        self.assertEqual(sorted(self.stub.files[REPO_ID]), ["/repo/refs/heads/main"])

    def test_a_server_that_takes_the_largest_body_is_not_told_to_change_anything(self):
        code, out = self.upload_limit(4)
        self.assertEqual(code, 0, out)
        self.assertIn("was accepted (the most tried)", out)
        self.assertNotIn("Hint:", out)

    def test_a_limit_below_one_chunk_warns_that_large_objects_cannot_be_pushed(self):
        self.stub.faults["max_upload"] = 6 * MIB
        code, out = self.upload_limit(16)
        self.assertIn("such a file cannot be pushed until the limit is raised", out)

    def test_a_refusal_of_even_the_smallest_body_is_not_reported_as_a_size_limit(self):
        self.stub.faults["upload"] = 403
        code, out = self.upload_limit(4)
        self.assertEqual(code, 1, out)
        self.assertIn("that is not a size limit", out)

    def test_a_scratch_folder_that_cannot_be_removed_is_named(self):
        real = self.client.delete_entry

        def refuse(repo_id, path):
            if path.rsplit("/", 1)[-1].startswith(".git-seafile-doctor-"):
                raise RuntimeError("HTTP 500")
            return real(repo_id, path)

        with patch.object(self.client, "delete_entry", side_effect=refuse):
            code, out = self.upload_limit(2)
        self.assertEqual(code, 0, out)
        self.assertIn("Warning: could not remove /repo/.git-seafile-doctor-", out)

    def test_a_proxy_that_closes_the_connection_instead_of_answering_is_still_a_limit(self):
        """nginx answers 413 and closes while the client is still sending, so the client
        often sees a reset and never the status; the stub, which reads every body,
        cannot show that.  A front that does what nginx does can."""
        limit = 6 * MIB
        stub = self.stub

        class Front(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                if size > limit:
                    self.send_response(413)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    return
                request = urllib.request.Request(
                    stub.base_url + self.path, data=self.rfile.read(size),
                    headers=dict(self.headers), method="POST",
                )
                with urllib.request.urlopen(request) as reply:
                    data = reply.read()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        front = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Front)
        threading.Thread(target=front.serve_forever, daemon=True).start()
        self.addCleanup(front.server_close)
        self.addCleanup(front.shutdown)
        origin = f"http://127.0.0.1:{front.server_address[1]}"

        def through_the_front(client, url):
            return url.replace(stub.base_url, origin) if "/upload/" in url else url

        with patch.object(SeafileClient, "_adjust_url", through_the_front):
            code, out = self.upload_limit(32)
        self.assertEqual(code, 1, out)
        self.assertIn("the connection was closed during the upload", out)
        self.assertIn("Hint: git config seafile.gcmaxpackmib 4", out)

    # -- the command line --------------------------------------------------------
    def test_max_mib_needs_a_sensible_number_and_the_flag_it_belongs_to(self):
        for rest, words in (
            (["--upload-limit", "--max-mib", "0", "seafile://lib"], "needs a whole number"),
            (["--upload-limit", "--max-mib=5000", "seafile://lib"], "needs a whole number"),
            (["--upload-limit", "--max-mib", "many", "seafile://lib"], "needs a whole number"),
            (["--max-mib", "8", "seafile://lib"], "only applies with --upload-limit"),
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), patch.object(cli, "_doctor") as doctor:
                code = cli._cmd_doctor(rest)
            self.assertEqual(code, 2, rest)
            self.assertIn(words, err.getvalue())
            doctor.assert_not_called()

    def test_the_flags_reach_the_check(self):
        with patch.object(cli, "_doctor", return_value=0) as doctor:
            cli._cmd_doctor(["--upload-limit", "--max-mib", "8", "seafile://lib/x"])
        doctor.assert_called_once_with("seafile://lib/x", upload_limit=True, max_mib=8)


if __name__ == "__main__":
    unittest.main()
