"""Inject each kind of doc drift and confirm the guard actually catches it.

A guard that passes vacuously is worse than no guard: it certifies a consistency
nobody checked.  This runs each scenario by substituting drifted text for the
real file contents (so nothing on disk is touched) and asserts the specific test
FAILS.  Run from the repository root.
"""

import sys
import unittest
from pathlib import Path

# Run from anywhere: the guard under test is imported as `tests.<module>`, which
# needs the repository root on sys.path (the test package has no __init__.py and
# is found as a namespace package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests.test_docs_consistency as dc

_REAL_READ = dc._read
_REAL_SOURCE = dc._source_text

SCENARIOS = []


def scenario(name, test_name, overrides, source_override=None):
    SCENARIOS.append((name, test_name, overrides, source_override))


def everywhere(needle, replacement):
    """Override every doc that actually contains *needle*.

    Scoping matters: a key or variable usually appears in more than one file, and
    substituting it in only one leaves the guard a legitimate reason to pass --
    which reads as "the guard does not work" when in fact the drift was never
    injected.
    """
    return {
        name: [(needle, replacement)]
        for name in dc._DOC_NAMES
        if needle in _REAL_READ(name)
    }


scenario(
    "README badge still advertises 3.8",
    "TestPythonFloor.test_readme_badge_matches",
    {"README.md": [("python-3.9+-blue.svg", "python-3.8+-blue.svg")]},
)
scenario(
    "CI matrix drops the floor version",
    "TestPythonFloor.test_ci_tests_the_floor",
    {".github/workflows/ci.yml": [('["3.9", "3.10"', '["3.10", "3.11"')]},
)
scenario(
    "a config key the code reads is undocumented",
    "TestConfigKeys.test_every_key_the_code_reads_is_documented",
    everywhere("`seafile.forcefilehost`", "`forcefilehost`"),
)
scenario(
    "the documented default no longer matches the code",
    "TestConfigKeys.test_documented_defaults_match_the_code",
    {"USER_GUIDE.md": [("| `20` | Packfile count", "| `25` | Packfile count")]},
)
scenario(
    "an env var is dropped from the docs",
    "TestEnvironmentVariables.test_documented_and_read_agree",
    everywhere("SEAFILE_TOKEN", "SEAFILE_TOK"),
)
scenario(
    "the docs name a subcommand that does not exist",
    "TestSubcommands.test_every_documented_subcommand_exists",
    {"USER_GUIDE.md": [("## 7. URL Syntax", "Run `git-remote-seafile frobnicate`.\n\n## 7. URL Syntax")]},
)
scenario(
    "README links a document that was renamed away",
    "TestDocumentationIndex.test_linked_documents_exist",
    {"README.md": [("](CONTRIBUTING.md)", "](GONE.md)")]},
)


def run_one(test_name):
    suite = unittest.TestLoader().loadTestsFromName(f"tests.test_docs_consistency.{test_name}")
    result = unittest.TestResult()
    suite.run(result)
    return result


def main():
    print("baseline (real files):")
    baseline_ok = True
    for name, test_name, _, _ in SCENARIOS:
        if not run_one(test_name).wasSuccessful():
            print(f"  !! {test_name} already fails on real files")
            baseline_ok = False
    print("  all guards pass on real files" if baseline_ok else "  BASELINE BROKEN")
    print()

    caught = 0
    for name, test_name, overrides, _ in SCENARIOS:
        hits = set()
        wanted = {(doc, old) for doc, subs in overrides.items() for old, _ in subs}

        def drifted_read(doc_name, _o=overrides, _hits=hits):
            text = _REAL_READ(doc_name)
            for old, new in _o.get(doc_name, []):
                if old in text:
                    _hits.add((doc_name, old))
                    text = text.replace(old, new)
            return text

        dc._read = drifted_read
        try:
            result = run_one(test_name)
        finally:
            dc._read = _REAL_READ

        missed_targets = wanted - hits
        if missed_targets:
            # The drift was never injected, so a failure proves nothing.
            print(f"  SCENARIO BUG  {name}")
            for doc, old in sorted(missed_targets):
                print(f"                {doc} does not contain {old!r}")
            continue

        if result.wasSuccessful():
            print(f"  NOT CAUGHT  {name}\n              ({test_name} still passed)")
        else:
            caught += 1
            text = (result.failures + result.errors)[0][1]
            lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
            detail = next((ln for ln in reversed(lines) if "Error" in ln), lines[-1])
            print(f"  caught      {name}\n              -> {detail[:110]}")

    print()
    print(f"{caught}/{len(SCENARIOS)} injected drifts were caught")
    return 0 if caught == len(SCENARIOS) and baseline_ok else 1


if __name__ == "__main__":
    sys.exit(main())
