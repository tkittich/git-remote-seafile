"""Download resume (RESUME.md D2): ``download_file_to(resume_from=N)`` continues a partial, or starts again.

The server side is a function: given the ``Range`` header it was sent it answers with a status, headers and body.
That lets one test play a well-behaved server, one that ignores ``Range``, one that answers a shifted range and
one that lies about the size, and read back both what was written and what was requested.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

from git_remote_seafile.client import SeafileAPIError, SeafileClient

OBJECT = bytes(range(256)) * 4        # 1024 bytes


def _response(status: int, body: bytes = b"", headers: "dict[str, str] | None" = None) -> MagicMock:
    resp = MagicMock(status_code=status, headers=headers or {})
    resp.iter_content = MagicMock(side_effect=lambda chunk_size: iter([body[i:i + 300] for i in range(0, len(body), 300)]))
    return resp


def _honest(data: bytes) -> "Callable[[str | None], MagicMock]":
    def answer(range_header: "str | None") -> MagicMock:
        if range_header is None:
            return _response(200, data, {"Content-Length": str(len(data))})
        start = int(range_header.split("=")[1].rstrip("-"))
        if start >= len(data):
            return _response(416)
        return _response(206, data[start:], {
            "Content-Length": str(len(data) - start), "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}",
        })
    return answer


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SeafileClient(server_url="https://seafile.example.com", token="tok")
        self.ranges: "list[str | None]" = []
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name) / "object.part"

    def serve(self, answer: "Callable[[str | None], MagicMock]") -> None:
        link = MagicMock(status_code=200, text='"https://seafile.example.com/seafhttp/files/1/o"')

        def get(url: str, **kwargs: object) -> MagicMock:
            if "/api2/repos/" in url:
                return link
            headers = kwargs.get("headers") or {}
            sent = headers.get("Range") if isinstance(headers, dict) else None
            self.ranges.append(sent)
            return answer(sent)

        self.client.session.get = MagicMock(side_effect=get)  # type: ignore[method-assign]

    def download(self, **kwargs: object) -> bool:
        return self.client.download_file_to("repo1", "/o", self.dest, **kwargs)   # type: ignore[arg-type]


class ResumeTests(_Fixture):
    def test_a_partial_is_continued_with_a_range_and_ends_as_the_whole_object(self):
        self.dest.write_bytes(OBJECT[:300])
        self.serve(_honest(OBJECT))
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=300))
        self.assertEqual(self.ranges, ["bytes=300-"])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_no_range_is_sent_without_a_partial(self):
        self.serve(_honest(OBJECT))
        self.assertTrue(self.download(max_bytes=len(OBJECT)))
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=0))
        self.assertEqual(self.ranges, [None, None])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_partial_already_as_large_as_the_object_sends_no_request_at_all(self):
        self.dest.write_bytes(OBJECT)
        self.serve(_honest(OBJECT))
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=len(OBJECT)))
        self.assertEqual(self.ranges, [])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_server_that_ignores_range_makes_the_download_start_from_byte_zero(self):
        self.dest.write_bytes(OBJECT[:300])
        self.serve(lambda _range: _response(200, OBJECT, {"Content-Length": str(len(OBJECT))}))
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=300))
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_shifted_range_is_never_appended(self):
        self.dest.write_bytes(OBJECT[:300])
        honest = _honest(OBJECT)

        def shifted(range_header: "str | None") -> MagicMock:
            if range_header is None:
                return honest(None)
            return _response(206, OBJECT[200:], {
                "Content-Length": str(len(OBJECT) - 200),
                "Content-Range": f"bytes 200-{len(OBJECT) - 1}/{len(OBJECT)}",
            })

        self.serve(shifted)
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=300))
        self.assertEqual(self.ranges, ["bytes=300-", None])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_partial_that_changes_while_the_request_runs_is_not_appended_to(self):
        """The size re-check after the response arrives (review R6 B9): a partial another process grew is not continued."""
        self.dest.write_bytes(OBJECT[:300])
        honest = _honest(OBJECT)

        def grown_meanwhile(range_header: "str | None") -> MagicMock:
            if range_header is not None:
                with open(self.dest, "ab") as handle:
                    handle.write(b"another process wrote this")
            return honest(range_header)

        self.serve(grown_meanwhile)
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=300))
        self.assertEqual(self.ranges, ["bytes=300-", None])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_416_starts_again_instead_of_keeping_the_partial(self):
        self.dest.write_bytes(b"x" * 300)
        honest = _honest(OBJECT)
        self.serve(lambda range_header: _response(416) if range_header else honest(None))
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=300))
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_size_that_lies_is_stopped_at_the_declared_size(self):
        """The bound is the whole object: the offset counts against ``max_bytes``."""
        self.dest.write_bytes(OBJECT[:300])
        body = OBJECT[300:] + b"extra"
        self.serve(lambda _range: _response(206, body, {
            "Content-Length": str(len(body)), "Content-Range": f"bytes 300-{len(OBJECT) + 4}/{len(OBJECT) + 5}",
        }))
        with self.assertRaises(SeafileAPIError):
            self.download(max_bytes=len(OBJECT), resume_from=300)
        self.assertLessEqual(self.dest.stat().st_size, len(OBJECT))

    def test_a_short_resumed_body_is_a_failure_not_a_small_file(self):
        self.dest.write_bytes(OBJECT[:300])
        self.serve(lambda _range: _response(206, OBJECT[300:900], {
            "Content-Length": str(len(OBJECT) - 300), "Content-Range": f"bytes 300-{len(OBJECT) - 1}/{len(OBJECT)}",
        }))
        with self.assertRaises(SeafileAPIError) as caught:
            self.download(max_bytes=len(OBJECT), resume_from=300)
        self.assertIn("Truncated download", str(caught.exception))

    def test_a_partial_that_is_not_the_size_the_caller_says_starts_again(self):
        self.dest.write_bytes(OBJECT[:100])
        self.serve(_honest(OBJECT))
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=300))
        self.assertEqual(self.ranges, [None])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_zero_byte_object_sends_no_range_and_no_content_range_is_expected(self):
        self.dest.write_bytes(b"")
        self.serve(lambda _range: _response(200, b"", {"Content-Length": "0"}))
        self.assertTrue(self.download(max_bytes=0, resume_from=0))
        self.assertTrue(self.download(max_bytes=0, resume_from=5))
        self.assertEqual(self.ranges, [None, None])
        self.assertEqual(self.dest.read_bytes(), b"")


class ProxyAndRangeTests(_Fixture):
    """Round 4 (Gemini N4/N10, DeepSeek N11): what a ``206`` must also say, and the cap without ``max_bytes``."""

    def test_a_206_whose_content_length_is_not_the_remainder_restarts_from_byte_zero(self):
        self.dest.write_bytes(OBJECT[:300])
        honest = _honest(OBJECT)

        def proxy(range_header: "str | None") -> MagicMock:
            if range_header is None:
                return honest(None)
            return _response(206, OBJECT, {            # a proxy that sends the whole body under a 206
                "Content-Length": str(len(OBJECT)), "Content-Range": f"bytes 300-{len(OBJECT) - 1}/{len(OBJECT)}",
            })

        self.serve(proxy)
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=300))
        self.assertEqual(self.ranges, ["bytes=300-", None])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_without_max_bytes_a_206_stops_at_the_total_it_declared(self):
        self.dest.write_bytes(OBJECT[:300])
        body = OBJECT[300:] + b"extra"
        self.serve(lambda _range: _response(206, body, {
            "Content-Range": f"bytes 300-{len(OBJECT) - 1}/{len(OBJECT)}",
        }))
        with self.assertRaises(SeafileAPIError):
            self.download(resume_from=300)
        self.assertLessEqual(self.dest.stat().st_size, len(OBJECT))

    def test_the_range_unit_is_matched_case_insensitively(self):
        self.dest.write_bytes(OBJECT[:300])
        self.serve(lambda _range: _response(206, OBJECT[300:], {
            "Content-Length": str(len(OBJECT) - 300), "Content-Range": f"Bytes 300-{len(OBJECT) - 1}/{len(OBJECT)}",
        }))
        self.assertTrue(self.download(max_bytes=len(OBJECT), resume_from=300))
        self.assertEqual(self.ranges, ["bytes=300-"])
        self.assertEqual(self.dest.read_bytes(), OBJECT)


class RestoreRetryTests(unittest.TestCase):
    """The restore's own retry: a failed transfer is continued, a partial that hashed wrong starts over (R5)."""

    def setUp(self) -> None:
        import hashlib
        from git_remote_seafile.snapshot import payload_restore
        self.restore = payload_restore
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scratch = Path(self._tmp.name)
        self.dest = self.scratch / "final.bin"
        self.item = {"sha256": hashlib.sha256(OBJECT).hexdigest(), "size": len(OBJECT)}
        self.resumed: "list[int]" = []

    def client(self, script: "list[Callable[[Path], None]]") -> MagicMock:
        steps = iter(script)

        def download(rid: str, path: str, dest: Path, max_bytes: int | None = None, resume_from: int = 0) -> bool:
            self.resumed.append(resume_from)
            if resume_from == 0:
                dest.write_bytes(b"")
            next(steps)(dest)
            return True

        client = MagicMock()
        client.download_file_to = MagicMock(side_effect=download)
        return client

    @staticmethod
    def append(data: bytes) -> "Callable[[Path], None]":
        def step(dest: Path) -> None:
            with open(dest, "ab") as handle:
                handle.write(data)
        return step

    @staticmethod
    def fail_after(data: bytes) -> "Callable[[Path], None]":
        def step(dest: Path) -> None:
            with open(dest, "ab") as handle:
                handle.write(data)
            raise OSError("connection reset")
        return step

    def test_a_dropped_transfer_is_continued_where_it_stopped(self):
        client = self.client([self.fail_after(OBJECT[:400]), self.append(OBJECT[400:])])
        why = self.restore._download_verified(client, "rid", "/o/x", self.dest, self.item, self.scratch)
        self.assertIsNone(why)
        self.assertEqual(self.resumed, [0, 400])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_partial_that_hashes_wrong_is_deleted_and_the_next_attempt_starts_from_byte_zero(self):
        client = self.client([self.append(b"\x00" * len(OBJECT)), self.append(OBJECT)])
        why = self.restore._download_verified(client, "rid", "/o/x", self.dest, self.item, self.scratch)
        self.assertIsNone(why)
        self.assertEqual(self.resumed, [0, 0])
        self.assertEqual(self.dest.read_bytes(), OBJECT)

    def test_a_resumed_download_that_hashes_wrong_is_not_resumed_again(self):
        bad = b"\x01" * (len(OBJECT) - 400)
        client = self.client([self.fail_after(OBJECT[:400]), self.append(bad), self.append(OBJECT)])
        why = self.restore._download_verified(client, "rid", "/o/x", self.dest, self.item, self.scratch)
        self.assertIsNone(why)
        self.assertEqual(self.resumed, [0, 400, 0])
        self.assertEqual(self.dest.read_bytes(), OBJECT)


    def test_a_placement_that_fails_keeps_the_verified_file_and_fails_that_one_file(self):
        from unittest.mock import patch
        client = self.client([self.append(OBJECT)])
        with patch.object(self.restore.os, "replace", side_effect=PermissionError("WinError 32")), \
                patch.object(self.restore.time, "sleep"):
            why = self.restore._download_verified(client, "rid", "/o/x", self.dest, self.item, self.scratch)
        self.assertIn("could not place", why or "")
        self.assertTrue((self.scratch / f"{self.item['sha256']}.{__import__('os').getpid()}.part").exists())

    def test_a_placement_held_for_a_moment_succeeds_after_a_retry(self):
        from unittest.mock import patch
        client = self.client([self.append(OBJECT)])
        real = self.restore.os.replace
        calls = {"n": 0}

        def flaky(src: object, dst: object) -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("WinError 32")
            real(src, dst)  # type: ignore[arg-type]

        with patch.object(self.restore.os, "replace", side_effect=flaky), patch.object(self.restore.time, "sleep"), \
                patch.object(self.restore.os, "name", "nt"):
            why = self.restore._download_verified(client, "rid", "/o/x", self.dest, self.item, self.scratch)
        self.assertIsNone(why)
        self.assertEqual(self.dest.read_bytes(), OBJECT)


if __name__ == "__main__":
    unittest.main()
