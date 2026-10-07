"""Guards for the release-notes format and the Release-creation script.

A GitHub Release is a *separate object* from its git tag.  Pushing a tag
therefore left a bare tag behind and nothing else -- which is exactly how
v0.3.0 ended up tagged with no release at all, while v0.2.0 and v0.2.1 have
releases because they were created by hand.

The fix automates creation from the tag, and this module keeps both halves
honest:

  * every file in ``.github/release-notes/`` follows the established format
    -- the same shape the two hand-made releases use, so an automated release
    is indistinguishable from a hand-written one; and
  * ``.github/create_github_release.sh`` turns such a file into the correct
    ``gh release create`` invocation, *including* the wheel and sdist assets.

The script is exercised against a stub ``gh`` on PATH that records its
arguments -- and the notes file it was handed, read while the file still
exists -- so the assertions cover the real execution path rather than a
re-implementation of it.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTES_DIR = REPO_ROOT / ".github" / "release-notes"
SCRIPT = REPO_ROOT / ".github" / "create_github_release.sh"

# The artifacts both hand-made releases attach.
ASSETS = (
    "git_remote_seafile-0.3.0-py3-none-any.whl",
    "git_remote_seafile-0.3.0.tar.gz",
)

NOTES_FILENAME = re.compile(r"^(v\d+\.\d+\.\d+)\.md$")
COMPARE_LINK = re.compile(
    r"^\*\*Full Changelog\*\*: "
    r"https://github\.com/tkittich/git-remote-seafile/compare/"
    r"(v\d+\.\d+\.\d+)\.\.\.(v\d+\.\d+\.\d+)$"
)

_INVOKE_MARKER = "<<<INVOKE>>>"
_BODY_MARKER = "<<<BODY>>>"
_BODY_END_MARKER = "<<<ENDBODY>>>"

# Built by replacement rather than str.format(): the stub is full of shell
# ${...} expansions and printf '%s', both of which collide with format fields.
#
# The body is captured here, at the moment gh is handed the notes file, because
# the script deletes its temporary body file on exit.
_GH_STUB = """#!/usr/bin/env bash
{
  printf '__INVOKE__\\n'
  for a in "$@"; do
    printf '%s\\n' "$a"
  done
  prev=""
  for a in "$@"; do
    if [ "$prev" = "--notes-file" ]; then
      printf '__BODY__\\n'
      cat -- "$a"
      printf '__BODYEND__\\n'
    fi
    prev="$a"
  done
} >> "$GH_STUB_LOG"
if [ "${1:-}" = "release" ] && [ "${2:-}" = "view" ]; then
  exit "${GH_STUB_VIEW_EXIT:-1}"
