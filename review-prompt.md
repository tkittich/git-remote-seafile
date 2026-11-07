You are an independent reviewer of `git-remote-seafile`, at the commit you were given
(`git log -1`). **This is a pre-release round**: version 0.9.23 is prepared in that
commit (`__version__`, `CHANGELOG.md` `[0.9.23]`, `.github/release-notes/v0.9.23.md`,
`UPGRADING.md`) but not tagged, and it will not be until your findings are triaged — so
the release files are under review too, and a finding there is cheap to fix. Another
model wrote the code, the tests and the specifications, and has reviewed
them itself over a dozen rounds: assume all three share blind spots. The last outside
review was a pre-release round at commit `4f1cdd4` (2026-11-07, three reviewers); its
triage is settled and is listed in Part F as dispositions. **Everything since `4f1cdd4`
has had no independent reader** — the twenty fixes that round produced, the backlog
done after it (a vanished repository path, the payload clock bound, the link probe's
debris, the gates' stale-file note, a refusal for non-owners on Professional), the
narrowing of `mkdir_p`'s clean-up to the helper's own folders, the first runs of the
CE/Pro container tier, and a faster test plan (`gates.py --changed`) — see Part G,
which is where most of your time should go. The work before `4f1cdd4` (0.9.22, issue 6,
the 0.9.23 backlog) had one outside round; Parts A–E still cover it.

WHAT IT IS
A Git remote helper (`seafile://<library>/<path>`) that stores a repository in a Seafile
library through its REST API, with an advisory lock built from file creation and the
server's `Date` header, and a whole-tree snapshot tool (`git seafile-snapshot`) shipped
in the same wheel. Read `README.md` for the overview, `DESIGN.md` §1–§4 for how the
helper works, `SNAPSHOT.md` and `PAYLOAD.md` for the snapshot tool and its large-file
store, and `CHANGELOG.md` `[0.9.23]` and `[0.9.22]` for what changed since the last
round (Seafile Professional, the CE/Pro container tier, the last two rounds' fixes).

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
§1.9 holds the dispositions of the last round, §1.8 those of the round before).
`git_remote_seafile/snapshot_tool.py` is the concatenation of `tools/_snapshot/NN_*.py`:
review the parts, never the generated file.

RULES FOR THE WHOLE REVIEW
- **Several reviewers are working on this commit at the same time**, each a different
  model, each on the whole prompt. Work independently: do not open, read, search or
  quote another reviewer's report (`REVIEW.*.md`) or working files, in the repository
  root or anywhere else, even if you come across them. Keep everything you make apart
  from theirs: your copy of the repository, your scripts, scratch trees and temporary
  directories all carry your own name (`review-<model>-...`), and you never delete,
  move or overwrite a file or directory you did not create — another reviewer's run may
  be using it. The same holds for containers and Docker volumes (see the container rule
  below).
- Work in a COPY of the repository. Leave the tree you were given untouched: no
  commits, no pushes, no edits. Make the copy with `git clone` (so the git-ignored
  earlier `REVIEW*.md` reports are not in it, and Phase 1 cannot read one by accident),
  then copy over any uncommitted or untracked files the maintainer told you about, and
  say which commit and which extra files you reviewed. HEAD may move while you work (a
  maintainer commits, a cleanup lands): keep reviewing the commit you copied, name it in
  the report, and say if a later commit touches a file you cite. Give the copy a name
  that does not start with `grs-` (use `review-<model>-`): the project's own cleanup
  sweeps `grs-*` directories, and one reviewer's copy was deleted under them mid-review.
- Never contact a real server, never use real credentials -- **and make it impossible, not
  only forbidden**: a reviewer at 4f1cdd4 ran `check-auth` with an empty git config and the
  machine's desktop Seafile client answered for it, one read-only request to the
  maintainer's real server. For *every* command, not only test runs, point `HOME`,
  `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` and `CCNET_CONF_DIR` at a directory of your own
  (`review-<model>-home`), and set `SEAFILE_SERVER`/`SEAFILE_TOKEN` to the stub's address
  and a dummy token. `tests/e2e_harness.py` is an
  in-process Seafile stub — reproduce every claim against it, or against a local bare
  repository, and keep the script so the next person can rerun it. The stub is MORE
  LENIENT than the real server (it accepts a copy into a missing directory, it never
  truncates a listing, it never rewrites a name you asked for, its mkdir creates
  nothing); `GRS_STUB_PRO=1` makes its file reads answer the way Professional does.
  **If you have Docker**, a throwaway container from `.github/seafile-ci.yml` is not a
  real server: `SEAFILE_IMAGE=seafileltd/seafile-pro-mc:13.0-latest docker compose -f
  .github/seafile-ci.yml up -d` (or the CE default) and `python tools/realserver_ci.py`
  give you CE or Pro to reproduce against; say which edition and version
  (`/api2/server-info/`) each result came from. **One container per machine:** the
  compose file publishes port 80 and writes `127.0.0.1` into the server's links, so a
  second one cannot start beside it. If a container is already running and you did not
  start it, it belongs to another reviewer: do not stop, reuse or reconfigure it, and use
  the stub instead. Start yours with `-p review-<model>` and take it down
  (`docker compose -p review-<model> -f .github/seafile-ci.yml down -v`) when you are
  done. Label each finding REPRODUCED (stub, container or local repository — say which)
  or INFERS-SERVER-BEHAVIOUR.
- **The full suite is run at this commit by CI and the maintainer — do not run it
  again**, and do not run whole modules to "check that the tests pass". Several reviewers
  work on this commit at once, and a full run is twelve minutes or more each that finds
  nothing new. CI's runs for the commit are on GitHub (`gh run list --commit <sha>`:
  `ci.yml` on Ubuntu, Windows and macOS, `realserver.yml` against CE and Pro); a run that
  is red or still going when you start is the maintainer's to deal with, not a finding.
  Done by the maintainer: `python tools/gates.py` on Windows 11 (build check, ruff, mypy,
  the docs guards and all 1,671 tests: green) and, two commits earlier, the whole suite on
  Debian (WSL; 1,673, the symlink scenarios included) with an
  empty git config. Run a test only to do something those runs could not: reproduce a
  finding, run a mutation (delete a guard, watch its test), or try a case no test covers —
  one named test or class at a time (`python -m unittest tests.<module>.<Class>.<test>`).
  `tests.test_snapshot_payload` takes about ten minutes, and each route-comparison
  scenario (`ExoticReplacementTests`, `SpecialFileAndNameTests`, one test per scenario)
  20 to 40 seconds on Windows, so name the test you need. Use `-j 12` or `-j 24` with
  `tools/run_tests_parallel.py`, nothing else. On a slow or antivirus-scanned disk use the smaller sizes of
  `tools/measure_pathspec_cost.py` (1000 to 5000).
  Several tests are timing based (`tests/test_snapshot_climb.py`, the staging ramp
  tests): if one fails for you, run it three times and say so; a test that fails on
  your machine and not on the maintainer's is itself a finding. Tests make `grs-*`
  directories in the temporary directory and remove them; do **not** sweep `grs-*` by
  pattern, because another reviewer's tests may be running in them. Point `TMPDIR` (and
  `TEMP`/`TMP` on Windows) at a `review-<model>-tmp` directory of your own for every run,
  and delete only that.
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
- **Known, and not findings** (report one only if you show the stated reason is wrong):
  the release date in `CHANGELOG.md` `[0.9.23]` and the ROADMAP line is a
  placeholder until the tag; *why* Professional answers a missing folder with 403/500 is
  inferred (its server code is closed) — the answers themselves were measured on 13.0.29;
  removing the empty copy a simultaneous `mkdir` leaves is best-effort and deliberately
  limited to the helper's own layout folders (a copy of a folder a person named is named
  in a warning and left, by the maintainer's decision); the CE/Pro container workflow is
  runs weekly and by hand, its images are pinned by digest, `release_check.py` requires
  it green on both legs at the tagged commit, and ROADMAP B8 records what its first runs
  found.
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

