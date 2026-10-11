"""Guards for the release-notes format, the Release-creation script, and the
workflows themselves.

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

The last third of the module widens that to the workflows as configuration: a
token scope, a concurrency group, a dependency cache, a worker count, an
attestation, a dependency floor and an ignore rule.  None of those can be seen
from a green test run, which is why they are pinned rather than reviewed.
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

try:
    # top-level import under `unittest discover tests` and the parallel runner
    from _helpers import find_bash as _find_bash  # noqa: E402
except ImportError:  # package-style: python -m unittest tests.test_launcher
    from tests._helpers import find_bash as _find_bash  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTES_DIR = REPO_ROOT / ".github" / "release-notes"
SCRIPT = REPO_ROOT / ".github" / "create_github_release.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

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

# The first tag whose GitHub Release is created by the automated workflow.
# v0.1.0/v0.2.x predate it and have no notes file; everything from here on
# is expected to have one.
FIRST_AUTOMATED_RELEASE = "v0.3.0"

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

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


def _version_key(tag: str) -> tuple[int, int, int] | None:
    """Order tags numerically, or ``None`` if it is not a release tag.

    String comparison would sort v0.10.0 before v0.9.0, which is a bug that
    only shows up once the project reaches a tenth minor release.
    """
    match = _TAG_RE.match(tag)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))




# -- a narrow reader for a workflow's `on:` block -------------------------
#
# Which events start a workflow is the one thing about release.yml that a
# regression can quietly ruin, and PyYAML is not a dependency -- pulling it in
# so a test can read one five-line block would make the guard heavier than the
# thing it guards.  These helpers understand only what that block uses:
# top-level keys, nested keys, block sequences, comments and quoted scalars.
# They are exercised against synthetic input below so that a bug in the reader
# cannot pass for a bug in the workflow.


def _strip_comment(line: str) -> str:
    """Remove a trailing YAML comment, leaving quoted text alone."""
    kept = []
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            break
        kept.append(char)
    return "".join(kept)


def _on_block(text: str) -> list[str]:
    """The lines of the workflow's top-level ``on:`` block, comments removed.

    Stops at the next line that starts in column zero, which is the next
    top-level key.
    """
    block: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not inside:
            if line.rstrip() == "on:":
                inside = True
            continue
        if line.strip() and not line[0].isspace():
            break
        block.append(line)
    return block


def _keys(lines: list[str]) -> list[str]:
    """The mapping keys at the shallowest indentation in ``lines``."""
    indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
    if not indents:
        return []
    base = min(indents)
    keys = []
    for line in lines:
        if not line.strip() or ":" not in line:
            continue
        if len(line) - len(line.lstrip()) != base:
            continue
        keys.append(line.strip().split(":", 1)[0].strip())
    return keys


def _child_lines(lines: list[str], key: str) -> list[str]:
    """The lines nested more deeply than the ``key:`` line itself."""
    out: list[str] = []
    inside = False
    base = 0
    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if not inside:
            if ":" in line and line.strip().split(":", 1)[0].strip() == key:
                inside = True
                base = indent
            continue
        if indent <= base:
            break
        out.append(line)
    return out


def _values(lines: list[str]) -> list[str]:
    """Block-sequence items, e.g. ``- 'v*'`` -> ``v*``."""
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            out.append(stripped[2:].strip().strip("'\""))
        elif stripped == "-":
            out.append("")
    return out


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
        if not existing:
            # actions/checkout clones shallow and fetches no tags by default,
            # whatever its major version, so there is nothing to compare
            # against here.  CI passes `fetch-depth: 0` to keep this check
            # meaningful; a bare `git clone --depth=1` cannot.
            self.skipTest("no tags in this clone (shallow checkout)")
        for path in self.files:
            match = COMPARE_LINK.match(
                [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()][-1]
            )
            with self.subTest(notes=path.name):
                self.assertIn(
                    match.group(1), existing,
                    f"{path.name} compares against {match.group(1)}, which is not a tag",
                )


def _released_tags() -> list[str] | None:
    """Tags reachable from HEAD, or ``None`` when this clone cannot say.

    ``--merged HEAD`` matters: a plain ``git tag -l`` also lists tags from
    other branches and from commits that do not exist yet on the branch under
    test, so checking the working tree against it would fail on an older
    branch for a tag it has never seen.  Only tags that are ancestors of the
    commit being tested were released from this line of history, and their
    notes files must be present in its tree.
    """
    if shutil.which("git") is None:
        return None
    proc = subprocess.run(
        ["git", "tag", "--merged", "HEAD", "-l"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    tags = proc.stdout.split()
    # actions/checkout clones shallow and fetches no tags by default, whatever
    # its major version.  CI passes `fetch-depth: 0` to keep this meaningful.
    return tags or None


class TestEveryTagHasReleaseNotes(unittest.TestCase):
    """A tag with no notes file still gets a Release -- but an empty one.

    ``create_github_release.sh`` falls back to ``gh release create
    --generate-notes`` when ``<tag>.md`` is missing, so nothing *fails* and
    nothing warns: the release simply appears with a bare commit list.  That
    is how v0.6.2 shipped while every tag around it carries curated notes --
    the tag was cut, its entry was written into CHANGELOG.md, and no file was
    ever added here.  The gap is invisible from both ends, which is what makes
    it worth a guard rather than a checklist.
    """

    def setUp(self):
        self.tags = _released_tags()
        if self.tags is None:
            self.skipTest("no tags reachable from HEAD (shallow checkout?)")

    def test_the_floor_is_a_real_released_tag(self):
        # If the floor is ever retyped or the history is rewritten, fail here
        # rather than silently exempting a whole range from the check below.
        self.assertIn(
            FIRST_AUTOMATED_RELEASE, self.tags,
            f"{FIRST_AUTOMATED_RELEASE} is not reachable from HEAD, so the "
            f"release-notes floor no longer means anything",
        )

    def test_every_release_since_the_floor_has_a_notes_file(self):
        floor = _version_key(FIRST_AUTOMATED_RELEASE)
        covered = {_tag_of(p) for p in _notes_files()}

        missing = [
            tag for tag in self.tags
            if (key := _version_key(tag)) is not None and key >= floor
            and tag not in covered
        ]

        self.assertEqual(
            missing, [],
            "these tags have no .github/release-notes/<tag>.md, so their "
            "GitHub Release falls back to generated notes instead of a "
            "curated body: " + ", ".join(missing),
        )


_ON_BLOCK_BEFORE = """\
name: Release

