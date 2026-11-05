You are an independent reviewer of `git-remote-seafile`, at the commit `git log -1`
shows (v<X.Y.Z> plus whatever `CHANGELOG.md` lists under Unreleased). Another model
wrote the code, the tests and the specifications, and has reviewed them itself over a
dozen rounds: assume all three share blind spots. The last outside review was a
targeted one at commit `af9e9e1` (v0.9.17, 2026-10-28); the last full round was
v0.9.14. **Everything since `af9e9e1` has had no independent reader** — see Part G,
which is where most of your time should go.

WHAT IT IS
A Git remote helper (`seafile://<library>/<path>`) that stores a repository in a
Seafile library — a self-hosted file-sync server — through its REST API: native
packfiles, refs as small files, a ticket-style advisory lock built from file creation
and the server's `Date` header because Seafile has no compare-and-swap, tail merge and
full `gc`, a Git LFS transfer agent. Every pack it writes is capped at a configurable
size and files of 1 GiB or more upload in `Content-Range` chunks, because a real server
reset a single 2 GiB request.
Shipped in the same wheel: `git seafile-snapshot`, a whole-tree snapshot tool. It
captures a directory, ignored files included, byte-exact, into a separate "vault" git
repository and pushes it; the source stays read-only. Payload mode (on by default for a
push to `seafile://`) keeps large files out of git: each is SHA-256-hashed as it is sent
and stored under its hash, and the commit carries a manifest; `--restore` downloads and
verifies every byte; `--payload-gc` is two-phase retention with a grace period;
`--payload-verify` and `--restore-drill` read the data back rather than trusting the
listing. Modification times are recorded, embedded repositories are captured as plain
files, a share's clock is probed for the cache rule, `--json` and `--exit-warnings`
(3) put a run under a scheduler's control.
Since the last review the project has also gained: helper commands `login` (trades a
password for a stored token), `config`, `lfs-setup` and `doctor --create-library` /
`--set-history` (the last creates and deletes a *library* on the server); a push whose
pack a proxy refuses with HTTP 413 is rebuilt in smaller packs and the working cap is
remembered at four fifths; a first-run prompt about reproducible directories; and, in
the snapshot tool, **adaptation as a design rule** (`DESIGN.md` §3.5): staging writes
objects with several `git hash-object` processes, payload uploads and payload hashing
find their own parallelism by measuring, and a long list of paths goes to
`git update-index` instead of `git add`, because `add` matches every listed path
against the index and a 222,000-file run spent 22 minutes doing it. What is stored must
not change with the machine; only the speed may.

THE DOCUMENTS ARE THE SPECIFICATION
`DESIGN.md` (invariants §3 including §3.5 Adaptation, protocol §4, failure model §5,
threat model §6, non-goals §7), `USER_GUIDE.md` (§1–14 the helper; §15 is a stub — the
snapshot tool's guide is `SNAPSHOT_GUIDE.md`, whose limitations tables are promises to
users), `UPGRADING.md`, `AUTOMATION.md` (exit codes, message levels, `--json`), `RUNBOOK.md`,
`SECURITY.md`, `SNAPSHOT.md` (cost §5 including "Git's own costs", caveats §8,
comparisons §11, non-goals §15), `PAYLOAD.md` (requirements R, decisions D including
D11, failure model §6 including §6.1's three review rounds, attack table A1–A35, test
matrix §7), `AGENTS.md` and `CONTRIBUTING.md` (what a contributor is told; a rule there
that the code or the tests do not keep is a finding), and `ROADMAP.md` §1.1–§1.7 (known
open items, each marked probed or transcribed; §1.6 holds the dispositions of the last
review and §1.7 what the newest work left open).
`git_remote_seafile/snapshot_tool.py` is the concatenation of `tools/_snapshot/NN_*.py`:
review the parts, never the generated file.