SEVEN QUESTIONS THAT KEEP FINDING THINGS IN THIS PROJECT. Ask each of them of every
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
6. Which equality does this name comparison use? git under `core.ignorecase` folds ASCII
   letters only, NTFS folds by its upcase table, APFS and HFS+ normalise Unicode, Seafile
   stores NFC, and Python's `casefold` folds the sharp s to `ss`. A guard that compares
   names with the wrong one drops or merges files: the 0.9.23 respelling dropped a new
   `straße/` file beside an indexed `strasse/`. Try `ß`/`ss`, the `ﬁ` ligature, Kelvin
   `K`/`k` and NFC/NFD `é` on every such guard.
7. Whose name is it? A folder or file the tool lays out itself (`.git-lock.d`, `pack`,
   `payload/objects/ab`) may be tidied; one a person named (the repository's path, a
   branch namespace, a payload view, a source file) may sit among their own things and
   must not be. Every delete, rename or overwrite: which kind of name can reach it, and
   what stops the other kind? `mkdir_p` removed a person's empty `app (1)` until 0.9.23.

ORDER OF WORK — follow it.
PHASE 1, specification only: Parts A and D, written down in full before you open code. A
Phase 1 finding cites a spec section and quotes its words; a `file:line` is added in Phase 2
if the code confirms it.
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

