<!--
Thanks for the pull request. The checklist below is the same one the maintainer
runs before tagging a release, so a change that satisfies it can be released
without a second pass.
-->

## What this changes

<!-- One paragraph. If it fixes something, say what was wrong before. -->

## Why

<!-- The reasoning, not a restatement of the diff. Link the issue if there is one. -->

---

## Checklist

- [ ] **The suite is green**: `python tools/run_tests_parallel.py`
- [ ] **Ruff is clean**: `ruff check .` (CI pins the version; see `CONTRIBUTING.md`)
- [ ] **The doc guards are green**: `python tools/verify_docs_guards.py`
- [ ] **New behaviour has a test.** Every silent-success bug this project has
      shipped is now pinned by one; a fix without a test is a fix that can come
      back.
- [ ] **No new dependency**, or the reason for one is argued in the description.
      The dependency floor in `pyproject.toml` is a security floor, not a
      convenience one.
- [ ] **Documentation is updated** for anything user-visible — `README.md`,
      `USER_GUIDE.md`, and `CHANGELOG.md` under `## [Unreleased]`. The doc guards
      check config keys, environment variables and subcommands mechanically, but
      they cannot see prose drift, so a claim that is now false is on you.
- [ ] **Line endings are unchanged.** The repository's index is LF; a handful of
      tracked files are deliberately CRLF. An editor that rewrites a file's line
      endings produces a whole-file diff — check `git diff --stat` before
      committing.
- [ ] **No secrets.** No tokens, no `~/.git-seafile.json` contents, no real
      server hostnames in tests or fixtures.

## Notes for the reviewer

<!-- Anything you were unsure about, or deliberately left out of scope. -->
