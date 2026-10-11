# Engineering Roadmap & Backlog

**Baseline:** v0.8.0 (whole-tree snapshot tool — the vault, byte-exactness, and multi-machine capture)  
**Scope:** What shipped in each release line, the active backlog, and the v1.0.0 horizon.  
**Test Suite:** 576 tests across 112 targets, all green (`tools/run_tests_parallel.py`). **Python:** 3.10+ (3.9 EOL).

> [!NOTE]
> All critical and high-severity findings from `archive/REVIEW.*.md` (including ticket-based distributed locking, abandoned ticket cleanup, post-lock ref verification, exception propagation, GC lock fencing, pack index validation, surrogateescape paths, container PID isolation, D/F ref pruning, multi-spec pack batching, Git LFS transfer progress, safety guardrails, parallel ref enumeration, smart pack fetch filtering, disk-staged streaming, and modular helper decoupling) have been completed. All findings from the October 2026 review cycle (REVIEW.glm/gemini/qwen/VERIFY/BACKLOG, now archived) were verified fixed or explicitly dispositioned, as was the earlier v0.7.0 cycle (REVIEW.deepseek/gemini/qwen), closed in v0.7.1. Minor or low-priority items remain tracked in the backlog below. See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

---

## 1. Active Backlog & Next Steps

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **Fault Injection** | `tests/` | Comprehensive End-to-End Fault Injection: Clustered Seafile tests and high-latency simulation. | L | Low | v1.0.0 |
| **Credential Store**| `client.py` | Enterprise Credential Store: Windows Credential Manager and macOS Keychain integration. | M | Medium | v1.0.0 |

### 1.1 Deferred polish (v0.8.0 review cycle — cosmetic, no behavioral stake)

Findings from `archive/REVIEW.*-v0.8.0.md` judged not worth their churn during the fix pass; they live here so they are not lost.

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **Helper test factory** | `tests/test_helper.py` | ~25 `RemoteHelper.__new__(RemoteHelper)` + hand-set-attribute sites already forced a source workaround (`dry_run` as a class attribute); a shared `make_helper()` factory would localise the next attribute addition. Mechanical, zero behavior change. | S | Low | v0.8.1 |
| **De-glob test_gc** | `tests/test_gc.py` | 8 sites patch `pathlib.Path.glob` process-wide; a shared `_lock_aware_client()`-style helper returning staged packs would stop masking unrelated glob users inside `compact_repository`. | S | Low | v0.8.1 |
| **CLI stream/exit conventions** | `cli.py` | Errors from most subcommands go to stdout with exit 1; `unlock`/`lfs-transfer` use stderr; `test` exits 1 for a *non-blocking* SafetyWarning. Standardise (errors → stderr; decide the warning exit code). Touches many CLI test assertions. | M | Low | v0.9.0 |
| **PROPOSALS.md numbers** | `PROPOSALS.md` | Still says "Seafile Web API v2.1" (×3) and "400+ automated tests". Gitignored local file, so it cannot drift into a release — fix opportunistically. | S | None | next touch |
| **Fixture hostname robustness** | `tests/test_lock.py` | `_ticket` defaults to `machine="nodeA"`/`pid=1234`; a CI host literally named `nodeA` with pid 1234 alive would flip the foreign-host premise. Use a name no host can have (e.g. read the hostname and negate it). | S | Low | v0.8.1 |
| **Settlement-window coverage** | `tests/e2e_harness.py` | `init_repo` sets `seafile.locksettle 0` for every e2e repo, so the shipped 1 s default is exercised by exactly one e2e test. Either add a guard that fails if that test disappears, or run one class with the default. | S | Low | v0.8.1 |

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

---

## 3. Future Milestones (v1.0.0 horizon; tracked in §1)

* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation.
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.
