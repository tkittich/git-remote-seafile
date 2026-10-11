# Engineering Roadmap & Backlog

**Baseline:** v0.8.5 (the documentation restructure: `DESIGN.md` rebuilt as a specification — invariants, protocol, failure model, security, non-goals — with the release narratives moved to the CHANGELOG, plus `SECURITY.md` and `RUNBOOK.md`)  
**Scope:** What shipped in each release line, the active backlog, and the v1.0.0 horizon.  
**Test Suite:** 642 tests across 124 targets, all green (`tools/run_tests_parallel.py`). **Python:** 3.10+ (3.9 EOL).

> [!NOTE]
> All critical and high-severity findings from `archive/REVIEW.*.md` (including ticket-based distributed locking, abandoned ticket cleanup, post-lock ref verification, exception propagation, GC lock fencing, pack index validation, surrogateescape paths, container PID isolation, D/F ref pruning, multi-spec pack batching, Git LFS transfer progress, safety guardrails, parallel ref enumeration, smart pack fetch filtering, disk-staged streaming, and modular helper decoupling) have been completed. All findings from the October 2026 review cycle (REVIEW.glm/gemini/qwen/VERIFY/BACKLOG, now archived) were verified fixed or explicitly dispositioned, as was the earlier v0.7.0 cycle (REVIEW.deepseek/gemini/qwen), closed in v0.7.1. Minor or low-priority items remain tracked in the backlog below. See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

---

## 1. Active Backlog & Next Steps

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **Fault Injection** | `tests/` | Comprehensive End-to-End Fault Injection: Clustered Seafile tests and high-latency simulation. Per-path `file_404_on` / `file_500_on` / `upload` injection already ships in `e2e_harness.py` (v0.5.4), so "comprehensive" is the load-bearing word — what is missing is clustering and latency simulation, not fault injection itself. | L | Low | v1.0.0 |
| **Credential Store**| `client.py` | Enterprise Credential Store: Windows Credential Manager and macOS Keychain integration. | M | Medium | v1.0.0 |

### 1.1 Deferred polish (v0.8.0 review cycle — cosmetic, no behavioral stake)

Findings from `archive/REVIEW.*-v0.8.0.md` judged not worth their churn during the fix pass; they live here so they are not lost.

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **Helper test factory** | `tests/test_helper.py` | ~52 `RemoteHelper.__new__(RemoteHelper)` + hand-set-attribute sites already forced a source workaround (`dry_run` as a class attribute); a shared `make_helper()` factory would localise the next attribute addition. Mechanical, zero behavior change. | S | Low | v0.9.0 |
| **De-glob test_gc** | `tests/test_gc.py` | 12 sites patch `pathlib.Path.glob` process-wide; a shared `_lock_aware_client()`-style helper returning staged packs would stop masking unrelated glob users inside `compact_repository`. | S | Low | v0.9.0 |
| **CLI stream/exit conventions** | `cli.py` | Errors from most subcommands go to stdout with exit 1; `unlock`/`lfs-transfer` use stderr; `test` exits 1 for a *non-blocking* SafetyWarning. Standardise (errors → stderr; decide the warning exit code). Touches many CLI test assertions. | M | Low | v0.9.0 |
| **PROPOSALS.md numbers** | `PROPOSALS.md` | Still says "Seafile Web API v2.1" (×3) and "400+ automated tests". Gitignored local file, so it cannot drift into a release — fix opportunistically. | S | None | next touch |
| **Fixture hostname robustness** | `tests/test_lock.py` | `_ticket` defaults to `machine="nodeA"`/`pid=1234`; a CI host literally named `nodeA` with pid 1234 alive would flip the foreign-host premise. Use a name no host can have (e.g. read the hostname and negate it). | S | Low | v0.9.0 |
| **Settlement-window coverage** | `tests/e2e_harness.py` | `init_repo` sets `seafile.locksettle 0` for every e2e repo, so the shipped 1 s default is exercised by exactly one e2e test. Either add a guard that fails if that test disappears, or run one class with the default. | S | Low | v0.9.0 |

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

**v0.8.5 (2026-10-12) — the specification rebuild.**
- `DESIGN.md` rebuilt from a release-by-release narrative into a specification: **Invariants**, **Protocol**, a **Failure Model**, a **Security & Threat Model** and **Non-Goals**, with the version narratives dropped because the CHANGELOG already carries them in more detail. Nothing normative was lost — the split was checked identifier by identifier against the previous revision — and the stale parts are corrected: the release-tagged storage diagram, the sequence diagram that showed a ref uploaded to a path it never is, and the missing credential precedence rule.
- New `SECURITY.md` (credentials and precedence, threat model, TLS/CA/proxy, disclosure) and `RUNBOOK.md` (symptom → cause → fix for a stuck lock, stray refs, a ref at a missing object, half-uploaded packs, conflict artifacts), with a supported-versions matrix and TLS/upgrade guidance alongside.
- Found and fixed a prose contradiction the doc guards cannot see: `USER_GUIDE` §6 still described the pre-v0.8.3 behaviour in which a bare `SEAFILE_TOKEN` followed whatever host the URL named. A new structure guard (`TestDesignIsASpecNotAChangelog`) keeps `DESIGN.md` from drifting back into a changelog, proven by two added drift scenarios (10 → 12). Suite: 640 → 642 tests / 123 → 124 targets.

---

## 3. Future Milestones (v1.0.0 horizon; tracked in §1)

* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation. (Per-path 404/500 injection already ships — see §1.)
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.