RULES FOR THE WHOLE REVIEW
- Work in a COPY of the repository. Leave the tree you were given untouched: no
  commits, no pushes, no edits. Never contact a real server, never use real
  credentials. `tests/e2e_harness.py` is an in-process Seafile stub — reproduce every
  claim against it, or against a local bare repository, and keep the script so the
  next person can rerun it. The stub is MORE LENIENT than the real server (it accepts a
  copy into a missing directory, it never truncates a listing, it never rewrites a name
  you asked for), so label each finding REPRODUCED or INFERS-SERVER-BEHAVIOUR.
- Run only the tests for the area you review: `python -m unittest tests.<module>`.
  `tests.test_snapshot_payload` takes a few minutes; the full suite is not wanted.
  Several new tests are timing based (`tests/test_snapshot_climb.py`, the staging ramp
  tests): if one fails for you, run it three times and say so; a test that fails on
  your machine and not on the maintainer's is itself a finding (Part G5). Tests leave
  `grs-*` directories in the temporary directory on Windows (ROADMAP W2): clean them up.
- The snapshot tool is run only on scratch trees you create, with `--no-push` or against
  a local bare repository as `--remote`. `tools/measure_pathspec_cost.py` and
  `tools/gates.py --list` are safe to run. Do not point anything at a real vault.
- If you can read web pages, say so, and do the one task in Part G7 that needs it.
  `tools/build_snapshot.py` regenerates the tool if you edit a part — and `--check`
  tells you whether the generated file is stale, which is how an earlier reviewer's
  stray edit got caught.
- The recorded dispositions are settled ground. Re-reporting one requires showing that
  its stated reasoning is wrong, and saying so explicitly; §1.2's won't-do rows,
  `PAYLOAD.md` §6.1's "not changed, recorded" paragraphs, `SNAPSHOT.md` §15's
  non-goals and `DESIGN.md` §7 are all in that set. §1.4 is a fresh sweep: it is yours
  to check, not to re-derive (Part F).
- Separate what a document SAYS from what you INFER, and for an inference say what
  would have to be true. Every finding needs: `file:line` (code) or section (spec) **plus
  a verbatim quote of a few words actually on that line** — a citation whose quoted
  words are not in the file at that line voids the finding and puts the whole report
  under line-by-line re-verification; the exact input or interleaving; the bad outcome;
  REPRODUCED (say how) or PLAUSIBLE (say what would confirm it); and the smallest fix
  you would accept. No redesigns unless a finding cannot be fixed any other way. No
  style, naming or formatting comments.
- Rank: LOSS (committed or backed-up data unrecoverable), CORRUPT (repository unusable
  or wrong data accepted), WRONG (a wrong answer accepted or restored), LEAK (a token or
  data reaches a place it should not), CRASH (traceback), SURPRISE (works as designed,
  surprises the operator), NIT. Five solid findings beat thirty speculative ones.

FIVE QUESTIONS THAT KEEP FINDING THINGS IN THIS PROJECT. Ask each of them of every
component you read; the rounds so far produced the same shapes again and again, and
the findings still open are mostly of these kinds.
1. Is that an acknowledgement or a proof? A 2xx, a listing that shows a name at a size,
   a `False` from "does it exist", a `True` returned with no evidence, a state file that
   parsed to `{}`. Each fix so far has been a read-back; hunt the read-backs that are
   still missing, and the ones that were applied to one path and not to its siblings.
2. Which walks, sizes and platforms does this guard actually cover? Guards here were
   repeatedly sized by the feature that introduced them — a name check that lives behind
   a threshold, a free-space check for one phase and not the next, three walks that
   disagree about a junction until someone reconciles them.
3. Does this decide-then-act loop still describe the world when it acts? Anything that
   reads state, spends minutes, then deletes: how wide is the window, and what stops it?
4. What bounds the things nobody has measured? Server-side state files, directory
   listings, histories, and any "measured" number that came from one deployment.
5. Is this a substitute that was only compared on the cases its author thought of?
   The newest work swapped one git command for another (`add` for `update-index`) and
   one fixed number for a search. The swap was found wrong three times, each time by a
   case nobody had listed: a directory where a file had been, a path that vanished, an
   empty list. Name the cases the comparison did not cover, and run them.

