"""test_packs.py - Unit tests for packfile operations and validation."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from git_remote_seafile.packs import (
    PACK_NAME_RE,
    check_remote_has_packs,
    is_valid_pack_name,
)


class TestPacksModule(unittest.TestCase):
    """Direct tests for packs.py pack naming and verification."""

    def test_valid_pack_names(self):
        valid = [
            "pack-1234567890abcdef.pack",
            "pack-0123456789abcdef0123456789abcdef01234567.pack",
            "pack-auto-abc.pack",
            "pack-test_123.pack",
        ]
        for name in valid:
            with self.subTest(name=name):
                self.assertTrue(is_valid_pack_name(name))
                self.assertIsNotNone(PACK_NAME_RE.match(name))

    def test_invalid_pack_names(self):
        invalid = [
            "../evil.pack",
            "malformed.pack",
            "pack-123.idx",
            "pack/sub/123.pack",
            "pack-123.pack/something",
            "pack-123\\traversal.pack",
            "",
        ]
        for name in invalid:
            with self.subTest(name=name):
                self.assertFalse(is_valid_pack_name(name))

    def test_check_remote_has_packs(self):
        client = MagicMock()
        client.list_dir.return_value = [{"name": "pack-123.pack"}]
        self.assertTrue(check_remote_has_packs(client, "r1", "/git-repo/objects/pack"))

        client.list_dir.return_value = [{"name": "some-other-file.txt"}]
        self.assertFalse(check_remote_has_packs(client, "r1", "/git-repo/objects/pack"))

        client.list_dir.side_effect = Exception("network failure")
        self.assertFalse(check_remote_has_packs(client, "r1", "/git-repo/objects/pack"))

        client.list_dir.side_effect = None
        client.list_dir.return_value = None
        self.assertFalse(check_remote_has_packs(client, "r1", "/git-repo/objects/pack"))

    def test_fetch_and_install_pack_with_none_git_dir(self):
        from git_remote_seafile.packs import fetch_and_install_pack
        client = MagicMock()
        client.get_file_bytes.return_value = b"PACKBYTES"
        installer = MagicMock()
        fetch_and_install_pack(
            client=client,
            repo_id="r1",
            remote_pack_dir="/git-repo/objects/pack",
            pack_name="pack-123.pack",
            git_dir=None,
            installer=installer,
        )
        installer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
