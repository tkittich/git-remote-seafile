You are an independent reviewer of `git-remote-seafile`, at the commit the tag `v0.9.23`
names (`git rev-parse v0.9.23`; if you were given a later commit, review that and say
which). Another model wrote the code, the tests and the specifications, and has reviewed
them itself over a dozen rounds: assume all three share blind spots. The last outside
review was a full round at commit `be01e37` (2026-11-05, five reviewers); its triage is
settled and is listed in Part F as dispositions. **Everything since `be01e37` has had no
independent reader** — the fixes that round produced (0.9.22), the Seafile Professional
work (issue 6), the container test tier and the 0.9.23 backlog — see Part G, which is
where most of your time should go.

WHAT IT IS
A Git remote helper (`seafile://<library>/<path>`) that stores a repository in a
Seafile library — a self-hosted file-sync server — through its REST API: native
packfiles, refs as small files, a ticket-style advisory lock built from file creation
and the server's `Date` header because Seafile has no compare-and-swap, tail merge and
full `gc`, a Git LFS transfer agent. Every pack it writes is capped at a configurable
size and files of 1 GiB or more upload in `Content-Range` chunks, because a real server
reset a single 2 GiB request. Helper commands include `login`, `config`, `lfs-setup`,
`doctor` (which can create a library and probe a proxy's upload limit) and `repair`.
Shipped in the same wheel: `git seafile-snapshot`, a whole-tree snapshot tool. It
captures a directory, ignored files included, byte-exact, into a separate "vault" git
repository and pushes it; the source stays read-only. Payload mode (on by default for a
push to `seafile://`) keeps large files out of git: each is SHA-256-hashed as it is sent
and stored under its hash, and the commit carries a manifest; `--restore` downloads and
verifies every byte; `--payload-gc` is two-phase retention with a grace period;
`--payload-verify` and `--restore-drill` read the data back rather than trusting the
listing. Staging, hashing and uploads find their own parallelism by measuring
(`DESIGN.md` §3.5), and a long or costly path list goes to `git update-index` instead of
`git add`. What is stored must not change with the machine; only the speed may.
Since `be01e37`: Seafile **Professional** answers some requests differently from
Community Edition (a file read or delete under a missing folder is 403 or a 500 page, not
404; folder permissions; group-owned libraries), and the helper now handles both; the
real-server suite runs in throwaway **CE and Pro** containers in CI, with a second
account; and the ten-item backlog of the last round (ROADMAP §1.8 X9) and the case-rename
leftover (X2) are done.

THE DOCUMENTS ARE THE SPECIFICATION
`DESIGN.md` (invariants §3 including §3.5 Adaptation, protocol §4, failure model §5,
threat model §6, non-goals §7), `USER_GUIDE.md` (§1–14 the helper; §15 is a stub — the
snapshot tool's guide is `SNAPSHOT_GUIDE.md`, whose limitations tables are promises to
users), `UPGRADING.md`, `AUTOMATION.md` (exit codes, message levels, `--json`), `RUNBOOK.md`,
`SECURITY.md`, `SNAPSHOT.md` (cost §5 including "Git's own costs", caveats §8,
comparisons §11, non-goals §15), `PAYLOAD.md` (requirements R, decisions D including
D11, failure model §6 including §6.1's review rounds, attack table A1–A35, test
matrix §7), `AGENTS.md` and `CONTRIBUTING.md` (what a contributor is told, including the
list of measured differences between the stub and a real server; a rule there that the
code or the tests do not keep is a finding), and `ROADMAP.md` §1 (known open items;
§1.8 holds the dispositions of the last round).
`git_remote_seafile/snapshot_tool.py` is the concatenation of `tools/_snapshot/NN_*.py`:
review the parts, never the generated file.

RULES FOR THE WHOLE REVIEW
- Work in a COPY of the repository. Leave the tree you were given untouched: no
  commits, no pushes, no edits. Make the copy with `git clone` (so the git-ignored
  earlier `REVIEW*.md` reports are not in it, and Phase 1 cannot read one by accident),
  then copy over any uncommitted or untracked files the maintainer told you about, and
  say which commit and which extra files you reviewed. HEAD may move while you work (a
  maintainer commits, a cleanup lands): keep reviewing the commit you copied, name it in
  the report, and say if a later commit touches a file you cite. Give the copy a name
  that does not start with `grs-` (use `review-<model>-`): the project's own cleanup
  sweeps `grs-*` directories, and one reviewer's copy was deleted under them mid-review.
- Never contact a real server, never use real credentials. `tests/e2e_harness.py` is an
  in-process Seafile stub — reproduce every claim against it, or against a local bare
  repository, and keep the script so the next person can rerun it. The stub is MORE
  LENIENT than the real server (it accepts a copy into a missing directory, it never
  truncates a listing, it never rewrites a name you asked for, its mkdir creates
  nothing); `GRS_STUB_PRO=1` makes its file reads answer the way Professional does.
  **If you have Docker**, a throwaway container from `.github/seafile-ci.yml` is not a
  real server: `SEAFILE_IMAGE=seafileltd/seafile-pro-mc:13.0-latest docker compose -f
  .github/seafile-ci.yml up -d` (or the CE default) and `python tools/realserver_ci.py`
  give you CE or Pro to reproduce against; say which edition and version
  (`/api2/server-info/`) each result came from. Label each finding REPRODUCED (stub,
  container or local repository — say which) or INFERS-SERVER-BEHAVIOUR.
- Run only the tests for the area you review: `python -m unittest tests.<module>`.
  `tests.test_snapshot_payload` takes about ten minutes, and each route-comparison class
  (`ExoticReplacementTests`, `SpecialFileAndNameTests`) 100 to 300 seconds on Windows;
  budget for that, run them in the background, and do not use that copy for anything
  else meanwhile. The full suite is not wanted. On a slow or antivirus-scanned disk use
  the smaller sizes of `tools/measure_pathspec_cost.py` (1000 to 5000).
  Several tests are timing based (`tests/test_snapshot_climb.py`, the staging ramp
  tests): if one fails for you, run it three times and say so; a test that fails on
  your machine and not on the maintainer's is itself a finding. Tests leave `grs-*`
  directories in the temporary directory on Windows (ROADMAP W2): clean them up.
- The snapshot tool is run only on scratch trees you create, with `--no-push` or against
  a local bare repository as `--remote`. `tools/measure_pathspec_cost.py` and
  `tools/gates.py --list` are safe to run. Do not point anything at a real vault.
- `tools/build_snapshot.py` regenerates the tool if you edit a part — and `--check`
  tells you whether the generated file is stale. The tests import the **generated**
  `snapshot_tool.py`, so a mutation of a part (deleting a guard) tests nothing until you
  run `python tools/build_snapshot.py`, and again after restoring it. Run mutations in a
  copy of their own: a background mutation run rewrites the generated file under any
  experiment that imports it from the same copy.
- The recorded dispositions are settled ground: Part F's table, §1.2's won't-do rows,
  `PAYLOAD.md` §6.1's "not changed, recorded" paragraphs, `SNAPSHOT.md` §15's
  non-goals and `DESIGN.md` §7. Re-reporting one requires showing that its stated
  reasoning is wrong, and saying so explicitly.
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
component you read; the rounds so far produced the same shapes again and again.
1. Is that an acknowledgement or a proof? A 2xx, a listing that shows a name at a size,
   a `False` from "does it exist", a `True` returned with no evidence, a state file that
   parsed to `{}`. Each fix so far has been a read-back; hunt the read-backs that are
   still missing, and the ones that were applied to one path and not to its siblings.
2. Which walks, sizes, platforms **and server editions** does this guard actually cover?
   Guards here were repeatedly sized by the feature that introduced them. Issue 6 was a
   guard ("404 means not there yet") that held on CE and not on Pro.
3. Does this decide-then-act loop still describe the world when it acts? Anything that
   reads state, spends minutes, then deletes: how wide is the window, and what stops it?
4. What bounds the things nobody has measured? Server-side state files, directory
   listings, histories, and any "measured" number that came from one deployment.
5. Is this a substitute that was only compared on the cases its author thought of?
   Two routes that agree can both be wrong (a directory renamed by case was lost by both
   staging routes); a name check that matched `<name> (1)` never saw Seafile's real
   `<stem> (1).<ext>`. Name the cases the comparison did not cover, and run them.

ORDER OF WORK — follow it.
PHASE 1, specification only: Parts A and D, written down in full before you open code.
Do not open `git_remote_seafile/`, `tools/` or `tests/`, and do not read a prior
`REVIEW.*.md`. If you open code first, say so in the report: the design sections are
then the first thing to distrust, because the code will have anchored your attack.
PHASE 2, code: Part G first (it is the least reviewed), then B, C, E, F, then H. You
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
SHA-256 repositories; shallow, partial and borrowing (`alternates`) clones; Git LFS;
library history, quota and server-side GC; chunked upload and the pack cap; the two
server editions and folder permissions; and what each "measured" claim justifies and
what it does not.

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

PART G — everything since `be01e37` (Phase 2, first and longest)
The map: `git log --oneline be01e37..v0.9.23`, `git diff --stat be01e37 v0.9.23 --
git_remote_seafile tools tests .github`, and `CHANGELOG.md` `[0.9.23]` and `[0.9.22]`.
Nobody but the author has read any of it. Number findings G1…, G2… by the sub-area below,
e.g. G2.1.

G1. The fixes of the last round (0.9.22, ROADMAP §1.8 X1–X10). Each was made in a day,
with a test, from a reviewer's report: `login` and redirects / loopback (`login.py`,
`url.is_loopback_host`); `--payload-delete` and the server clock
(`72_payload_guards.py`); the stall limit after a pre-write; `--default-excludes` per
directory (`94_first_run.py`, `96_cli_checks.py`, `97_settings.py`); the lease re-proven
after a ref write and a delete (`helper._publish_ref`); symlinks written as text on
restore (`90_restore.py`); `--no-push` on a payload vault (`78_payload_cli.py`). For each:
does the test fail without the fix (delete it and run), does the fix cover the siblings
of the reported path, and did it move the failure somewhere quieter?