ORDER OF WORK — follow it.
PHASE 1, specification only: Parts A and D, written down in full before you open code.
Do not open `git_remote_seafile/`, `tools/` or `tests/`, and do not read a prior
`REVIEW.*.md`. If you open code first, say so in the report: the design sections are
then the first thing to distrust, because the code will have anchored your attack.
PHASE 2, code: Parts G first (it is the least reviewed), then B, C, E, F, then H. You
may revisit A and D; mark what you changed and why.

PART A — design of the helper (Phase 1)
Find ways the design loses commits, corrupts a repository, lets two writers silently
overwrite each other, accepts a forged or damaged pack, or does not do what it claims.
Cover: the lock (clock sources and their offsets, lease expiry while a writer is still
working, a client that ignores the lock, the one-second `Date` resolution, retries after
a timeout, a lease taken over while the holder is mid-push, a queue whose ordering a
second writer can change); what a reader sees at each instant of a push; tail merge and
full `gc` while another machine pushes or fetches; ref writes, ref deletions and
conflict copies `"(SFConflict ...)"`; interrupted runs and partial uploads; SHA-1 versus
SHA-256 repositories; shallow and partial clones (accepted, then ignored — say what
that costs); Git LFS; library history, quota and server-side GC; chunked upload and the
pack cap; and what each "measured" claim justifies and what it does not.

PART D — design of the snapshot tool and payload mode (Phase 1)
Find ways the design loses data, silently restores wrong data, leaks data, or does not do
what it claims. Cover: several machines on one vault; retention and every way a `gc`
could delete something needed, including the state that retention itself relies on;
clocks, timestamps and the cache that trusts them (the share-clock probe, the racy
window, the change time, the remembered setting, the default-on conversion of an
existing vault); a hostile or damaged manifest, marker or server response; filesystem
differences (Windows, macOS, Linux, network shares, cloud placeholders, case folding,
Unicode normalisation, long paths, symlinks, junctions, reparse points); interrupted and
killed runs; very large inputs; nested repositories and embedded repositories; secrets;
and every requirement whose "how it is checked" column would not catch a violation.

PART G — everything since the last review (Phase 2, first and longest)
The map: `git log --oneline af9e9e1..HEAD` (about 50 commits, four releases and the
unreleased work), `git diff --stat af9e9e1 HEAD -- git_remote_seafile tools tests`,
and `CHANGELOG.md` from `[Unreleased]` down to `[0.9.18]`. Nobody but the author has
read any of it. Number findings G1…, G2… by the sub-area below, e.g. G2.1.

G1. The new helper commands and the push path.
`login.py` (a password is traded for a token: where is it read, can it be echoed,
logged, put on a command line or in an environment a child inherits; how is the token
stored, with what permissions; what happens over plain `http://`, on a redirect, with a
wrong host), `lfs_setup.py` (writes repository configuration from the remote's URL:
argument or config injection, a setting that persists and weakens a later run),
`library_setup.py` and `tools/seafile_doctor.py` (`doctor --create-library`,
`--set-history`, `--upload-limit` **create and delete things on the server**: prove it
can only delete what it created — prefix, uniqueness, race between create and delete,
a name that already exists — and what a half-finished run leaves), `settings_report.py`
(`config` prints every setting and where it came from: does it ever print a token, and
does its resolution order match what the code really does), the 413 handling in
`helper._upload_push_pack` (a pack the server refuses is rebuilt smaller: can objects be
lost or duplicated, can it loop, can the remembered four-fifths cap persist a value that
breaks a later push, is every rebuilt pack proven to have landed), `refs.py`'s
no-retry for a permanent error (is any transient failure classified permanent, and what
does the push then do), and the new auth hints. Tests: `test_login`, `test_lfs_setup`,
`test_doctor_library`, `test_pack_split`, `test_e2e_pack_split`, `test_settings_report`.

