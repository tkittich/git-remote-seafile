"""Scripting the snapshot tool: exit-code classes, ``--json``, resumed runs, progress, the drill.

These drive the real tool (``snap.main``) against the in-process Seafile stub,
with the fixture the payload tests use.  The drill and the resume tests exist
because the answers they give -- "would a restore work today", "does an
interrupted run lose its hashing" -- were guesses until something ran them.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT / "tests"))

import seafile_snapshot as snap  # noqa: E402
from e2e_harness import REPO_ID  # noqa: E402
from test_snapshot_payload import OLD_NS, _PayloadFixture  # noqa: E402


class _ReportFixture(_PayloadFixture):
    """A tree whose snapshot prints one ``Warning:`` (a credentials-looking file)."""

    def with_secret(self) -> None:
        (self.src / ".env").write_bytes(b"TOKEN=abc\n")

    def old_payload_files(self) -> None:
        for path in (self.src / "big1.bin", self.src / "sub" / "big2.bin"):
            os.utime(path, ns=(OLD_NS, OLD_NS))


class ExitCodeTests(_ReportFixture):
    def test_a_warning_still_exits_zero_by_default(self):
        self.with_secret()
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("look like credentials", out)

    def test_exit_warnings_turns_a_completed_run_with_a_warning_into_three(self):
        self.with_secret()
        rc, out, err = self.snapshot("--exit-warnings")
        self.assertEqual(rc, snap.EXIT_WARNINGS, out + err)
        self.assertIn("look like credentials", out)

    def test_exit_warnings_leaves_a_clean_run_at_zero(self):
        rc, out, err = self.snapshot("--exit-warnings")
        self.assertEqual(rc, 0, out + err)

    def test_exit_warnings_does_not_hide_a_failure(self):
        into = self.case / "occupied"
        into.mkdir()
        (into / "x").write_bytes(b"x")
        rc, out, err = self.run_tool(
            "--restore", "--remote", self.remote, "--into", into, "--exit-warnings"
        )
        self.assertEqual(rc, 1, out + err)
        self.assertIn("is not empty", err)


class JsonTests(_ReportFixture):
    def document(self, out: str) -> dict:
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        return json.loads(out)

    def test_a_clean_snapshot_is_one_json_line_and_the_text_moves_to_stderr(self):
        rc, out, err = self.snapshot("--json")
        self.assertEqual(rc, 0, out + err)
        doc = self.document(out)
        self.assertEqual((doc["schema"], doc["status"], doc["exit_code"]), (1, "ok", 0))
        self.assertEqual(doc["mode"], "snapshot")
        self.assertEqual(doc["warnings"], [])
        self.assertIsNone(doc["error"])
        self.assertEqual(doc["result"]["files"], 5)
        self.assertTrue(doc["result"]["pushed"])
        self.assertEqual(doc["result"]["payload_objects_to_upload"], 2)
        self.assertIn("files  :", err)

    def test_warnings_are_listed_and_the_status_says_so(self):
        self.with_secret()
        rc, out, err = self.snapshot("--json")
        self.assertEqual(rc, 0, out + err)
        doc = self.document(out)
        self.assertEqual(doc["status"], "warnings")
        self.assertEqual(len(doc["warnings"]), 1)
        self.assertTrue(doc["warnings"][0].startswith("Warning:"))
        # The warning is wrapped over lines and lists the paths: all of it is kept.
        self.assertIn("only you can read", doc["warnings"][0])
        self.assertIn(".env", doc["warnings"][0])

    def test_a_failed_run_carries_the_error_and_the_exit_code(self):
        into = self.case / "occupied"
        into.mkdir()
        (into / "x").write_bytes(b"x")
        rc, out, err = self.run_tool("--restore", "--remote", self.remote, "--into", into, "--json")
        self.assertEqual(rc, 1)
        doc = self.document(out)
        self.assertEqual((doc["status"], doc["exit_code"], doc["mode"]), ("failed", 1, "restore"))
        self.assertIn("is not empty", doc["error"])

    def test_payload_verify_reports_what_it_checked(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.run_tool("--payload-verify", "all", "--remote", self.remote, "--json")
        self.assertEqual(rc, 0, out + err)
        doc = self.document(out)
        self.assertEqual(doc["mode"], "payload-verify")
        self.assertEqual((doc["result"]["checked"], doc["result"]["damaged"]), (2, 0))

    def test_json_and_exit_warnings_together(self):
        self.with_secret()
        rc, out, err = self.snapshot("--json", "--exit-warnings")
        self.assertEqual(rc, snap.EXIT_WARNINGS)
        self.assertEqual(self.document(out)["exit_code"], snap.EXIT_WARNINGS)


class ResumeAndProgressTests(_ReportFixture):
    def test_a_run_after_a_failed_upload_hashes_nothing_again_and_uploads_the_rest(self):
        self.old_payload_files()
        self.stub.faults["upload"] = 500
        rc, out, err = self.snapshot()
        self.assertNotEqual(rc, 0)
        self.assertIn("hashing [", out)
        del self.stub.faults["upload"]
        rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("hashing [", out)
        self.assertEqual(self.object_count(), 2)

    def test_a_killed_hashing_pass_keeps_what_it_hashed(self):
        self.old_payload_files()
        calls = []
        real = snap._sha256_file

        def second_one_dies(path):
            calls.append(path)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(path)

        with unittest.mock.patch.object(snap, "_CHECKPOINT_SECONDS", 0.0), \
                unittest.mock.patch.object(snap, "_sha256_file", second_one_dies):
            with self.assertRaises(KeyboardInterrupt):
                self.snapshot()
        cache = json.loads((self.vault / ".git" / "payload-cache.json").read_text("utf-8"))
        self.assertEqual(list(cache), ["big1.bin"])

    def test_hashing_prints_how_much_is_left(self):
        with unittest.mock.patch.object(snap, "_PULSE_SECONDS", 0.0):
            rc, out, err = self.snapshot()
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"progress: [\d.]+ \w+ of [\d.]+ \w+ hashed \(\d+%\), \d+ MiB/s")

    def test_a_payload_restore_prints_how_much_is_left(self):
        self.assertEqual(self.snapshot()[0], 0)
        with unittest.mock.patch.object(snap, "_PULSE_SECONDS", 0.0):
            rc, out, err, _into = self.restore("progress")
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"progress: [\d.]+ \w+ of [\d.]+ \w+ downloaded \(\d+%\)")

    def test_a_payload_verify_prints_how_much_is_left(self):
        self.assertEqual(self.snapshot()[0], 0)
        with unittest.mock.patch.object(snap, "_PULSE_SECONDS", 0.0):
            rc, out, err = self.run_tool("--payload-verify", "all", "--remote", self.remote)
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"progress: [\d.]+ \w+ of [\d.]+ \w+ checked \(\d+%\)")


class RestoreDrillTests(_ReportFixture):
    def drill(self, *extra: object) -> tuple[int, str, str]:
        scratch = self.case / "tmp"
        scratch.mkdir(exist_ok=True)
        with unittest.mock.patch.object(tempfile, "tempdir", str(scratch)):
            return self.run_tool("--restore-drill", *extra, "--remote", self.remote)

    def leftovers(self) -> list[pathlib.Path]:
        return list((self.case / "tmp").glob("grs-drill-*"))

    def test_a_good_snapshot_passes_and_the_directory_is_removed(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.drill("1")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("restore drill passed", out)
        self.assertIn("restoring 1 of 2 payload file(s)", out)
        self.assertIn("git fsck: object store intact", out)
        self.assertEqual(self.leftovers(), [])

    def test_the_default_sample_covers_a_small_payload_and_all_is_accepted(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.drill()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("restoring 2 payload file(s)", out)
        rc, out, err = self.drill("all")
        self.assertEqual(rc, 0, out + err)

    def test_a_damaged_object_fails_the_drill_and_keeps_the_directory(self):
        self.assertEqual(self.snapshot()[0], 0)
        key = self.object_key(self.big1)
        files = self.stub.files[REPO_ID]
        files[key] = b"x" * len(self.big1)
        rc, out, err = self.drill("all")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("were not restored", err)
        self.assertIn("left in", err)
        self.assertEqual(len(self.leftovers()), 1)

    def test_the_drill_is_json_too(self):
        self.assertEqual(self.snapshot()[0], 0)
        rc, out, err = self.drill("1", "--json")
        self.assertEqual(rc, 0, out + err)
        doc = json.loads(out)
        self.assertEqual((doc["mode"], doc["status"], doc["result"]["sample"]), ("restore-drill", "ok", 1))

    def test_the_drill_refuses_what_does_not_apply_to_it(self):
        rc, out, err = self.run_tool(
            "--restore-drill", "--remote", self.remote, "--into", self.case / "x", "--dry-run"
        )
        self.assertEqual(rc, 1)
        self.assertIn("--into does not apply to --restore-drill", err)
        self.assertIn("--dry-run does not apply to --restore-drill", err)

    def test_the_drill_needs_a_remote_and_a_sane_count(self):
        rc, out, err = self.run_tool("--restore-drill")
        self.assertEqual(rc, 1)
        self.assertIn("--restore-drill requires --remote", err)
        rc, out, err = self.run_tool("--restore-drill", "0", "--remote", self.remote)
        self.assertEqual(rc, 1)
        self.assertIn("takes a positive number", err)


if __name__ == "__main__":
    unittest.main()
