You are an independent reviewer of `git-remote-seafile`, at the commit you were given
(`git log -1`; name it in your report). Version 0.9.27 is released (tag `v0.9.27`, commit `fc3ffd0`,
2026-11-16); the release before it is 0.9.26 (tag `v0.9.26`, commit `b23c481`). The commits after the
tag are new to every reader; `git log --oneline v0.9.26..HEAD` is the batch Part G0 covers, and the
whole project is in scope. Another model wrote the code, the tests and the specifications, and has
reviewed them itself over a dozen rounds: assume all three share blind spots. Earlier outside rounds
(the last: Fable and Gemini on `134c8af`) read Part G0, the Pro paths from the code, the 429 handling
and most of the helper, client and payload code; their findings are the rows of Part F. **What no
reader has yet read, and where most of your time goes (ORDER OF WORK says how):**
`git_remote_seafile/repair.py`, `diagnose.py`, `login.py`, `lfs_setup.py`; `tools/_snapshot/70_stage.py`
line by line; `SNAPSHOT.md` sections 1-3 and 9-16; the settle paths in `lfs.py` and
`tools/_snapshot/75_payload.py` (two copies of one rule: do they still agree?); and everything that
only happens on Seafile Professional. Two things this round cannot check and you should not claim
to: Professional behaviour (no Pro server is available; `GRS_STUB_PRO=1` is the stub's model of it,
measured on one 13.0.29 image) and any real-server answer (the container legs of `realserver.yml`
are the maintainer's). Say "unverified" and read the code instead. Read "the next tag" wherever an
older sentence says a version is unreleased. Known and not findings: `tools/release_check.py
--offline` fails its `tag` check in a clone (the tag exists) and prints `ok pushed` for a
local-directory `origin` (ROADMAP AA45). Phase 1 (Parts A and D, written before you read the code)
comes first; read Part G0 only after Phase 1 is written. A message split across two source lines may
be quoted from one line. When a mutation needs a test whose name you do not know, a whole module is
allowed for that one run (`python -m unittest tests.<module>`, not the suite); say so in section 7.


WHAT IT IS
A Git remote helper (`seafile://<library>/<path>`) that stores a repository in a Seafile
library through its REST API, with an advisory lock built from file creation and the
server's `Date` header, and a whole-tree snapshot tool (`git seafile-snapshot`) shipped
in the same wheel. Read `README.md` for the overview, `DESIGN.md` §1–§4 for how the
helper works, `SNAPSHOT.md` and `PAYLOAD.md` for the snapshot tool and its large-file
store, and `CHANGELOG.md` `[0.9.27]`, `[0.9.26]` and `[0.9.25]` for what changed recently.

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
§1.10 holds the dispositions of the last round, §1.9 those of the round before — the row
IDs there, `Z1`, `Y4`, `B8`, are the ones this prompt uses).
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
  earlier `REVIEW*.md` reports are not in it, and Phase 1 cannot read one by accident).
  **Review the commit only**: the tree you were given may hold uncommitted or untracked
  files (the maintainer's work in progress); ignore them unless this prompt names them,
  and say which commit you reviewed. HEAD may move while you work (a maintainer commits,
  a cleanup lands): keep reviewing the commit you copied, name it in the report, and say
  if a later commit touches a file you cite. Give the copy a name that does not start
  with `grs-` (use `review-<model>-`): the project's own cleanup sweeps `grs-*`
  directories, and one reviewer's copy was deleted under them mid-review.
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
  truncates a listing, it never rewrites a name you asked for, its copy and rename
  overwrite where Seafile numbers the name, its mkdir creates nothing); `GRS_STUB_PRO=1`
  makes its file reads answer the way Professional does, and its `pro_server` and `owner`
  faults make it report Professional and another owner.
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
  done. **Without Docker** (every reviewer so far), do not conclude what a real server
  does from the stub: a claim that depends on Seafile's answer (a status code, a
  listing's shape, what a folder permission hides, the owner field of a group library)
  is INFERS-SERVER-BEHAVIOUR, however well the stub reproduces it, and its smallest fix
  names the container measurement that would settle it. Label each finding REPRODUCED
  (stub, container or local repository — say which) or INFERS-SERVER-BEHAVIOUR.
