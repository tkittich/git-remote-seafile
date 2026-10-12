"""test_url.py - Unit tests for Seafile URL parsing and validation."""

from __future__ import annotations

import unittest

from git_remote_seafile.url import mask_url, normalize_netloc, parse_seafile_url


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

    def test_bare_host_port_is_honoured(self):
        """A port on the bare form is an explicit instruction, not a hint.

        Regression: ``seafile://host:80/lib/repo`` used to come back as
        ``https://host`` -- the port was stripped (as a comparison-oriented
        normaliser does) and HTTPS forced onto it, silently redirecting the
        request from :80 to :443.
        """
        res = parse_seafile_url("seafile://host:80/lib/repo")
        self.assertEqual(res.server_url, "http://host")
        self.assertEqual(res.library_name, "lib")
        self.assertEqual(res.repo_path, "/repo")

        self.assertEqual(parse_seafile_url("seafile://Host:443/lib/repo").server_url, "https://host")
        self.assertEqual(parse_seafile_url("seafile://host:9000/lib/repo").server_url, "https://host:9000")
        # A portless bare host keeps the documented HTTPS default.
        self.assertEqual(parse_seafile_url("seafile://seafile.example.com/lib/repo").server_url,
                         "https://seafile.example.com")

    def test_bare_host_non_numeric_port_rejected(self):
        """A malformed port must be a clear URL error, not a raw ValueError."""
        with self.assertRaises(ValueError) as ctx:
            parse_seafile_url("seafile://my:lib/a/b")
        self.assertIn("Invalid Seafile URL format", str(ctx.exception))
        self.assertIn("invalid port 'lib'", str(ctx.exception))

    def test_unbracketed_ipv6_is_rejected_with_the_working_spelling(self):
        """Colons make bare-host port splitting meaningless for IPv6.

        ``seafile://2001:db8::1/lib/repo`` used to rpartition into a nonsense
        host and port; it now names the explicit-scheme bracketed form that
        does parse.
        """
        with self.assertRaises(ValueError) as ctx:
            parse_seafile_url("seafile://2001:db8::1/lib/repo")
        self.assertIn("IPv6", str(ctx.exception))
        self.assertIn("seafile://https://[2001:db8::1]/", str(ctx.exception))

    def test_bracketed_ipv6_bare_host_is_parsed_as_one_address(self):
        """The brackets are what make an IPv6 literal's colons unambiguous.

        ``seafile://[::1]/lib/repo`` used to rpartition into the nonsense
        authority ``https://[:]:1``; the bracketed form now parses like any
        other host, with the optional port honoured like the non-IPv6 form.
        """
        res = parse_seafile_url("seafile://[::1]/lib/repo")
        self.assertEqual(res.server_url, "https://[::1]")
        self.assertEqual(res.library_name, "lib")
        self.assertEqual(res.repo_path, "/repo")

        # port honoured, scheme inferred, hex lowercased -- same rules as the
        # non-IPv6 bare-host form
        self.assertEqual(
            parse_seafile_url("seafile://[2001:DB8::1]:8443/lib/repo").server_url,
            "https://[2001:db8::1]:8443",
        )
        self.assertEqual(
            parse_seafile_url("seafile://[::1]:80/lib/repo").server_url,
            "http://[::1]",
        )

        # malformed brackets are refused, not half-parsed
        with self.assertRaises(ValueError) as ctx:
            parse_seafile_url("seafile://[::1/lib/repo")
        self.assertIn("unterminated", str(ctx.exception))

    def test_empty_host_explicit_scheme_rejected(self):
        for url in ("seafile://https:///lib/repo", "seafile://http:///lib/repo"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    parse_seafile_url(url)
                self.assertIn("invalid host", str(ctx.exception))


class TestHostAndLibraryDisambiguation(unittest.TestCase):
    """A7 — a dotted library name must not be read as a server.

    The old rule called any dot a host signal, so `seafile://my.lib/team/repo`
    became server `my.lib`, library `team` -- and the `_looks_like_host`
    docstring claimed the opposite.
    """

    def test_version_suffixed_library_is_not_a_host(self):
        # A numeric final label is not something a hostname can have, so a
        # library named after a release stays a library even with a sub-path.
        for name in ("notes.v2", "proj.2026", "docs.v1.2"):
            with self.subTest(name=name):
                res = parse_seafile_url(f"seafile://{name}/team/repo")
                self.assertIsNone(res.server_url)
                self.assertEqual(res.library_name, name)
                self.assertEqual(res.repo_path, "/team/repo")

    def test_hostname_shaped_library_is_read_as_a_server(self):
        # `my.lib` and `seafile.local` are genuinely ambiguous -- both are valid
        # hostnames -- so the bare form reads them as servers.  Pinned so the
        # choice is deliberate rather than accidental, and so a future change
        # has to argue with a test.
        res = parse_seafile_url("seafile://my.lib/team/repo")
        self.assertEqual(res.server_url, "https://my.lib")
        self.assertEqual(res.library_name, "team")
        self.assertEqual(res.repo_path, "/repo")

    def test_explicit_server_makes_a_hostname_shaped_name_a_library(self):
        # The documented escape hatch for the ambiguous case.
        res = parse_seafile_url("seafile://https://server.example/my.lib/team/repo")
        self.assertEqual(res.server_url, "https://server.example")
        self.assertEqual(res.library_name, "my.lib")
        self.assertEqual(res.repo_path, "/team/repo")

    def test_userinfo_makes_a_bare_segment_a_host(self):
        # A single-label host is not hostname-shaped on its own; `user@` is an
        # explicit instruction, like a port.
        res = parse_seafile_url("seafile://user@localhost/lib/repo")
        self.assertEqual(res.server_url, "https://localhost")
        self.assertEqual(res.library_name, "lib")


class TestPercentEncodedSeparators(unittest.TestCase):
    """A7 — an encoded separator must not smuggle a traversal past the checks.

    Splitting before unquoting left `a%2F..%2F..%2Fb` as one component,
    `a/../../b`, which is not `..` and so passed both checks.
    """

    def test_encoded_separator_is_decoded_before_splitting(self):
        res = parse_seafile_url("seafile://code/a%2Fb")
        self.assertEqual(res.library_name, "code")
        self.assertEqual(res.repo_path, "/a/b")

    def test_encoded_traversal_is_refused(self):
        for url in (
            "seafile://code/a%2F..%2F..%2Fb",
            "seafile://https://seafile.example.com/code/a%2F..%2F..%2Fb",
            "seafile://code%2F..%2F..",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    parse_seafile_url(url)
                self.assertIn("'.' or '..'", str(ctx.exception))

    def test_nul_byte_in_a_segment_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            parse_seafile_url("seafile://code/a%00b/c")
        self.assertIn("NUL", str(ctx.exception))


class TestPortParsingIsGuarded(unittest.TestCase):
    """A7.4 — a malformed port is this tool's error, not urllib's.

    `urlparse().port` raises a bare `ValueError` naming no URL, which escaped
    from `normalize_netloc` and from the explicit-scheme branch.
    """

    def test_explicit_scheme_non_numeric_port_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            parse_seafile_url("seafile://https://seafile.example.com:abc/code/repo")
        self.assertIn("Invalid Seafile URL format", str(ctx.exception))
        self.assertIn("invalid port", str(ctx.exception))

    def test_out_of_range_port_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            parse_seafile_url("seafile://https://seafile.example.com:99999/code/repo")
        self.assertIn("Invalid Seafile URL format", str(ctx.exception))

    def test_normalize_netloc_reports_the_port_rather_than_raising_raw(self):
        with self.assertRaises(ValueError) as ctx:
            normalize_netloc("https://host:abc")
        self.assertIn("Invalid Seafile URL format", str(ctx.exception))
        self.assertIn("host:abc", str(ctx.exception))

    def test_normalize_netloc_still_normalises_good_input(self):
        self.assertEqual(normalize_netloc("HTTPS://Host:443"), "host")
        self.assertEqual(normalize_netloc("https://host:8443"), "host:8443")
        self.assertEqual(normalize_netloc(""), "")


class TestMaskUrl(unittest.TestCase):
    """Userinfo in a user-typed URL must not reach output."""

    def test_userinfo_is_redacted(self):
        self.assertEqual(mask_url("seafile://user:pass@host/lib/repo"),
                         "seafile://host/lib/repo")

    def test_nested_scheme_userinfo_is_redacted(self):
        # The explicit-server spelling nests the real scheme:
        # urlparse must see the *inner* authority.
        self.assertEqual(mask_url("seafile://https://user:pass@host:8443/lib/repo"),
                         "seafile://https://host:8443/lib/repo")

    def test_username_only_is_redacted(self):
        self.assertEqual(mask_url("seafile://user@host/lib"), "seafile://host/lib")

    def test_at_sign_without_userinfo_is_left_alone(self):
        # A stray @ with nothing before it parses to username=None; guessing
        # would corrupt a legal URL, so it is returned unchanged.
        self.assertEqual(mask_url("seafile://@host/lib"), "seafile://@host/lib")

    def test_urls_without_userinfo_are_unchanged(self):
        for url in ("seafile://host/lib/repo", "seafile://https://host/lib",
                    "plain"):
            self.assertEqual(mask_url(url), url)

    def test_non_seafile_scheme_is_redacted_too(self):
        self.assertEqual(mask_url("https://user:pass@host/lib"), "https://host/lib")


class TestDefaultedPath(unittest.TestCase):
    def test_a_url_with_only_a_library_is_flagged_as_defaulted(self):
        parsed = parse_seafile_url("seafile://code")
        self.assertEqual(parsed.repo_path, "/git-repo")
        self.assertTrue(parsed.path_defaulted)

    def test_an_explicit_path_is_not_flagged(self):
        self.assertFalse(parse_seafile_url("seafile://code/git-repo").path_defaulted)
        self.assertFalse(parse_seafile_url("seafile://code/a/b").path_defaulted)


class TestNoticeRateLimit(unittest.TestCase):
    def test_a_notice_is_shown_once_per_day_per_key(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from git_remote_seafile import helper

        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, "home", return_value=Path(home)):
            self.assertTrue(helper._notice_due("u", now=1000.0))
            self.assertFalse(helper._notice_due("u", now=1000.0 + 60))
            self.assertTrue(helper._notice_due("other", now=1000.0 + 60))
            self.assertTrue(helper._notice_due("u", now=1000.0 + 86401))


if __name__ == "__main__":
    unittest.main()
