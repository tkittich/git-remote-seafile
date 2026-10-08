"""test_url.py - Unit tests for Seafile URL parsing and validation."""

from __future__ import annotations

import unittest

from git_remote_seafile.url import parse_seafile_url


class TestUrlParsingModule(unittest.TestCase):
    """Direct tests for the url.py parsing module."""

    def test_dotted_library_name(self):
        res = parse_seafile_url("seafile://my.library/repo")
        self.assertIsNone(res.server_url)
        self.assertEqual(res.library_name, "my.library")
        self.assertEqual(res.repo_path, "/repo")
        self.assertEqual(res.to_tuple(), (None, "my.library", "/repo"))

    def test_host_with_library_and_path(self):
        res = parse_seafile_url("seafile://seafile.example.com/code/myproject")
        self.assertEqual(res.server_url, "https://seafile.example.com")
        self.assertEqual(res.library_name, "code")
        self.assertEqual(res.repo_path, "/myproject")

    def test_nested_library_path(self):
        res = parse_seafile_url("seafile://Documents/seafile-git/myproject")
        self.assertIsNone(res.server_url)
        self.assertEqual(res.library_name, "Documents")
        self.assertEqual(res.repo_path, "/seafile-git/myproject")

    def test_bare_host_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_seafile_url("seafile://seafile.example.com")
        self.assertIn("no library", str(ctx.exception))

    def test_default_repo_path(self):
        res = parse_seafile_url("seafile://Documents/")
        self.assertIsNone(res.server_url)
        self.assertEqual(res.library_name, "Documents")
        self.assertEqual(res.repo_path, "/git-repo")

    def test_empty_urls_rejected(self):
        for url in ("seafile://", "seafile:///"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    parse_seafile_url(url)

    def test_explicit_scheme_form(self):
        res = parse_seafile_url("seafile://https://my.library/repo")
        self.assertEqual(res.server_url, "https://my.library")
        self.assertEqual(res.library_name, "repo")
        self.assertEqual(res.repo_path, "/git-repo")

    def test_percent_encoded_segments(self):
        res = parse_seafile_url("seafile://My%20Library/my%20repo")
        self.assertIsNone(res.server_url)
        self.assertEqual(res.library_name, "My Library")
        self.assertEqual(res.repo_path, "/my repo")

    def test_path_traversal_rejected(self):
        urls = (
            "seafile://my-lib/../secret",
            "seafile://https://seafile.example.com/my-lib/./repo",
            "seafile://../lib/repo",
            "seafile://https://../lib/repo",
            "seafile://..",
        )
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    parse_seafile_url(url)

    def test_explicit_port_stripping_and_lowercase(self):
        res = parse_seafile_url("seafile://https://Cloud.EXAMPLE.com:443/code/repo")
        self.assertEqual(res.server_url, "https://cloud.example.com")
        self.assertEqual(res.library_name, "code")
        self.assertEqual(res.repo_path, "/repo")


if __name__ == "__main__":
    unittest.main()