G2. Staging only what changed, and the `update-index` route (`69_prewrite.py`
`paths_to_stage`, `staging_commands`, `add_retrying`; `70_stage.py`).
The claim (SNAPSHOT.md §5, "Git's own costs"): the recorded tree is exactly what
`git add` with every path would record, and only the speed differs. Attack it. Does
`paths_to_stage` ever omit a path `add` would have staged (stat cache and racy entries,
`core.ignorecase`, type changes, a changed `.gitattributes` or filter on an unchanged
file, a path `ls-files -o -m` spells differently from `ls-files -c -o`)? Does
`update-index --refresh` or the listing run under `--dry-run` or `--status` in a way
those modes promise they do not? Directories go to `add` and files to `update-index`:
what does that do to a nested repository with a commit, one with none, a submodule
`.git` file, a payload path, an excluded path, a junction (Windows always takes this
route)? A path that vanishes is dropped from the index: can that drop a file that still
exists (a path `lstat` cannot read for another reason, a case-insensitive rename)? The
first version of this route had three bugs found only by running both routes through
one history; `ExoticReplacementTests` and `SpecialFileAndNameTests` are that
comparison. List five replacement or special-file cases they do not cover, run them on
your platform, and report any difference. For five guards of your choice (the directory
command, the vanished-path check, `--remove`, `--replace`, the empty-list guard, the
retry, the refresh) delete the guard and report whether a named test fails.

G3. The parallel pre-write and the stall watchdog (`69_prewrite.py` `prewrite_objects`,
`_Ramp`, `stall_limit_after_prewrite`; `20_gitrun.py`).
Up to 16 `git hash-object -w` processes write objects before `add` records them. Can
the writer count change the tree? What happens to the processes and to half-written
loose objects on Ctrl-C, a kill, a full disk, an exception in the loop? A batch that
fails is dropped silently and left to `add`: can that hide an error `add` then does not
raise? The watchdog counts bytes new under `objects/`, and the `add` after a pre-write
writes none, so its limit is three times the write pass: is that right on a slow disk,
and can a healthy run still be killed (or a hung one never)? `GIT_TIMEOUT` is 600 s for
tree-sized calls (`ls-files`, `update-index --refresh`, `write-tree`): ROADMAP W4.

G4. Adaptive payload uploads and hashing (`68_climb.py`, `67_hashing.py`,
`75_payload.py`, `74_payload_manifest.py`).
`_Climber` doubles while the rate grows by 15%, with patience of two levels, settles on
the best level seen, and halves on a `429/502/503/504`, requeueing the object twice.
What is stored and verified must not depend on the level: check the read-back of each
object, duplicates after a requeue, partial uploads left behind, the order of manifest
entries, the hash-cache entries, and that a file that changes while it is hashed is
still caught when several are hashed at once. Check the controller for thread safety
(`_ByteMeter`, `_RateWindow`), the clock it reads, what a worker's exception does to the
others (`abort=cancel.set`), memory (16 workers times the block size), the exactness of a
typed `--payload-jobs N` and of `SEAFILE_SNAPSHOT_STAGE_WORKERS` /
`SEAFILE_SNAPSHOT_HASH_WORKERS` (`1` must be the old serial path), and what a throttling
server does to a run that was already at the ceiling. The numbers in the code come from
one NVMe machine and one Seafile CE 13.0.28: say which constants are only that.

G5. The tests for all of it.
Many are timing based (`tests/test_snapshot_climb.py`; `StagingRampTests`,
`ParallelStagingTests` in `tests/test_snapshot_tool.py`) and use made-up machines (fast,
saturating, slow). Which can fail on a loaded CI runner or a slow disk? Which pass
without the rule they are named for (delete the rule; report survivors)? Do any assert
only on their own mock? Classes are the unit of parallel work in `tools/run_tests_parallel.py`:
does any new class depend on another's state or on the order of tests? Test fixtures
leave `grs-*` directories behind (ROADMAP W2): confirm, and say which fixture and why.

