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
| `USER_GUIDE.md`, `SNAPSHOT_GUIDE.md`, `UPGRADING.md` | the user-facing guides; `SNAPSHOT_GUIDE.md` is the old User Guide section 15 (a stub with a map stays there), `UPGRADING.md` holds the per-release "what may change" notes |
| `CHANGELOG.md`, `.github/release-notes/` | per-release notes (checked by tests) |
| `RUNBOOK.md` | the operations runbook: §0 is three diagnostics, then a recipe per failure mode |
| `ROADMAP.md` | §1 is the live backlog — review findings become its rows; §2 records what shipped, by release |
| `review-prompt.md`, `REVIEW.*.md`, `archive/` | the prompt every review round is run from; reports land in root as `REVIEW.<model>-v<X.Y.Z>.md`, are triaged into ROADMAP §1, then move to `archive/` |

## Gates (all must be green before a commit that touches code)

```bash
python tools/gates.py    # build, ruff, mypy ×2, docs guards, suite — in order
```

That is the whole sequence: `tools/gates.py` runs the seven gates, echoes each
command, and stops at the first failure; `--list` prints the commands it runs,
and `-k <substring>` reaches the suite step only. Run it bare — through a pipe
(`| tail`) the exit status is the pipe's, and a red suite scrolled off the top
reads as green.

Run the full suite (no `-k`) **before a release and when code outside the area
you changed is touched**. For a change confined to one area, run that area's
module(s) plus `tests.test_snapshot_build` and `tests.test_docs_consistency` —
for the payload code that is `python tools/gates.py -k test_snapshot_payload`
(five classes; the class is the unit of parallel work, so keep new test classes
modest). Do not re-run the full suite just to be sure; run it once before a
commit that touches shared code.

Also worth one run per release: the suite with an empty git config
(`GIT_CONFIG_GLOBAL=<empty file> GIT_CONFIG_NOSYSTEM=1`), because CI runners have no
identity and the first CI failures of a release have always been environmental.

## Rules that are enforced by tests (so you will hit them)

- A snapshot-tool part may not exceed **700 lines** — the largest parts sit
  close to that limit, so move code out before adding to one. `python
  tools/build_snapshot.py --check` prints the live sizes against the limit
  (never write them here; they age). The argument checks live in
  `96_cli_checks.py`, embedded repositories in `71_embedded.py`, the first-run
  exclude prompt in `94_first_run.py`, modification times in `73_mtimes.py`,
  the placement report and the machine/link measurement in `81_placement.py`.
  A name used as a *default argument* is evaluated at import, so it must come
  from an earlier part (use `None` and resolve it inside the function). All
  imports live in `10_setup.py`; a top-level
  name is defined in exactly one part; nothing evaluated at import time may come
  from a later part.
- A test class that defines `test_*` methods must not be subclassed.
- Notifications are `Warning:`/`Note:`/`Notice:`/`Hint:`/`Error:` in title case, byte
  counts are binary (`KiB`, not `KB`), and every new message is mentioned by a test
  (`tests/test_message_style.py`; see Conventions below).
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
  Test assertions on file metadata (mtime, size, inode) are part of this: gate
  them on the platform or round them, never assert one platform's exact values.
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
- **A patch undone anywhere but on the patcher stays live.** A `patch(...)`
  whose `stop()` was skipped — or spent on the mock it returned — leaks into
  every later test and fails somewhere unrelated. Use `with patch(...)` or
  `addCleanup(patcher.stop)` so every exit path undoes it.
- **A guard test must go red when its guard is deleted.** More than one test
  named after a guard kept passing after the guard was removed: it asserted
  the guard was *called*, not that the run was *refused*. Assert on the
  observable refusal (the message, the exit code, the file that must not
  exist), and once per guard, delete the guard and watch the test fail.
- **A test runs in whatever class receives it.** A method pasted into the
  wrong class runs under the wrong fixtures, or — pasted into a class that is
  then subclassed — twice. Put a new test in the class whose `setUp` it needs
  (the no-subclass rule above exists because `unittest` inherits tests).
- **`Path.write_text` translates newlines on Windows.** The platform default
  writes `\r\n` for `\n`, which corrupts any fixture the code hashes or
  byte-compares. Pass `newline="\n"` (or use `write_bytes`); this is the
  test-side face of the LF rule above.

## Real-server tests (opt-in)

`SEAFILE_TEST_LIBRARY=<scratch library> python -m unittest tests.test_realserver`
works under one unique `grs-*` top-level folder and deletes it. Do the same for any
ad-hoc smoke test: scratch work goes only in a scratch library, under a unique
`grs-*` folder, removed afterwards. Never print tokens.

The deployment differs from the stub in ways that were measured, not assumed —
nginx answers 502 to a burst of concurrent writes, an upload onto a taken name
is renamed rather than refused, the listing endpoint ignores paging
parameters. CONTRIBUTING.md ("Testing against a real Seafile server") carries
the list; read it before trusting a stub-green change to server semantics.

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
- **Message style** (enforced by `tests/test_message_style.py`): a notification opens
  with `Warning:` (needs attention), `Note:` (FYI), `Notice:` (a default changed what
  this run does) or `Hint:` (advice that changes nothing); a fatal error is `Error:`.
  Title case, never `WARNING:`/`tip:`/`error:`. Status lines (`placement:`, `progress:`,
  `tuned:`) are labels, not levels. Byte counts use `human_bytes` in the snapshot
  tool and `git_remote_seafile.units.format_bytes` in the package -- one function, two
  copies kept equal by a test -- and print KiB/MiB/GiB, never KB/MB/GB.
- **A new message needs a test** that triggers it and quotes three words in a row of
  its text. `tests/untested_messages.txt` lists the older messages that have none; it
  may only shrink, and a new message is not added to it to get past the check.
- Before a release, run a real snapshot against a scratch tree and *read the whole
  output*. Reading one real run found the missing progress line, the `GB`/`GiB` mix
  and a credentials warning on a CA bundle; none of it showed in a green test run.
- A feature that can lose data needs a guard before it, a test that fails without
  the guard, and a line in the spec's failure model (PAYLOAD.md section 6).
