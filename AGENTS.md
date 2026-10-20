# AGENTS.md

Guidance for coding agents (and a short orientation for people) working in this
repository. [CONTRIBUTING.md](CONTRIBUTING.md) is the authoritative developer guide;
this file is the part that is easy to get wrong.

## What this is

`git-remote-seafile` is a Git remote helper that stores a repository in a Seafile
library (`seafile://<library>/<path>`), plus a **whole-tree snapshot tool**
(`git seafile-snapshot`) that is shipped in the same wheel and version. Python 3.10+,
no runtime dependency except `requests`, no dev dependencies.

| Where | What |
| :--- | :--- |
| `git_remote_seafile/` | the package: helper, client, packs, gc, lock, LFS, safety guards |
| `git_remote_seafile/snapshot_tool.py` | **generated** — never edit it |
| `tools/_snapshot/NN_*.py` | the sources of the snapshot tool (one flat module, concatenated in name order) |
| `tools/seafile_snapshot.py` | hand-written wrapper that runs the generated module from a checkout |
| `tests/` | `unittest` suite; `e2e_harness.py` is an in-process Seafile stub |
| `PAYLOAD.md`, `SNAPSHOT.md`, `DESIGN.md` | specifications; the code is held to them |
| `CHANGELOG.md`, `.github/release-notes/` | per-release notes (checked by tests) |

## Gates (all must be green before a commit that touches code)

```bash
python tools/build_snapshot.py            # after editing any tools/_snapshot/*.py
python -m ruff check .
mypy                                      # and: mypy --platform linux --python-version 3.11
python tools/build_snapshot.py --check
python tools/verify_docs_guards.py        # and: python -m unittest tests.test_docs_consistency
python tools/run_tests_parallel.py -j 0   # the whole suite; several minutes
```

Run the full suite **before a release and when code outside the area you changed
is touched**. For a change confined to one area, run that area's module(s) plus
`tests.test_snapshot_build` and `tests.test_docs_consistency` — for the payload
code that is `python tools/run_tests_parallel.py -j 0 -k test_snapshot_payload`
(five classes, about 2 minutes; the class is the unit of parallel work, so keep
new test classes modest). Do not re-run the full suite just to be sure; it takes
about 2 minutes now, so run it once before a commit that touches shared code.

Also worth one run per release: the suite with an empty git config
(`GIT_CONFIG_GLOBAL=<empty file> GIT_CONFIG_NOSYSTEM=1`), because CI runners have no
identity and the first CI failures of a release have always been environmental.

## Rules that are enforced by tests (so you will hit them)

- A snapshot-tool part may not exceed **700 lines** (`99_cli.py`, with
  `parse_args` and `main`, is the largest at about 600; `75_payload.py` is about
  560; the argument checks live in `96_cli_checks.py`, embedded repositories in
  `71_embedded.py`, modification times in `73_mtimes.py`, the placement report and
  the machine/link measurement in `81_placement.py`). A name used as a *default
  argument* is evaluated at import, so it must come from an earlier part (use
  `None` and resolve it inside the function). All imports live in `10_setup.py`; a top-level
  name is defined in exactly one part; nothing evaluated at import time may come
  from a later part.
- A test class that defines `test_*` methods must not be subclassed.
- The version is declared once; docs, release notes and CHANGELOG must agree with
  it (`tests/test_version_consistency.py`, `tests/test_docs_consistency.py`).
- Docs make claims that guards check: a spec table row that names a test must name
  one that exists. Run `tools/verify_docs_guards.py` after editing `PAYLOAD.md`,
  `USER_GUIDE.md` or `ROADMAP.md`.

## Things that have bitten before

- **POSIX vs Windows.** CI is ubuntu/windows/macos × Python 3.10–3.14. Windows-only
  assumptions fail on the others: file names over 255 bytes cannot exist on POSIX,
  a local path such as `/tmp/x.git` parses leniently as a `seafile://` library, and
  background `git gc --auto` can race with temp-directory removal. A Debian WSL
  distribution is the quickest way to reproduce (copy the tree there; run with a
  `.git` present, or `tests.test_git_util` errors for the wrong reason).
- **A default is not a request.** Anything that comes from a default or from a
  remembered setting (payload mode, remote, excludes) must never turn a run that
  worked into an error; only flags typed on the command line are validated
  strictly.
- **The stub is more lenient than the server** (for example, a copy into a missing
  directory). Anything touching server semantics deserves a run against a real
  server; see below.
- **Line endings are LF** (`.gitattributes`). Edit with tools that preserve them.
- **Shell quoting.** On Windows, heredocs through the Bash tool collapse
  backslashes (a `\n` in a script's string literal becomes a real newline, a
  `\\0` becomes NUL) and an apostrophe can end the heredoc early. Write edit
  scripts with the Write tool, to the Windows temp directory (Python's `/tmp`
  is not Git Bash's), and use the Edit tool for any string containing `\n`.
  `ruff` finds the damage; a NUL byte in a test file is a SyntaxError.
- **Check `git status` before `git add -A`.** A scratch file in the root
  (`empty`, `REVIEW.*.tmp`) is easy to commit.

## Real-server tests (opt-in)

`SEAFILE_TEST_LIBRARY=<scratch library> python -m unittest tests.test_realserver`
works under one unique `grs-*` top-level folder and deletes it. Do the same for any
ad-hoc smoke test: scratch work goes only in a scratch library, under a unique
`grs-*` folder, removed afterwards. Never print tokens.

**Never run anything that deletes or rewrites data (`--payload-gc --payload-delete`,
`--payload-keep`, `--reset-to-remote`, library deletes) against a library that holds
real data**, even to test. Read-only commands (`--payload-verify`, report-only
`--payload-gc`, `--restore` into a new directory) are fine.

## Git and releases

- The maintainer pushes and tags. Do not `git push`, tag, or create a release unless
  asked. Commit messages end with the co-author trailer your harness specifies.
- Make several small commits rather than one large one; each should pass the gates.
- Release recipe is in CONTRIBUTING.md ("Tagging"): CHANGELOG `[x.y.z]`, release
  notes `.github/release-notes/vX.Y.Z.md`, version bump, gates, push, **wait for CI
  green on the pushed commit**, `python tools/release_check.py`,
  then tag and push the tag.
- Never edit a published tag's notes to hide a change; add a new release.

## Conventions

- Comments explain *why* (a measurement, an incident, a rejected alternative), not
  what. Match the surrounding density.
- User-visible text is part of the contract: messages name the flag that fixes the
  problem, and an error that cannot be acted on is a bug.
- A feature that can lose data needs a guard before it, a test that fails without
  the guard, and a line in the spec's failure model (PAYLOAD.md section 6).