G2. Seafile Professional (issue 6) and the client's answers to it (`client.py`).
`_file_is_under_a_missing_folder`: a 403 or 500 on a file read asks for the listing of
the file's folder, and only a clean 404 there reads as "not there yet". Attack it: a
folder that exists but lists as 404 to this account, a listing that answers 404 for
another reason, a race with a peer creating the folder, a file at the library root, a
proxy that answers 500 for everything, an account whose folder permission is
`invisible`. A ref wrongly read as absent is a ref a push may overwrite: find the
interleaving. `mkdir_p` now removes an empty copy a simultaneous `mkdir` leaves
(`_is_seafile_copy_of`) instead of aborting: can it remove something it did not
create, or something in use? The 403 hints (`_denied_hint`). CONTRIBUTING lists the
measured CE/Pro differences: which client calls would break on a third behaviour?

G3. The 0.9.23 backlog (X9, X2). The scratch-space check before `gc`, `gc --tail`
and `repair` (`gc.scratch_needed`, `scratch_shortfall`: is twice the packs right for
each, and is the check made where it should be, given the pack list is only known under
the lock); the `update-index` route by paths x index entries (`69_prewrite.py`
`index_entry_count`, `_INDEX_ROUTE_MIN_WORK`) and `spell_like_index` after a rename by
case; the pack-split floor (`diagnose.split_cap_bytes`); a typed `--payload-jobs N`
requeueing at a fixed level (`68_climb._Climber.forced`); `lfs-setup` quoting and
`lfs-transfer` refusing a split URL (measured: git-lfs 3.8.1 splits the args on
whitespace and honours double quotes — is that true of other git-lfs versions?); the
fetch skip withheld for `alternates`.