on:
  release:
    types: [published]
  push:
    tags:
      - 'v*'

permissions:
  contents: read
"""

_ON_BLOCK_AFTER = """\
name: Release

# A comment that mentions release: and on: and push: tags.
on:
  push:
    tags:
      - 'v*'   # every version tag

permissions:
  contents: read
"""


class TestWorkflowTriggerReader(unittest.TestCase):
    """Check the reader itself, so a bug in it cannot pass for a bug in the file.

    The reader is only as trustworthy as its behaviour on the two shapes that
    matter -- the one that caused the duplicate release run, and the one that
    replaced it -- plus the comment and quoting cases that a naive
    line-by-line scan gets wrong.
    """

    def test_it_reads_both_triggers_out_of_the_shape_that_was_wrong(self):
        block = _on_block(_ON_BLOCK_BEFORE)
        self.assertEqual(_keys(block), ["release", "push"])

    def test_it_reads_only_the_tag_push_out_of_the_shape_that_replaced_it(self):
        block = _on_block(_ON_BLOCK_AFTER)
        self.assertEqual(_keys(block), ["push"])

    def test_it_stops_at_the_next_top_level_key(self):
        for text in (_ON_BLOCK_BEFORE, _ON_BLOCK_AFTER):
            with self.subTest(text=text.splitlines()[0]):
                block = _on_block(text)
                self.assertNotIn("permissions", _keys(block))
                self.assertFalse(
                    [ln for ln in block if "contents" in ln],
                    "the reader ran past the end of the on: block",
                )

    def test_it_does_not_mistake_a_comment_for_a_trigger(self):
        # The comment above `on:` names both `release:` and `push: tags`.  A
        # scan that ignored comments would call this file a double-trigger.
        block = _on_block(_ON_BLOCK_AFTER)
        self.assertEqual(_keys(block), ["push"])

    def test_it_does_not_mistake_a_comment_for_a_tag_pattern(self):
        push = _child_lines(_on_block(_ON_BLOCK_AFTER), "push")
        self.assertEqual(_values(_child_lines(push, "tags")), ["v*"])

    def test_it_keeps_a_hash_inside_quotes(self):
        self.assertEqual(_strip_comment("  - 'v#1'"), "  - 'v#1'")
        self.assertEqual(_strip_comment("  - 'v1'  # a note"), "  - 'v1'  ")
        self.assertEqual(_strip_comment("# whole line"), "")

    def test_it_reaches_a_nested_sequence(self):
        push = _child_lines(_on_block(_ON_BLOCK_BEFORE), "push")
        self.assertEqual(_keys(push), ["tags"])
        self.assertEqual(_values(_child_lines(push, "tags")), ["v*"])


class TestReleaseWorkflowTriggers(unittest.TestCase):
    """A tag push must be the *only* event that starts a release run.

    The workflow used to trigger on both ``push: tags: ['v*']`` and
    ``release: types: [published]``, and those two overlap.  The tag-triggered
    run calls create_github_release.sh, which creates a Release with
    ``gh release create`` -- and *publishing* that Release fired the second
    trigger.  So every release built the wheel and sdist, ran the PyPI publish
    job, and re-ran the release script a second time.  The duplicate PyPI
    upload was harmless only because of ``skip-existing: true``, and the second
    release job was a no-op only because the script checks ``gh release view``
    first; neither accident is a reason to run the pipeline twice.

    The overlap cannot simply be reordered away: on the ``release: published``
    path the release already exists by definition, so the script's idempotency
    check always short-circuits and the wheel and sdist are *never* attached.
    That path could not do the one job the workflow exists for, which is why it
    was removed rather than kept as a recovery route.  Re-running a failed run
    from the Actions tab is the recovery route.
    """

    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}")
        self.block = _on_block(WORKFLOW.read_text(encoding="utf-8"))
        self.assertTrue(
            self.block, "release.yml has no top-level `on:` block, or it is empty"
        )

    def test_a_version_tag_push_starts_a_release(self):
        self.assertIn(
            "push", _keys(self.block),
            "release.yml no longer triggers on a push; pushing a v* tag is the "
            "only thing that creates a release",
        )
        push = _child_lines(self.block, "push")
        self.assertIn("tags", _keys(push), "the push trigger is not restricted to tags")
        self.assertIn(
            "v*", _values(_child_lines(push, "tags")),
            "the push trigger no longer matches version tags",
        )

    def test_publishing_a_release_does_not_start_a_second_run(self):
        self.assertNotIn(
            "release", _keys(self.block),
            "release.yml triggers on `release:` again.  Its own tag-triggered "
            "run creates a Release, so that trigger makes every release run "
            "twice -- and on that path the release already exists, so "
            "create_github_release.sh short-circuits and the wheel and sdist "
            "are never attached.",
        )


def _flow_sequence(lines: list[str], key: str) -> list[str]:
    """The items of a ``key: [a, b]`` flow sequence."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}:") and "[" in stripped:
            inner = stripped.split("[", 1)[1].rsplit("]", 1)[0]
            return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    return []


