You are an independent reviewer of `git-remote-seafile`, at the commit you were given
(`git log -1`). **Addendum for the 0.9.26 round:** version 0.9.25 is released (tag `v0.9.25`,
commit `3ca9db3`); the commits you review are `git log --oneline v0.9.25..HEAD` (the 0.9.26 batch,
Part G0 below), and the whole project is in scope as before. The statements below about 0.9.25
being unreleased describe the round it was written for; read them as history. Two things this
round cannot check and you should not claim to: Seafile Professional behaviour (no Pro server
is available; `GRS_STUB_PRO=1` is the stub's model of it, measured only on one 13.0.29 image)
and any real-server answer (the container legs of `realserver.yml` are the maintainer's). Say
"unverified" and read the code instead. The Pro-only paths (folder permissions, group-owned
libraries, the 403/500 answers for a missing folder, `hidden_history_risk`) have had the least
independent reading: give them a pass of their own.

This was written for an earlier round, whose opening follows. **This is a pre-release round**: version 0.9.25 is prepared at that commit
(`__version__`, `CHANGELOG.md` `[0.9.25]`, `.github/release-notes/v0.9.25.md`, `UPGRADING.md`)
and **not yet tagged**; your findings are folded into 0.9.25 before it ships, or recorded for the
next one. The previous release is 0.9.24 (tag `v0.9.24`, commit `b14c4f7`, 2026-11-09), and
`git log --oneline v0.9.24..HEAD` is what the round before it never saw. Another model wrote the
code, the tests and the specifications, and has reviewed them itself over a dozen rounds: assume
all three share blind spots. The last outside review was a second pre-release round at commit
`06cc623` (2026-11-07, four reviewers); its triage is settled and is listed in Part F as
dispositions. **Everything between `06cc623` and now has had no independent reader**: the
twenty-six fixes that round produced (ROADMAP §1.10, Z1-Z26), the 0.9.23 release, the whole of
0.9.24 (FAT32 and exFAT measured on Linux loop devices and on real Windows drives: a restore, a
payload cache and a lock were changed by what they showed, Z28-Z48) and the whole of 0.9.25 (a
data-loss fix from the first real Seafile Professional measurements, Z22, Z27, Z29, Z47-Z50, W4, and
a week of test hardening). `SNAPSHOT.md` section 5 holds the measured FAT tables, which are claims
you can check. See Part G, which is where most of your time should go, but not all of it (see ORDER
OF WORK). Parts A-E cover everything older.

WHAT IT IS
A Git remote helper (`seafile://<library>/<path>`) that stores a repository in a Seafile
library through its REST API, with an advisory lock built from file creation and the
server's `Date` header, and a whole-tree snapshot tool (`git seafile-snapshot`) shipped
in the same wheel. Read `README.md` for the overview, `DESIGN.md` §1–§4 for how the
helper works, `SNAPSHOT.md` and `PAYLOAD.md` for the snapshot tool and its large-file
store, and `CHANGELOG.md` `[0.9.25]`, `[0.9.24]` and `[0.9.23]` for what changed since the last review.

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
- **The full suite is run at this commit by CI and the maintainer — do not run it
  again**, and do not run whole modules to "check that the tests pass". Several reviewers
  work on this commit at once, and a full run is twelve minutes or more each that finds
  nothing new. The repository is public: the CI runs for your commit are on its checks
  page, `https://github.com/tkittich/git-remote-seafile/commit/<commit>` (`ci.yml`, every
  OS and Python, and `realserver.yml`, CE and Pro 13.0.29), and in the Actions tab; `gh`
  will not see them from your isolated `HOME`, and that is expected. If they are not
  green there, say so in the verdict. Run a test only to do something those runs could
  not: reproduce a finding, run a mutation (delete a guard, watch its test), or try a
  case no test covers — one named test or class at a time (`python -m unittest
  tests.<module>.<Class>.<test>`). With `tools/run_tests_parallel.py`, `-k` matches
  *targets*: a class, or a single test only with `--granularity test`; a filter that
  matches nothing exits 2 with "no test targets matched", which is not a failing test —
  one reviewer read four such runs as killed mutations. `tests.test_snapshot_payload`
  takes about ten minutes, and each route-comparison scenario (`ExoticReplacementTests`,
  `SpecialFileAndNameTests`, one test per scenario) 20 to 40 seconds on Windows, so name
  the test you need. Use `-j 12` or `-j 24` with `tools/run_tests_parallel.py`, nothing
  else. On a slow or antivirus-scanned disk use the smaller sizes of
  `tools/measure_pathspec_cost.py` (1000 to 5000).
  A Debian WSL distribution loses `/tmp` and its mounts between invocations: build the tree a
script needs in the same call that uses it. Several tests are timing based (`tests/test_snapshot_climb.py`, the staging ramp
  tests): if one fails for you, run it three times and say so; a test that fails on
  your machine and not on CI is itself a finding. Tests make `grs-*`
  directories in the temporary directory and remove them; do **not** sweep `grs-*` by
  pattern, because another reviewer's tests may be running in them. Point `TMPDIR` (and
  `TEMP`/`TMP` on Windows) at a `review-<model>-tmp` directory of your own for every run,
  and delete only that.
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
  Z36); the open rows Z27–Z41 are known.
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
round (the 0.9.25 round was read at two commits one fix apart, and three reports described a
module the fourth saw changed). If time runs short, finish G1, G2, G7, G9 and G4 before the
other sub-areas and say which you did not reach; a part you did not read is not a part with no
finding.
PHASE 2, code: Part G first (it is the least reviewed), then B, C, E, F, then H. You
may revisit A and D; mark what you changed and why. **Give Parts B, C and E together at
least a third of your Phase 2 time**: last round three of four reviewers spent nearly all
of it on G and reported no finding in B, C or E, while the fourth found three of the
round's worst defects there (Z6, Z7, Z11).

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