G4. The real-server and container tiers. `tests/test_realserver.py` (including
`SharedUserTests`: folder permissions through `user-folder-perm`, groups, departments,
two accounts racing a first push), `tools/realserver_ci.py` (creates a second account),
`.github/seafile-ci.yml` and `.github/workflows/realserver.yml` (a CE/Pro matrix on a
moving `13.0-latest` tag). Do the tests clean up everything they create on every exit
path, including a failure in `setUpClass`? Can one run's leftovers break the next? Is
any assertion true on one edition by accident? `_require_the_same_server` exists because
a run once nearly sent its pushes to a different server than its reads: are there other
ways the in-process client and the helper processes can disagree?

G5. The release and CI tooling: `tools/gates.py`, `tools/release_check.py`,
`.github/workflows/` (permissions, pinned actions, what a tag can publish, what a fork's
pull request can run), packaging (`pyproject.toml`, `MANIFEST.in`, what the wheel and
sdist contain — build them), and the minimum dependency versions against Python 3.10
and 3.14. Could a release ship something the gates did not test?

G6. Two modules no round has read closely: `sqlite_read.py` (reads the desktop
client's `accounts.db` for a token: a hostile or corrupt database, a locked one, one
from another client version) and `lfs.py` (the transfer agent: hostile pointers and
oids, paths, partial downloads, what reaches git-lfs on stdout).