def _job(text: str, job: str) -> list[str]:
    """The lines of one top-level job in a workflow, comments removed."""
    lines = [_strip_comment(raw) for raw in text.splitlines()]
    return _child_lines(_child_lines(lines, "jobs"), job)


def _matrix_of(text: str) -> dict[str, list[str]]:
    """The ``test`` job's matrix, as ``{dimension: values}``."""
    matrix = _child_lines(_job(text, "test"), "matrix")
    return {key: _flow_sequence(matrix, key) for key in ("os", "python-version")}


class TestReleaseGateShape(unittest.TestCase):
    """D2/D3 — what the gate has to check before it publishes anything.

    The gate ran a single interpreter on a single platform and never opened the
    artifact it was about to attach, so neither the platform claim a release
    makes nor the wheel itself was exercised.
    """

    def _workflow(self) -> str:
        return WORKFLOW.read_text(encoding="utf-8")

    def test_the_gate_covers_the_supported_matrix(self):
        matrix = _matrix_of(self._workflow())
        self.assertTrue(
            matrix["os"],
            "the release gate's `test` job names no operating systems: one "
            "platform proves the suite passes there, which is not what a "
            "release claims",
        )
        self.assertTrue(
            matrix["python-version"],
            "the release gate's `test` job names no Python versions",
        )

    def test_the_gate_matches_the_matrix_ci_runs(self):
        """A gate narrower than CI would wave through what CI fails."""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertEqual(
            _matrix_of(self._workflow()),
            _matrix_of(ci),
            "the release gate's matrix and CI's have diverged; a release would "
            "then be gated on a narrower set of platforms than the project "
            "claims to support",
        )

    def test_the_tag_check_runs_before_the_suite(self):
        """A mistyped tag should fail in seconds, not after the whole matrix."""
        workflow = self._workflow()
        self.assertNotIn(
            "Verify git tag",
            "\n".join(_job(workflow, "test")),
            "the tag/version check is back inside the test job, where it ran "
            "*after* the suite",
        )
        verify = "\n".join(_job(workflow, "verify-tag"))
        self.assertIn(
            "Verify git tag", verify,
            "release.yml has no `verify-tag` job to check the tag against "
            "__version__",
        )
        self.assertIn(
            "needs: verify-tag",
            "\n".join(_job(workflow, "test")),
            "the test job does not depend on the tag check, so it can run "
            "before the tag has been validated",
        )

    def test_the_build_job_smoke_installs_the_artifact(self):
        """D3 — the artifact is what users install, so something must open it."""
        build = "\n".join(_job(self._workflow(), "build"))
        self.assertIn(
            "smoke_install_wheel.sh", build,
            "the build job uploads the wheel and sdist without ever installing "
            "them, so a broken wheel is published unchecked",
        )

    def test_the_smoke_install_script_exists_and_is_runnable(self):
        script = REPO_ROOT / ".github" / "smoke_install_wheel.sh"
        self.assertTrue(script.is_file(), "smoke_install_wheel.sh is missing")
        self.assertIn("twine check", script.read_text(encoding="utf-8"))


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


