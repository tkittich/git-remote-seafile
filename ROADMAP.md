# Engineering Roadmap & Backlog

**Baseline:** v0.9.14 (0.9.13: the six-model review round's fixes — a lease renewed inside long compute phases, the real server's create-or-duplicate rename honoured, a fetch skip that proves the tips' history locally, `--rebuild-index`; 0.9.0: the snapshot tool ships in the wheel and gains payload mode: large files stored by SHA-256, with gc, verify, retention and browsable views; on top of 0.8.8's measured pack cap, chunked upload, autogc, `doctor`, `gc --tail` and one output/exit convention)  
**Scope:** What shipped in each release line, the active backlog, and the v1.0.0 horizon.  
**Test Suite:** all green on `tools/run_tests_parallel.py` (the runner prints the current count; it is deliberately not repeated here). **Python:** 3.10+ (3.9 EOL).

> [!NOTE]
> All critical and high-severity findings from `archive/REVIEW.*.md` (including ticket-based distributed locking, abandoned ticket cleanup, post-lock ref verification, exception propagation, GC lock fencing, pack index validation, surrogateescape paths, container PID isolation, D/F ref pruning, multi-spec pack batching, Git LFS transfer progress, safety guardrails, parallel ref enumeration, smart pack fetch filtering, disk-staged streaming, and modular helper decoupling) have been completed. All findings from the October 2026 review cycle (REVIEW.glm/gemini/qwen/VERIFY/BACKLOG, now archived) were verified fixed or explicitly dispositioned, as was the earlier v0.7.0 cycle (REVIEW.deepseek/gemini/qwen), closed in v0.7.1. What is *left* is enumerated in §1.1 and §1.2 — §1.2 records, per row, whether the finding has been re-derived against the current release or is still only transcribed from the review that raised it. See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

---

## 1. Active Backlog & Next Steps

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **Fault Injection** | `tests/` | Comprehensive End-to-End Fault Injection: Clustered Seafile tests and high-latency simulation. Per-path `file_404_on` / `file_500_on` / `upload` injection already ships in `e2e_harness.py` (v0.5.4), so "comprehensive" is the load-bearing word — what is missing is clustering and latency simulation, not fault injection itself. | L | Low | idea (not planned) |
| **Credential Store**| `client.py` | Enterprise Credential Store: Windows Credential Manager and macOS Keychain integration. | M | Medium | idea (not planned) |
| **Parallel staging in the snapshot tool** | `tools/_snapshot/70_stage.py` | `git add` hashes on one core: measured 190–250 MiB/s on git for Windows (collision-detecting SHA-1, ~7x slower than OpenSSL's; Python's `hashlib` does 1.6 GB/s on the same file), so a 130 GiB tree takes ~11 minutes with the raw profile. Idea: hash the big files in N parallel `git hash-object -w --no-filters` workers, then feed the index with `git update-index --index-info` (no re-hash). Must preserve modes (100644/100755), symlinks (120000) and gitlinks (160000) exactly, and the byte-exactness contract; verify with a restore-and-compare on a tree that has all four. Expected gain: a few times, bounded by the disk. Not worth the risk until someone's wall-clock actually hurts. | M | Medium | backlog |
| **Payload mode in the snapshot tool** (shipped in 0.9.0; row kept for the design notes) | `tools/_snapshot/75_payload.py` | Big immutable files bypass git: stream-hashed, uploaded by hash to `<path>/payload/objects/`, with a manifest (path, mode, size, SHA-256) in the vault commit. A snapshot is a manifest; renames and duplicates upload nothing; no server name rule applies. Removes the local object copy and the 12-20 minute staging pass on a 131 GiB tree. Shipped with hashing under a racy-clean rule, parallel uploads, verified restore, the `auto` selection rule, a remembered setting, `--payload-rehash`, two-phase `--payload-gc` with `--payload-keep`, `--payload-verify` and `--payload-view`; exercised on a 131 GiB tree against a real server. Spec: PAYLOAD.md; measurements: SNAPSHOT.md section 15.1. | L | Medium | shipped in v0.9.0 |