G7. Documents for the same period. `CHANGELOG.md` `[0.9.23]` and `[0.9.22]` against
`git log` (an entry with no commit, a commit with no entry, a number that does not match
what a test or script prints); `UPGRADING.md`'s entries for both; the CE/Pro
differences in CONTRIBUTING against the tests that pin them; USER_GUIDE's tested-servers
claim against the workflow.

PART B — code of the helper (Phase 2)
Scope: `git_remote_seafile/{helper,client,lock,gc,packs,refs,git_util,safety,url,config,
cli,seafile_paths,units,repair,diagnose,login,lfs_setup,library_setup,settings_report}.py`,
`tools/seafile_doctor.py` and their tests (what Part G names is G, not here). In priority
order:
1. Data loss or corruption on push, fetch, merge and `gc`: any ordering in which a
   crash, timeout, second writer or retry leaves a ref pointing at a missing object, or
   deletes something still referenced. Note the shipped shape: `gc` mirrors refs as one
   `packed-refs` file, re-reads the refs before deleting and verifies the uploaded pack's
   SHA-256; a push re-reads refs under the lock, proves its packs landed, fences every
   ref and proves the lease again after each ref write; a fetch whose listed pack 404s
   re-lists the pack directory once and retries, and still fails loudly if the requested
   tips are then absent; lock tickets carry a per-install id and the dead-PID reclaim
   requires it to match; `repair` restores refs whose objects no remote pack carries,
   without touching a ref; `--force-with-lease` is honoured.
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
did not verify; make the helper read a refusal as an absence (the issue 6 shape: which
other status could a server send to get a ref treated as missing?); or persist a setting
that weakens a later run. Give the exact bytes or URL, the line that mishandles them, and
the shortest fix. Skip anything that needs the attacker to control the user's machine
already.

PART E — code of the snapshot tool and payload mode (Phase 2)
Scope: `tools/_snapshot/NN_*.py`, `tools/seafile_snapshot.py`, `tests/test_snapshot_*.py`.
What Part G names is G, not here. Then, newest first (`git log -- tools/_snapshot`):
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
   strictly (PAYLOAD D11 records the one deliberate exception).
5. Errors: tracebacks, swallowed exceptions, exit codes, and a message that names no
   action. User-visible text is part of the contract, and the style guard
   (`tests/test_message_style.py`) is the current statement of it.
6. Windows versus POSIX, in both directions.
7. Mutation check: for five requirements of your choice, make the one-line change that
   violates the requirement and report whether any named test fails. Report every
   survivor.

PART F — the recorded dispositions (Phase 2, after G)
The last round's triage (rows T1–T30, from the reports of five reviewers at `be01e37`) is
settled as follows; the ROADMAP §1.8 row each became is in brackets. For each row marked
done, say whether its named test fails when the fix is removed. For each row marked
open, say CONFIRMED, FIXED SINCE, STALE (the description no longer matches the code —
say what it does now) or MISRANKED (and why). Then spend the rest of the part on
ROADMAP §1 rows still open (W3, W4, W6, S1, S20, S21) and on the questions no machine in
reach could answer.