PART G — everything since `4f1cdd4` (Phase 2, first and longest)
The map: `git log --oneline 4f1cdd4..HEAD`, `git diff --stat 4f1cdd4 HEAD --
git_remote_seafile tools tests .github`, `ROADMAP.md` §1.9 (rows Y1–Y10, B1–B11) and
`CHANGELOG.md` `[0.9.23]`. Each change was made in a day, with a test, mostly from a
reviewer's report. For every one: does its test fail without it (delete it and run),
does it cover the siblings of the reported path, and did it move the failure somewhere
quieter? Number findings G1…, G2… by the sub-area below, e.g. G2.1.

G1. Names and the index (`69_prewrite.py` `ignorecase_key`, `spell_like_index`;
`70_stage.py`'s stale filter). Names are folded the way git folds them under
`core.ignorecase` (A–Z only) and a respelling is kept only when the disk resolves it.
Attack it with every fold in question 6, on both staging routes, with a directory renamed
by case, and on a filesystem that is case-sensitive but where `core.ignorecase` is set by
hand.

G2. What the server says it holds versus what it serves.
- A ref the ref directory lists but whose content cannot be read (a file-host 404 for that
  file, an empty body) is refused, not overwritten (`helper._decide_update`,
  `_listed_as_file`); a plain `--force` still overwrites. The lock's verification scan
  fails closed on such a ticket (`lock._scan_tickets`, `strict`). The stub faults are
  `raw_404_on` and `raw_empty_on` (`tests/e2e_harness.py`). Which other read decides
  "absent" from a download alone — packs, `HEAD`, `packed-refs`, payload manifests and
  markers, `gc-pending.json`?
- A repository folder that vanished (moved in the web UI) is no longer an empty
  repository: `cmd_list` fails when there is no `HEAD`, ref or pack and the clone holds
  refs the remote's fetch refspecs store (`git_util.remote_tracking_refs`,
  `_fetch_destinations`; the first version read only `refs/remotes/<name>/` and missed a
  `--mirror` clone). Is that the only place a vanished path matters (`gc`, `repair`, the
  snapshot tool's restore and retention, LFS)? Which fetches store refs a refspec does not
  name (a command-line refspec, `remote.<name>.mirror`, `fetch.prune` with tags), and what
  does `fetch --prune` do to them now?
- An LFS upload is proven by listing the object at its size (`lfs.py`).
- On Seafile Professional a full `gc` and `--payload-delete` refuse for an account that
  does not own the library (`SeafileClient.hidden_history_risk`, `cli._cmd_gc`,
  `72_payload_guards._require_whole_history`; `--allow-non-owner`,
  `--payload-allow-non-owner`), because an `invisible` folder permission leaves the
  folder out of that account's listing (measured on Pro 13.0.29, ROADMAP B2). The first
  version compared the owner's email and refused the owner too: the library info
  answers its owner `"owner": "self"` (found by the Pro leg; the stub now answers the
  same). Which other field of a server answer does the stub invent rather than copy?
  Which other operation reads "what is there" from a listing an account may see only
  part of (the tail merge, `repair`, `doctor`, the snapshot tool's restore and
  `--status`)? Is "owner" the right test for a group-owned library, an admin, or a share
  to a group?
- After a ref write that was read back, a lease that cannot be proven is a warning, and a
  lost one an error (`helper._publish_ref`, `LEASE_UNKNOWN`). Is there an interleaving in
  which "unknown" hides a takeover?

G3. Seafile's numbered copies (`client.mkdir_p`, `_LAYOUT_DIR_NAMES`, `_is_layout_dir`).
The parent is listed before and after each creation; an empty copy that appeared is
removed only for the helper's and the tool's own layout folders, and any other is named in
a warning. Ask question 7 of it, and list every other way Seafile makes a `(N)` name the
code meets: an upload without `replace`, a rename or copy onto a taken name
(`75_payload.py`'s hash renames, `79_payload_view.py`), a move in the web UI, a restore from
the library's history. Can a layout name be a person's folder (a repository whose path is
`.../lfs`, a view named `objects`)?

G4. The snapshot tool's backlog: `--payload-delete` refuses clocks more than an hour apart
(`72_payload_guards._MAX_CLOCK_SKEW_SECONDS`; is the hour right for a server behind a
proxy that rewrites `Date`, or a laptop that sleeps?); the link probe's debris is swept a
day after it was left (`81_placement.sweep_probe_debris`, from the next probe and every
`--payload-delete` run: can it delete a probe file another machine is uploading, or
anything else under `payload/objects/`?); `--no-push` remembers no setting (`99_cli.py`).

G5. The tiers and the tooling. `tools/run_tests_parallel.py` now plans from the last
run's seconds per target (`.test-timings.json`): longest first, and a class over two
minutes runs one test per worker; `tools/gates.py --changed` narrows the suite to the
modules a change reaches (`suite_for_changes`): can a change escape its map and land
untested (a test that imports a module the map does not connect, a new file)? The two
route-comparison classes now make one test per scenario (`_RouteComparison.
__init_subclass__`). `tests/test_realserver.py` as it is now: the edition is
asked, not inferred from a status (CE 13.0 answers the folder-permission route 403); the
first-push race test allows numbered copies above the repository only if empty and named
in a warning; `EditionProbeTests` checks, without a server, that every `cls._name` a
set-up reads exists (a slip that failed both legs of the second CI run); B2's measurement
of an `invisible` rule. `tools/gates.py`'s note on a rebuilt `snapshot_tool.py` that
differs from `HEAD`. `tools/release_check.py` (it now asks GitHub through `gh` that
`ci.yml` and both legs of `realserver.yml` passed at HEAD, and only warns without `gh`:
can a release still be tagged on a commit nothing tested?) and `MANIFEST.in` (every tool a
shipped document names is shipped). The real-server workflow now also runs weekly. `.github/workflows/realserver.yml` and `.github/seafile-ci.yml`
with every image pinned by digest (ROADMAP B8): what still floats (the runner image, the
packages `realserver_ci.py` installs), and what would a stale pin hide?

G6. Documents for the period: `CHANGELOG.md` `[0.9.23]` against `git log 4f1cdd4..HEAD`
(an entry with no commit, a commit with no entry); `UPGRADING.md` (would an operator learn
every behaviour that may now surface: the refusals of G2, the warnings of G3, the clock
bound of G4?); the release notes against the changelog; the DESIGN additions (§3.3, §4.1,
§4.3 item 7, §4.4's fence-reuse paragraph, §4.6, §5's "An empty remote is not necessarily
a new one") against the code; the new USER_GUIDE §14 rows against the messages the code
prints (quote both); AGENTS.md "Review rounds"; and `python tools/release_check.py`
(expected to warn that HEAD is untagged — report anything else it says).

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
   ref and proves the lease again after each ref write, and refuses to overwrite a ref
   that is listed but cannot be read; a fetch whose listed pack 404s
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
The last round's triage (three reports at `4f1cdd4`: deepseek-v4.1-flash, glm-5.3,
opus-5.5) is settled as follows; the ROADMAP §1.9 row each became is in brackets. For each
row marked done, say whether its named test fails when the fix is removed. For each row
marked open, say CONFIRMED, FIXED SINCE, STALE (the description no longer matches the
code — say what it does now) or MISRANKED (and why). Then spend the rest of the part on
the ROADMAP §1 rows still open (B5, B8's remainder, B10, W3, W4, W6, S1, S20, S21) and on the
questions no machine in reach could answer.

| Row | Finding, short | Disposition |
| :--- | :--- | :--- |
| 1 | the respelling compared names with `casefold`; a new `straße/` file beside `strasse/` was dropped | done [Y1] |
| 2 | a `--no-push` run still remembered six settings | done: remembers nothing [Y2] |
| 3, 11 | an LFS upload proven by its 2xx alone; a non-object JSON line killed the agent | done [Y3] |
| 4 | a listed ref read as absent on a file-host 404 or an empty body, overwritten as a new branch; the same for a lock ticket | done: refused / fails closed [Y4] |
| 5 | `mkdir_p` deleted any empty `<name> (N)` beside a folder it made | done: only a copy its mkdir made appear, and (after the triage, the maintainer's call) only of the helper's own layout folders [Y5] |
| 6–8, 17–19 | the tested-servers wording; AUTOMATION's read-only column; PAYLOAD D6/D9; SNAPSHOT §8; the Pro folder-permission row | done (docs) [Y6] |
| 9, 20 | three tools missing from the sdist; `release_check` printed a remote that does not exist | done [Y7] |
| 10 | the tail merge's scratch check missed its read-back copy | done: up to three times [Y8] |
| 12, 13 | `login` and `http://[::1]`; an unproven lease after a proven write was an error | done [Y9] |
| 14–16 | the real-server guard wrote to the server; set-up leaked on failure; the race test looked only inside the repository; the stall test asserted presence | done [Y10] |
| B1 | a vanished repository path advertised as empty; `fetch --prune` deleted every tracking ref | done after the triage [B1] |
| B3 | `--payload-delete` allowed clocks a day apart (triage Q3) | done: an hour [B3] |
| B4 | a killed link probe left 16 MiB under `payload/objects/` | done [B4] |
| B7, B9 | the gates passed a stale generated file; the fence's one-second reuse was undocumented | done [B7, B9] |
| B2 | whether a Pro `invisible` rule hides refs from a non-owner's `gc` | measured (it does); done: `gc` and `--payload-delete` refuse for a non-owner on Pro [B2] |
| B8 | floating CI images | done: pinned by digest; the weekly schedule and `release_check` requiring a green run wait for both legs green [B8] |
| ds-A1 | two `holding` tickets fall back to `order_ts` | not accepted: deterministic, the loser fails at its next fence (§3.3); measurement in the container (Q10) |
| ds-A2 | a cached queue scan lets a pack deletion run under preemption | not accepted as a defect: the deleted packs are proven redundant; documented [B9] |

Settled ground in this part: an "unknown" lease after a write that was read back is a
warning, not an error, and a reaped ticket reads as lost, not unknown; a plain `--force`
overwrites a listed-but-unreadable ref because that is what it asks for; the lease check
is deliberately not raised into acquire.

PART H — what this prompt does not ask (Phase 2, last; suggestions, not findings)
You have now read more of this project than most people will. Say what you would do
about it, and what the prompt left out. This part is an open invitation; it is judged on
how useful it is, not on how many items it has.
H1. What else should be reviewed: the areas this prompt does not cover (macOS, locale,
older Seafile releases and the size of the documentation set are known gaps), and how you
would review each.
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
   scenario | reproduced? | smallest fix. Number them G1.1…, B1…, C1…, E1…, each
   prefixed with your `<model>` name (below): `glm-5.3-G2.1`.
4. Part F: a table of row | your verdict | evidence.
5. Part H: H1 (areas), H2 (suggestions) and H3 (this prompt). End H2 with a one-line
   answer: would you tag this commit as 0.9.23, and if not, which findings block it?
6. What you checked and found sound: one line each, and for every part with no finding
   a line saying that and what you attacked there (a part with an empty table is not
   one you skipped). This is not filler — a named list of paths you attacked and could
   not break is what stops the next round from repeating that work.
7. What you could not check, and why; and, for everything else, whether you executed it
   or only read it. Include the commands you ran and the platform, git version, Python
   version and (if you used a container) Seafile edition and version you ran them on, and
   the wall-clock time the review took. Every runtime you state is one you measured; an
   estimate says so.

Save the report in the root of the tree you were given (the one exception to "leave it
untouched": `REVIEW*.md` is git-ignored there) as `REVIEW.<commit>.<model>.md`, where `<commit>`
is `git rev-parse --short HEAD` of the checkout you reviewed and `<model>` is a short
lower-case name for you (for example `REVIEW.1a2b3c4.gemini-3.8-flash.md`). Do not
commit it: `REVIEW*.md` is git-ignored on purpose. If a file of that name already
exists, it is not yours: add a suffix rather than overwrite it. Keep the part headings
and the `A1`, `B3`, `C2`, `D4`, `E5`, `F6`, `G2.1`, `H2` numbering, prefixed with your
`<model>` name: the reports of one round are merged by ID, and a report whose findings
are unnumbered, or whose IDs collide with another reviewer's, cannot be merged.
