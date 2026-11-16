"""The two copies of the settle rule must agree (review dfd2269; ROADMAP AA58, AA61).

An uploaded LFS object (``lfs.LFSTransferAgent._settle_object_name``) and a payload object
(``_commit_object`` in the snapshot tool) are renamed onto their hash name, and Seafile answers a rename
onto a taken name with success and a ``<name> (1)`` copy.  Each program settles that its own way and they
cannot share code (the snapshot tool runs without the package), so one drifted: the LFS copy ignored a
refused rename after it had deleted the damaged object.  This runs the same scenarios through both
against one model of the server and requires the same final state.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import seafile_snapshot as snap  # noqa: E402

from git_remote_seafile.lfs import LFSTransferAgent  # noqa: E402

DIR = "/r/objects/ab/cd"
NAME = "a" * 64
SIZE = 100


class _Server:
    """One folder of a Seafile library: rename onto a taken name makes ``<name> (N)``."""

    def __init__(self, files: dict[str, int], refuse_rename_number: int | None = None):
        self.files = dict(files)
        self.renames = 0
        self.refuse_rename_number = refuse_rename_number
        self.deleted: list[str] = []

    def list_dir(self, repo_id: str, path: str):
        return [{"type": "file", "name": n, "size": s} for n, s in sorted(self.files.items())]

    def delete_entry(self, repo_id: str, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        self.deleted.append(name)
        return self.files.pop(name, None) is not None

    def rename_entry(self, repo_id: str, path: str, new_name: str) -> bool:
        self.renames += 1
        if self.refuse_rename_number == self.renames:
            return False
        old = path.rsplit("/", 1)[-1]
        if old not in self.files:
            return False
        size = self.files.pop(old)
        target = new_name
        number = 0
        while target in self.files:
            number += 1
            target = f"{new_name} ({number})"
        self.files[target] = size
        return True


def _through_lfs(server: _Server) -> bool:
    server.files["tmp.part"] = SIZE
    server.rename_entry("rid", f"{DIR}/tmp.part", NAME)
    agent = LFSTransferAgent(server, "rid", "/r")
    try:
        entry = agent._settle_object_name(DIR, NAME, SIZE)
    finally:
        agent._temp_dir.cleanup()
    return entry is not None and entry.get("size") == SIZE


def _through_payload(server: _Server) -> bool:
    server.files["tmp.part"] = SIZE
    return bool(snap._commit_object(server, "rid", DIR, "tmp.part", NAME, SIZE))


SCENARIOS = {
    "a free name": ({}, None),
    "the same bytes already there": ({NAME: SIZE}, None),
    "a damaged object under the name": ({NAME: 9}, None),
    # the first rename is the upload's own; the second is the settle's move of the right copy
    "a refused rename after the damaged object went": ({NAME: 9}, 2),
}


class TestTheTwoSettleCopiesAgree(unittest.TestCase):
    def test_both_end_in_the_same_state_for_every_scenario(self):
        for label, (before, refuse) in SCENARIOS.items():
            with self.subTest(label):
                lfs, payload = _Server(before, refuse), _Server(before, refuse)
                lfs_ok, payload_ok = _through_lfs(lfs), _through_payload(payload)
                self.assertEqual(lfs.files, payload.files, f"{label}: the folders differ")
                self.assertEqual(lfs_ok, payload_ok, f"{label}: one reports success, the other not")

    def test_a_whole_copy_is_never_left_without_a_name_it_can_be_found_by(self):
        """Whatever happens, one whole copy stays on the server (never zero)."""
        for label, (before, refuse) in SCENARIOS.items():
            for through in (_through_lfs, _through_payload):
                with self.subTest(label, through=through.__name__):
                    server = _Server(before, refuse)
                    through(server)
                    self.assertIn(SIZE, server.files.values(), f"{label}: no whole copy left")


if __name__ == "__main__":
    unittest.main()
