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
        # The error must say how to write it instead.
        self.assertIn("seafile://https://seafile.example.com/", str(ctx.exception))

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

    def test_scheme_prefix_is_case_insensitive(self):
        """RFC 3986 schemes are caseless, so SEAFILE://... must parse identically."""
        up = parse_seafile_url("SEAFILE://code/myrepo")
        lo = parse_seafile_url("seafile://code/myrepo")
        self.assertEqual((up.server_url, up.library_name, up.repo_path), (lo.server_url, lo.library_name, lo.repo_path))
        mixed_up = parse_seafile_url("SeaFile://HTTPS://Host:8443/code/myrepo")
        mixed_lo = parse_seafile_url("seafile://https://host:8443/code/myrepo")
        self.assertEqual(mixed_up.server_url, mixed_lo.server_url)

    def test_percent_encoded_segments(self):
        res = parse_seafile_url("seafile://My%20Library/my%20repo")
        self.assertIsNone(res.server_url)
        self.assertEqual(res.library_name, "My Library")
        self.assertEqual(res.repo_path, "/my repo")

        res2 = parse_seafile_url("seafile://https://seafile.example.com/Team%20Docs/nested%20repo/sub")
        self.assertEqual(res2.server_url, "https://seafile.example.com")
        self.assertEqual(res2.library_name, "Team Docs")
        self.assertEqual(res2.repo_path, "/nested repo/sub")

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

        # Non-default ports are kept; a trailing slash does not reach the path.
        res2 = parse_seafile_url("seafile://example.com:8443/code/team/repo")
        self.assertEqual(res2.server_url, "https://example.com:8443")
        self.assertEqual(res2.library_name, "code")
        self.assertEqual(res2.repo_path, "/team/repo")

        res3 = parse_seafile_url("seafile://http://internal.LAN:80/docs/repo")
        self.assertEqual(res3.server_url, "http://internal.lan")
        self.assertEqual(res3.library_name, "docs")
        self.assertEqual(res3.repo_path, "/repo")

        res4 = parse_seafile_url("seafile://https://cloud.internal.org/library-name/sub/project/")
        self.assertEqual(res4.server_url, "https://cloud.internal.org")
        self.assertEqual(res4.library_name, "library-name")
        self.assertEqual(res4.repo_path, "/sub/project")

    def test_empty_host_explicit_scheme_rejected(self):
        for url in ("seafile://https:///lib/repo", "seafile://http:///lib/repo"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    parse_seafile_url(url)
                self.assertIn("invalid host", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