G6. The adaptation rule itself (`DESIGN.md` §3.5, `AGENTS.md` "Adapt, do not tune").
Clauses: measure at run time; a constant is only a ceiling or a first guess; a typed
setting beats a measurement; the run says what it chose; start cautious, grow on
evidence, back out; adaptation changes speed and never what is stored. For each
adaptive component, find the clause it breaks, if any. Then find a constant that
should be adaptive and is not (name the measurement that would make it so), and any
"measured" figure in a doc or comment that came from one machine and is written as a
fact about all.

G7. Claims that need the web (skip, and say so, if you cannot read pages).
SNAPSHOT.md §5 says a path list handed to `git add` costs files times paths, that this
is a known cost upstream, and that nothing found shows a fix for plain literal paths.
The author could not open the sources (a 403 and a 502) and cites them from search
summaries: Torvalds' 2008 commit 88ea8112b4abca416993e2a3145616cd732771c8
("Optimize match_pathspec() to avoid fnmatch()"), the 2022 sparse-checkout report
https://lore.kernel.org/git/c5662c60-4a21-be94-8fe0-13c6730292dd@github.com/ and
Elijah Newren's reply https://public-inbox.org/git/CABPp-BFRHjF7+9T8dGG=ixsezksZUNU7pm29MG2kcLEYwE06Og@mail.gmail.com/.
Read them. Say whether the document describes them fairly, whether a later git release
changed the many-pathspec cost, whether git's source (`dir.c`, `pathspec.c`, `builtin/add.c`)
confirms the loop the author only inferred, and which rows of the `add` versus
`update-index` table in that section are documented behaviour and which are only
observed. Run `python tools/measure_pathspec_cost.py` and report your numbers and git
version.

G8. Documents for the same period.
`CHANGELOG.md` `[Unreleased]` against `git log` (an entry with no commit, a commit with
no entry, a number that does not match what a test or script prints); the user-facing
claims in `SNAPSHOT_GUIDE.md`, `PAYLOAD.md` D11 and A25b, `DESIGN.md` §3.5 against the
code; every new environment variable and `staging:` / `payload:` status line against
the docs and the message-style test; whether `UPGRADING.md` would tell an operator what
changed for them (it has no entry yet for the unreleased work: draft the entry you
think is needed, as a finding).

PART B — code of the helper (Phase 2)
Scope: `git_remote_seafile/{helper,client,lock,gc,packs,refs,git_util,safety,url,config,
lfs,cli,seafile_paths,sqlite_read,units,repair,diagnose}.py` and their tests; the
commands added since the last review (`login.py`, `lfs_setup.py`, `library_setup.py`,
`settings_report.py`, `tools/seafile_doctor.py`) are Part G1, not here. In priority order:
1. Data loss or corruption on push, fetch, merge and `gc`: any ordering in which a
   crash, timeout, second writer or retry leaves a ref pointing at a missing object, or
   deletes something still referenced. Note the shipped shape: `gc` mirrors refs as one
   `packed-refs` file, re-reads the refs before deleting and verifies the uploaded pack's
   SHA-256; a push re-reads refs under the lock, proves its packs landed and fences every
   ref; a fetch whose listed pack 404s re-lists the pack directory once and retries, and
   still fails loudly if the requested tips are then absent; lock tickets carry a
   per-install id and the dead-PID reclaim requires it to match; `repair` restores refs
   whose objects no remote pack carries, without touching a ref; `--force-with-lease` is
   honoured.
2. The lock: lease and renewal arithmetic, clock source, release on every exit path
   (exceptions, `KeyboardInterrupt`, `SIGTERM`), reentrancy, `Date` going backwards,
   starvation, two holders, a recycled pid (the ticket carries a process start time).
3. Subprocess and environment: argument injection into git from a ref, a URL or a
   remote-supplied value; `GIT_DIR`/`GIT_WORK_TREE`/`GIT_CONFIG_*` leakage; repository
   config that weakens a guard; temp directories; Windows paths; every `subprocess` and
   HTTP call's bound.