- **Do not run the full suite or whole modules**: CI and the maintainer ran it at this commit
  (`ci.yml`, every OS and Python, and `realserver.yml`, CE and Pro 13.0.29; the checks page is
  `https://github.com/tkittich/git-remote-seafile/commit/<commit>`; `gh` will not see it from your
  isolated `HOME`, and that is expected). If they are not green there, say so in the verdict. Run a
  test only to reproduce a finding, run a mutation, or try a case no test covers, one named test or
  class at a time (`python -m unittest tests.<module>.<Class>.<test>`). With
  `tools/run_tests_parallel.py` use `-j 12` or `-j 24` and nothing else; `-k` matches targets (a
  class, or a test only with `--granularity test`), and a filter that matches nothing exits 2 with
  "no test targets matched", which is not a failing test. `tests.test_snapshot_payload` takes about
  ten minutes and each route-comparison scenario 20 to 40 seconds on Windows, so name the test you
  need. Several tests are timing based: if one fails for you, run it three times and say so (a test
  that fails on your machine and not on CI is itself a finding). Tests make `grs-*` directories in
  the temporary directory and remove them: do **not** sweep `grs-*` by pattern, and point `TMPDIR`
  (`TEMP`/`TMP` on Windows) at a `review-<model>-tmp` directory of your own. A Debian WSL
  distribution loses `/tmp` and its mounts between invocations: build the tree a script needs in
  the same call that uses it.
- The snapshot tool is run only on scratch trees you create, with `--no-push` or against
  a local bare repository as `--remote`. `tools/measure_pathspec_cost.py`,
  `tools/gates.py --list` and `tools/release_check.py --offline` are safe to run. Do not
  point anything at a real vault.
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
  *why* Professional answers a missing folder with 403/500 is
  inferred (its server code is closed) — the answers themselves were measured on 13.0.29;
  removing the empty copy a simultaneous `mkdir` leaves is best-effort and deliberately
  limited to the helper's own layout folders, inside the repository's own path (a copy of
  a folder a person named is named in a warning and left); the snapshot tool's payload
  client still judges a layout folder by its name alone (Z16); the source's case rule is
  judged from the first folder near the root that has a letter to flip, so a source that
  mixes case-sensitive and folding folders is judged by one of them (Z1); a FAT change
  time of 0 was simulated, not measured on a FAT volume (Z12); the owner a group-owned
  Pro library reports is taken from reviewers' reports and is not yet measured (Z22,
  Z36); the open rows of Part F are known.
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

TEN QUESTIONS THAT KEEP FINDING THINGS IN THIS PROJECT. Ask each of them of every
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
   `straße/` file beside an indexed `strasse/`, and the payload skip dropped an NFD file
   beside an NFC one (Z7). Try `ß`/`ss`, the `ﬁ` ligature, Kelvin `K`/`k` and NFC/NFD `é`
   on every such guard.
7. Whose name is it? A folder or file the tool lays out itself (`.git-lock.d`, `pack`,
   `payload/objects/ab`) may be tidied; one a person named (the repository's path, a
   branch namespace, a payload view, a source file) may sit among their own things and
   must not be. Every delete, rename or overwrite: which kind of name can reach it, and
   what stops the other kind? `mkdir_p` removed a person's empty `app (1)` until 0.9.23.
8. **Do the two filesystems agree?** The source, the vault, the scratch directory and a
   restore destination can sit on different filesystems (a WSL-created folder beside an
   NTFS vault, a FAT stick, a share, a case-sensitive APFS volume). Every rule the tool
   learned by probing one of them — case folding, Unicode form, change time, long paths,
   timestamps — is it applied to the one it was probed on? (Z1: the vault's case rule was
   applied to the source.)
9. **What is inferred from what is *not* there?** An object format read from refs when
   there are none (Z6), "first run" read from a marker that did not download (Z2), "not
   Professional" from an edition probe that failed (Z4), "unknown" from a queue that
   could not be listed (Z5). Every "none found, so ...": what else could have made it
   none?
10. **Which siblings did the last fix leave?** For each fix since the last round, name the
    search that would find the other sites of the same shape, run it, and say what it
    found — then check whether the fix's guard would catch a new site, or only the ones
    that existed when it was written.

