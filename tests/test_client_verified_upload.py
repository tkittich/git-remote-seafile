"""The client calls the verified-upload primitive stands on (RESUME.md D1, round 4).

``rename_entry_result`` says where the bytes landed, and ``uploaded_bytes`` reads the server's count for a chunked
upload without ever turning "unknown" into "nothing".  Both answers are what a CE 13.0.29 container sent.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from git_remote_seafile.client import SeafileClient


def _client(post: "object | None" = None, get: "object | None" = None) -> SeafileClient:
    client = SeafileClient(server_url="https://seafile.example.com", token="tok")
    if post is not None:
        client.session.post = post  # type: ignore[method-assign]
    if get is not None:
        client.session.get = get  # type: ignore[method-assign]
    return client


def _reply(status: int, text: str = "", payload: object = None) -> MagicMock:
    resp = MagicMock(status_code=status, text=text, headers={})
    if payload is None:
        resp.json = MagicMock(side_effect=ValueError("not json"))
    else:
        resp.json = MagicMock(return_value=payload)
    return resp


class RenameLandingTests(unittest.TestCase):
    def rename(self, reply: MagicMock) -> "tuple[int, str | None]":
        return _client(post=MagicMock(return_value=reply)).rename_entry_result("rid", "/d/tmp", "final")

    def test_a_free_name_lands_under_itself(self):
        url = '"https://seafile.example.com/seafhttp/files/1234/final"'
        self.assertEqual(self.rename(_reply(200, url)), (200, "final"))

    def test_a_taken_name_lands_beside_it_and_the_name_is_decoded(self):
        url = '"https://seafile.example.com/seafhttp/files/1234/c%20%281%29.txt"'
        self.assertEqual(self.rename(_reply(200, url)), (200, "c (1).txt"))

    def test_a_success_that_names_nothing_has_no_landing_name(self):
        for text in ('"success"', "", '"https://seafile.example.com/"'):
            with self.subTest(text=text):
                self.assertEqual(self.rename(_reply(200, text)), (200, None))

    def test_every_failure_status_is_a_status_and_no_name(self):
        for status in (400, 404, 409, 500, 520):
            with self.subTest(status=status):
                self.assertEqual(self.rename(_reply(status, "x")), (status, None))

    def test_the_boolean_form_still_answers_for_callers_that_only_need_that(self):
        client = _client(post=MagicMock(return_value=_reply(200, '"https://h/seafhttp/files/1/final"')))
        self.assertTrue(client.rename_entry("rid", "/d/tmp", "final"))
        client = _client(post=MagicMock(return_value=_reply(404, "x")))
        self.assertFalse(client.rename_entry("rid", "/d/tmp", "final"))


class UploadedBytesTests(unittest.TestCase):
    def ask(self, reply: "MagicMock | Exception") -> "int | None":
        get = MagicMock(side_effect=reply) if isinstance(reply, Exception) else MagicMock(return_value=reply)
        return _client(get=get).uploaded_bytes("rid", "/d", "name.part-ab")

    def test_the_count_is_returned_and_the_query_names_the_folder_and_the_file(self):
        get = MagicMock(return_value=_reply(200, payload={"uploadedBytes": 2097152}))
        self.assertEqual(_client(get=get).uploaded_bytes("rid", "d/", "name.part-ab"), 2097152)
        _, kwargs = get.call_args
        self.assertEqual(kwargs["params"], {"parent_dir": "/d", "file_name": "name.part-ab"})

    def test_zero_is_an_answer_not_an_absence(self):
        self.assertEqual(self.ask(_reply(200, payload={"uploadedBytes": 0})), 0)

    def test_an_answer_that_is_not_a_count_is_unknown(self):
        for payload in ({"uploadedBytes": -1}, {"uploadedBytes": "5"}, {"uploadedBytes": True}, {}, []):
            with self.subTest(payload=payload):
                self.assertIsNone(self.ask(_reply(200, payload=payload)))

    def test_a_failure_is_unknown_and_never_zero(self):
        self.assertIsNone(self.ask(_reply(500)))
        self.assertIsNone(self.ask(_reply(404)))
        self.assertIsNone(self.ask(_reply(200)))      # a body that is not JSON
        import requests
        self.assertIsNone(self.ask(requests.ConnectionError("reset")))


if __name__ == "__main__":
    unittest.main()