4. Resources: unbounded reads, memory or disk proportional to repository size, handles,
   retries without backoff, state files whose size nobody has measured.
5. Error reporting: tracebacks, swallowed exceptions, exit codes git reads the wrong
   way, messages that name no way forward.
6. Docs versus code: every row of the limitations tables, every `seafile.*` key's
   documented default, range and refusal, every documented guarantee. A promise the code
   does not keep is a finding even when the behaviour is fine.
7. Tests that pass for the wrong reason: asserting nothing observable, mocking the thing
   under test, covering only the happy path of a guard.

PART C — hostile server and hostile input, helper side (Phase 2)
Assume the server operator, a compromised account with write access to the library, or a
man in the middle can return anything at any point. Using `url.py`, `safety.py`,
`client.py`, `packs.py`, `refs.py`, `lfs.py`, `sqlite_read.py` and DESIGN §6, find every
way a malicious or corrupted response, or a malicious `seafile://` URL, `.git/config`
value, ref name, listing entry or LFS pointer, can: write outside the intended directory;
overwrite a file git or the user cares about; make git run a different program or option;
exhaust memory, disk or time; leak the token to another host (redirects, URL parsing,
`server=` overrides, cross-host `Location`, transfer links); report success for data it
did not verify; or persist a setting that weakens a later run. Give the exact bytes or
URL, the line that mishandles them, and the shortest fix. Skip anything that needs the
attacker to control the user's machine already.

PART E — code of the snapshot tool and payload mode (Phase 2)
Scope: `tools/_snapshot/NN_*.py`, `tools/seafile_snapshot.py`, `tests/test_snapshot_*.py`.
Priority, from newest first (`git diff af9e9e1..HEAD -- tools tests` is your map; the
newest of it is Part G and is not repeated here): the `--status` report — a read-only mode whose two promises
are attackable, that it writes nothing and that its stat heuristic errs only in the
two named directions (make the scan disagree with a run about what belongs, or find
a state where the report asserts more than the records hold); the
`--restore-drill`, `--json` and `--exit-warnings` surface; the hash cache written
while a tree is hashed and resumed after a kill; the measured `auto` cut-off and the
`placement:` report; the shared message style and byte units; `--default-excludes`; the
modification-time record; embedded-repository capture and junctions; the
unreadable-directory stop; restore hardening (scratch under `.git`, links into `.git`
refused, download size caps, extended-length paths); payload retention's two phases and
its state files; the cache key's device, inode and change time; the argument checks in
`96_cli_checks.py`. Then the rest of payload mode and the plain git path.
1. Data loss or a wrong restore: any ordering in which a crash, a second machine, a
   retry or a stale cache leaves a manifest naming bytes that are not what the file
   held, an object missing, or a retention run deleting a referenced object. Can any path
   make the cache vouch for a changed file (size, mtime, inode, device, change time, the
   racy window, a clock offset)?
2. Capture: a path that escapes the tree; a link, junction or reparse point that makes
   the walk leave it or loop; `.git` files and directories; an excluded or payload path
   that gets in anyway; a repository that changes while it is walked.
3. Anything from the server or a manifest reaching a path, a command line or git:
   traversal, reserved names, case collisions, over-long names, NUL, a name starting with
   `-` or `"`, a name that is not UTF-8, a name git quotes.
4. Refusals: every flag that is silently ignored somewhere; and the standing rule that a
   default is not a request — a remembered or default setting must never turn a run that
   worked into an error, while only flags typed on the command line are validated
   strictly.
5. Errors: tracebacks, swallowed exceptions, exit codes, and a message that names no
   action. User-visible text is part of the contract, and the style guard
   (`tests/test_message_style.py`) is the current statement of it.
6. Windows versus POSIX, in both directions.
7. Mutation check: for five requirements of your choice, make the one-line change that
   violates the requirement and report whether any named test fails. Report every
   survivor.

