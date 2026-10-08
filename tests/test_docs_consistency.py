"""Doc claims that can be executed, so they cannot quietly drift.

Every inconsistency this project has shipped was a *claim about the code* that
nothing checked:

* a README badge advertising Python 3.8 for a package that needs 3.9;
* a USER_GUIDE section describing an `autogc` feature that had never worked;
* a test count frozen at "49 tests" while the suite grew to 192;
* a DESIGN entry naming a delete endpoint the code had moved away from.

None of those were typos.  Each was a statement about the code that was true
when it was written, after which the code moved and the prose did not.  Prose
cannot be verified in general -- but a specific subset can: the claims that name
something *mechanical*.  A config key, an environment variable, a subcommand, a
default value, a version floor.  Those are cross-referenced here, in both
directions, so a key that exists but is undocumented fails as loudly as a
documented key that does not exist.

What is deliberately **not** here is whether the prose is *true*.  A test can
check that `seafile.autogc` is read by the code, and that its documented default
matches the code's; it cannot check that the paragraph explaining auto-GC is an
honest account of the behaviour.  That part stays a review job, and pretending
otherwise would just relocate the problem.

Extraction is by regex over the source, not by import.  That is a deliberate
trade: it keeps this file independent of the code's structure, but it means a
key assembled at runtime (``get_git_config_bool(f"seafile.{name}")``) would go
unseen.  Nothing does that today; if something starts to, these tests must grow
an AST pass rather than silently passing.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from git_remote_seafile import MINIMUM_PYTHON

_ROOT = Path(__file__).resolve().parent.parent
_SOURCE = _ROOT / "git_remote_seafile"

# The shipped documentation.  PROPOSALS.md is excluded on purpose: it is
# gitignored, so a claim in it never reaches a user and correcting one there
# would not ship either.
_DOC_NAMES = ("README.md", "USER_GUIDE.md", "DESIGN.md", "CHANGELOG.md", "CONTRIBUTING.md", "ROADMAP.md")

# Config keys are written in backticks throughout the docs.  Requiring the
# backticks is what keeps `https://seafile.example.com` and `.git-seafile.json`
# out of the results.
_DOC_CONFIG_KEY = re.compile(r"`(seafile\.[a-z][a-z0-9]*)`")

# A *read* of a config key.  Only literal calls are seen (see the module
# docstring).
_CODE_CONFIG_READ = re.compile(r'get_git_config_[a-z]+\("([^"]+)"\s*,\s*([^)]+)\)')

# An environment variable the code actually consults -- not merely one it
# mentions in an error message, which several do.
_CODE_ENV_READ = re.compile(
    r"os\.environ(?:\.get\(|\[)\s*[\"'](SEAFILE_[A-Z0-9_]+)[\"']"
)

_DOC_ENV = re.compile(r"\b(SEAFILE_[A-Z0-9_]+)\b")

_CODE_SUBCOMMAND_EQ = re.compile(r'args\[0\]\s*==\s*"([^"]+)"')
_CODE_SUBCOMMAND_IN = re.compile(r"args\[0\]\s+in\s+\(([^)]+)\)")
# The separator must be spaces or tabs, not `\s`: several docs end a line with
# the bare program name and continue with something unrelated on the next line
# (`git-remote-seafile` / `pip install -e .` in the install instructions, and a
# mermaid `participant` in DESIGN), which a `\s+` would read as a subcommand.
_DOC_SUBCOMMAND = re.compile(r"git-remote-seafile[ \t]+([a-z][a-z-]*)")

# Subcommands that exist for the program's own sake rather than to do something
# to a repository.  They are documented as flags (`--version`), so the
# "implemented must be documented" direction skips them.
_PSEUDO_SUBCOMMANDS = frozenset({"help", "version"})

# `chck-auth` is documented *on purpose*: the user guide uses it as the worked
# example of a typo, to show what the unknown-command error looks like.  It is
# the one documented token that must not be a real subcommand.
_DOCUMENTED_NON_COMMANDS = frozenset({"chck-auth"})

# USER_GUIDE's behavioural-configuration table:
#   | `seafile.autogc` | — | `false` | Compact the remote automatically ... |
_DOC_CONFIG_TABLE_ROW = re.compile(
    r"^\|\s*`(seafile\.[a-z][a-z0-9]*)`\s*\|[^|]*\|\s*`([^`]*)`\s*\|",
    re.MULTILINE,
)


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def _all_docs() -> dict[str, str]:
    return {name: _read(name) for name in _DOC_NAMES}


def _source_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_SOURCE.glob("*.py"))
    )


def _config_keys_read_by_code() -> dict[str, str]:
    """Map every config key the code reads to the default it passes."""
    found: dict[str, str] = {}
    for key, default in _CODE_CONFIG_READ.findall(_source_text()):
        found[key] = _normalise_default(default)
    return found


def _normalise_default(raw: str) -> str:
    """Reduce a Python default literal to the form the docs use."""
    raw = raw.strip()
    if raw == "True":
        return "true"
    if raw == "False":
        return "false"
    if raw == "None":
        return "none"
    return raw.strip("\"'").strip()


def _subcommands_implemented() -> set[str]:
    source = _source_text()
    names = set(_CODE_SUBCOMMAND_EQ.findall(source))
    for group in _CODE_SUBCOMMAND_IN.findall(source):
        for token in group.split(","):
            token = token.strip().strip("\"'")
            if token and not token.startswith("-"):
                names.add(token)
    return names


def _subcommands_documented() -> set[str]:
    names: set[str] = set()
    for text in _all_docs().values():
        names |= set(_DOC_SUBCOMMAND.findall(text))
    return names - _DOCUMENTED_NON_COMMANDS


class TestConfigKeys(unittest.TestCase):
    """A config key has to exist in the code and in the docs, or in neither."""

    def test_every_documented_key_is_read_by_the_code(self):
        documented = set()
        for text in _all_docs().values():
            documented |= set(_DOC_CONFIG_KEY.findall(text))

        read = set(_config_keys_read_by_code())
        self.assertEqual(
            sorted(documented - read),
            [],
            "documented but never read -- the guide promises a setting that "
            "does nothing",
        )

    def test_every_key_the_code_reads_is_documented(self):
        documented = set()
        for text in _all_docs().values():
            documented |= set(_DOC_CONFIG_KEY.findall(text))

        self.assertEqual(
            sorted(set(_config_keys_read_by_code()) - documented),
            [],
            "read by the code but absent from the docs -- a setting users can "
            "only discover by reading the source",
        )

    def test_documented_defaults_match_the_code(self):
        """The autogc drift was a wrong documented default, not a missing key."""
        code_defaults = _config_keys_read_by_code()
        rows = dict(_DOC_CONFIG_TABLE_ROW.findall(_read("USER_GUIDE.md")))

        self.assertTrue(rows, "no rows parsed from the config table in USER_GUIDE")
        for key, documented_default in sorted(rows.items()):
            with self.subTest(key=key):
                self.assertIn(key, code_defaults, "table documents an unread key")
                self.assertEqual(
                    documented_default.strip(),
                    code_defaults[key],
                    f"{key}: the guide says the default is "
                    f"{documented_default!r}, the code uses {code_defaults[key]!r}",
                )


class TestEnvironmentVariables(unittest.TestCase):
    def test_documented_and_read_agree(self):
        documented = set()
        for text in _all_docs().values():
            documented |= set(_DOC_ENV.findall(text))

        read = set(_CODE_ENV_READ.findall(_source_text()))

        with self.subTest(direction="documented but not read"):
            self.assertEqual(sorted(documented - read), [])
        with self.subTest(direction="read but not documented"):
            self.assertEqual(sorted(read - documented), [])


class TestSubcommands(unittest.TestCase):
    def test_every_documented_subcommand_exists(self):
        """The direction that breaks users: the guide names a command that 404s."""
        implemented = _subcommands_implemented()
        missing = sorted(_subcommands_documented() - implemented)
        self.assertEqual(
            missing,
            [],
            "the docs tell users to run a subcommand the CLI does not handle",
        )

    def test_every_real_subcommand_is_documented(self):
        mentioned = " ".join(_all_docs().values())
        undocumented = sorted(
            name
            for name in _subcommands_implemented() - _PSEUDO_SUBCOMMANDS
            if not re.search(rf"\b{re.escape(name)}\b", mentioned)
        )
        self.assertEqual(undocumented, [], "shipped subcommand that no doc mentions")


class TestPythonFloor(unittest.TestCase):
    """One constant, six places that restate it.

    The 3.8 badge drifted because the floor lived in prose in four files and in
    a constant in a fifth, and only the constant changed.  Each place is read
    back and compared to `MINIMUM_PYTHON` here, so a future bump is a failing
    test rather than a wrong promise.
    """

    def setUp(self):
        self.floor = f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}"

    def test_requires_python_matches(self):
        match = re.search(r'(?m)^requires-python\s*=\s*"[>=!~]*([0-9]+\.[0-9]+)"',
                          _read("pyproject.toml"))
        self.assertIsNotNone(match, "no requires-python in pyproject.toml")
        self.assertEqual(match.group(1), self.floor)

    def test_readme_badge_matches(self):
        match = re.search(r"badge/python-([0-9]+\.[0-9]+)\+", _read("README.md"))
        self.assertIsNotNone(match, "no python badge in README.md")
        self.assertEqual(match.group(1), self.floor)

    def test_user_guide_matches(self):
        match = re.search(r"Python ([0-9]+\.[0-9]+)\+", _read("USER_GUIDE.md"))
        self.assertIsNotNone(match, "no Python floor stated in USER_GUIDE.md")
        self.assertEqual(match.group(1), self.floor)

    def test_lowest_classifier_matches(self):
        classifiers = re.findall(
            r"Programming Language :: Python :: ([0-9]+\.[0-9]+)",
            _read("pyproject.toml"),
        )
        self.assertTrue(classifiers, "no version classifiers in pyproject.toml")
        lowest = min(classifiers, key=lambda v: tuple(int(p) for p in v.split(".")))
        self.assertEqual(lowest, self.floor)

    def test_ci_tests_the_floor(self):
        """CI must actually run the oldest supported interpreter."""
        match = re.search(r'python-version:\s*\[([^\]]+)\]', _read(".github/workflows/ci.yml"))
        self.assertIsNotNone(match, "no python-version matrix in ci.yml")
        versions = [v.strip().strip("\"'") for v in match.group(1).split(",")]
        lowest = min(versions, key=lambda v: tuple(int(p) for p in v.split(".")))
        self.assertEqual(lowest, self.floor)


class TestBuildBackendFloor(unittest.TestCase):
    """The declared build floor has to cover the pyproject features in use.

    ``license = "Apache-2.0"`` is a PEP 639 SPDX expression, which setuptools
    learned to read in 77.  The declared floor said 61 -- the version that
    wanted the older ``license = {text = "..."}`` table -- and with setuptools
    76.1.0, inside the declared range, the build does not merely warn:

        ValueError: invalid pyproject.toml config: `project.license`.
        configuration error: `project.license` must be valid exactly by one
        definition (2 matches found)

    Nothing caught it because an isolated build installs the *newest* setuptools
    satisfying the floor, and the newest one works.  Only a build that pins its
    own -- ``--no-isolation``, or a distribution building from the sdist -- sees
    it.  Verified by building both ways: 76.1.0 fails, 77.0.1 succeeds and
    emits ``License-Expression: Apache-2.0`` at metadata version 2.4.
    """

    # Where `license` as an SPDX string became readable.  Below this it has to
    # be a table, so the two tests below only hold for the SPDX form.
    SPDX_LICENSE_FLOOR = 77

    _SPDX_LICENSE = re.compile(r'(?m)^license\s*=\s*"[^"]+"\s*$')
    _SETUPTOOLS_FLOOR = re.compile(r'"setuptools>=([0-9]+)')

    def test_the_license_is_declared_as_an_spdx_expression(self):
        self.assertRegex(
            _read("pyproject.toml"), self._SPDX_LICENSE,
            "pyproject no longer declares `license` as a PEP 639 SPDX string. "
            "If that was deliberate, this class goes with it -- but the build "
            "floor may then be lowered too.",
        )

    def test_the_build_floor_can_read_that_license(self):
        match = self._SETUPTOOLS_FLOOR.search(_read("pyproject.toml"))
        self.assertIsNotNone(match, 'no "setuptools>=N" in build-system requires')
        floor = int(match.group(1))
        self.assertGreaterEqual(
            floor, self.SPDX_LICENSE_FLOOR,
            f"build-system requires setuptools>={floor}, but a PEP 639 SPDX "
            f"`license` string needs >={self.SPDX_LICENSE_FLOOR}. With 76.1.0 "
            "the build stops at 'invalid pyproject.toml config: "
            "`project.license`'",
        )


class TestSdistManifest(unittest.TestCase):
    """The docs have to be *named* in MANIFEST.in to reach the sdist.

    ``README.md`` and ``LICENSE`` come along on their own -- pyproject.toml
    names them in ``readme`` and ``license`` -- but nothing else does.  A built
    sdist contained the package, the tests (only because a stale egg-info
    SOURCES.txt happened to list them) and no user guide, no CHANGELOG, no
    DESIGN and no launcher.  A source distribution that omits its own
    documentation is not a source distribution.

    This reads the manifest rather than building an sdist on purpose: a build
    needs setuptools, CI installs the package with ``pip install -e .`` and
    nothing else, and a test that skips where it matters is worth less than one
    that always runs.  It checks coverage by the manifest's patterns, not that
    a particular tarball was correct -- the tarball was checked by hand.
    """

    def _manifest_lines(self) -> list[list[str]]:
        manifest = _ROOT / "MANIFEST.in"
        self.assertTrue(manifest.is_file(), "MANIFEST.in is missing")
        lines = []
        for raw in manifest.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(stripped.split())
        return lines

    def _is_covered(self, path: str) -> bool:
        """Approximate MANIFEST.in semantics for include/graft/recursive-include."""
        for parts in self._manifest_lines():
            directive = parts[0]
            if directive == "include":
                if any(fnmatch.fnmatch(path, pattern) for pattern in parts[1:]):
                    return True
            elif directive in ("graft", "recursive-include") and len(parts) >= 2:
                if path.startswith(parts[1].rstrip("/") + "/"):
                    return True
        return False

    def test_every_shipped_document_is_in_the_manifest(self):
        for name in _DOC_NAMES:
            with self.subTest(document=name):
                self.assertTrue(
                    self._is_covered(name),
                    f"{name} is not covered by MANIFEST.in, so it will not be "
                    "in the source distribution",
                )

    def test_the_source_checkout_launcher_is_in_the_manifest(self):
        self.assertTrue(
            self._is_covered("bin/git-remote-seafile"),
            "bin/ is not covered by MANIFEST.in",
        )


class TestManifestTargetsAreReal(unittest.TestCase):
    """MANIFEST.in must not name files that will not be there.

    A release is built from a clean checkout, so a path that is gitignored --
    or that only exists in one developer's working tree -- is silently absent
    from the sdist.  ``MANIFEST.in`` naming a missing file is a warning during
    the build, not an error, so nothing goes red.

    That is exactly how ``tools/seafile_doctor.py`` sat ignored while the tools
    beside it shipped, and how ``tests/`` reached the sdist by accident.  Both
    are the same mistake seen from opposite sides: the manifest and the
    repository disagreeing about what exists.
    """

    def _included_paths(self) -> list[str]:
        paths = []
        for raw in _read("MANIFEST.in").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if parts[0] == "include":
                paths.extend(parts[1:])
            elif parts[0] in ("graft", "recursive-include") and len(parts) > 1:
                paths.append(parts[1].rstrip("/"))
        return paths

    def test_every_named_path_exists(self):
        for rel in self._included_paths():
            with self.subTest(path=rel):
                self.assertTrue(
                    (_ROOT / rel).exists(),
                    f"MANIFEST.in names {rel}, which does not exist -- the "
                    "sdist will silently omit it",
                )

    def test_every_named_file_is_tracked_by_git(self):
        """A gitignored file is absent from a clean checkout, so it cannot ship.

        Skipped only where the question does not apply: an sdist has no git and
        no manifest to disagree with.  CI has both.
        """
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        if not (_ROOT / ".git").exists():
            self.skipTest("not a git checkout (built from an sdist)")

        listed = subprocess.run(
            ["git", "ls-files"], cwd=str(_ROOT),
            capture_output=True, text=True,
        ).stdout.split()
        tracked = set(listed)
        for rel in self._included_paths():
            with self.subTest(path=rel):
                if (_ROOT / rel).is_dir():
                    continue  # a grafted directory; its contents are checked above
                self.assertIn(
                    rel, tracked,
                    f"MANIFEST.in ships {rel} but git does not track it, so a "
                    "clean checkout will not have it",
                )


class TestLintIsWired(unittest.TestCase):
    """A lint job only helps if it exists, runs the configured rules, and is pinned.

    Three ways it can quietly stop protecting anything, each worth a check:

    * the job is deleted, and the unused imports and shadowed test methods come
      back with nothing left to notice them;
    * the rules move into the workflow, so a local ``ruff check .`` and CI stop
      agreeing about what is being checked;
    * the version is unpinned, so a ruff release reddens a green branch with a
      failure that says nothing about what changed in this repository.
    """

    _CI = ".github/workflows/ci.yml"

    def test_ci_has_a_lint_job(self):
        self.assertRegex(
            _read(self._CI), r"(?m)^  lint:",
            "ci.yml has no top-level `lint` job",
        )

    def test_the_job_actually_runs_ruff(self):
        self.assertIn(
            "ruff check", _read(self._CI),
            "the lint job does not invoke ruff",
        )

    def test_the_ruff_version_is_pinned(self):
        self.assertIsNotNone(
            re.search(r"pip install ruff==\S+", _read(self._CI)),
            "ruff is installed unpinned, so a new ruff release can fail CI "
            "without anything in this repository having changed",
        )

    def test_the_rules_live_in_pyproject_so_local_and_ci_agree(self):
        pyproject = _read("pyproject.toml")
        self.assertIn("[tool.ruff", pyproject, "no [tool.ruff] section")
        self.assertRegex(
            pyproject, r"(?m)^select\s*=\s*\[",
            "[tool.ruff.lint] declares no `select`, so CI would run ruff's "
            "defaults rather than the intended rule set",
        )


class TestDocumentationIndex(unittest.TestCase):
    def test_linked_documents_exist(self):
        """A rename that misses the index leaves a dead link, not an error."""
        linked = set(re.findall(r"\]\(([A-Za-z_][A-Za-z0-9_]*\.md)\)", _read("README.md")))
        self.assertTrue(linked, "no local document links found in README.md")
        for name in sorted(linked):
            with self.subTest(document=name):
                self.assertTrue(
                    (_ROOT / name).is_file(),
                    f"README.md links to {name}, which does not exist",
                )


if __name__ == "__main__":
    unittest.main()