### 1.0 Ideas kept for later (from the 0.9.12 suggestion pass)

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **`git credential` fallback** | `client.py` | Ask `git credential fill/approve/reject` when no token is configured, so any OS helper (Windows Credential Manager, Keychain, libsecret) works with no new dependency. The cheap first step of the credential-store row. | M | Medium | backlog |
| **`--status` (read-only)** | snapshot tool | **Done (2026-10-27):** `--status` reports the last snapshot's time and size, what changed in the source since it, the settings the next run would use (resolved through the run's own ladder), the vault lock, and names the quota as not-reported (no credential surface). The change comparison is a named stat heuristic against the two records the tip itself carries — the payload manifest (size+mtime) and `mtimes.json` (mtime) — not the per-machine hash cache, so it works cross-machine; the report states both error directions (a change that keeps its recorded stats hides — size and mtime, and for git-stored files of a snapshot taken before `mtimes.json` gained its additive `sizes` key, mtime alone; touched files show), and compares the tip with the vault's own record of its last push (`snapshot.pushed`), which is what this vault did and not a look at the server. SNAPSHOT.md §4 gains the contract; `--json` mode `status` carries the counts. | M | Low | done |
| **`--payload-gc` retention presets** | `76_payload_gc.py` | `--keep-last N` / `--keep-days D` over `--payload-keep`'s count. | S | Low | backlog |
| **`--restore --only <path>`** | `90_restore.py` | Partial restore of one folder from a large snapshot. | M | Medium | backlog |
| **Scheduler recipe** | docs / snapshot tool | A Task Scheduler XML or systemd timer snippet (or `--print-schedule`). | S | None | backlog |
| **`--on-failure <cmd>` hook** | snapshot tool | Run a command with the JSON document when a run fails, for notifications. | S | Low | backlog |
| **`--limit-rate` and a time window** | `75_payload.py` | Keep a backup from saturating a home link in working hours. | M | Low | backlog |
| **Shareable doctor bundle** | `tools/seafile_doctor.py` | A redacted report (no tokens) meant for pasting into an issue. | S | Low | backlog |
| **Scheduled CI against a Seafile container** | `.github/` | **Partly done (see below):** `realserver.yml` + `seafile-ci.yml` + `tools/realserver_ci.py` stand up a throwaway CE container, run `tests.test_realserver` against it and delete the scratch library — but the workflow could not be exercised before landing (no Docker where it was written), so it is dispatch-only until one green manual run (**first green run 2026-10-28**, the sixth dispatch: 7 of 7 tests against Seafile CE 13.0, about two minutes end to end; the five before it found the missing `pip install`, the 12.0-versus-13.0 variable names, a hostname with a port, and a suite that strips its credentials on purpose; enabling the weekly cron in the workflow header is the maintainer's call); the workflow header carries the commented-out weekly cron and the likely failure suspects. | M | Low | first green run |
| **Seafile version matrix** | docs | A compatibility table; only CE 13.0.28 is measured. | S | None | backlog |
| **Drill against the live tree** | `90_restore.py` | `--restore-drill --source` compares sampled restored files with the live ones and reports differences as a Note. | S | Low | backlog |

### 1.1 Deferred polish (v0.8.0 review cycle — cosmetic, no behavioral stake)

Findings from `archive/REVIEW.*-v0.8.0.md` judged not worth their churn during the fix pass; they live here so they are not lost.

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **De-glob test_gc** | `tests/test_gc.py` | 12 sites patch `pathlib.Path.glob` process-wide; a shared `_lock_aware_client()`-style helper returning staged packs would stop masking unrelated glob users inside `compact_repository`. | S | Low | backlog |
| **PROPOSALS.md numbers** | `PROPOSALS.md` | Fixed: updated "Seafile Web API v2.1" references to Seafile Web API and test count to 1,100+ tests across 200+ targets. | S | None | done (v0.9.10) |

### 1.2 Backlog from the v0.8.2 verification and the r2 reviews

The Tier 3 list in `archive/VERDICT.sonnet-v0.8.2.md`, plus the findings the
`-r2` reviews (`REVIEW.glm-v0.8.0-r2.md`, `REVIEW.gemini-v0.8.0-r2.md`,
`REVIEW.qwen-v0.8.0-r2.md`) and the snapshot-tool passes left open. The archive
is gitignored, so until this table existed none of it was in the repository.

**Read the verification column.** Rows marked **probed** were re-derived against
v0.8.6 by reading the cited code or by executing it, and the description is
evidence. Rows marked **transcribed** were confirmed against v0.8.0/v0.8.2 and
have *not* been re-checked since — treat them as leads, not as facts, and
re-probe before fixing.

| ID / Source | Component | Description & Impact | Verified | Effort | Risk | Planned Target |
|---|---|---|---|:---:|:---:|:---:|
| **R3-5** | `client.py` | Bounding the server's `Date` offset (say to an hour) was **rejected, not deferred**: the offset exists to absorb a client clock that is wrong, and a machine off by more than the bound would then fail every lock for a reason the user cannot see, to defend against a hostile server that can already do worse. | — | — | won't do |
| **C9** | `README.md`, `USER_GUIDE.md` | The two overlap heavily — the comparison tables are near-duplicates. **Partly done:** USER_GUIDE §3–§4 moved to ALTERNATIVES.md and the AI-disclosure paragraph appears once; the README comparison tables remain. | transcribed | M | None | v1.0.0 |
| **T1 / T2** | `tests/` | **Partly done:** `tests/test_realserver.py` (opt-in, `SEAFILE_TEST_LIBRARY`) runs the API semantics, the SHA-1/SHA-256 round trip, concurrent pushers and the conflict-copy observation against a real server (CE 13.0.28). Concurrency is covered (nine pushers through the lock; twelve same-path uploads) and a 4 GiB soak was run by hand. Still missing: Pro, clustered/object-store deployments. A scheduled CI job against a container now exists (`realserver.yml`, dispatch-only until its first green manual run — see §1.0). | probed | M | Low | v1.0.0 |
| **D8** | — | **Rejected, not deferred.** Installing the tools as `git-remote-seafile-snapshot` / `-doctor` console scripts contradicts the standing decision that the snapshot tool stays a `tools/` script and is never a subcommand. The acquisition gap it pointed at was a documentation gap, closed in v0.8.2. | probed | — | — | won't do |

**Closed on the way here, kept for the record.** `H-17`/`C-06` (uploads wait for the reply), `H-18` (HEAD read before any ref line), `H-19` (dangling HEAD warned), `H-22` (stderr UTF-8), `H-9` (first-token dispatch), `H-25` and the helper-test-factory and CLI-conventions rows landed in v0.8.8.
 `C7` (DESIGN restructure) and
`C10` (SECURITY.md, threat model, TLS/proxy guidance) landed in v0.8.5;
`A12.2` (`match` → `fullmatch`), `A12.16` (`desktop-url`), `D1`, `D4`, `D5`,
`D7`, `D9` and `D10` landed in v0.8.6; `A11` (the unconditional "no `SFConflict`
files" claim) was scoped to the paths the tool writes in v0.8.6; `C6` (the
unverifiable LFS memory figure) was replaced with a measured one. v0.8.7 closed
`H-24`, `C8`, `A12.10`, `H-7`, `H-8` and the `D6` residual — the six rows above
that a patch release could carry, which is why every row that is left still
names `backlog`, v1.0.0 or `won't do`. `A12.1` (multipart values refuse CR/LF), `A12.3` (global before system), `T-10`
(`is_ancestor` tells a git failure from "no") and `A13` (push cost measured: ~2.3 s, ~13 library
commits per push, 6.5 s on the 20th with autogc) landed after v0.8.7, together with
the pack-size cap found by the 4 GiB soak. `T-5` (the vault lock's descriptor and half-written file on a failed write), `T-6` (a non-UTF-8 file name aborting an `--exclude` run, reproduced in Debian), `T-7` (a name starting with `"` or holding a newline now verified one at a time) and `T-8` (`--code-remote` with `--no-push` is refused) landed after v0.9.2; `T-11` was already fixed (SNAP-06) and only its row was stale. `H-23` (a torn or damaged `repo.db` read as "no libraries", so the synced-folder guard silently did not run: now retried, and said once), the R1/R2/R4/P group (the "matches the remote" message no longer exists, the rollback message is truthful since 0.9.3, a failed restore names the partial tree it left; the vault lock covering the push is by design, since it is per vault and the rollback depends on it) and `A12.7` (a force unlock that swept nothing because the tickets were already gone reports "released", which is the true state) were probed against 0.9.4 and closed after it. `H-13` / `C-01` (a reverse-proxy context path: the server setting carries it, see USER_GUIDE 7.1; a `seafile://` URL still cannot) landed after v0.9.4. `G-5…G-10` (the six suite gaps: untested credential branches, `cmd_fetch` fed a spec the helper never produces, the unreached "empty" pack outcome, a scheduling-dependent concurrency assertion, a gc test that asserted less than its name, a stale cross-reference), `T-14`/`T-02` (the parallel runner now stops its workers and everything they started on Ctrl-C and tolerates a held temp directory) and `T-15`/`T-21` (a failed spawn closed its log; a worker that runs past `--timeout` is killed and named; a target that ran nothing is warned about) landed after v0.9.4. `C-05` (the shared `requests.Session` invariant is now stated beside the session in `client.py`) and the settlement-window coverage row (a config test fails if `TestDefaultSettlementWindow` disappears) landed after 0.9.11. `T-17` / `SNAP-01` (the vault's `git()`
inheriting an ambient `GIT_DIR`) and `C-08` (bracketed IPv6 netloc) were fixed
earlier and re-verified against v0.8.6, so they are not listed above. `A12.6 / H-10` (the lock ticket records process start time on Windows and Linux, so a PID recycled after reboot is recognized as dead and fast-reclaimed), `T-4` (the `repo.db` schema reader and live-database retry logic consolidated in `sqlite_read.read_repo_properties`), and the `test_lock` fixture hostname robustness row (`_ticket` machine defaults to
`foreign-<hostname>`) landed after v0.9.6.  `H-20` (mixed ticket formats forcing
an mtime-ordered queue, and waiters re-uploading their ticket on every poll) was
**re-probed against v0.9.12 and is stale — its behaviour is gone**: the queue
sorts each ticket on its own `order_ts`, falling back per ticket (never queue-wide)
to its timestamp and mtime, and a waiter uploads its ticket at most once per
refresh interval rather than once per poll. It left §1.2 rather than joining it.


### 1.3 Found during the v0.8.7 pass

Gaps the release work itself exposed. None is a defect in shipped code, so none
belongs in the table above. (The release pre-flight, `tools/release_check.py`,
landed after v0.9.2.)

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|---|:---:|:---:|:---:|
| **`push.followTags`** | — | Not a repository change — a maintainer-side git setting. With it a bare `git push` carries any tag that points at what is being pushed, which is the failure that produced v0.8.6's "pushed, but no tag". `CONTRIBUTING.md` §Tagging now names the tag explicitly and says why, so this is a convenience rather than a fix. | S | None | next touch |

### 1.4 The 2026-10-25 archive sweep

`archive/` is where the reviews live, and the backlog above was written from the
reviews dated on or before 2026-10-12. Everything in the archive newer than that —
the payload round (glm-5.3, lms-128, gemini and its triage), the opus-project /
R2 / R4 rounds, the two opus-snapshot reports, the two Phase-1 records, the
gemini / GLM-5.3 / deepseek-v4.1-flash full reports, the Seafile sync analysis and
its upstream drafts, the backup-tool comparison, and the suite measurement note —
was read end to end and re-derived against v0.9.12. Most of it is already in the
§1.2 closed block or in CHANGELOG 0.9.1–0.9.12, and the payload round's fifteen
triage items are all verifiably present in code (the re-read-chunk fix, the
vault's tags in retention, the unparseable listing size, the collected
temporaries, the pre-push re-list). What is left is below.

**Read the Verified column.** *probed* means the current file was read and the gap
is there; *lead* means it was not confirmed — re-derive it before fixing anything.
The ID namespace is new (`S`) because the archive's own identifiers collide across
reviews (A5, A13 and T5 each mean two different things), and a row is keyed on file
and behaviour, never on an ID.

| ID / Source | Component | Description & Impact | Verified | Effort | Risk | Planned Target |
|---|---|---|---|:---:|:---:|:---:|
| **S1 / deepseek B1+A4; phase1-notes A4** | `client.py`, `refs.py`, `gc.py` | The one open LOSS-class item, and it was predicted in Phase 1 before any code was opened. `list_dir` (client.py:838) sends no paging parameter and treats the returned array as the whole directory; nothing in the package asks for a page or a total. DESIGN l.239 already records the fact ("returned all 130 files… Not measured above 130 entries… not defended against beyond the empty-listing refusal"), but no backlog row existed. The *empty* listing is handled (`refs._listing` retries a known directory; `gc` refuses an empty mirror); the *short* one is not, and a short listing advertises fewer branches to a clone and mirrors fewer roots for `git repack -a -d`, whose objects then look unreachable. **Measured 2026-10-28 (CE 13.0.28):** a directory of 1,200 files came back complete, and `page=2`/`per_page` are ignored (the same array again), so "page to exhaustion" has nothing to page with; `test_a_listing_of_more_than_a_thousand_entries_is_complete_and_unpaged` pins both in the real-server tier and in the container workflow. What stays open is the defence: no limit above 1,200 is known, and a server with a cap would still shrink a listing silently. Fix: a cheap cross-check where a short listing is costly (compare the listing's count with the size of the directory's parent entry, or re-list after a write), or state the 1,200 fact as the measured edge in the guide. The vault still needs a second copy (see S9). | probed and measured to 1,200 | S | Low | v1.0.0 |
| **S2 / R4-6** | `76_payload_gc.py` | The history-rewrite fence has no baseline before the first delete. The fence reads the ref tips the last delete run saw, but the report branch returns at :479-483, *above* the `write_seen_tips` call at :548 — so the report-only run that A26 schedules every week neither warns nor records anything, and a run whose only history-reading has been reports has nothing to compare against. A force push or branch delete before anyone had ever deleted is therefore invisible, and the rewritten-away snapshots' objects are marked for deletion. Fix: have the report read and write the baseline; the named test seeds an existing baseline, so it needs a second scenario that starts from an empty store. | probed | S | Low | done (`8feea1f`, v0.9.14) |
| **S3 / R4-15 residual** | `72_payload_guards.py`, `76_payload_gc.py` | The same family, the read side: an unreadable marker is `{}`. The marker reader catches every exception and returns an empty dict, so a torn or garbage `gc-seen.json` erases the S2 fence for that run — and then the run writes a fresh baseline built from the rewritten tips, which hides the rewrite permanently. Beside it, `gc-pending.json` is handled the other way (:451-455: an unparseable one stops the run and names the remedy), and markers are uploaded directly rather than written `.part`-then-renamed, so a torn marker is a realistic state rather than a hypothetical one. Fix: state the rule for both marker files — stop, as the pending file does — and put all three gc state files through the write discipline already used for objects. | probed | S | Low | done (`8feea1f`, v0.9.14) |
| **S4 / R4-11 + Q5** | `75_payload.py`, PAYLOAD A13 | A guard sized by the feature that found it: the placeholder test at :246 sits *behind* the size-floor comparison at :235, so a OneDrive "files on demand" placeholder below the payload floor is never refused by name — `git add` hydrates it (or fails), on a tree where the user was told placeholders are refused. PAYLOAD A13 says "a placeholder is refused by name" with no size qualifier, so the spec claims more than the code does. Fix: move the reparse-attribute check ahead of the floor so it applies to every walk, or qualify A13 and say which walks are covered. | probed | S | Low | done (`bb4d0ab`, v0.9.13) |
| **S5 / opus-project F9 (second half)** | `helper.py`, `lock.py` | A lost lease paces the renewal but never stops the transfer. The upload progress callback calls `maybe_renew()` and drops its result, and nothing reads `ownership_state()` again until each ref write, so after `LEASE_LOST` a multi-GiB pack keeps uploading to a push that is already doomed — cost and a late error, not corruption, because every ref is fenced. Fix: abort the transfer on `LEASE_LOST` in the callback and test that a lease killed mid-upload stops the upload. | probed | S | Low | done (`7ffbb82`, v0.9.13) |
| **S6 / opus-project §3 residual** | `helper.py`, `git_util.py` | v0.9.3 refused the *option-shaped* half of a hostile remote ref value; the revision-expression half is open. A ref file containing `HEAD` or a `@{…}` form reaches `git merge-base --is-ancestor` and is evaluated against the **local** repository, so the fast-forward decision is made from the wrong words. The value is screened only for a leading `-`. Fix: `HEX_SHA_RE.fullmatch` the remote value before the ancestry check, as `cmd_list` already does for advertised tips. | probed | S | Low | done (`8a5acc7`, v0.9.15) |
| **S7 / opus-snapshot F6, opus-snapshot-2 G6 residual** | `76_payload_gc.py`, `99_cli.py` | Retention still copies the vault to work out what is referenced. An explicit `--payload-gc` / `--payload-delete` fetches into a scratch repository under `%TEMP%` (:208-213 reads the vault in place only when there is no remote to compare with), and the run's one free-space check is the staging one (`99_cli.py:391`), which says nothing about the volume the fetch lands on. So a disk-full can stop a retention run mid-fetch. Fix: read the vault in place for the local side of the comparison too, and bound the fetch with the same refusal used before staging. | probed | M | Low | done (`d1ba34c`, v0.9.17) |
| **S8 / gemini-payload §4 suspicion 3 (never triaged)** | `76_payload_gc.py`, `client.py` | A server-side state file nobody has measured. The mark list is read whole through the text reader, which refuses anything over 16 MiB, and it is rewritten whole on every run — so a store with a couple of hundred thousand marked objects dies before it can print a single report line, and the failure message names no way out. The manifest's growth was measured (D4, A18); this file never has been. Fix: measure it, then shard or stream it — and make the refusal name the remedy, which is the standing rule for errors. | probed (path); the size that triggers it is inferred | M | Low | done (`780f4e1`, v0.9.17; measured at ~88 bytes an entry, limit raised to 64 MiB) |
| **S9 / deepseek §meta** | `USER_GUIDE.md`, `SNAPSHOT.md` | The comparison review's verdict ("not as the sole copy") never reached a shipped document. The RUNBOOK and DESIGN warn about a half-complete pack set during `gc`, but nothing tells a user that while S1 is open the vault is a backup that a short directory listing can quietly shrink. Fold into the S1 fix: one row in the limits table naming the desktop client's library history as the second copy. | probed | S | None | with S1 |
| **S10 / triage T12 (origin glm F15)** | `PAYLOAD.md`, `75_payload.py`, `76_payload_gc.py` | The shipped spec contradicts tested behaviour, and the docs are the contract. D5 ("possibly a `.part-` file, which retention ignores") and D9 ("temporary `.part-` files and strangers are reported and left alone") are both false: the collectable pattern matches `<64 hex>.part-<token>` and the temporary is deleted through the same two phases, and a test pins exactly that. The code comment at `75_payload.py:40` and the retention module's header repeat the stale sentence. Round 2 recorded the fix, but no release note and no spec edit followed it. Fix: restate D5/D9 and the two comments to say what the code and the test do. | probed | S | None | done (`232690e`, v0.9.13) |
| **S11 / comparison §9** | `SNAPSHOT.md` | A file arguing with itself: §11's table says "a copied file is stored twice" at l.705, while 35 lines earlier l.670 gives the measurement that says the opposite (two identical 1 MB files share one blob, 1099 KiB of object store for 2 MB of content). Reword to what is true — git does not dedup *similar* files; a one-byte difference is a whole new blob until `gc`. Also stale in the same table: the line saying full delta-compaction "remains a manual gc", although the tail merge has been automatic and on by default since v0.8.8. The doc guards check that a named test exists; a sentence contradicting a measurement in the same document is invisible to them. | probed | S | None | done (`602abe0`, v0.9.13) |
| **S12 / comparison §13** | `SNAPSHOT.md` | Prior art missing its closest cousin: §13 lists git-annex, Borg, restic and safe-gitignore but not bup, which is git-packfile-based and chunk-deduplicating — the design this project deliberately does not take, so the comparison is worth having. One row. | probed | S | None | done (`602abe0`, v0.9.13) |
| **S13 / sonnet §5 E8** | `gc.py` | The helper's slowest command is the quietest. `gc` and the tail merge print one header per step and then nothing, while the long phases run with their output captured; the snapshot tool has had a heartbeat and a `progress:` line since v0.9.11 and v0.9.12 extended it to every long phase. A multi-GiB compaction on a link that takes twenty minutes reads as a hang. Fix: reuse the one-line heartbeat in the helper's gc and tail paths (it is a status label, not a level, so the message-style guard is satisfied). | probed | S | None | done (`e8196b2`, v0.9.14) |
| **S14 / sonnet-v0.8.2 C10 + A11(b)** | `git_remote_seafile/diagnose.py`, `cli.py` | Nothing finds the remote-side conflict copies that block `gc`. A Seafile conflict copy under `refs/` stops compaction permanently: `gc` names it and points at the RUNBOOK's manual steps, and doctor's `conflicts` walks synced *worktrees* only, so the one place the copies matter is unexamined. DESIGN §7 has carried "a repository health command" as proposed-but-unimplemented with no backlog row. Fix: a doctor subcommand that lists conflict residue under a repository path, and cross-reference it from the `gc` refusal message (a message must name the flag or command that fixes it). | probed | S | Low | done (`52e49f2`, v0.9.17; in `git-remote-seafile doctor`, not the sync-client script) |
| **S15 / phase1-notes A7** | `git_remote_seafile/diagnose.py`, `cli.py` | The load-bearing pack cap is calibrated to one server. The 1 GiB ceiling came from one CE 13.0.28 Docker deployment (DESIGN l.238 records the measurement); a proxy with a smaller body limit turns every large push into a failure *after* the lock is taken. Fix: a doctor probe that measures the largest accepted upload body and prints the cap that follows from it — or state plainly in the guide that the default is one deployment's number. (deepseek withdrew its own version of this in Phase 2; it survives here as tooling, not as a defect.) | probed | M | Low | done (`52e49f2`, v0.9.17; the container step that sets an nginx limit passed on its first dispatch, run 40645428649) |
| **S16 / sonnet-v0.8.2 C12 residual** | `git_remote_seafile/`, `MANIFEST.in` | The type check stops at the package boundary. v0.8.7 made the package and `tools/` mypy-clean and moved the settings into `pyproject.toml`, but no marker file ships, so a downstream type checker reads this package as untyped while `CONTRIBUTING.md` still asks contributors to annotate every signature. Fix: ship the marker as package data and add the manifest rule; the docs guard that asks whether the manifest names anything git does not track will catch a missed half. | probed | S | None | done (`1455932`, v0.9.14) |
| **S17 / deepseek E2 (residual of phase1 D3)** | `PAYLOAD.md` R3, `75_payload.py` | R3 ("a run never replaces an object with different bytes, and never deletes one") is still checked by *design* plus two narrow cases. Nothing would fail if the wrong-size-replacement branch overwrote an object that was already correct — the dangerous carve-out in R3's own text. The loss path was refuted by execution, so this is a missing test, not a bug. Fix: a stub where the hash name exists at the right size with different content, asserting it is left alone, and name that test in R3's "how it is checked" column. | probed | S | Low | done (`d90bcce`, v0.9.13) |
| **S18 / test note §3.1** | `tools/run_tests_parallel.py` | The summary prints a skip *count*, never a name or a reason. There are twenty-odd conditional skips (junctions, symlinks, POSIX bash, tags, non-UTF-8 names), so a run on a machine that quietly cannot exercise a guard looks identical to one that can. The workers already collect the skip entries with their reasons. Fix: print each skipped test and its reason in the summary, so the platform-blind-spot problem below becomes visible instead of inferred. | probed | S | None | done (`1455932`, v0.9.14) |
| **S19 / test note §4.3, §5; PAYLOAD §7** | `tests/` | The suite's blind spots are geographic, not logical, and the same list keeps reappearing: the literal-pathspec guard is exercised with one bracketed name only (`*` and `?` are unproven, and cheap to test on the ubuntu and macOS legs), and PAYLOAD §7's "not covered" list — macOS decomposed names, SMB/NFS clock skew, a cluster, a proxy — is prose with no row anywhere. §1.2 T1/T2 tracks the server-topology half of that sentence; the name-shape half has never had a home. Fix: POSIX-only fixtures for the glob shapes, then rows for the platform cases so a green run stops implying coverage it does not have. | probed | M | Low | glob shapes done (`1455932`, v0.9.14); the platform rows are still open |
| **S20 / opus-snapshot F-embedded (unverified)** | `71_embedded.py` | A lead, not a finding. The embedded-repository walk normalises to NFC for *comparisons* (`:94`, `:103`) but feeds the names the walk produced straight into `update-index --stdin` (`:110`), so on macOS a decomposed name may be written as the walk reported it and the next run could hold two spellings of one file. Nothing here can confirm it — no reviewer in any round had a Mac, and CI has no case-folding or NFD filesystem. Fix if it survives re-derivation: normalise to the form git reports before writing the index. | **lead** | M | Medium | needs a Mac |
| **S21 / upstream drafts, sync analysis §4** | external (Seafile client, REST API) | **Verified 2026-10-27, ready to file** (`archive/REVIEW.seafile-upstream-issues.md`): issue 1 confirmed against master — `common/index/index.c` zeroes nsec *and* git's racily-clean machinery sits in the file under `#if 0`; issue 2 confirmed — `wt-monitor-linux.c` skips regular-file `IN_CREATE` on the exact premise `link(2)` violates, threads #2677/#2833 and PR #2988 still open; issue 4 confirmed against a live CE 13 server — a wrong `expected_id` field and a wrong `If-Match` header are both answered `200` with the write applied (silent ignore, not even a rejection); issue 3 partly — exclusive `CreateFileW` (share mode 0) found in `daemon/vc-utils.c`, the repro script remains the decider. **Updated 2026-10-28:** issue 4 gained two more silent-ignore findings (a listing asked for `page=2` or `per_page` answers the same complete 1,200-entry array, measured on a directory of 1,200 files; `replace=0` renames instead of failing), and four drafts were added from the 2026-10-13/14 measurements: the ~2 GiB upload reset (5), silent NFC/`%22`/255-byte name rewriting (6), server-side copy free on disk but charged to quota (7), and the client downloading copies in full (8, unreproduced). **Client repro 2026-10-28 (Seafile 9.0.22, Windows 11, NTFS, scratch library `grs-repro`):** issue 1 reproduced with a control (a same-size, same-mtime rewrite stayed unsynced for 181 s; touching only the mtime synced it in 3 s); issue 3 did not (0 `PermissionError` in 600 replaces). Remaining: repeat issue 1 on the newest client, a Linux client for issue 2, search for newer duplicates, file — or delete. | probed (absent from the repo) | S | None | one-time |

**What the sweep did *not* produce, so it is not lost twice.** The grace period for
unreachable objects, the comparison table's chunk dedup, encryption at rest and
compression control, and bounding the server's `Date` header offset are each
recorded as decided — rejected in §1.2 or listed as deliberate non-goals — and stay
that way; the feature candidates the comparison review raised that are *not*
non-goals (`--keep-snapshots` for the git side, §1.0 covers payload objects only; a
snapshot listing that includes git-stored files, since views are built from the
manifest alone; a desktop agent) are the ones worth a decision, and none is
promised here.

**Closed by the v0.9.13 batch (2026-10-26, commits `45af1f9..3d62db7`).** The
triage of this sweep and of the four other model reviews
(`REVIEW.TRIAGE-v0.9.12.md`, now in `archive/`) produced a fix-now batch; every
commit in it passes the gates, the full suite (1,213 tests) and the real-server
suite:

- **S4** — the placeholder attribute check now precedes the size floor, so a
  placeholder of any size is refused by name; PAYLOAD A13 names the test
  (`bb4d0ab`).
- **S5** — the lease is renewed from *inside* long local compute phases (the
  bounded subprocess wait runs in slices), and an upload whose lease is
  provably lost aborts instead of streaming a doomed push (`7ffbb82`).
- **S6 (destination half; the table's S6 is the ref-*value* half, closed below)** — a push *destination* whose component is not NFC-normalised, holds a
  double quote, or exceeds 255 UTF-8 bytes is refused; names the remote
  already holds are still advertised (`7d4f030`). Found reproduced on the real
  server: the NFD branch overwrote the NFC twin's value.
- **S10** — PAYLOAD D5/D9 and the `75_payload.py` comment now say retention
  *collects* `.part-` temporaries through the two phases (`232690e`).
- **S11 / S12** — SNAPSHOT §11 no longer contradicts its own dedup
  measurement, the stale "manual gc" line is gone, and §13 names bup
  (`602abe0`).
- **S17** — superseded rather than fixed as worded: the triage's mutation
  showed the keep-branch *was* pinned by the collision test, and the real
  server's create-or-duplicate rename (sonnet E7) replaced the concern. The
  missing scenario test exists and R3's column names it (`d90bcce`).

**Closed by the v0.9.14 batch (2026-10-26, commits `42b2fa8..2a1f418` and the
release prep):** **S13** — the helper's `gc` and tail merge print a throttled
`progress:` line during the long captured phases (`e8196b2`); **S16** — the
package ships its PEP 561 `py.typed` marker with the sdist and wheel rules;
**S18** — the parallel runner prints every skipped test and its reason;
**S19** — the literal-pathspec fixture gains the `*` and `?` glob shapes
(POSIX-only). The v0.9.13 CI failure (all ten POSIX legs red) was a single
platform-conditional test added with S13's sibling docs change — rewritten
platform-neutral, mutation-checked (`713c538`), and the CI matrix trimmed to
nine legs for the same coverage logic (`1932fce`).

**Closed since the v0.9.14 tag (released in v0.9.15 to v0.9.17; the four model reviews of
`6d0ca5d` are triaged in `scratch/TRIAGE.v0.9.14.md`):** **S2** and **S3**
were already fixed in v0.9.14 (`8feea1f`: a report-only run seeds the fence
baseline, and an unreadable or unparseable `gc-seen.json` / `gc-pending.json`
stops the run) and their rows still said `backlog`; **S6** (a revision
expression in a remote ref value) is closed — only a full object id reaches
the ancestry check, anything else answers `fetch first` (`8a5acc7`). The
review round also closed: `repair` validates listed pack names (`2e95077`),
`--status` names an unreadable directory, applies the run's own filters and
refuses `--no-push` / `--dry-run` (`6d01a98`, `534def2`), the byte-exact pin
unsets `working-tree-encoding` (`90bec7e`), a push refuses an object format
that differs from what the remote holds under the lock (`d3493a7`), and
R6's digest-of-bytes-sent guard has its own test (`6591a4f`). A second batch
hardened `repair` (strict tips, listing under the lock), made the payload
pre-push check refuse when it cannot list its commits, stopped `check-auth` /
`doctor` crashing on a non-object account answer, and capped the LFS and
read-back downloads at their known size (`abf4514`..`1f5b1ec`).

**Updated by measurement, still open:** **S1** — listings measured whole at
5,000 entries on CE 13.0.28 behind nginx (four probes agree; the endpoint
ignores paging parameters), so the row's risk here is nil and the open half
is portability; its *empty-answer* half is fixed (`ab27d2b` — a directory
that exists and answers empty is a read failure). The open half is
still another server.

### 1.5 From the v0.9.13 triage (drafts were `archive/REVIEW.TRIAGE-v0.9.12.md` §7)

| ID | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **T2** | `76_payload_gc.py` | **Done (8feea1f, shipped in v0.9.14):** the grace clock stamps and compares `gc-pending.json` with the server-`Date` estimate, so an 8-day clock jump on the deleting machine no longer deletes an object marked seconds earlier. | S | Low | done |
| **T3** | `helper.py` | Push packs are proven by listing size only; `gc` additionally proves SHA-256. Decide explicitly: extend `_verify_content` to pushes or record the cost reasoning in DESIGN §4.5. | S | Low | backlog |
| **T4** | snapshot tool | Git-side stat-cache defeats have `--rebuild-index`; payload side has `--payload-rehash`. Remaining: keep both documented next to the mapped-writer and FAT-family limits rows. | S | Low | done in 0.9.13 (docs half) |
| **T5** | `lock.py` | **Done (47cd9d9):** tickets carry a per-install token (`~/.git-remote-seafile/install-id`); the dead-PID reclaim and the plain `unlock` require it to match, so containers sharing a bridge MAC and a hostname can no longer reap each other's live locks. Additive field: old tickets fall back to hostname+MAC, and an unpersistable home directory disables the check (today's behavior). | S | Low | done |
| **T6** | `74`/`78_payload_cli.py` | **Done (f67a3bb):** the manifest records the ratio beside the rule (validated on read); a fresh vault whose default differs from the remote's recorded ratio is refused, naming the flag; the cache is stamped with the writing machine and a foreign cache re-hashes everything, saying so. | M | Medium | done |
| **T7** | `config.py` | **Done (3e52952):** resolved as the DESIGN §4.8 scope note, not a scope change — the lease a contender honours travels in the holder's ticket (`lock._is_lock_active`), so a repo-local `locklease` shortens only the checkout that set it, and takeovers still fail loudly at the fence; trusted-scope reads would have broken documented per-checkout tuning for no cross-machine hardening. | S | Low | done |
| **T8** | `lock.py` | **Done (42b2fa8):** the verification scans fail closed on a ticket that exists but does not parse (LEASE_UNKNOWN / locked: None); acquisition stays lenient so a planted file cannot block the lock for ever. | S | Medium | done |
| **T9** | `helper.py` | **Done (ffb4129):** a listed pack that 404s at download raises `ListedPackVanishedError`; the fetch re-lists the pack directory once and retries from the fresh listing (both download passes guarded by one retry budget), and fails loudly there if the requested tips are then in no remote pack — a scheduled fetch no longer needs a human for a concurrent gc, but a remote that truly lost packs still stops the fetch. | S | Low | done |
| **T10** | docs/tooling | **Done (204110e):** `git-remote-seafile repair <url> [--dry-run]` — reads the remote's packs into a scratch repo, diffs every ref tip against what they carry, and uploads one pack per broken ref from the local clone, proven by size and read-back SHA-256. Never deletes, never writes a ref; the RUNBOOK §3.2 manual procedure stays for clones without the objects. Real-server smoke passed the full arc. | M | Low | done |
| **T11** | `74`/`76_payload_gc.py` | **Done (measured 2026-10-26):** a 1M-file manifest is ~156 MB raw, ~6 s parse, ~600 MB transient peak; the retention mark list is ~82 bytes/entry, so the 16 MiB text-reader cap bites at ~205k marked objects. Numbers recorded in PAYLOAD D4 and USER_GUIDE §14; `--exclude` is the lever. | M | Low | done |
| **T12** | various | **Done (8feea1f, 1455932):** every nit in the list landed with tests. | S each | Low | done |

---

## 2. Shipped by Release Line

One line per capability — see [CHANGELOG.md](CHANGELOG.md) for the full detail and `archive/` for the reviews behind each cycle.

**v0.1.x–v0.2.x (v0.1.0–v0.2.1) — the helper and its guardrails.**
- Core remote helper: native `git clone` / `push` / `pull` against Seafile, packfile transport over the Web API, distributed locking, remote compaction, a Git LFS custom transfer agent (v0.1.0)
- Pre-flight safety guardrails — Trap 1 path collision, Trap 2 download reflection, the `seafile-git/` subfolder convention (v0.2.0); cross-volume packfile fix and the first comprehensive unit suites (v0.2.x)

**v0.3.x (v0.3.0–v0.3.3) — fail-closed and tooling.**
- Recursive nested-ref discovery — slash-named branches no longer lost
- Auto-compaction actually runs; lock hardened; force-push allowed when the remote tip is unknown locally
- Fail-closed principle: never report success for a failed or unverifiable operation
- Safety guards extended to fetch and clone, not just push; LFS payloads streamed instead of buffered in RAM
- `tools/seafile_doctor.py` shipped — read-only diagnostics for the sync client (report/libs/where/errors/conflicts/churn/identity)
- Test infrastructure: parallel process runner; doc claims executed as tests in CI

**v0.4.x (v0.4.0–v0.4.6) — distributed locking.**
- Ticket-based distributed locking replaces the single-file lock: `.git-lock.d/<nonce>.json` tickets, dead-PID fast reclaim, lease renewal during transfer
- Multi-spec pack batching + delta path hints; D/F ref pruning; orphan ticket teardown; remote GC heartbeat (auto-compaction after push)
- Credential scoping, revision-walker exclusion, pre-upload lock renewal

**v0.5.x (v0.5.0–v0.5.4) — scale and integrity.**
- Parallel ref enumeration (worker pool, deterministic ordering preserved)
- Smart pack fetch filtering — redundant remote packs skipped when the requested commits already exist locally
- Zero-copy push staging; post-lock ref verification; lock fencing; pack index (.idx) verification

**v0.6.x (v0.6.0–v0.6.4) — protocol completion.**
- Object-format negotiation (`option object-format` / `:object-format`) — SHA-256 repositories clone and push natively
- Fail-closed remote gc with honest exit codes; modular helper architecture (`url.py` / `config.py` / `packs.py`); legacy lock deprecated
- URL scheme case-insensitivity, legal `@` ref names, sound fencing with takeover-proof ticket ordering
- Clone-destination collision guard (Trap 1 closed for clones too)

**v0.7.x (v0.7.0–v0.7.2) — cleanup and dry-run.**
- **Breaking:** legacy single-file lock removed; Python floor raised to 3.10+; test monolith split into per-module files
- `git push --dry-run`: genuinely read-only preview — no lock taken, no packfile built, no ref written
- Per-command help; CLI error handling hardened; gc names the packs it deletes

**v0.8.0 (2026-10-11) — whole-tree snapshots.**
- `tools/seafile_snapshot.py`: versioned, byte-exact snapshots of a working tree *including* ignored files into a separate vault repo (the source stays read-only); `--restore` with per-run byte verification; LFS-safe; multi-machine safe
- Sensible defaults and mistake prevention: bare-command re-runs, settings remembered in the vault, synced-folder and shared-remote guards, per-run `--no-push`
- All findings from the four-way post-release review (glm/qwen/gemini/deepseek — `archive/REVIEW.*-v0.8.0.md`) are folded into this release: gc refuses to compact when refs sit outside heads/tags, `--no-push` no longer erases the vault's memory, the push rollback survives a retry failure, restore refuses synced destinations, shrunken-upload guard, bracketed IPv6 host parsing, download-token redaction, safety transient errors warn instead of degrading silently. Suite: 463 → 576 tests / 112 targets.

**v0.8.1 (2026-10-11) — the review-cycle fix pass.**
- The 2026-10-11 review cycle closed: 41 findings from the full-tree review (REVIEW.glm.md, cross-checked against the gemini and qwen peer reviews) and the three snapshot-tool reviews (REVIEW.snap{,1,2}.md, archived). Helper: single protocol-stream sanitizer, pack-before-index publish order, repo-local config can no longer disarm the safety guardrails, URL userinfo masking, non-force `unlock` deletes only the winner's ticket, `lock-status`/`unlock` distinguish "unlocked" from "could not inspect", unknown helper commands fail loudly, LFS unknown-event replies carry `event`. Snapshot tool: symlink-aware byte verification, restore-time gitlink warning, `git()` environment scrub + `GIT_TERMINAL_PROMPT=0`, multi-segment/backslash/case-insensitive excludes, CAS-honoring `delete_branch`, `--dry-run` writes nothing. Doctor: guarded date keys, real-date churn window, stderr diagnoses, `where` exit 2 for an unreadable repo.db. Docs: nonexistent `gc --aggressive` removed; v2 API links corrected. Suite: 576 → 595 tests / 113 targets.

**v0.8.2 (2026-10-11) — the symlink-restore fix.**
- **A snapshot holding a symlink could not be restored, and one holding nothing but symlinks crashed the restore.** `verify_restore` looked each link up by its *position* in a list that excludes links, so a correctly restored link was reported as a mismatch — and a mismatch aborts the restore — while an all-symlink tree raised `IndexError`. Links are now looked up by path, and each is still judged by reading the link against its own blob.
- Docs: the tools' **acquisition** is now stated where it matters — they ship with the source checkout and the sdist, not the wheel, so `pip install git+…` followed by `python tools/seafile_snapshot.py` was a dead end. Symlinks also gained user-facing notes (`USER_GUIDE` §8.2/§15.4, the README snapshot warning, and a rewritten `SNAPSHOT.md` paragraph).
- `tools/run_tests_parallel.py`'s curve re-measured: the published 52.7 s headline predated the snapshot module and read as a sub-minute suite. Suite: 607 → 612 tests / 118 targets.

**v0.8.3 (2026-10-11) — the review-verification fix pass.**
- The Tier 1 list from the verification of `REVIEW.sonnet.md` (archived as `archive/VERDICT.sonnet-v0.8.2.md`, with a per-finding table for all 35 items). Helper: a Seafile token is scoped to the server it names (a bare `SEAFILE_TOKEN` used to follow whatever host the URL named), push destinations are validated against git's own ref rule (`git push origin :refs/heads/` used to delete every branch), a ref deletion is read first and fenced by the lease, every `error` line goes through one sanitizer, and both launchers bootstrap with `-I` so a repository's `requests.py` is no longer executed. Tools: `seafile_doctor.py` reports exit 2 for "could not check" instead of crashing into exit 1, and the snapshot tool names the vault's initial branch with `symbolic-ref` so the documented Git 2.20+ floor is met rather than raised. Docs: `USER_GUIDE` §8.2 no longer claims `--depth`/`--filter` are supported (they are silently a full clone), §5 names the conditional SHA-256 floor, `DESIGN.md` §7.10/§7.11 record that the `get_repo_id` cache revalidation was removed in v0.7.1, and `README` documents `where`'s exit 2. Suite: 612 → 630 tests / 121 targets.

**v0.8.4 (2026-10-12) — verify before you believe.**
- Two `REVIEW.sonnet.md` findings the v0.8.3 pass deferred, both of them code trusting something it had not checked. The lock ordered its queue on `order_ts` — a *client-side estimate* of server time, fine for deciding who waits and useless for deciding who holds — so a newcomer whose estimate came out half a second behind an active holder's acquired the lock while the holder was mid-push (reproduced against a stateful store: two clients marked as holders of one repository). A winner now publishes `"holding": true`, the queue sorts granted tickets ahead of every ticket that has merely asked with `order_ts` demoted to the tie-break, and `verify_ownership()` scans the queue as well as its own ticket — the failure was invisible to a ticket read, because the preempted holder's ticket is still there, still ours, still unexpired. It is reported as `LEASE_PREEMPTED`, kept distinct from a takeover.
- And `gc` deleted the superseded packfiles on the strength of an upload that returned `True` and nothing else. The consolidated `.pack`/`.idx` must now appear in a fresh listing at the size they have locally, or every old packfile stays — until the new ones are proven present, the old ones are the only complete copy. The lock is also re-validated before *each* removal rather than once before the loop: `maybe_renew()`'s discarded result could not distinguish "the renewal interval has not elapsed" from "the lease is gone", so a lost lease read as an ordinary no-op and the loop kept deleting inside another client's push.
- Docs: `USER_GUIDE` §12 loses the claim that ticket ordering prevents "write-write collision races and client clock drift" (it did not — that was the bug) and gains the mixed-version limit of the marker; §10 and `DESIGN.md` §7.2 describe what actually gates the deletions; `lock-status` names a winning ticket that carries no grant marker. Suite: 630 → 640 tests / 123 targets.

**v0.8.5 (2026-10-12) — the specification rebuild and the Tier 2 fix pass.**
- `DESIGN.md` rebuilt from a release-by-release narrative into a specification: **Invariants**, **Protocol**, a **Failure Model**, a **Security & Threat Model** and **Non-Goals**, with the version narratives dropped because the CHANGELOG already carries them in more detail. Nothing normative was lost — the split was checked identifier by identifier against the previous revision — and the stale parts are corrected: the release-tagged storage diagram, the sequence diagram that showed a ref uploaded to a path it never is, and the missing credential precedence rule.
- New `SECURITY.md` (credentials and precedence, threat model, TLS/CA/proxy, disclosure) and `RUNBOOK.md` (symptom → cause → fix for a stuck lock, stray refs, a ref at a missing object, half-uploaded packs, conflict artifacts), with a supported-versions matrix and TLS/upgrade guidance alongside.
- Found and fixed a prose contradiction the doc guards cannot see: `USER_GUIDE` §6 still described the pre-v0.8.3 behaviour in which a bare `SEAFILE_TOKEN` followed whatever host the URL named. A new structure guard (`TestDesignIsASpecNotAChangelog`) keeps `DESIGN.md` from drifting back into a changelog, proven by two added drift scenarios (10 → 12).
- The six items the v0.8.2 verification left in its Tier 2 list are closed. **URL parsing (A7):** a dotted library name with a nested path is no longer read as a server — a port or userinfo is honoured outright, a bare dot only when the segment has a hostname's shape — `%2F` can no longer smuggle a separator past the `.`/`..` refusal, `normalize_netloc()` reports a malformed port in this tool's own message rather than urllib's bare `ValueError` (the gap `DESIGN.md` used to record as a limitation), and the shipped context-menu patch emits an explicit-scheme, percent-encoded URL. **Snapshot tool (B2):** excludes are applied *before* staging, so an excluded tree is no longer read, hashed and written to the object store and then left unreferenced. **Helper (A9):** the installed pack set is recorded per repository id and destination, so a local `gc` no longer makes the next fetch re-download packs that are already present. **Docs (C5):** the repo-wide `lfs.standalonetransferagent` trap, its scoped form and the `args`/`concurrent` settings are written up in `USER_GUIDE` §11.1. **CI (D2/D3/B4):** the release gate checks the tag before running the suite and covers the same interpreter matrix as `ci.yml`, the build smoke-installs its own wheel and asserts `tools/` stayed out, and the doc-guard drift scenarios run in CI instead of never. Suite: 640 → 671 tests / 123 → 130 targets.

**v0.8.6 (2026-10-12) — the hardening pass: a token scope, a tested floor, an attested release.**
- The v0.8.0 cycle's two remaining tranches, both of them configuration rather than code, which is why nothing here is visible from a green test run. **CI (D1/D6/D7):** `ci.yml` declares `permissions: contents: read` instead of inheriting a default that is read/write on older repository settings; a `concurrency` group cancels an in-flight run when a newer push supersedes it, instead of queueing fifteen more jobs behind the previous push's fifteen; the matrix caches pip against `pyproject.toml`; and the worker count is the runner's own CPU count rather than a literal `-j 4` justified by a four-vCPU assumption that is neither true on every OS in the matrix nor stable across runner-image resizes.
- **The dependency floor is now a tested claim (D5).** `requests>=2.25.0` predated three CVEs in requests' own credential handling — CVE-2023-32681 (2.31.0), CVE-2024-35195 (2.32.0) and CVE-2024-47081 (2.32.4) — which matters more here than in most projects, because the helper holds a Seafile token and runs inside every `git` invocation. The floor is `>=2.32.4`, and a new `min-deps` job pins exactly that version, installs it with `--no-deps` so the resolver cannot upgrade past it, asserts the pin survived, and runs the whole suite against it. Every other leg installs the *latest* requests, so before this the floor was never exercised at all.
- **Release artifacts are attested and checksummed (D4).** The build job now generates a build-provenance attestation signed against the workflow's own OIDC identity and writes a `SHA256SUMS` file, both of which ride to the Release with the wheel and sdist. A checksum answers "is this the file that was published"; the attestation answers "who built it, from which commit" — the question worth asking about a program you are about to hand a token to. `SECURITY.md` documents both checks.
- **Repository and community files (D9/D10).** Dependabot keeps the SHA-pinned actions fresh (and deliberately does *not* move the dependency floor — a bot raising a security floor is not a security review); issue forms route a vulnerability report to the private channel instead of into a public issue; a pull-request template carries the checklist a release actually runs; `CODEOWNERS` claims every path; and `CODE_OF_CONDUCT.md` adopts the Contributor Covenant 2.1 by reference. `.gitignore` names `.review-venv/` and `.ruff_cache/` rather than depending on the `.gitignore` files inside them.
- **Two code one-liners (A12.2) and two doc corrections.** `HEX_SHA_RE` is now asked with `fullmatch` at both call sites — `$` also matches before a trailing newline, so `match` accepted a 41-character string that is not a SHA. The `USER_GUIDE`'s unconditional "no `SFConflict` files are ever created" is scoped to the paths the tool writes, with the unexercised server-side-merge case named as such; and the README's LFS memory figure is replaced with a measured one (0.16 MB streaming against 128 MB buffered on a 64 MB payload, `tracemalloc` over 16 KiB reads) instead of an unverifiable decimal.
- **`ROADMAP` §1.2** now carries the backlog the archive had been holding — the Tier 3 list from the v0.8.2 verification plus the findings the `-r2` reviews left open, each row marked **probed** (re-derived against this release) or **transcribed** (a lead, not a fact). Suite: 671 → 681 tests / 130 → 134 targets.

**v0.8.7 (2026-10-12) — the fail-loud pass.**
- **Nothing shells out to `git` without a bound any more (H-24).** Every `timeout=` in the tree was on an HTTP call; the 25 `subprocess` sites had none. A wedged `git repack` / `index-pack` inside `gc` held the remote lease open past its expiry, at which point another client lawfully takes over; a credential helper waiting on a GUI prompt hung a scheduled snapshot forever. `git_util.run_git`, `gc` and the snapshot tool's runner now bound every call and fail loudly, naming the command and the limit — and the limit is a constant, not a config key, because a repository must not be able to raise the ceiling that protects the lease. `filter_existing_objects` is the one deliberate exception: it degrades to "assume no objects are present" with a warning on stderr, because a false negative there costs a re-upload rather than an object.
- **Three client defects that each reported a failure as a success (A12.10, H-7, H-8).** A download read `Content-Length` into the progress callback and never compared it against what it had written, so a truncated body returned `True`; a short body now raises. `dir_exists()` answered `False` for *every* non-200 — "cannot tell" reported as "does not exist" — while `list_dir()` beside it raised for anything that was not 200/404, so the two disagreed about the same state; only an explicit 404 is a `False` now. And `get_server_time()` fell back to the *local* clock whenever no `Date` header had been seen, quietly resting the lock's ordering on a clock the protocol does not trust; it still falls back, but says so once, on stderr.
- **The shipped source stops citing documents a reader cannot open (C8).** 23 comments across seven files named `REVIEW.*.md`, `archive/…`, `(N-16)`, `(Qwen …)` — every one of them in the gitignored `archive/`, so none could be followed from a checkout. The reasoning is inlined; an identifier survives only where it names a fact about the review itself rather than standing in for the explanation.
- **The type check and the coverage report are enforced rather than aspirational (D6 residual).** mypy was installed and the package and `tools/` made clean, and `pyproject.toml` now carries `[tool.mypy]` so a new `typecheck` job and a local `mypy` cannot disagree about scope or strictness. `[tool.coverage.*]` and a new `coverage` job produce a per-file report and publish it to the job summary. `tests/` is deliberately outside the type check — it is a namespace package, so mypy resolves each module under two names and stops before checking anything, and its test doubles are `MagicMock`s no annotation can describe. There is deliberately no coverage threshold either: a `fail_under` chosen to make today's build green is one that gets raised for the wrong reason or lowered for a worse one.
- **`.gitignore` gained `.coverage.*`.** Parallel mode — which `pyproject.toml` turns on for the whole suite, so that a worker cannot overwrite the driver's data — writes one data file per process, and the plain `.coverage` rule matched none of them.
- **`CONTRIBUTING.md` §Tagging names the tag, and says why it has to be named.** Its `-m` example was a bare `v<x.y.z>` where every real tag in the history carries the release-notes title, and it did not say that a bare `git push origin` leaves the tag behind — which is exactly how v0.8.6 came to be reported as pushed with no tag. `mirror` is named too: it carries heads *and* tags, so it needs the tag as well. Suite: 681 → 693 tests / 134 → 138 targets.

**v0.8.8 (2026-10-13) — measured against a real server.**
- A single request of 2 GiB is reset (1.75 GiB accepted), so every pack is capped at `seafile.gcmaxpackmib` and files of 1 GiB or more upload in `Content-Range` chunks (5 GiB verified byte-identical); the tail merge is automatic (`seafile.autogc` on by default), with `doctor` and `gc --tail`; one output/exit convention.

**v0.9.0 (2026-10-14) — payload mode, and the snapshot tool installs with the package.**
- Large immutable files are stored by SHA-256 outside git (`--payload-min-size auto`, on by default for a push to `seafile://`) with a manifest in the vault commit; parallel verified uploads, a verified restore, `--payload-gc` (two-phase retention, `--payload-keep`), `--payload-verify`, `--payload-view`. A 131 GiB tree in 38 minutes, restored byte-exact. Specification and attack table: `PAYLOAD.md`.
- `git seafile-snapshot` and `git-remote-seafile snapshot` ship in the wheel (`git_remote_seafile/snapshot_tool.py`, generated from `tools/_snapshot/*.py`).

**v0.9.1 (2026-10-14) — the helper review release.**
- Three independent reviews of the helper: `gc` mirrors refs as one `packed-refs` file and refuses on a ref it cannot read (it used to lose a branch whose name collides on Windows or macOS); a push re-reads the refs under the lock, proves its packs landed and fences every ref; transfer-link authority and token scrubbing; lowercase pack names; remotes named like subcommands; recycled scratch directories; nested repositories captured as files; the share-clock probe, long paths, a weekly report-only payload gc; manifest growth measured.

**v0.9.2 (2026-10-14) — times survive a restore, and a hole is a failure.**
- Modification times of git-stored files are recorded (`.seafile-payload/mtimes.json`, `--record-mtimes`) and restored; `git push --force-with-lease`; a fetch that will not advertise half a repository; a directory the tool cannot read stops the run (git only warns); the second Opus review of the snapshot tool and the two R4 specification reviews: `.part` collision, link-to-`.git`, size-capped downloads, the cache key's device and ctime, unprobed shares, junctions, recycled pids, strict gc listing and a refs re-read before deletion.

**v0.9.3 (2026-10-15) — the third review round.**
- `gc` re-reads the refs before deleting and the payload retention loop re-reads them as it runs; an expired unreaped lock ticket is not resurrected; ref and lock reads are bounded as they are read; the payload cache key uses NTFS's change time; non-UTF-8 names, quoted names and a failing lock write are handled; `tools/release_check.py`. Decided: no grace period for unreachable objects (a lock-ignoring writer's lost race stays documented); the payload conversion of an existing vault is announced again at the end of the run.