PART F — the recorded open items (Phase 2, after G)
`ROADMAP.md` §1.4 is a sweep of the review archive against an older release; each row
is marked probed or lead, §1.5 carries the v0.9.13 triage rows, §1.6 the dispositions of
the last review (V1–V9: say whether each "done" row's named test would fail if the fix
were removed) and §1.7 what the newest work left open (W1–W6). For every open
row, say CONFIRMED, FIXED SINCE (name the commit or release), STALE (the description
no longer matches the code — say what the code does now), or MISRANKED (the severity
or effort is wrong, and why). Then spend the rest of the part on the row marked lead,
and on the questions the sweep left open because no machine in reach could answer
them. Also state, in one line each, whether any row is a duplicate of another or of an
existing §1 row. Settled ground here: the triage's T3 (push-pack SHA-256 proof) is an
explicit maintainer cost decision, and raising the lease check into acquire (T8's
residual) is deliberately not wanted — a planted garbage file must not block the lock
for ever.

PART H — what this prompt does not ask (Phase 2, last; suggestions, not findings)
You have now read more of this project than most people will. Say what you would do
about it, and what the prompt left out. This part is an open invitation; it is judged on
how useful it is, not on how many items it has.
H1. What else should be reviewed. Name the areas this prompt does not cover and say how
you would review each. Candidates, not a checklist: packaging (`pyproject.toml`,
`MANIFEST.in`, what the wheel and sdist contain), the CI and release workflows in
`.github/workflows/` (permissions, pinned actions, what a tag can publish), `tools/gates.py`
and `tools/release_check.py`, the minimum dependency versions and Python 3.10 against
3.14 for the new code, macOS (no machine in reach), locale-dependent git output,
the size and shape of the documentation set (is anything duplicated, stale or
contradicted elsewhere?), SECURITY.md against the code, and the test suite's runtime and
flakiness as a cost to contributors.
H2. Suggestions, at most ten, each as: what | why, with evidence | effort (S/M/L) |
risk | what would show it worked. Rank them by value for effort. Include at least one
thing you would **delete or stop doing**, and name the single change you would make
first. Mark each SUGGESTION; do not rank them LOSS to NIT and do not count them in the
tally.
H3. Where this prompt wasted your time or left you guessing, in a few lines, so the next
one is better. Say which instruction you could not follow and why.

OUTPUT — one markdown document, sections in this order.
1. Verdict: four sentences at most, then a tally by rank.
2. Part A and Part D findings (written in Phase 1): a table of ID | rank | scenario |
   the invariant, requirement or decision it breaks | smallest fix. Then, for each part,
   the three assumptions you trust least with the experiment that would test each, and
   the questions a maintainer must answer before the next release.
3. Part G findings, then Part B, C and E findings: a table of ID | rank | `file:line` |
   scenario | reproduced? | smallest fix. Number them G1.1…, B1…, C1…, E1…
4. Part F: a table of row | your verdict | evidence.
5. Part H: H1 (areas), H2 (suggestions) and H3 (this prompt).
6. What you checked and found sound: one line each. This is not filler — the
   2026-10-25 sweep of the archive found that most findings in its newest documents were
   already fixed or already guarded, and a named list of paths you attacked and could not
   break is what stops the next round from repeating that work.
7. What you could not check, and why; and, for everything else, whether you executed it
   or only read it. Include the commands you ran and the platform, git version and Python
   version you ran them on.

Save the report in the repository root as `REVIEW.<commit>.<model>.md`, where `<commit>`
is `git rev-parse --short HEAD` of the checkout you reviewed and `<model>` is a short
lower-case name for you (for example `REVIEW.1a2b3c4.gemini-3.8-flash.md`). Do not
commit it: `REVIEW*.md` is git-ignored on purpose. Keep the part headings and the `A1`,
`B3`, `C2`, `D4`, `E5`, `F6`, `G2.1`, `H2` numbering: rounds are compared by ID, and a
report whose findings are unnumbered cannot be merged.