ORDER OF WORK — follow it.
PHASE 1, specification only: Parts A and D, written down in full before you open code. A
Phase 1 finding cites a spec section and quotes its words; a `file:line` is added in Phase 2
if the code confirms it.
Write the Phase 1 findings to a file of your own (`review-<model>-phase1.md`) before you open
code: a long session can be compacted, and notes that exist only in the conversation are gone
(one reviewer could not show theirs). Do not open `git_remote_seafile/`, `tools/` or `tests/`,
and do not read a prior `REVIEW.*.md`. If you open code first, say so in the report: the design sections are
then the first thing to distrust, because the code will have anchored your attack.
Every reviewer reads the same commit: the maintainer names it and does not move it for the
round. If time runs short, finish G1, G2, G6, G8 and G4 before the other areas and say which you
did not reach; a part you did not read is not a part with no finding.
PHASE 2, code: first the unread files named in the opening (they have no part of their own:
file a finding under B, C or E by area), then Part G0, then G, F, H. You may revisit A and D;
mark what you changed and why. **Give Parts B, C and E and the unread files together at least
half of your Phase 2 time**: in earlier rounds most reviewers spent nearly all of it on the
newest commits and reported nothing in B, C or E, while the one who read them found three of
the worst defects there (Z6, Z7, Z11).
**Mutation budget.** A mutation costs a rebuild and a test run. Mutate every row of Part G0 and
the rows of Part F added since `v0.9.26` (AA37 and later); mutate an older Part F row only when
its test looks wrong on reading (it asserts a call, not the refusal), and say which you skipped.
Five well-chosen mutations that survive are worth more than fifty that are killed.

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
server editions, folder permissions and group-owned libraries; redirects; and what each
"measured" claim justifies and what it does not.

PART D — design of the snapshot tool and payload mode (Phase 1)
Find ways the design loses data, silently restores wrong data, leaks data, or does not do
what it claims. Cover: several machines on one vault; retention and every way a `gc`
could delete something needed, including the state that retention itself relies on;
clocks, timestamps and the cache that trusts them (the share-clock probe, the racy
window, the change time, the remembered setting, the default-on conversion of an
existing vault); a hostile or damaged manifest, marker or server response; filesystem
differences (Windows, macOS, Linux, network shares, cloud placeholders, case folding,
Unicode normalisation, long paths, symlinks, junctions, reparse points — and question 8);
interrupted and killed runs; very large inputs; nested repositories and embedded
repositories; secrets; and every requirement whose "how it is checked" column would not
catch a violation.

PART G0 — the batch since 0.9.26, `v0.9.26..HEAD` (Phase 2, read first, it is short)
ROADMAP §1.11 rows AA24-AA59 (AA9-AA23 are settled: Part F). For each: is the fix right, does its
test fail without it (`tools/mutate_check.py`), and the siblings. Attack in particular:
- the settle rule, which exists twice (`lfs._settle_object_name`, `75_payload._commit_object`; AA51):
  do the two copies still agree on every branch, and which interleaving of two uploaders and a
  `gc` still loses the only whole object (the server has no compare-and-delete: the race is
  narrowed, not closed, so name the window and what bounds it);
- `helper._publish_ref`, `_absent_ref_folders`, `_drop_empty_folders` (can a lost lease still reach
  a DELETE; can the `dir_exists` skip hide a folder that was made) and the held-pack rule of a push
  and of `gc` (AA37, AA50: a name already on the remote is never uploaded over, and is verified);
- `client._stream_chunks` (is any streamed read left bare; does the generator close the response
  on an early exit), the 429 handling (`_send_waiting_out_a_throttle`: rename, delete, the folder
  create and the batch copy wait; an upload POST to the file host and the library create do not:
  say which could still fail on one 429 and whether that matters), `Content-Encoding` (AA42) and the
  token's scope as a path (`url.py`, AA52);
- `walk_long` (`77_payload_restore.py`, AA24): the five source walks now run on the extended-length
  form and hand back plain paths; find a caller that compares a yielded path with a plain one
  and gets a different answer, or a walk it was not applied to (`git grep -n 'os.walk\|scandir'
  tools/_snapshot`). It could only be tested on a machine with long paths on;
- the advertised-refs record and a listed-but-unreadable ref (AA39, AA53), `packs.fetch_pack_artifact`'s
  bounded fallback, `93_cli_args.py` (the parser moved out of `99_cli.py`: anything evaluated at
  import order?), usage errors exiting 2 (AA55, with `--json`), the retention mark a reused payload
  object clears (AA59).