# -- the workflow-hardening tranche (D1/D4/D5/D6/D7/D10) -------------------
#
# Everything below guards a property of a *configuration file* rather than of
# the code: a workflow with no token scope, no concurrency group and a worker
# count copied from an assumption about the runner; a release that publishes
# artifacts nothing attested; a dependency floor picked for a feature instead of
# for a CVE; ignore rules that live in the directory they ignore.  None of them
# can be seen from a green test run, which is exactly why each is pinned here.
#
# The readers are the same narrow ones the `on:`-block guards use.  They are
# deliberately not a YAML parser: PyYAML is not a dependency of this project,
# and the shapes read here are a handful of nested keys.


_FLOOR_RE = re.compile(r'"requests>=([0-9][0-9.]*)"')
_PIN_RE = re.compile(r'"requests==([0-9][0-9.]*)"')


def _whole(text: str) -> list[str]:
    """A workflow's lines, with trailing comments removed."""
    return [_strip_comment(raw) for raw in text.splitlines()]


def _top_level(text: str, key: str) -> list[str]:
    """The lines nested under a workflow's top-level ``key:``."""
    return _child_lines(_whole(text), key)


def _runner_lines(text: str) -> list[str]:
    """Every line that invokes the parallel runner, comments removed.

    Comments have to go first: the replacement for the hard-coded worker count
    is *explained* by a comment that quotes the old value, and a naive scan of
    the raw text would find `-j 4` in that explanation.
    """
    return [
        _strip_comment(line).strip()
        for line in text.splitlines()
        if "run_tests_parallel.py" in _strip_comment(line)
    ]


def _declared_requests_floor() -> str | None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = _FLOOR_RE.search(text)
    return match.group(1) if match else None


