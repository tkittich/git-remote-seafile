# Engineering Roadmap & Backlog

**Baseline:** v0.9.10 (v0.9.0: the snapshot tool ships in the wheel and gains payload mode: large files stored by SHA-256, with gc, verify, retention and browsable views; on top of 0.8.8's measured pack cap, chunked upload, autogc, `doctor`, `gc --tail` and one output/exit convention)  
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

### 1.1 Deferred polish (v0.8.0 review cycle — cosmetic, no behavioral stake)

Findings from `archive/REVIEW.*-v0.8.0.md` judged not worth their churn during the fix pass; they live here so they are not lost.

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **De-glob test_gc** | `tests/test_gc.py` | 12 sites patch `pathlib.Path.glob` process-wide; a shared `_lock_aware_client()`-style helper returning staged packs would stop masking unrelated glob users inside `compact_repository`. | S | Low | backlog |
| **PROPOSALS.md numbers** | `PROPOSALS.md` | Fixed: updated "Seafile Web API v2.1" references to Seafile Web API and test count to 1,100+ tests across 200+ targets. | S | None | done (unreleased) |
| **Settlement-window coverage** | `tests/e2e_harness.py` | The harness sets `SEAFILE_TEST_LOCK_SETTLE=0` for every e2e run, so the shipped 1 s default is exercised by exactly one e2e test (`TestDefaultSettlementWindow`). Either add a guard that fails if that test disappears, or run one class with the default. | S | Low | backlog |

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
| **H-20 / qwen** | `lock.py` | Mixed ticket formats force the mtime fallback, and waiters re-upload on every poll, so the "winner" can flip each round while neither lease expires. Needs a legacy client in contention. | transcribed | M | Low | backlog |
| **C-05 / qwen** | `client.py` | The shared `requests.Session` across threads is an unenforced invariant: safe only because the read paths do not mutate it and the one shared mutation (the `Date` hook's list) is GIL-atomic. Nothing states or tests that. | probed | S | Low | backlog |
| **R3-5** | `client.py` | Bounding the server's `Date` offset (say to an hour) was **rejected, not deferred**: the offset exists to absorb a client clock that is wrong, and a machine off by more than the bound would then fail every lock for a reason the user cannot see, to defend against a hostile server that can already do worse. | — | — | won't do |
| **C9** | `README.md`, `USER_GUIDE.md` | The two overlap heavily — the comparison tables are near-duplicates. **Partly done:** USER_GUIDE §3–§4 moved to ALTERNATIVES.md and the AI-disclosure paragraph appears once; the README comparison tables remain. | transcribed | M | None | v1.0.0 |
| **T1 / T2** | `tests/` | **Partly done:** `tests/test_realserver.py` (opt-in, `SEAFILE_TEST_LIBRARY`) runs the API semantics, the SHA-1/SHA-256 round trip, concurrent pushers and the conflict-copy observation against a real server (CE 13.0.28). Concurrency is covered (nine pushers through the lock; twelve same-path uploads) and a 4 GiB soak was run by hand. Still missing: Pro, clustered/object-store deployments, a scheduled CI job against a container. | probed | M | Low | v1.0.0 |
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
the pack-size cap found by the 4 GiB soak. `T-5` (the vault lock's descriptor and half-written file on a failed write), `T-6` (a non-UTF-8 file name aborting an `--exclude` run, reproduced in Debian), `T-7` (a name starting with `"` or holding a newline now verified one at a time) and `T-8` (`--code-remote` with `--no-push` is refused) landed after v0.9.2; `T-11` was already fixed (SNAP-06) and only its row was stale. `H-23` (a torn or damaged `repo.db` read as "no libraries", so the synced-folder guard silently did not run: now retried, and said once), the R1/R2/R4/P group (the "matches the remote" message no longer exists, the rollback message is truthful since 0.9.3, a failed restore names the partial tree it left; the vault lock covering the push is by design, since it is per vault and the rollback depends on it) and `A12.7` (a force unlock that swept nothing because the tickets were already gone reports "released", which is the true state) were probed against 0.9.4 and closed after it. `H-13` / `C-01` (a reverse-proxy context path: the server setting carries it, see USER_GUIDE 7.1; a `seafile://` URL still cannot) landed after v0.9.4. `G-5…G-10` (the six suite gaps: untested credential branches, `cmd_fetch` fed a spec the helper never produces, the unreached "empty" pack outcome, a scheduling-dependent concurrency assertion, a gc test that asserted less than its name, a stale cross-reference), `T-14`/`T-02` (the parallel runner now stops its workers and everything they started on Ctrl-C and tolerates a held temp directory) and `T-15`/`T-21` (a failed spawn closed its log; a worker that runs past `--timeout` is killed and named; a target that ran nothing is warned about) landed after v0.9.4. `T-17` / `SNAP-01` (the vault's `git()`
inheriting an ambient `GIT_DIR`) and `C-08` (bracketed IPv6 netloc) were fixed
earlier and re-verified against v0.8.6, so they are not listed above. `A12.6 / H-10` (the lock ticket records process start time on Windows and Linux, so a PID recycled after reboot is recognized as dead and fast-reclaimed), `T-4` (the `repo.db` schema reader and live-database retry logic consolidated in `sqlite_read.read_repo_properties`), and the `test_lock` fixture hostname robustness row (`_ticket` machine defaults to `foreign-<hostname>`) landed after v0.9.6.

### 1.3 Found during the v0.8.7 pass

Gaps the release work itself exposed. None is a defect in shipped code, so none
belongs in the table above. (The release pre-flight, `tools/release_check.py`,
landed after v0.9.2.)

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|---|:---:|:---:|:---:|
| **`push.followTags`** | — | Not a repository change — a maintainer-side git setting. With it a bare `git push` carries any tag that points at what is being pushed, which is the failure that produced v0.8.6's "pushed, but no tag". `CONTRIBUTING.md` §Tagging now names the tag explicitly and says why, so this is a convenience rather than a fix. | S | None | next touch |

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

---

## 3. Future Milestones (v1.0.0 horizon; tracked in §1)

* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation. (Per-path 404/500 injection already ships — see §1.)
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.