PART G — standing attack areas (Phase 2, after G0)
Each area was changed in a day, from a reviewer's report, with a test watched failing without it.
For each fix you read: does its test fail without it (`python tools/mutate_check.py <file> --old ...
--new ... <tests>` deletes the guard, rebuilds the snapshot tool for a part and restores the file),
does it cover the siblings of the reported path (question 10), and did it move the failure somewhere
quieter? Number findings G1.1, G2.1 … by area. The ROADMAP row of each (§1.10 Z-rows, §1.11 AA-rows)
holds the history; this list holds only what to attack.
G1. Names, filesystems and the index: the source's case probe and the vault's `core.ignorecase`
(`30_vault.source_folds_case`), payload files kept out of git by exact path and by Unicode form only
when both names are one file (`71_embedded.payload_skip_matcher`), the respelling's disk check
(`69_prewrite.spell_like_index`). Questions 6 and 8: a source whose root folds and whose subfolders do
not, no letter near the root, `--dry-run` and `--status` (which set no rule), a destination on another
filesystem.
G2. What the server says it holds versus what it serves, and who may see it: one reader for every
state file whose absence decides something (`client.read_listed_text`, `is_listed_file`; the test
`TestAStateFileTheFolderListsIsNeverAbsent` lists every raw read left with its reason: is each true?);
the Pro owner guard (`hidden_history_risk`, `is_group_owner`: the owner value of a group library was
never measured) and every other operation that reads "what is there" from a listing an account sees
only part of; the lease after a write (`lock.verify_ownership`, `_lapsed_if_unknown`: an interleaving
where "unknown" hides a takeover, or a held lease reads as lost); redirects (`client._ScopedSession`,
`_xfer`: what still follows one, and what can it carry?).
G3. Seafile's numbered copies (`client.mkdir_p`, `_is_layout_dir`, `layout_root`;
`79_payload_view._place_object`): question 7, which callers set `layout_root`, which do not, and what
can a person name that a caller without it treats as layout?
G4. The snapshot tool's writes and caches: `--no-push` writes nothing (find every other writer); a run
on another payload rule than the last snapshot's warns; FAT change time 0, a zero probe `mtime`, an
unreadable server clock; the caches (config per directory signature, the index listing, the tip, a
manifest per commit id): can one answer for a state that changed under it?
G5. The tiers and the tooling: `tools/gates.py --changed` builds its map from imports (what does a
regex over text miss or over-count: a dynamic import, a comment, a test that runs the helper as a
process); `tools/release_check.py`; `MANIFEST.in` and its test; the CI images and what still floats.
G6. FAT, exFAT and a restore destination (`SNAPSHOT.md` section 5 is the spec; every table row is a
claim): `destination_name_problem` probes the folder with four names and applies only the rules it
enforces (`91_restore_names.py`). Attack the probe: rules that differ by folder, a probe name that
collides with a real file, an unwritable destination, a listing that fails halfway, Unicode that
normalises on the way, a name that is a prefix of another. With a Linux box, restore onto a loop
device (`CONTRIBUTING.md`, "Testing on FAT and exFAT") and say what you could not do.
G7. Locks, copies and the pack cap: the settlement window follows this client's own ticket upload,
capped at ten seconds (`lock.acquire`: can a slow upload hold a push longer than `seafile.locktimeout`
allows?); a holder deletes the conflict copies of its own ticket (`lock._delete_own_tickets`: can it
delete another client's copy, or leave one that blocks for a lease?); the pack cap a 413 teaches is kept
per remote (`config.remote_cap_key`: which readers still call `RemoteConfig.load()` without a name; a
remote name with a slash or a dot).
G8. What a fetch may delete and what a delete may reach: the advertised-refs record
(`git_util.advertised_record_path`: a remote emptied on purpose, a repository re-created at the same
URL, two clones sharing a git dir, a worktree, a record written for the wrong repository); the prune
of emptied ref folders is skipped for an account that may not see every ref (another recursive delete
of a folder the account did not create? what does the empty folder left behind do to the refs
listing?).
G9. The tests themselves: the stub's `dir_fail_nth` and `conflict_copy_for`; the virtual clock of the
hashing search (can it pass while the real controller is wrong?); prose numbers tied to constants
(which are not?); the `MEASURED` lines of `test_realserver.py` (do they measure what the docstring
says?); `LargeListingTests` settles "complete at 5,100" and no further; every `sleep` and elapsed-time
assertion in `tests/` (find one that can still flake); `test_gc` patching `Path.glob` for `pack-*`
only; the locale legs of `ci.yml` (is the second really an ASCII-only process, and what would a third
catch?).
G10. Documents against code: `CHANGELOG.md` against `git log` (an entry with no commit, a commit with
no entry); `UPGRADING.md` (would an operator learn every behaviour that may now surface: new
refusals, new warnings, exit codes?); the release notes against the changelog; DESIGN, PAYLOAD,
SNAPSHOT, USER_GUIDE, CONTRIBUTING and AGENTS.md against the code (quote both); and `python
tools/release_check.py --offline` (expected: every check `ok` except the commit not being on
`origin/main`, the two CI legs, and a dirty-tree line if your copy has local files; report anything
else). The typed git timeout `SEAFILE_SNAPSHOT_GIT_TIMEOUT` (`20_gitrun.git_timeout`): every place
that used `GIT_TIMEOUT` as a number, a value that is huge, `inf` or `nan`, and children that inherit
`SEAFILE_GIT_TIMEOUT` instead; the restore's listing of the destination on a 200,000-file tree.

PART B — code of the helper (Phase 2)
Scope: `git_remote_seafile/{helper,client,lock,gc,packs,refs,git_util,safety,url,config,
cli,seafile_paths,units,repair,diagnose,login,lfs,lfs_setup,library_setup,settings_report,
sqlite_read}.py`, `tools/seafile_doctor.py` and their tests (what Part G names is G, not
here). In priority order:
1. Data loss or corruption on push, fetch, merge and `gc`: any ordering in which a
   crash, timeout, second writer or retry leaves a ref pointing at a missing object, or
   deletes something still referenced. Note the shipped shape: `gc` mirrors refs as one
   `packed-refs` file, re-reads the refs before deleting and verifies the uploaded pack's
   SHA-256; a push re-reads refs under the lock, proves its packs landed, fences every
   ref and proves the lease again after each ref write, and refuses to overwrite a ref
   that is listed but cannot be read; a fetch whose listed pack 404s
   re-lists the pack directory once and retries, and still fails loudly if the requested
   tips are then absent; lock tickets carry a per-install id and the dead-PID reclaim
   requires it to match; `repair` restores refs whose objects or history no remote pack
   carries, without touching a ref; `--force-with-lease` is honoured.
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
exhaust memory, disk or time; leak the token or a body to another host or path
(redirects, URL parsing, `server=` overrides, cross-host `Location`, transfer links);
report success for data it did not verify; make the helper read a refusal as an absence
(the issue 6 shape: which other status could a server send to get a ref treated as
missing?); or persist a setting that weakens a later run (the per-remote pack cap a 413 teaches is one). Give the exact bytes or URL, the line that mishandles them, and the shortest fix.
Skip anything that needs the attacker to control the user's machine already.

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
   that gets in anyway, or a path that is neither in git nor in the manifest; a
   repository that changes while it is walked.
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
Every earlier row (ROADMAP §1.10 Z1-Z50, B12, W4, T3, and AA1-AA19 of §1.11) is settled with code and
a test; the ROADMAP row names the test. Mutate one of them only when its test looks wrong on reading
(it asserts a call, not the refusal), and say which. For each row below marked done **with code**, say
whether its named test fails when the fix is removed; for **read only** rows, say whether the document
says what the code does; for each row marked open, say CONFIRMED, FIXED SINCE, STALE (say what the code
does now) or MISRANKED. The **Test** column is best effort: a named test that does not exercise its
row is itself a finding. Then spend the rest of the part on the open rows (Z39's remainder, Z43 option
C, a payload file over 4 GiB on exFAT through a real server and the Windows remount, Seafile 11 and 12,
an HDD or slow-link run) and on the questions no machine in reach could answer.

| Row | Finding, short | Disposition | Test that fails without the fix |
| :--- | :--- | :--- | :--- |
| Z36 | real-server measurements | partly: `MEASURED` lines ran; group owner, 5,000-entry listing and an admin's view are open | `test_realserver.py`: `test_a_library_shared_to_a_group_works_for_a_member`; `test_realserver.py`: `test_measure_the_mtime_a_listing_reports_for_an_upload`; `test_realserver.py`: `LargeListingTests` |
| Z43 | the payload cache re-reads everything after a FAT remount | option A done (a Note); option C open | `test_snapshot_payload.py`: `test_a_file_hashed_again_only_for_its_inode_is_named_as_that` |
| Z22 | a group-owned library's owner value | **corrected**: `share` is every non-owner's value, so it identifies nothing; the discriminator is unmeasured | `test_client.py`: `test_the_hidden_history_risk_is_named_only_on_pro_for_a_non_owner`; `test_realserver.py`: `test_a_library_shared_to_a_group_works_for_a_member`; `test_realserver.py`: `test_a_group_owned_library_works_for_a_member_and_the_owner` |
| AA22 | plain `lstat`/`open` on source paths past 260 characters on Windows | done with a guard test that scans every snapshot part for a source path not passed through `extended_path`; try a site shape the pattern misses | `test_snapshot_tool.py`: `test_the_respelling_check_uses_the_long_path_form`; `test_snapshot_tool.py`: `test_no_part_stats_or_opens_a_source_path_in_its_plain_form` |
| AA21 | gemini's A1-A3, D3, D4, G3.1, B1, B2, C1, C2, G8.1, E1; "an empty ref leaf is empty"; exit 2 for an incomplete restore | declined with reasons in ROADMAP §1.11 and the triage; re-report only by showing the reason is wrong | - (read only, declined or measured: no test) |
| AA23, AA32, AA33 | 429 and retry handling: the ref walk's worker back-off, `Retry-After` capped at 10 s, a write or a POST never re-sent once it may have reached the server, `other=0` (no re-send after an SSL failure), a connection error that names its cause (`SeafileConnectionError`) | done with code and a test each (0.9.26). the rename, the delete, the folder create and the batch copy now wait out a 429 (the stub-level tests are `test_client_retry_cap.py`); an upload POST to the file host and a payload or snapshot worker's own throttle reading are not covered: say which can still fail on one 429, with the input | `test_client_retry_cap.py`: `TestAFolderCreateAndACopyAreSentAgainAfterAThrottle`; `test_realserver.py`: `test_a_listing_holds_every_subfolder_of_a_large_folder`; `test_refs_throttle.py` |
| AA24 | Windows long-path walks with long paths off | done with code and a test on a machine with long paths ON: `walk_long` walks the extended-length form and hands back plain paths (the plain walk's failure was never shown here; the test shows the helper's contract). Try a walk it was not applied to, and a caller that compares its paths with plain ones | `test_snapshot_walk_long.py`: `TestWalkLong` |
| AA27, AA31 | a dropped connection is a `SeafileAPIError`; the advertised-refs record of a linked worktree; the Pro owner refusal for `--payload-delete` before the history download | done with code and a test each; try a sibling | `test_client_connection_error.py`; `test_e2e_failed_nested_push.py`: `test_an_error_that_is_not_a_seafile_error_still_triggers_the_cleanup` |
| AA34-AA36, AA31's rest | fable2 C1 (a body cut mid-stream), C3 (the pack fallback's bound), the surviving mutations, G0.3(b)(c) (the failed-write folder cleanup), `99_cli.py`'s size | done with code in the commits after `v0.9.26` (a test each, mutations KILLED); try a sibling: another streamed read, another `delete_entry` without a lease check (both pack cleanups of a push are fenced since AA37) | `test_client_retry_cap.py`: `test_a_long_retry_after_is_not_slept_through_and_is_said_once`; `test_e2e_failed_nested_push.py`: `test_an_error_that_is_not_a_seafile_error_still_triggers_the_cleanup`; `test_pack_split.py`: `test_a_name_the_remote_held_before_is_not_removed_by_a_refused_attempt` (+1 more: `git grep -n AA34 tests`) |
| AA37-AA49 | held packs (a held name is skipped), the lease fence of both own-upload cleanups, an unreadable known ref, the Content-Encoding refusal, the throttled `DELETE`, the payload and gc retention reports | done with code and a test each in the commits after `9fe723f`; review 134c8af found a regression in the first (AA50) | `test_e2e_stale_clone_pack.py`: `test_a_pack_the_remote_holds_is_not_sent_again`; `test_gc.py`: `TestAMergedPackNamedLikeAHeldOneIsNotUploadedOver`; `test_pack_split.py`: `test_a_pack_the_remote_already_holds_is_not_uploaded_again` (+8 more: `git grep -n AA37 tests`) |
| AA50-AA59 | review 134c8af: a held pack verified by size (AA50), the LFS and payload settle races and a short LFS object (AA51), the token scope as a path, `http` never replacing `https`, IPv6 brackets, `:secret@` masking (AA52), a listed ref with a bad body and a vanished `refs/heads` (AA53), a non-ASCII case twin and a name over 255 bytes (AA54), the `--no-push` source, `_run_live`'s pipe, exit 2 and `--json` on a bad command line (AA55), small ones (AA56, AA57), a reused payload object clearing its retention mark (AA59) | done with code and a test each in the commits after `134c8af`; try a sibling of each: another place a size or a name is trusted without being read back, another URL parse that drops brackets or the scheme, another `fnmatch` or `casefold` that differs by platform, another stdout pipe read after the child exits | `test_e2e_stale_clone_pack.py`: `test_a_held_pack_at_another_size_is_uploaded_over_not_trusted`; `test_lfs.py`: `test_a_short_local_object_never_replaces_a_good_remote_one`; `test_url.py`: `test_an_ipv6_literal_keeps_its_brackets` |
| S1 | an unpaged listing may be short | measured complete at 5,100 on CE and Pro; read the claim, and the assertion, adversarially | - (read only, declined or measured: no test) |
| Z39 | locale and environment legs | partly done: the tr_TR, ASCII-only and C-locale legs run in CI (`ci_locale_legs`) and a macOS leg exists; antivirus, Task Scheduler as SYSTEM and a subpath behind nginx remain, and cannot be read from code | `test_ci_locale_legs.py`; `test_git_util.py`: `test_run_git`; `test_snapshot_tool.py`: `test_the_probe_names_its_file_in_bytes` |