| Row | Finding, short | Disposition |
| :--- | :--- | :--- |
| T1, T2 | `login` followed a 307/308 to another host; `127.` prefix counted as loopback | done 0.9.22 [X1] |
| T3 | a new file in a directory renamed by case dropped as stale | done 0.9.22; the `update-index` route's two spellings done 0.9.23 [X2] |
| T4 | `--payload-delete` on the local clock | done 0.9.22: refused without a server `Date`, or with clocks a day apart [X3] |
| T5 | the raised stall limit after a pre-write was silent | done 0.9.22 (printed); the 3x multiplier is unmeasured on a cold read larger than RAM [X4, W3] |
| T6, T7, T9, T21, T27 | URL port rule; pathspec sources; offline machine and the grace period; RUNBOOK §8 title; a touched payload file makes a commit | documented 0.9.22 [X5] |
| T8 | `--status` hides a mode change; restore never verifies mode | documented 0.9.22; recording mode is backlog |
| T10 | a proxy limit under 128 MiB and an object between it and 1 GiB cannot be pushed | documented; the chunk size following the measured limit is backlog |
| T11 | the route switch counted paths, not paths x index entries | done 0.9.23 [X9] |
| T12, T13 | a conflict copy of a lock ticket; the stub never makes an `(SFConflict ...)` copy | open [X7]: a stub mode is wanted |
| T14 | restore on Windows without `core.symlinks` failed whole | done 0.9.22: reported, the restore goes on [X8] |
| T15, T17, T18, T19, T20, T22, T23, T24 | scratch space before `gc`/`repair`; the split floor; empty vs missing download; typed `--payload-jobs` and push-back; two stall sites held by one test; the measuring script's traceback; `--status`/`--maintain` write nothing; `lfs-setup` and a space | done 0.9.23 [X9] |
| T16 | a docstring said `gc` proves its pack the way a push does | fixed in the docstring; T3's cost decision (a push proves by size, `gc` by SHA-256) is settled and not re-opened |
| T25, T30 | a ref write after the lease lapsed | done 0.9.22: the lease is proven again after each ref write and delete; the suspension itself was not reproduced [X7] |
| T26 | `--default-excludes` dropped never-committed source under `build`/`dist`/`venv` | done 0.9.22: decided per directory [X6] |
| T28 | `--status` is read-only | confirmed by the triage; pinned by a test in 0.9.23 (T23) |
| T29 | a `--no-push` run on a payload vault is refused | deliberate: R10 wins over the "a default is not a request" rule, recorded in PAYLOAD D11 [X10] |

Settled ground in this part: the lease check is deliberately not raised into acquire (a
planted garbage file must not block the lock for ever); DS A3's `alternates` answer (the
skip is withheld) is done.

PART H — what this prompt does not ask (Phase 2, last; suggestions, not findings)
You have now read more of this project than most people will. Say what you would do
about it, and what the prompt left out. This part is an open invitation; it is judged on
how useful it is, not on how many items it has.
H1. What else should be reviewed. Name the areas this prompt does not cover and say how
you would review each. Candidates, not a checklist: macOS (no machine in reach),
locale-dependent git output, older Seafile releases (only 13.0 is tested), the size and
shape of the documentation set (is anything duplicated, stale or contradicted
elsewhere?), SECURITY.md against the code, and the test suite's runtime and flakiness as
a cost to contributors.
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
6. What you checked and found sound: one line each, and for every part with no finding
   a line saying that and what you attacked there (a part with an empty table is not
   one you skipped). This is not filler — a named list of paths you attacked and could
   not break is what stops the next round from repeating that work.
7. What you could not check, and why; and, for everything else, whether you executed it
   or only read it. Include the commands you ran and the platform, git version, Python
   version and (if you used a container) Seafile edition and version you ran them on.

Save the report in the repository root as `REVIEW.<commit>.<model>.md`, where `<commit>`
is `git rev-parse --short HEAD` of the checkout you reviewed and `<model>` is a short
lower-case name for you (for example `REVIEW.1a2b3c4.gemini-3.8-flash.md`). Do not
commit it: `REVIEW*.md` is git-ignored on purpose. Keep the part headings and the `A1`,
`B3`, `C2`, `D4`, `E5`, `F6`, `G2.1`, `H2` numbering: rounds are compared by ID, and a
report whose findings are unnumbered cannot be merged.