**v0.9.4 (2026-10-15) — trust, then verify.**
- `gc` and the tail merge compare the uploaded pack's SHA-256 before deleting; a ref deletion is read back; a snapshot lists its payload objects again after the push; the unreadable-directory guard and the English-messages environment have tests that run on Windows. Rejected: bounding the server's `Date` offset.

**v0.9.5 (2026-10-15) — a server under a path, and a runner that does not hang.**
- A Seafile behind a reverse proxy under a path works through the server setting (USER_GUIDE 7.1; the no-code alternative is a proxy host of its own), verified against a real server; a torn `repo.db` is retried and said once; a failed restore names the partial tree; the six suite gaps are closed; the parallel runner kills a wedged target and stops its workers on Ctrl-C.

**v0.9.6 (2026-10-15) — long paths on Windows.**
- A push no longer fails with "expected object ID, got garbage" when a path exceeds git for Windows' 302-byte `pack-objects --stdin` line; the path hint is dropped for such a line.

**v0.9.7 (2026-10-16) — hardened upload retries, recycled PID reclaim, and Unicode CLI polish.**
- Upload retries obtain a fresh single-use upload link, preventing HTTP 400 errors on transiently dropped connections; `seafile-doctor errors` displays all records under default `--days 0`; stale remote locks from recycled local PIDs are fast-reclaimed by matching process start times; `repo.db` SQLite properties access is consolidated (`T-4`); snapshot staging pins `core.quotepath = false` to preserve UTF-8/Thai paths verbatim.