Settled ground in this part: an "unknown" lease after a write that was read back is a
warning while the lease is within its time by this machine's clock, and lost past it; a
plain `--force` overwrites a listed-but-unreadable ref because that is what it asks for;
the lease check is deliberately not raised into acquire; a missing branch, a recreated
path and a differing payload rule warn rather than refuse, because each has a legitimate
cause the tool cannot tell from the harmful one; skipping the prune for a Pro non-owner
(gemini-4-argon's proposal for Z29, first declined) is accepted since it was measured to
destroy hidden refs: the empty folder left behind reads as a failed listing, which is loud
and harmless; reaping another client's ticket copies is not accepted (a live holder's newer
refresh can be the copy); a `git fetch --prune-tags` against an empty remote deletes local
tags by git's own rule, which the helper does not stop; a push verifies packs by size only (T3, with
reasons in DESIGN); the empty folder a Pro non-owner's delete leaves is loud on purpose (Z49);
the leftover `index.lock` after a power cut is not removed automatically (Z46); a listing is
complete at 5,100 entries on the two editions measured (S1), so "the listing may be short" needs an
experiment above that or a folder of directories, not an argument.

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
   answer: which findings, if any, should block the next tag, and which would you ship in a
   point release instead?
6. What you checked and found sound: one line each, and for every part with no finding
   a line saying that and what you attacked there (a part with an empty table is not
   one you skipped). This is not filler — a named list of paths you attacked and could
   not break is what stops the next round from repeating that work.