PART G0 — the 0.9.26 batch, `v0.9.25..HEAD` (Phase 2, read first, it is short)
ROADMAP §1.11 rows AA9-AA19, AA22, AA23. For each: is the fix right, does its test fail without it
(`tools/mutate_check.py`), and the siblings. Specifically: AA10 (`helper._publish_ref`,
`_absent_ref_folders`, `_drop_empty_folders`: can it delete a folder that holds something, race a
writer, or run when the write actually landed?); AA11 (the wall clock beside the monotonic one in
`lock._lapsed_if_unknown`: can a clock step make a healthy lease read as lost?); AA13
(`git_util.tracked_remote_names` for a mirror); AA16 (`tools/gates.py` `_with_test_importers`);
AA22 (`test_no_part_stats_or_opens_a_source_path_in_its_plain_form`: find a source-path
`stat`/`open` shape its pattern misses); the stub's new `explicit_dirs` model in
`tests/e2e_harness.py` (does it model Seafile's folders honestly?); and the new real-server tests
in `tests/test_realserver.py` (can any of them fail for a reason that is not a finding, or pass
on a wrong answer?). AA23 is an open measurement: read the client's retry and throttle handling
(`client._mount_retries`-style adapter, POST never retried) and say what a 429 does to a refs
walk, a push and `gc`.

PART G — everything since `06cc623` (Phase 2, first and longest)
The map: `git log --oneline 06cc623..HEAD`, `git diff --stat 06cc623 HEAD` (the whole
tree: code, tests, `.github`, `MANIFEST.in`, `pyproject.toml` and the documents),
`ROADMAP.md` §1.10 (rows Z1-Z50, and B12 in §1.9) and `CHANGELOG.md` `[0.9.25]`,
`[0.9.24]` and `[0.9.23]`. Each
change was made in a day, with a test that was watched failing without it, mostly from
a reviewer's report. For every one: does its test fail without it (delete it and run: `python tools/mutate_check.py <file> --old
... --new ... <tests>` does it, rebuilds the snapshot tool for a part and restores the file),
does it cover the siblings of the reported path (question 10), and did it move the
failure somewhere quieter? Number findings G1…, G2… by the sub-area below, e.g. G2.1.

G1. Names, filesystems and the index. The source is probed for case folding and the
vault's `core.ignorecase` set to match (`30_vault.source_folds_case`, `ensure_case_rule`,
Z1); payload files are kept out of git by exact path, and by Unicode form only when both
names are one file (`71_embedded.payload_skip_matcher`, Z7); the respelling's disk check
uses the long-path form (`69_prewrite.spell_like_index`, Z13). Attack them with question
6 and question 8: a source whose root folds and whose subfolders do not, a source with no
letter near the root, `--dry-run` and `--status` (which do not set the rule), a restore
destination on another filesystem.

G2. What the server says it holds versus what it serves, and who may see it.
- One reader for every state file whose absence decides something
  (`client.read_listed_text`/`read_listed_bytes`, `is_listed_file`, Z2): the payload's
  `gc-seen.json` and `gc-pending.json`, `HEAD` on a push, `unlock --force`'s ticket; a
  ref whose folder listing fails is refused (`helper._listed_as_file`, Z3); and a test
  (`TestAStateFileTheFolderListsIsNeverAbsent`) lists every raw `get_file_text` /
  `get_file_bytes` read left, each with the reason its "absent" is safe. Is each reason
  true? Which reads of a *listing* decide "absent" (question 9)?
- The Pro owner guard: an edition the server will not name is a risk (`hidden_history_risk`,
  Z4); a group-owned library is meant to be named as one (`is_group_owner`, `owner_advice`, Z22; the owner value it matches was never measured, and `share` turned out to be every non-owner's); a
  non-owner's fetch warns when a tracked branch is missing (`_warn_if_tracked_refs_may_be_hidden`,
  `git_util.tracked_remote_names`, Z25). Which other operation reads "what is there" from a
  listing an account may see only part of (`_prune_empty_ref_parents` was one, Z29, now skipped for a non-owner: find the next)?
- The lease after a write (`lock.verify_ownership`, `_lapsed_if_unknown`, Z5): a failed
  queue scan now reads the own ticket, and "unknown" past the lease by this machine's
  monotonic clock is lost. Is there still an interleaving in which "unknown" hides a
  takeover, or one in which a held lease is now reported lost?
- A remote with no refs takes its object format from its pack names (`_pack_hash_width`,
  Z6); a push that recreates a tracked path warns (Z24); `repair` walks each present tip's
  history (`_incomplete_histories`, Z21).
- Redirects (`client._ScopedSession`, `_xfer`, Z11): the token is dropped outside the
  server's URL, and a transfer that is not a GET follows no redirect. What still follows
  one (a GET download, `login`, a `doctor` probe), and what can it carry?

G3. Seafile's numbered copies (`client.mkdir_p`, `_is_layout_dir`, `layout_root`, Z16;
`79_payload_view._place_object`, Z23). Ask question 7: which callers set `layout_root`,
which do not, and what can a person name that a caller without it treats as layout?

G4. The snapshot tool: `--no-push` writes nothing (`99_cli.py`, Z8 — find every other
writer); `--status` uses the capture's walk (`82_status.py`, Z18); a run on another payload
rule than the last snapshot's warns (`78_payload_cli.py`, Z26 — and the two established
vaults it is meant for); a FAT change time of 0, a zero probe `mtime`, an unreadable
server clock (Z12, Z14, Z15); PAYLOAD R4 and R7's named tests (Z19); and B12's caches
(config per directory signature, the index listing, the tip, a manifest per commit id):
can a cache answer for a state that changed under it?

G5. The tiers and the tooling. `tools/gates.py --changed` builds its map from the imports
(`_package_graph`, `_reached_by`, `_importers`, Z9): what does a regex over text miss or
over-count (a dynamic import, a name in a comment, a test that runs the helper as a
process)? `tools/release_check.py` (Z10, Z42): it fails without GitHub unless
`--offline`, requires `origin/main`, and warns while a triage in the root has an open
item. `MANIFEST.in` and its test (Z17). The CI images and what still floats (Z37: a monthly unpinned run now, gating nothing).

G7. FAT, exFAT and a restore destination (0.9.24; `SNAPSHOT.md` section 5 is the spec, and
every row of its two tables is a claim). `destination_name_problem` / `probed_name_problem`
probe the folder restored into with four names and apply only the rules it enforces
(`91_restore_names.py`, Z44); `classify_missing_names` / `report_names_not_restored_as_named`
list the destination and compare it, in NFC, with the snapshot's names (Z45); a payload
re-read caused only by an inode change is named (`74_payload_manifest.build_manifest`,
Z43 option A). Attack the probe: a destination whose rules differ by *folder* (an ext4 mount
under a FAT root, a case-sensitive folder on NTFS), a probe name that collides with a real
file, a destination that cannot be written, a listing that fails halfway, Unicode that
normalises on the way (HFS+, APFS), a name that is a prefix of another. If you have a Linux
box, make a loop device (`CONTRIBUTING.md`, "Testing on FAT and exFAT") and restore onto it;
say what you could not do.

G8. Locks, copies and the pack cap. The settlement window follows this client's own ticket
upload, capped at ten seconds (`lock.acquire`, Z28): can a slow upload now hold a push
longer than `seafile.locktimeout` allows, or shorten the guard it exists for? A holder
deletes the conflict copies of its own ticket at release, only the ones a scan saw plus one
listing when it saw any (`lock._delete_own_tickets`, Z47): can it delete a copy that is
another client's, or leave one that blocks others for a lease? An LFS object goes up as
`<oid>.part-<token>` and is renamed, a copy beside an object of the right size removed and a
wrong-size object replaced (`lfs._settle_object_name`, Z30): what if two machines rename at
once, or the listing after the rename is stale? The pack cap a 413 teaches is kept in
`remote.<name>.seafilegcmaxpackmib` and read before `seafile.gcmaxpackmib`
(`config.remote_cap_key`, `RemoteConfig.load(remote_name)`, Z31): which readers still call
`RemoteConfig.load()` without a name, and what does a remote whose name contains a slash or
a dot do to the key?

G9. What a fetch may delete, and what a delete may reach. The helper remembers what each
remote advertised (`<git-dir>/seafile-advertised/<remote>`, per URL, `git_util.
write_advertised_refs`/`read_advertised_refs`, Z27) and refuses an empty advertisement for a
remote that was not empty: find a shape that is refused wrongly (a remote emptied on
purpose, a repository re-created at the same URL, two clones sharing a git dir, a worktree)
and a shape that still prunes. The record is read and written only where git set `GIT_DIR`
(`git_util.advertised_record_path`): a first version resolved the git directory from the working
directory and wrote a fake record into the project's own `.git` from unit tests. Is there another
way a record is written or read for the wrong repository (a helper run by hand, a script, `GIT_DIR`
pointing at a different repository than the one fetching)? The prune of emptied ref folders is skipped for an account
that may not see every ref (`helper._prune_empty_ref_parents`, `_risk_of_unseen_refs`, Z29,
measured on Pro: a non-owner's recursive `DELETE` took a hidden child's contents): is there
another recursive delete of a folder the account did not create, and what does the empty
folder left behind do to the refs listing (Z49: a fetch of the repository stops at it, naming it,
until the owner removes it or a branch is pushed there; is that failure reachable some other way,
and is the Note's promise of it true)? A report-only `--payload-gc` by a Pro non-owner
says its count may be too high (`72_payload_guards._require_whole_history`, Z34).

G10. The tests and the tiers added in 0.9.24-0.9.25. The stub fails exactly the n-th listing
(`dir_fail_nth`, Z38) and leaves `(SFConflict ...)` copies (`conflict_copy_for`, Z33/X7); the
hashing search's serialising-disk test runs on a virtual clock (`run_hashing(clock=...)`,
Z48): is the virtual clock itself a way for the test to pass while the real controller is
wrong? Prose numbers are read back from their constants (`TestProseNumbersMatchTheirConstants`,
Z35): which numbers in the specs are still not tied to one? `release_check` warns when a
green run is followed by a failed re-run (Z41). `.github/workflows/realserver-latest.yml`
(Z37) runs the unpinned images monthly and gates nothing: could it gate something by
accident? The `MEASURED` lines in `tests/test_realserver.py` (Z22, Z29, Z36) never fail: do
they measure what their docstrings say? `LargeListingTests` (S1) writes 5,100 files and asserts one
`list_dir` returns every name; it passed on CE and Pro, which settles "complete at 5,100" and no
further: what would a listing capped at 10,000, or a folder of directories, show? Z50 audited every
`sleep` and elapsed-time assertion in `tests/` (the audit row in ROADMAP lists the verdicts): find one
it classified as safe that can still flake, or one it missed. `test_gc` patches `Path.glob` only for
the `pack-*` patterns (`_glob_of_staged_packs`): does that still mask an unrelated glob inside the
code under test? `ci.yml` has locale legs (`tr_TR.UTF-8`, and `LC_ALL=C` with `PYTHONUTF8=0` and
`PYTHONCOERCECLOCALE=0`, Z39): is the second one really an ASCII-only process, and what would a
third leg catch that these do not?

G11. The typed git timeout and what else changed in the snapshot tool in 0.9.25.
`SEAFILE_SNAPSHOT_GIT_TIMEOUT` raises the wall-clock bound on a git child that has no progress to
measure (`20_gitrun.git_timeout`, W4): every place that used `GIT_TIMEOUT` as a number (`git grep
GIT_TIMEOUT tools/_snapshot`), the status path that ignores it by design, a value that is huge, `inf`
or `nan`, and the children the tool starts that inherit `SEAFILE_GIT_TIMEOUT` instead. The payload
cache's note for a re-read caused only by a changed inode (`74_payload_manifest.build_manifest`,
Z43): can the count be wrong when the old entry has no `ino`? The restore's listing of the
destination (`91_restore_names`, Z45) runs on every restore: what does it cost on a 200,000-file
tree, and does `os.walk(onerror=...)` fail a restore it should not?

G6. Documents for the period: `CHANGELOG.md` `[0.9.25]` and `[Unreleased]` (0.9.26) against
`git log v0.9.25..HEAD`
(an entry with no commit, a commit with no entry); `UPGRADING.md` (would an operator learn
every behaviour that may now surface: the new refusals, the new warnings, the case rule's
first run on a case-sensitive source?); the release notes against the changelog; the
DESIGN, PAYLOAD, SNAPSHOT and USER_GUIDE additions against the code (quote both);
AGENTS.md "Review rounds"; and `python tools/release_check.py --offline` (expected: every
check `ok` except the commit not being on `origin/main` (a warning) and the two CI legs, which
are warnings under `--offline` only when `gh` cannot answer: with `gh` signed in and the commit
not on GitHub, `--offline` still prints `[FAIL] no run at <sha>` for both legs, which is that
fact and not a defect — and the dirty-tree line if your copy has local files; report
anything else it says). (0.9.25 was released after that round: the tag `v0.9.25` exists; for the 0.9.26 round, `[Unreleased]` in CHANGELOG.md is the entry to read, and the version bump is not yet made.) The guides and specs that 0.9.24 changed to
match what was measured (`PAYLOAD.md` and the `USER_GUIDE.md` limits row on FAT,
`SNAPSHOT_GUIDE.md` on restore destinations, `DESIGN.md` on the LFS rename, the ticket
copies and the remembered advertisement, `CONTRIBUTING.md`, `AGENTS.md`) against the code.

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
missing?); or persist a setting that weakens a later run (`seafile.gcmaxpackmib` is open
as Z31). Give the exact bytes or URL, the line that mishandles them, and the shortest fix.
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
The last round's triage (four reports at `06cc623`: deepseek-v4.1-flash, gemini-4-argon,
glm-5.3, opus-5.5) is settled as follows, by ROADMAP §1.10 row. For each row marked done
**with code**, say whether its named test fails when the fix is removed. A row marked
**read only** is a document or a test: read it and say whether it says what the code
does; do not mutate it. For each row marked open, say CONFIRMED, FIXED SINCE, STALE (the
description no longer matches the code — say what it does now) or MISRANKED (and why).
Then spend the rest of the part on the open rows (Z39's remainder, B10, W3, W6's remainder: a payload
file over 4 GiB on exFAT through a real server and the Windows remount, S21, Z43 option C) and on the questions no machine in reach could answer.

| Row | Finding, short | Disposition |
| :--- | :--- | :--- |
| Z1 | the vault's case rule applied to a case-sensitive source; one of `d/x.txt`, `D/x.txt` lost | done with code |
| Z2, Z3 | `gc-seen.json`, `gc-pending.json`, `HEAD`, `unlock --force`'s ticket read as absent when listed but unreadable; a failed listing read a ref as absent | done with code: one reader |
| Z4 | an unreadable edition probe read as Community Edition | done with code |
| Z5 | a failed queue scan hid a lost lease; an unknown lease past its time | done with code |
| Z6 | a SHA-1 push into a SHA-256 remote with no refs | done with code |
| Z7 | an NFD file beside an NFC payload file dropped | done with code |
| Z8 | `--no-push` wrote the profile and the secrets digest | done with code |
| Z9 | `--changed` missed test modules; `-k` with `--changed` | done with code |
| Z10 | `release_check` "Ready" without GitHub; any remote branch counted as pushed | done with code |
| Z11 | the token kept on a same-host redirect; an upload re-sent to a redirect | done with code |
| Z12–Z15 | FAT change time 0; long-path respelling; zero probe `mtime`; unreadable server clock | done with code |
| Z16, Z23 | a repository named like a layout folder; a copy after a failed listing; a hash-named view file | done with code |
| Z17 | the sdist lacked `tests/untested_messages.txt` | done with code (the manifest test) |
| Z18 | `--status` reported a submodule's `.git` | done with code |
| Z19 | R4 and R7's named tests survived their clause's removal | read only (a new test and a column) |
| Z20 | seven stale doc lines; a commit with no CHANGELOG entry | read only |
| Z21 | `repair` checked tips, not histories | done with code |
| Z22 | a group-owned Pro library sent to its "owner" | done with code; the owner value unmeasured (Z36) |
| Z24–Z26 | warn: a push recreating a tracked path; a hidden branch on Pro; another payload rule | done with code (warnings, by the maintainer's decision) |
| Z42 | the review process rules | read only |
| B12 | 42 git processes per snapshot run | done with code (31) |
| Z27 | the vanished-path guard missed a bare clone's command-line refspec and `--prune-tags` | done with code (0.9.25): the helper remembers what was advertised |
| Z28 | the lock's settlement window was a fixed second | done with code (0.9.24), capped at ten |
| Z29 | `_prune_empty_ref_parents` deleted a parent a Pro non-owner sees as empty | measured on Pro (a hidden child's contents were taken); done with code (0.9.25): not for a non-owner |
| Z30 | two machines uploading one LFS object left conflict copies | done with code (0.9.24) |
| Z31 | a 413 overwrote the repository-wide pack cap | done with code (0.9.24): per remote |
| Z32 | NTFS folds non-ASCII case, git does not | read only (SNAPSHOT.md) |
| Z34 | a Pro non-owner's report-only `--payload-gc` under-counts | done with code (a Note) |
| Z35 | prose numbers not tied to constants | done with code (tests; REF_WRITE timeout is stated nowhere) |
| Z36 | real-server measurements | partly: `MEASURED` lines ran; group owner, 5,000-entry listing and an admin's view are open |
| Z37 | unpinned images never tested | done (workflow ran once, green) |
| Z38 | no fault that fails exactly the n-th listing | done with code; it does not reproduce Z3 (recorded) |
| Z40 | `spell_like_index`'s test-only default; two `format_bytes` | done (the second declined: one module) |
| Z41 | a green run then a failed re-run passed `release_check` | done with code |
| Z43 | the payload cache re-reads everything after a FAT remount | option A done (a Note); option C open |
| Z44, Z45 | a restore onto FAT stopped at git's first error; a case twin read as a defect; `dot.` passed the byte check | done with code (0.9.24) |
| Z22 | a group-owned library's owner value | **corrected**: `share` is every non-owner's value, so it identifies nothing; the discriminator is unmeasured |
| Z47 | a conflict copy of a released holder's ticket blocks one lease | done with code (0.9.25): the holder deletes its own; reaping orphans was rejected as unsafe |
| Z48 | the serialising-disk test failed on macOS runners | done with code (0.9.25): a virtual clock; the wall-clock smoke test that was kept failed once more and now asserts only structure |
| Z49 | skipping the prune leaves an empty ref folder that stops a fetch | decision, with code: the guard stays (issue 6), the Note says the consequence and is printed only when a folder would go |
| Z50 | three timing flakes in a week | read only (an audit in the ROADMAP row) plus one fixed assertion |
| W4 | a 600 s wall clock on tree-sized git calls | done with code: `SEAFILE_SNAPSHOT_GIT_TIMEOUT`; the default is unchanged and a slow disk was never measured |
| T3 | a push verifies packs by size only | decided, recorded in DESIGN (the reasons are there to attack) |
| Z33 | stub duplicate-on-collision for copy | done: `copy_duplicate` fault; the real server's answer to a copy onto a taken name is unmeasured |
| Z46 | a whole-group kill leaves git's `index.lock` | decided: leave it |
| AA1-AA8 | the 8eb416b / 853a7a2 round (ROADMAP §1.11): `inf`/`nan` typed seconds, the restore onto Windows and FAT in four shapes, the dead disk floor, a listed-but-unserved lock ticket, `git~` names, the DESIGN §3.2-3.4 drift | done with code (0.9.25); each named test fails when its fix is removed (`mutate_check`), except the bytes-listing mutation, which only a POSIX run can show |
| AA9-AA19 | `--dry-run`'s case rule, an empty ref folder after a failed nested push (measure first), the suspend-blind lease clock, the settle budget, the mirror-clone warning, the restore's "verified" wording, a surviving R6 mutation, `gates --changed`'s test imports, the long-path skip matcher, the 5,000-folder listing, the payload-verify note | done with code and a test each in 0.9.26, except AA10 (fixed on a stub that models folders; the CE and Pro answer is measured by `test_an_empty_ref_folder_is_measured_against_a_listing_of_the_refs`) and AA18 (tests added, listing sizes measured in the container run). Re-report only a fix that is wrong or incomplete, with the input that shows it |
| AA22 | plain `lstat`/`open` on source paths past 260 characters on Windows | done with a guard test that scans every snapshot part for a source path not passed through `extended_path`; try a site shape the pattern misses |
| AA21 | gemini's A1-A3, D3, D4, G3.1, B1, B2, C1, C2, G8.1, E1; "an empty ref leaf is empty"; exit 2 for an incomplete restore | declined with reasons in ROADMAP §1.11 and the triage; re-report only by showing the reason is wrong |
| S1 | an unpaged listing may be short | measured complete at 5,100 on CE and Pro; read the claim, and the assertion, adversarially |
| Z39 | locale and environment legs | partly done: tr_TR and ASCII-only legs added; antivirus, Task Scheduler as SYSTEM and a subpath behind nginx remain |

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
   answer: which findings, if any, should block the 0.9.25 tag, and which would you ship in a
   0.9.25.1 hot-fix instead?
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
