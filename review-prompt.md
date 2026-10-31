You are an independent reviewer of `git-remote-seafile`, at the commit `git log -1`
shows (v<X.Y.Z>). Another model wrote the code, the tests and the specifications, and
has reviewed them itself over a dozen rounds: assume all three share blind spots.

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

THE DOCUMENTS ARE THE SPECIFICATION
`DESIGN.md` (invariants §3, protocol §4, failure model §5, threat model §6, non-goals
§7), `USER_GUIDE.md` (§1–14 the helper, §15 the snapshot tool — its limitations tables
are promises to users), `RUNBOOK.md`, `SECURITY.md`, `SNAPSHOT.md` (caveats §8,
comparisons §11, non-goals §15), `PAYLOAD.md` (requirements R, decisions D, failure
model §6 including §6.1's three review rounds, attack table A1–A34, test matrix §7),
and `ROADMAP.md` §1.1–§1.4 (known open items, each marked probed or transcribed).
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

FOUR QUESTIONS THAT KEEP FINDING THINGS IN THIS PROJECT. Ask each of them of every
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

ORDER OF WORK — follow it.
PHASE 1, specification only: Parts A and D, written down in full before you open code.
Do not open `git_remote_seafile/`, `tools/` or `tests/`, and do not read a prior
`REVIEW.*.md`. If you open code first, say so in the report: the design sections are
then the first thing to distrust, because the code will have anchored your attack.
PHASE 2, code: Parts B, C, E, F. You may revisit A and D; mark what you changed and why.

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

PART B — code of the helper (Phase 2)
Scope: `git_remote_seafile/{helper,client,lock,gc,packs,refs,git_util,safety,url,config,
lfs,cli,seafile_paths,sqlite_read,units}.py` and their tests. In priority order:
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
Priority, from newest first (a `git diff` over the last few releases of `tools/` and
`tests/` is your map): the `--status` report — a read-only mode whose two promises
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

PART F — the recorded open items (Phase 2, last)
`ROADMAP.md` §1.4 is a sweep of the review archive against the release before yours;
each row is marked probed or lead, and §1.5 carries the triage rows. For every open
row, say CONFIRMED, FIXED SINCE (name the commit or release), STALE (the description
no longer matches the code — say what the code does now), or MISRANKED (the severity
or effort is wrong, and why). Then spend the rest of the part on the row marked lead,
and on the questions the sweep left open because no machine in reach could answer
them. Also state, in one line each, whether any row is a duplicate of another or of an
existing §1 row. Settled ground here: the triage's T3 (push-pack SHA-256 proof) is an
explicit maintainer cost decision, and raising the lease check into acquire (T8's
residual) is deliberately not wanted — a planted garbage file must not block the lock
for ever.

OUTPUT — one markdown document, sections in this order.
1. Verdict: four sentences at most, then a tally by rank.
2. Part A and Part D findings (written in Phase 1): a table of ID | rank | scenario |
   the invariant, requirement or decision it breaks | smallest fix. Then, for each part,
   the three assumptions you trust least with the experiment that would test each, and
   the questions a maintainer must answer before the next release.
3. Part B, C and E findings: a table of ID | rank | `file:line` | scenario |
   reproduced? | smallest fix. Number them B1…, C1…, E1…
4. Part F: a table of row | your verdict | evidence.
5. What you checked and found sound: one line each. This is not filler — the
   2026-10-25 sweep of the archive found that most findings in its newest documents were
   already fixed or already guarded, and a named list of paths you attacked and could not
   break is what stops the next round from repeating that work.
6. What you could not check, and why; and, for everything else, whether you executed it
   or only read it.

Save the report as `REVIEW.<model>-v<X.Y.Z>.md` in root. Keep 
the part headings and the `A1`, `B3`, `C2`, `D4`, `E5`, `F6` numbering: rounds are
compared by ID, and a report whose findings are unnumbered cannot be merged.