def _ci_pinned_requests() -> str | None:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = _PIN_RE.search(text)
    return match.group(1) if match else None


class TestCiWorkflowHardening(unittest.TestCase):
    """D1/D6/D7 — three absences in ci.yml that a green run cannot reveal.

    A workflow with no token scope, no concurrency group and a worker count
    copied from an assumption about the runner all produce exactly the same
    green pipeline as one without those problems, until the day the assumption
    is wrong or the minutes are billed.
    """

    def setUp(self):
        self.path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(self.path.is_file(), f"missing workflow: {self.path}")
        self.text = self.path.read_text(encoding="utf-8")

    def test_the_token_is_scoped(self):
        """D1 — least privilege, stated rather than inherited."""
        perms = _top_level(self.text, "permissions")
        self.assertTrue(
            perms,
            "ci.yml has no top-level `permissions:` block, so every job runs "
            "with whatever the repository's default token scope happens to be. "
            "That default is read/write on older repository settings, and this "
            "workflow only ever reads.",
        )
        self.assertIn(
            "contents: read", "\n".join(perms),
            "ci.yml scopes its token, but not to `contents: read`",
        )

    def test_a_superseded_run_is_cancelled(self):
        """D6 — the matrix is fifteen jobs, and pushes arrive in series."""
        block = _top_level(self.text, "concurrency")
        self.assertTrue(
            block,
            "ci.yml has no `concurrency:` group, so a second push to a branch "
            "queues fifteen more jobs behind the previous push's fifteen "
            "instead of superseding them",
        )
        joined = "\n".join(block)
        self.assertIn("group:", joined, "the concurrency block names no group")
        self.assertRegex(
            joined, r"cancel-in-progress:\s*true",
            "the concurrency group is declared but does not cancel, so a "
            "superseded run still holds a runner to completion",
        )

    def test_the_matrix_caches_pip(self):
        """D6 — fifteen legs resolving the same dependency set from scratch."""
        test_job = "\n".join(_job(self.text, "test"))
        self.assertRegex(
            test_job, r"cache:\s*'?pip'?",
            "the test job installs its dependencies without a pip cache, so "
            "each of the fifteen legs resolves the same set from the index",
        )
        self.assertIn(
            "cache-dependency-path: pyproject.toml", test_job,
            "the pip cache is enabled but its key is left to the action's file "
            "search, so it is not tied to the file that declares the "
            "dependencies",
        )

    def test_the_worker_count_is_not_a_literal(self):
        """D7 — `-j 4` was justified by a claim about the runner's size."""
        lines = _runner_lines(self.text)
        self.assertTrue(
            lines, "ci.yml no longer runs tools/run_tests_parallel.py at all"
        )
        for line in lines:
            with self.subTest(invocation=line):
                self.assertIn(
                    "-j 0", line,
                    "the worker count is pinned again. `-j 0` asks the runner "
                    "for one worker per CPU it can see; a literal assumes a "
                    "vCPU count that differs per OS in this matrix and changes "
                    "whenever GitHub resizes a runner image.",
                )

    def test_the_declared_dependency_floor_is_the_one_that_is_tested(self):
        """D5 — a floor nothing installs is a number, not a claim."""
        job = "\n".join(_job(self.text, "min-deps"))
        self.assertTrue(
            job,
            "ci.yml has no `min-deps` job.  Every other job installs the "
            "*latest* requests, so nothing exercises the floor pyproject.toml "
            "declares.",
        )
        declared = _declared_requests_floor()
        pinned = _ci_pinned_requests()
        self.assertIsNotNone(declared, "pyproject.toml declares no `requests>=` floor")
        self.assertIsNotNone(
            pinned,
            "the `min-deps` job does not pin a `requests==` version, so it "
            "tests whatever the resolver picks",
        )
        self.assertEqual(
            pinned, declared,
            "the version pinned in ci.yml's `min-deps` job is not the floor "
            "pyproject.toml declares, so the job tests a version the package "
            "does not claim to support",
        )