fi
exit 0
""".replace("__INVOKE__", _INVOKE_MARKER).replace("__BODY__", _BODY_MARKER).replace(
    "__BODYEND__", _BODY_END_MARKER
)


def _notes_files() -> list[pathlib.Path]:
    if not NOTES_DIR.is_dir():
        return []
    return sorted(p for p in NOTES_DIR.glob("v*.md") if NOTES_FILENAME.match(p.name))


def _tag_of(path: pathlib.Path) -> str:
    return NOTES_FILENAME.match(path.name).group(1)


def _find_bash() -> str | None:
    """Locate a POSIX bash, preferring the one that ships with git.

    On Windows ``shutil.which("bash")`` can resolve to ``System32\\bash.exe``,
    which is not a shell at all -- it is a launcher for WSL, where the paths and
    the toolchain differ from the Git Bash this repository's scripts are written
    for.  Git Bash is the right interpreter there, so look next to git first and
    reject the WSL launcher outright.
    """
    override = os.environ.get("GRS_BASH")
    if override:
        return override if pathlib.Path(override).is_file() else None

    git = shutil.which("git")
    if git:
        git_dir = pathlib.Path(git).resolve().parent
        for candidate in (git_dir / "bash.exe", git_dir.parent / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)

    found = shutil.which("bash")
    if found and "system32" not in found.replace("\\", "/").lower():
        return found
    return None


class TestReleaseNotesFormat(unittest.TestCase):
    """The notes file becomes the published release body, so it is a contract."""

    def setUp(self):
        self.files = _notes_files()
        if not self.files:
            self.skipTest("no .github/release-notes/v*.md files to check yet")

    def test_first_line_is_the_title_in_the_established_format(self):
        for path in self.files:
            tag = _tag_of(path)
            first = path.read_text(encoding="utf-8").splitlines()[0]
            with self.subTest(notes=path.name):
                self.assertTrue(
                    first.startswith(f"# {tag} - "),
                    f"{path.name} must open with '# {tag} - <summary>'; got {first!r}",
                )
                self.assertGreater(
                    len(first), len(f"# {tag} - "),
                    f"{path.name} has an empty summary in its title",
                )

    def test_body_starts_at_the_whats_changed_heading(self):
        # The title lives in the release's *name* field, not in the body: the
        # two hand-made releases start straight at "## What's Changed in vX".
        for path in self.files:
            tag = _tag_of(path)
            lines = path.read_text(encoding="utf-8").splitlines()
            body = [line for line in lines[1:] if line.strip()]
            with self.subTest(notes=path.name):
                self.assertEqual(
                    body[0], f"## What's Changed in {tag}",
                    f"{path.name} must start its body at '## What's Changed in {tag}'",
                )

    def test_ends_with_the_full_changelog_link_for_this_tag(self):
        for path in self.files:
            tag = _tag_of(path)
            lines = [
                line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            with self.subTest(notes=path.name):
                self.assertEqual(
                    lines[-2], "---",
                    f"{path.name} must end with '---' before the changelog link",
                )
                match = COMPARE_LINK.match(lines[-1])
                self.assertIsNotNone(
                    match,
                    f"{path.name} must end with the Full Changelog compare link; "
                    f"got {lines[-1]!r}",
                )
                self.assertEqual(
                    match.group(2), tag,
                    f"{path.name} links a comparison ending at {match.group(2)}, not {tag}",
                )
                self.assertNotEqual(match.group(1), tag)

    def test_compare_link_base_is_an_existing_tag(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        existing = subprocess.run(
            ["git", "tag", "-l"], cwd=str(REPO_ROOT),
            capture_output=True, text=True,
        ).stdout.split()
        self.assertTrue(existing, "no git tags found")
        for path in self.files:
            match = COMPARE_LINK.match(
                [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()][-1]
            )
            with self.subTest(notes=path.name):
                self.assertIn(
                    match.group(1), existing,
                    f"{path.name} compares against {match.group(1)}, which is not a tag",
                )


class TestCreateGithubReleaseScript(unittest.TestCase):
    """Drive the real script with a stub `gh` and inspect the arguments."""

    def setUp(self):
        self.bash = _find_bash()
        if self.bash is None:
            self.skipTest("no POSIX bash found (set GRS_BASH to point at one)")
        self.assertTrue(SCRIPT.is_file(), f"missing release script: {SCRIPT}")

        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="grs-release-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.log = self.tmp / "gh-argv.log"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stub = self.bin / "gh"
        stub.write_text(_GH_STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        self.dist = self.tmp / "dist"
        self.dist.mkdir()
        for name in ASSETS:
            (self.dist / name).write_bytes(b"stub artifact")

        self.notes = self.tmp / "release-notes"
        self.notes.mkdir()

    # -- helpers ---------------------------------------------------------

    def _write_notes(self, tag: str, title: str, body: str) -> None:
        (self.notes / f"{tag}.md").write_text(f"# {title}\n\n{body}", encoding="utf-8")

    def _run(self, tag: str = "v0.3.0", view_exit: str = "1"):
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["GH_STUB_LOG"] = str(self.log)
        env["GH_STUB_VIEW_EXIT"] = view_exit
        env["NOTES_DIR"] = str(self.notes)
        env["DIST_DIR"] = str(self.dist)
        return subprocess.run(
            [self.bash, str(SCRIPT), tag],
            cwd=str(self.tmp), env=env, capture_output=True, text=True,
        )

    def _invocations(self) -> list[dict]:
        """Return ``[{"argv": [...], "body": str | None}, ...]``."""
        if not self.log.is_file():
            return []
        calls = []
        for block in self.log.read_text(encoding="utf-8").split(f"{_INVOKE_MARKER}\n"):
            if not block.strip():
                continue
            body = None
            if _BODY_MARKER in block:
                head, _, rest = block.partition(f"{_BODY_MARKER}\n")
                body, _, _ = rest.partition(f"{_BODY_END_MARKER}\n")
                block = head
            calls.append({"argv": block.splitlines(), "body": body})
        return calls

    def _creates(self) -> list[dict]:
        return [c for c in self._invocations() if c["argv"][:2] == ["release", "create"]]

    @staticmethod
    def _basenames(argv: list[str]) -> list[str]:
        return [pathlib.PurePath(a.replace("\\", "/")).name for a in argv]

    # -- tests -----------------------------------------------------------

    def test_uses_the_notes_title_and_body_and_attaches_the_artifacts(self):
        self._write_notes(
            "v0.3.0", "v0.3.0 - Test Summary",
            "## What's Changed in v0.3.0\n\n### Bug Fixes\n- fixed a thing\n",
        )

        proc = self._run("v0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        creates = self._creates()
        self.assertEqual(len(creates), 1, f"expected one release create: {self._invocations()}")
        argv = creates[0]["argv"]

        self.assertEqual(argv[2], "v0.3.0", f"wrong tag position: {argv}")
        self.assertIn("--title", argv)
        self.assertEqual(argv[argv.index("--title") + 1], "v0.3.0 - Test Summary")

        self.assertIn("--notes-file", argv)
        body = creates[0]["body"]
        self.assertIsNotNone(body, "the notes file was never read by gh")
        self.assertTrue(
            body.startswith("## What's Changed in v0.3.0"),
            f"body must start at the heading; got {body[:80]!r}",
        )
        self.assertIn("- fixed a thing", body)
        self.assertNotIn(
            "# v0.3.0 - Test Summary", body,
            "the title must not be repeated in the body",
        )

        names = self._basenames(argv)
        for asset in ASSETS:
            self.assertIn(asset, names, f"{asset} was not attached: {argv}")

    def test_leaves_an_existing_release_alone(self):
        self._write_notes("v0.3.0", "v0.3.0 - Test Summary", "## What's Changed in v0.3.0\n")

        proc = self._run("v0.3.0", view_exit="0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self._creates(), [],
            "an existing release must not be recreated or overwritten",
        )

    def test_falls_back_to_generated_notes_when_there_is_no_notes_file(self):
        proc = self._run("v9.9.9")  # no notes file written for this tag

        self.assertEqual(proc.returncode, 0, proc.stderr)
        creates = self._creates()
        self.assertEqual(len(creates), 1, f"expected one release create: {self._invocations()}")
        argv = creates[0]["argv"]

        self.assertIn("--generate-notes", argv)
        self.assertNotIn("--notes-file", argv)
        self.assertEqual(argv[argv.index("--title") + 1], "v9.9.9")


if __name__ == "__main__":
    unittest.main()
