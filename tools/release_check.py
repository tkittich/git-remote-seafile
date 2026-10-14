#!/usr/bin/env python3
"""Answer, before the tag exists, the questions the release job answers after it.

A malformed release used to announce itself as a red tag, which is public.
This runs the same checks locally:

* ``__version__`` is the version being released (and the tag does not exist yet);
* ``.github/release-notes/vX.Y.Z.md`` has the shape the release script and
  ``tests/test_release_workflow.py`` require, and its compare link starts at the
  previous release tag, which exists;
* ``CHANGELOG.md`` has the ``## [X.Y.Z]`` section and its reference link;
* the working tree is clean, and the commit is on a remote branch (a warning
  only: CI must be green on the pushed commit before the tag, and this cannot
  see CI).

Usage::

    python tools/release_check.py            # the version in __init__.py
    python tools/release_check.py v0.9.3     # must equal __version__

Exit status: 0 when every check passes (warnings allowed), 1 otherwise, 2 for a
usage error.  Standard library only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_REPO = "https://github.com/tkittich/git-remote-seafile"
_COMPARE = re.compile(
    r"^\*\*Full Changelog\*\*: " + re.escape(_REPO) + r"/compare/(v\d+\.\d+\.\d+)\.\.\.(v\d+\.\d+\.\d+)$"
)

Result = tuple[str, str, str]  # (status "ok"|"FAIL"|"warn", check, detail)


def _git(root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode, proc.stdout


def _key(tag: str) -> tuple[int, int, int]:
    m = _TAG.match(tag)
    assert m is not None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def package_version(root: Path) -> str | None:
    text = (root / "git_remote_seafile" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else None


def previous_tag(root: Path, tag: str) -> str | None:
    """The highest release tag below *tag*, numerically (v0.10.0 > v0.9.0)."""
    _, out = _git(root, "tag", "--list", "v*")
    older = [t for t in out.split() if _TAG.match(t) and _key(t) < _key(tag)]
    return max(older, key=_key) if older else None


def check(root: Path, tag: str | None = None) -> list[Result]:
    results: list[Result] = []

    def ok(name: str, detail: str = "") -> None:
        results.append(("ok", name, detail))

    def fail(name: str, detail: str) -> None:
        results.append(("FAIL", name, detail))

    def warn(name: str, detail: str) -> None:
        results.append(("warn", name, detail))

    version = package_version(root)
    if version is None:
        fail("version", "no __version__ in git_remote_seafile/__init__.py")
        return results
    expected = f"v{version}"
    if tag is None:
        tag = expected
    if not _TAG.match(tag):
        fail("tag", f"{tag!r} is not vX.Y.Z")
        return results
    if tag != expected:
        fail("version", f"tag {tag} but __version__ is {version}")
    else:
        ok("version", f"__version__ is {version}")

    rc, out = _git(root, "tag", "--list", tag)
    if out.strip():
        fail("tag", f"{tag} already exists; a published tag is never moved")
    else:
        ok("tag", f"{tag} is free")

    base = previous_tag(root, tag)
    if base is None:
        fail("previous tag", "no earlier vX.Y.Z tag exists locally (fetch tags?)")
    else:
        ok("previous tag", base)

    notes = root / ".github" / "release-notes" / f"{tag}.md"
    if not notes.is_file():
        fail("notes", f"{notes.relative_to(root)} is missing")
    else:
        lines = notes.read_text(encoding="utf-8").splitlines()
        body = [ln for ln in lines if ln.strip()]
        problems: list[str] = []
        if not lines or not lines[0].startswith(f"# {tag} - ") or len(lines[0]) <= len(f"# {tag} - "):
            problems.append(f"first line must be '# {tag} - <summary>'")
        if len(body) < 3 or body[1] != f"## What's Changed in {tag}":
            problems.append(f"body must start at '## What's Changed in {tag}'")
        link = _COMPARE.match(body[-1]) if body else None
        if len(body) < 2 or body[-2] != "---":
            problems.append("must end with '---' and then the Full Changelog link")
        if link is None:
            problems.append("last line is not the Full Changelog compare link")
        else:
            if link.group(2) != tag:
                problems.append(f"compare link ends at {link.group(2)}, not {tag}")
            if base is not None and link.group(1) != base:
                problems.append(f"compare link starts at {link.group(1)}, previous tag is {base}")
        if problems:
            fail("notes", "; ".join(problems))
        else:
            ok("notes", f"{notes.name} (title: {lines[0][2:].strip()})")

    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8") if changelog.is_file() else ""
    if not re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$", text, re.M):
        fail("changelog", f"no '## [{version}] - YYYY-MM-DD' section")
    else:
        ok("changelog", f"section [{version}]")
    want = f"[{version}]: {_REPO}/compare/{base}...{tag}" if base else None
    link_re = re.compile(rf"^\[{re.escape(version)}\]: (\S+)\s*$", re.M)
    m = link_re.search(text)
    if m is None:
        fail("changelog link", f"no reference link '[{version}]: ...'")
    elif want is not None and m.group(1) != want.split(": ", 1)[1]:
        fail("changelog link", f"is {m.group(1)}; expected {want.split(': ', 1)[1]}")
    else:
        ok("changelog link", m.group(1))
    if re.search(r"^## \[Unreleased\]\s*\n+(?=- |### )", text, re.M):
        warn("changelog", "[Unreleased] still has entries; did they belong in this release?")

    rc, status = _git(root, "status", "--porcelain")
    if rc != 0:
        fail("tree", f"git status failed: {status.strip()}")
    elif status.strip():
        fail("tree", "not clean:\n    " + "\n    ".join(status.strip().splitlines()[:10]))
    else:
        ok("tree", "clean")

    rc, out = _git(root, "branch", "-r", "--contains", "HEAD")
    if rc == 0 and out.strip():
        ok("pushed", "HEAD is on " + out.split()[0])
    else:
        warn("pushed", "HEAD is on no remote branch: push, and wait for CI green, before tagging")
    return results


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1 or (args and args[0] in ("-h", "--help")):
        print(__doc__)
        return 2
    root = Path(__file__).resolve().parent.parent
    results = check(root, args[0] if args else None)
    for status, name, detail in results:
        print(f"[{status:>4}] {name}: {detail}")
    failed = [r for r in results if r[0] == "FAIL"]
    if failed:
        print(f"\n{len(failed)} check(s) failed; nothing was tagged.")
        return 1
    version = package_version(root)
    notes = root / ".github" / "release-notes" / f"v{version}.md"
    title = notes.read_text(encoding="utf-8").splitlines()[0][2:].strip()
    print(f'\nReady. Tag with:\n  git tag -a v{version} -m "{title}"\n'
          f"  git push origin v{version}\n  git push origin\n  git push mirror")
    return 0


if __name__ == "__main__":
    sys.exit(main())
