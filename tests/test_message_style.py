"""What the tools print: one prefix style, one unit system, and a test per message.

These are guards, not tests of behaviour.  Each of them exists because the thing
it checks drifted once: a later batch of messages was written as ``WARNING:`` and
``NOTE:`` after the convention was ``Warning:`` and ``Note:``; a byte count was
printed as ``292.4 GB`` by one function and ``9 GiB`` by another; the helper's
``13489 KB`` divided by 1024.  The convention is written down in AGENTS.md
("User-visible text") and CONTRIBUTING.md.

The scan reads the *source* with ``ast`` -- string literals only, so comments and
docstrings are free to say "KB" when they talk about something else -- over the
snapshot tool's parts and the package (not the generated ``snapshot_tool.py``,
which would only repeat the parts).
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))

import seafile_snapshot as snap  # noqa: E402
from git_remote_seafile.units import format_bytes  # noqa: E402

#: The notification levels, spelled as the convention spells them.
LEVELS = ("Warning", "Note", "Notice", "Hint", "Error")
#: Words that mean a level when they open a message, in any spelling.
_LEVEL_WORDS = {"warning", "note", "notice", "hint", "tip", "error", "info"}
_LEVEL = re.compile(r"^\s*([A-Za-z]+):(\s|$)")
#: A number (or a formatted value) followed by a decimal-looking unit.  The tools
#: divide by 1024, so the label must be KiB/MiB/GiB/TiB.
_DECIMAL_UNIT = re.compile(r"(\d|\})\s?(KB|MB|GB|TB)\b")
#: Messages no test mentions yet.  It may only shrink: a new message needs a test.
_BASELINE = pathlib.Path(__file__).with_name("untested_messages.txt")


def _sources() -> list[pathlib.Path]:
    parts = sorted((_ROOT / "tools" / "_snapshot").glob("*.py"))
    package = sorted(
        p for p in (_ROOT / "git_remote_seafile").rglob("*.py") if p.name != "snapshot_tool.py"
    )
    return parts + package


def _literals(path: pathlib.Path):
    """``(line, text)`` for every string literal that is not a docstring.

    An f-string is joined with ``{}`` standing for each formatted value.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                skip.add(id(body[0].value))
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            inside.update(id(child) for child in ast.walk(node) if child is not node)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip and id(node) not in inside:
                yield node.lineno, node.value


def _where(path: pathlib.Path, line: int) -> str:
    return f"{path.relative_to(_ROOT).as_posix()}:{line}"


def _words(fragment: str) -> list[str]:
    return [w.strip(".,;:()'\"") for w in re.sub(r"\s+", " ", fragment).split()]


def _messages():
    """``(key, text)`` for each message that opens with a level word."""
    for path in _sources():
        for _line, text in _literals(path):
            match = _LEVEL.match(text)
            if match and match.group(1) in LEVELS:
                fragments = [re.sub(r"\s+", " ", f).strip() for f in text.split("{}")]
                long = [f for f in fragments if len(f) >= 15]
                if long:  # a message with nothing distinctive to assert cannot be checked
                    yield f"{path.relative_to(_ROOT).as_posix()}: {long[0][:60]}", text


def _mentioned(text: str, tests: str) -> bool:
    """Whether the tests quote any three consecutive words of the message."""
    for fragment in text.split("{}"):
        words = _words(fragment)
        for i in range(len(words) - 2):
            gram = " ".join(words[i:i + 3])
            if len(gram) >= 14 and gram in tests:
                return True
    return False


def _tests_text() -> str:
    me = pathlib.Path(__file__).resolve()
    chunks = [
        p.read_text(encoding="utf-8")
        for p in sorted((_ROOT / "tests").glob("*.py"))
        if p.resolve() != me
    ]
    flat = re.sub(r"\s+", " ", "\n".join(chunks))
    # Words as the messages are tokenised: no stray punctuation to defeat a match.
    return " ".join(w.strip(".,;:()'\"") for w in flat.split())


class MessageStyleTests(unittest.TestCase):
    def test_notification_prefixes_are_title_case(self):
        wrong = []
        for path in _sources():
            for line, text in _literals(path):
                match = _LEVEL.match(text)
                if match and match.group(1).lower() in _LEVEL_WORDS and match.group(1) not in LEVELS:
                    wrong.append(f"{_where(path, line)}: {text.strip()[:50]!r}")
        self.assertEqual(
            wrong, [],
            "notifications open with Warning:/Note:/Notice:/Hint:/Error: (title case; 'tip' "
            "is Hint, 'info' is Note) -- see AGENTS.md, User-visible text",
        )

    def test_byte_counts_are_labelled_in_binary_units(self):
        wrong = []
        for path in _sources():
            for line, text in _literals(path):
                if _DECIMAL_UNIT.search(text):
                    wrong.append(f"{_where(path, line)}: {text.strip()[:60]!r}")
        self.assertEqual(
            wrong, [],
            "the tools divide by 1024, so print KiB/MiB/GiB/TiB (use format_bytes / human_bytes), "
            "not KB/MB/GB/TB",
        )

    def test_the_two_byte_formatters_agree(self):
        # The snapshot tool must run without the package installed, so it carries a
        # copy of git_remote_seafile.units.format_bytes; this is what keeps it a copy.
        samples = [0, 1, 1023, 1024, 1536, 1048575, 1048576, 3 * 1024 ** 2 + 1,
                   1024 ** 3 - 1, 5 * 1024 ** 3, 291 * 1024 ** 3, 3 * 1024 ** 4,
                   1024 ** 5, 12345.6]
        for n in samples:
            with self.subTest(n=n):
                self.assertEqual(snap.human_bytes(n), format_bytes(n))
        self.assertEqual(format_bytes(1536), "1.5 KiB")
        self.assertEqual(format_bytes(5 * 1024 ** 3), "5.0 GiB")

    def test_a_new_message_comes_with_a_test_that_mentions_it(self):
        tests = _tests_text()
        listed = _read_baseline()
        fresh = sorted(
            key for key, text in _messages()
            if not _mentioned(text, tests) and key not in listed
        )
        self.assertEqual(
            fresh, [],
            "these messages are not mentioned by any test: add one that triggers the message "
            "and asserts a distinctive part of its text (three words in a row are enough). "
            "Only a message that cannot be triggered in a test may go in "
            "tests/untested_messages.txt, with the reason as a comment above it",
        )

    def test_the_untested_list_only_shrinks(self):
        tests = _tests_text()
        current = {key: text for key, text in _messages()}
        stale = []
        for key in sorted(_read_baseline()):
            if key not in current:
                stale.append(f"{key}  (no longer in the source: reworded or removed)")
            elif _mentioned(current[key], tests):
                stale.append(f"{key}  (a test mentions it now)")
        self.assertEqual(
            stale, [],
            "delete these lines from tests/untested_messages.txt -- the list is only allowed "
            "to shrink",
        )


def _read_baseline() -> set[str]:
    keys = set()
    for line in _BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keys.add(line)
    return keys


if __name__ == "__main__":
    unittest.main()