7. What you could not check, and why; and, for everything else, whether you executed it
   or only read it. Include the commands you ran and the platform, git version, Python
   version and (if you used a container) Seafile edition and version you ran them on, and
   the wall-clock time the review took. Every runtime you state is one you measured; an
   estimate says so.

Write as long a report as the findings need, but keep the detail where it can be rerun:
each reproduction and mutation is a script you keep (in your `review-<model>-` directory,
named in section 7), and the report gives the finding, the line of evidence that matters
(the output that shows it), and the script's name — not the script's whole output.

Save the report in the root of the tree you were given (the one exception to "leave it
untouched": `REVIEW*.md` is git-ignored there) as `REVIEW.<commit>.<model>.md`, where `<commit>`
is `git rev-parse --short HEAD` of the checkout you reviewed and `<model>` is a short
lower-case name for you (for example `REVIEW.1a2b3c4.gemini-3.8-flash.md`). Do not
commit it: `REVIEW*.md` is git-ignored on purpose. If a file of that name already
exists, it is not yours: add a suffix rather than overwrite it. Keep the part headings
and the `A1`, `B3`, `C2`, `D4`, `E5`, `F6`, `G2.1`, `H2` numbering, prefixed with your
`<model>` name: the reports of one round are merged by ID, and a report whose findings
are unnumbered, or whose IDs collide with another reviewer's, cannot be merged.