**v0.9.8 (2026-10-20) — snapshots are of the whole repository.**
- `git seafile-snapshot` run from a subfolder snapshots the repository root (with a note) instead of filing the folder's files on the repository's vault; an explicit `--source` naming a subfolder is refused; a vault that remembers a subfolder keeps working.

**v0.9.9 (2026-10-20) — v0.9.8, released.**
- `v0.9.8` was tagged but never released (its release job failed on a Windows test that depended on the runner's process list); 0.9.9 carries its snapshot fix and the test fix.

**v0.9.10 (2026-10-20) — the payload cut-off is reported, tunable and measured.**
- `--payload-auto-ratio R|measure`; a `placement:` line and a `tip:` line on every `auto` run; a new vault measures deflate speed and its link once and remembers the cut-off; the storage profile of a vault that outgrew its first verdict is decided before staging.

**v0.9.11 (2026-10-21) — failed uploads keep their hashes; one message style.**
- A failed upload keeps the hash cache and a changed file no longer stops the other uploads; an upload prints its progress; `--default-excludes` covers any virtualenv; the staging heartbeat says how much is expected; notifications are `Warning:`/`Note:`/`Notice:`/`Hint:`/`Error:` and bytes are KiB/MiB/GiB, guarded by `tests/test_message_style.py`.

**v0.9.12 (2026-10-22) — a backup you can prove restores; scripting.**
- `--restore-drill [N|all]`; `--json` and `--exit-warnings` (exit 3, opt-in); hashing, payload restore and `--payload-verify` print rate and time left; the hash cache is saved while a tree is hashed, so a killed pass keeps its work.

---

## 3. Future Milestones (v1.0.0 horizon; tracked in §1)

* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation. (Per-path 404/500 injection already ships — see §1.)
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.