class TestDependencyFloorIsASecurityFloor(unittest.TestCase):
    """D5 — `requests>=2.25.0` predated three CVEs in requests itself.

    This helper holds a Seafile token and runs inside every `git` invocation, so
    the transport library's credential handling is part of its attack surface.
    A floor below the fixes below is a floor that permits a known leak.
    """

    def test_the_floor_includes_the_credential_fixes(self):
        floor = _declared_requests_floor()
        self.assertIsNotNone(floor, "pyproject.toml declares no `requests>=` floor")
        parts = tuple(int(p) for p in floor.split("."))
        self.assertGreaterEqual(
            parts, (2, 32, 4),
            f"the requests floor is {floor}, below 2.32.4.  That is where "
            "CVE-2024-47081 was fixed -- a crafted URL could leak .netrc "
            "credentials to a third party.  The two earlier credential fixes in "
            "this line are CVE-2023-32681 (2.31.0, the Proxy-Authorization "
            "header followed a redirect) and CVE-2024-35195 (2.32.0, "
            "`verify=False` on a Session persisted into later requests).",
        )


class TestReleaseArtifactsAreAttested(unittest.TestCase):
    """D4 — a checksum says what was published, not who built it or from where.

    The Release page is the only distribution channel this project has, and the
    artifact is a program that will be handed a Seafile token.  Until now the
    workflow built a wheel, attached it, and offered no way to tell whether the
    file a user downloads came from this repository's release workflow at all.
    """

    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}")
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.build = "\n".join(_job(self.text, "build"))
        self.assertTrue(
            self.build.strip(), "release.yml has no `build` job to inspect"
        )

    def test_the_build_job_can_attest(self):
        """The two permissions an attestation needs, scoped to this job only."""
        perms = "\n".join(_child_lines(_whole(self.build), "permissions"))
        self.assertTrue(
            perms,
            "the `build` job declares no `permissions:` block, so it cannot "
            "mint the OIDC token an attestation is signed against",
        )
        self.assertIn(
            "id-token: write", perms,
            "the `build` job cannot request the OIDC token the attestation is "
            "signed against",
        )
        self.assertIn(
            "attestations: write", perms,
            "the `build` job cannot store the attestation it generates",
        )

    def test_the_build_job_attests_what_it_built(self):
        self.assertIn(
            "attest-build-provenance", self.build,
            "the release publishes the wheel and sdist without attesting them, "
            "so a user has no way to verify who built the file they installed",
        )
        self.assertRegex(
            self.build, r"subject-path:\s*dist/",
            "the attestation step names no subject, so it attests nothing",
        )

    def test_the_checksum_file_is_written_after_the_attestation(self):
        """The order is the point, not an accident of editing.

        The subject glob is `dist/*`, so a checksum file written *before* the
        attestation would be swept up as an artifact to attest alongside the
        wheel and the sdist.
        """
        self.assertIn(
            "SHA256SUMS", self.build,
            "release.yml writes no SHA256SUMS, so a user without `gh` has no "
            "digest to check a download against",
        )
        self.assertLess(
            self.build.index("attest-build-provenance"),
            self.build.index("SHA256SUMS"),
            "the checksum file is written before the attestation, so the "
            "subject glob `dist/*` attests the checksum file too",
        )


class TestIgnoreRulesAreVisible(unittest.TestCase):
    """D10 — the rule lived in the directory it was supposed to ignore.

    `.review-venv/` and `.ruff_cache/` each ship a `.gitignore` of their own, so
    the repository's own file could stay silent and `git status` would still be
    clean.  That holds right up until something inside one of them is deleted --
    at which point thousands of files appear as untracked and it turns out this
    repository never had a rule covering them.
    """

    def test_local_caches_are_named_in_the_repository_gitignore(self):
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        entries = {line.strip() for line in text.splitlines()}
        for name in (".review-venv/", ".ruff_cache/"):
            with self.subTest(entry=name):
                self.assertIn(
                    name, entries,
                    f"{name} is not named in .gitignore.  It relies on a "
                    ".gitignore inside the directory itself, which is not a "
                    "rule this repository controls.",
                )


if __name__ == "__main__":
    unittest.main()
