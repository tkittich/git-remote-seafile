"""tools/release_check.py answers, before the tag exists, what the release job asks after it."""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import json
import tempfile
import unittest
import unittest.mock

try:
    from _helpers import remove_tree  # noqa: E402
except ImportError:  # run as ``python -m unittest tests.<module>``
    from tests._helpers import remove_tree  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_check", _ROOT / "tools" / "release_check.py")
assert _spec is not None and _spec.loader is not None
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

REPO = "https://github.com/tkittich/git-remote-seafile"


def _git(cwd: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@x", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


class _GitHub:
    """A stand-in for ``gh``: *runs* per workflow, *jobs* per run id."""

    def __init__(self, runs=None, jobs=None, rc=0):
        self.runs = runs if runs is not None else {
            "ci.yml": [{"databaseId": 1, "status": "completed", "conclusion": "success"}],
            "realserver.yml": [{"databaseId": 2, "status": "completed", "conclusion": "success"}],
        }
        self.jobs = jobs if jobs is not None else {
            2: [{"name": "realserver (ce)", "conclusion": "success"},
                {"name": "realserver (pro)", "conclusion": "success"}],
        }
        self.rc = rc

    def __call__(self, *args):
        if self.rc:
            return self.rc, "gh: not logged in"
        if args[:2] == ("run", "list"):
            return 0, json.dumps(self.runs.get(args[args.index("--workflow") + 1], []))
        return 0, json.dumps({"jobs": self.jobs.get(int(args[2]), [])})


class WorkflowResultTests(unittest.TestCase):
    """ROADMAP B8: a tag needs CI and both CE/Pro legs green at HEAD."""

    def statuses(self, gh):
        return {name: status for status, name, _ in rc.workflow_results("a" * 40, gh)}

    def test_every_required_run_green_passes(self):
        self.assertEqual(self.statuses(_GitHub()), {"ci.yml": "ok", "realserver.yml": "ok"})

    def test_a_failed_leg_a_missing_run_or_a_running_one_fails(self):
        one_leg = _GitHub(jobs={2: [{"name": "realserver (ce)", "conclusion": "success"},
                                    {"name": "realserver (pro)", "conclusion": "failure"}]})
        self.assertEqual(self.statuses(one_leg)["realserver.yml"], "FAIL")
        none = _GitHub(runs={"ci.yml": [{"databaseId": 1, "status": "completed",
                                         "conclusion": "success"}], "realserver.yml": []})
        self.assertEqual(self.statuses(none)["realserver.yml"], "FAIL")
        running = _GitHub(runs={"ci.yml": [{"databaseId": 1, "status": "in_progress",
                                            "conclusion": ""}], "realserver.yml": []})
        self.assertEqual(self.statuses(running)["ci.yml"], "FAIL")
        red = _GitHub(runs={"ci.yml": [{"databaseId": 1, "status": "completed",
                                        "conclusion": "failure"}], "realserver.yml": []})
        self.assertEqual(self.statuses(red)["ci.yml"], "FAIL")

    def test_without_gh_it_fails_unless_offline(self):
        # Review 06cc623 (opus G5.2, gemini G5.3): "Ready" after skipping both CI legs
        # contradicted the docs' "requires".
        self.assertEqual(set(self.statuses(_GitHub(rc=127)).values()), {"FAIL"})
        offline = {name: status for status, name, _ in
                   rc.workflow_results("a" * 40, _GitHub(rc=127), offline=True)}
        self.assertEqual(set(offline.values()), {"warn"})

    def test_gh_is_told_the_repository(self):
        asked = []

        def gh(*args):
            asked.append(args)
            return _GitHub()(*args)
        rc.workflow_results("a" * 40, gh)
        self.assertTrue(asked)
        self.assertTrue(all(a[a.index("--repo") + 1] == "tkittich/git-remote-seafile"
                            for a in asked), asked)


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="grs-relcheck-"))
        self.addCleanup(remove_tree, self.root)
        _git(self.root, "init", "-q", "-b", "main", ".")
        _write(self.root / "git_remote_seafile" / "__init__.py", '__version__ = "0.2.0"\n')
        _write(
            self.root / "CHANGELOG.md",
            "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-01-02\n\nx\n\n"
            f"[0.2.0]: {REPO}/compare/v0.1.0...v0.2.0\n",
        )
        _write(
            self.root / ".github" / "release-notes" / "v0.2.0.md",
            "# v0.2.0 - A Title\n\n## What's Changed in v0.2.0\n\nstuff\n\n---\n\n"
            f"**Full Changelog**: {REPO}/compare/v0.1.0...v0.2.0\n",
        )
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "one")
        _git(self.root, "tag", "v0.1.0")
        _git(self.root, "tag", "v0.9.0")  # numeric order: v0.9.0 is above v0.2.0
        # The CI checks ask GitHub through gh; these tests are about the files, so
        # the answer is "every required run is green" unless a test says otherwise.
        patcher = unittest.mock.patch.object(rc, "_gh", _GitHub())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_recipe_names_only_remotes_the_checkout_has(self):
        """Review 4f1cdd4 (glm-5.3 G5.2): `git push mirror` was printed for a checkout
        with no `mirror` remote, a command that fails."""
        recipe = rc.push_recipe(self.root, "0.2.0", "v0.2.0 - A Title")
        self.assertIn("git push origin v0.2.0", recipe)
        self.assertNotIn("git push mirror", recipe)
        _git(self.root, "remote", "add", "mirror", "https://example.invalid/m.git")
        self.assertIn("git push mirror", rc.push_recipe(self.root, "0.2.0", "v0.2.0 - A Title"))

    def failed(self, results) -> dict[str, str]:
        return {name: detail for status, name, detail in results if status == "FAIL"}

    def test_a_consistent_release_passes(self):
        self.assertEqual(self.failed(rc.check(self.root)), {})

    def test_the_previous_tag_is_the_highest_below_not_the_latest(self):
        self.assertEqual(rc.previous_tag(self.root, "v0.2.0"), "v0.1.0")
        self.assertEqual(rc.previous_tag(self.root, "v0.10.0"), "v0.9.0")

    def test_a_tag_that_differs_from_the_version_fails(self):
        self.assertIn("version", self.failed(rc.check(self.root, "v0.2.1")))

    def test_an_existing_tag_fails(self):
        _git(self.root, "tag", "v0.2.0")
        self.assertIn("tag", self.failed(rc.check(self.root)))

    def test_a_compare_link_from_the_wrong_base_fails(self):
        notes = self.root / ".github" / "release-notes" / "v0.2.0.md"
        notes.write_bytes(notes.read_bytes().replace(b"v0.1.0...", b"v0.0.9..."))
        _git(self.root, "commit", "-qam", "x")
        self.assertIn("notes", self.failed(rc.check(self.root)))

    def test_a_missing_changelog_link_fails(self):
        cl = self.root / "CHANGELOG.md"
        cl.write_bytes(cl.read_bytes().replace(b"[0.2.0]: ", b"[0.2.0] "))
        _git(self.root, "commit", "-qam", "x")
        self.assertIn("changelog link", self.failed(rc.check(self.root)))

    def test_a_missing_changelog_section_fails(self):
        cl = self.root / "CHANGELOG.md"
        cl.write_bytes(cl.read_bytes().replace(b"## [0.2.0] - 2026-01-02", b"## [0.2.0]"))
        _git(self.root, "commit", "-qam", "x")
        self.assertIn("changelog", self.failed(rc.check(self.root)))

    def test_a_dirty_tree_fails(self):
        _write(self.root / "scratch.tmp", "x")
        self.assertIn("tree", self.failed(rc.check(self.root)))

    def test_a_commit_on_another_remote_branch_only_fails(self):
        # gemini G5.3: a commit only on origin/ci/<topic> read as "pushed".
        bare = self.root.parent / (self.root.name + "-origin.git")
        _git(self.root.parent, "init", "-q", "--bare", str(bare))
        self.addCleanup(remove_tree, bare)
        _git(self.root, "remote", "add", "origin", str(bare))
        _git(self.root, "push", "-q", "origin", "HEAD:refs/heads/ci/topic")
        _git(self.root, "fetch", "-q", "origin")
        self.assertIn("not origin/main", self.failed(rc.check(self.root)).get("pushed", ""))
        _git(self.root, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(self.root, "fetch", "-q", "origin")
        self.assertNotIn("pushed", self.failed(rc.check(self.root)))

    def test_missing_notes_fail(self):
        (self.root / ".github" / "release-notes" / "v0.2.0.md").unlink()
        _git(self.root, "commit", "-qam", "x")
        self.assertIn("notes", self.failed(rc.check(self.root)))


if __name__ == "__main__":
    unittest.main()
