# Engineering Roadmap & Backlog

**Baseline:** v0.8.0 (whole-tree snapshot tool — the vault, byte-exactness, and multi-machine capture)  
**Scope:** Active architectural backlog and milestones following releases v0.4.0 through v0.8.0.  
**Test Suite:** 580+ tests, all green (`tools/run_tests_parallel.py`). **Python:** 3.10+ (3.9 EOL).

> [!NOTE]
> All critical and high-severity findings from `archive/REVIEW.*.md` (including ticket-based distributed locking, abandoned ticket cleanup, post-lock ref verification, exception propagation, GC lock fencing, pack index validation, surrogateescape paths, container PID isolation, D/F ref pruning, multi-spec pack batching, Git LFS transfer progress, safety guardrails, parallel ref enumeration, smart pack fetch filtering, disk-staged streaming, and modular helper decoupling) have been completed. All findings from the October 2026 review cycle (REVIEW.glm/gemini/qwen/VERIFY/BACKLOG, now archived) were verified fixed or explicitly dispositioned, as was the cycle that followed it at v0.7.0 (REVIEW.deepseek/gemini/qwen), closed in v0.7.1. Minor or low-priority items remain tracked in the backlog below. See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

---

## 1. Active Backlog & Next Steps

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **Fault Injection** | `tests/` | Comprehensive End-to-End Fault Injection: Clustered Seafile tests and high-latency simulation. | L | Low | v1.0.0 |
| **Credential Store**| `client.py` | Enterprise Credential Store: Windows Credential Manager and macOS Keychain integration. | M | Medium | v1.0.0 |

---

## 2. Release Milestones

### Phase 4.0: v0.5.0 — Scalability & Architecture (Completed Deliverables)
* **Concurrent Ref Enumeration (M-9):** Concurrently fetch ref links and SHAs using `ThreadPoolExecutor(max_workers=8)` in `iter_refs` while preserving deterministic sorted output.
* **Smart Pack Fetch Filtering (M-9):** Inspect requested commit objects in `cmd_fetch` and bypass redundant remote packfile downloads when objects already exist locally.
* **Disk-Staged Pack Push Streaming (H-1):** Stage packfile generation on disk inside `.git` scratch directory in `cmd_push`, streaming directly via `StreamingMultipartFile` and cleaning up staging files on completion.

---

### Phase 4.1: v0.5.1 — Concurrency Safety & Integrity Hardening (Completed Deliverables)
* **Post-Lock Ref Re-Reading & CAS Verification (N-1):** In `cmd_push`, re-read all destination refs directly under the held lock to avoid stale fast-forward decisions, and execute an optimistic compare-and-swap check immediately prior to uploading ref updates.
* **Fail-Closed Ref Enumeration & GC Integrity (N-2):** Propagate exceptions from `_fetch_ref` in `refs.py` so network failures never silently drop refs; fail closed in GC if any ref yields an empty SHA or fails to read.
* **Lock Fail-Closed on Listing Errors & Deterministic Ordering (N-3):** Treat transient directory listing errors during ticket scanning as retryable errors that fail closed; order tickets deterministically using server `mtime` and unique nonces.
* **Lease Fencing & GC Guard (N-4):** Added `RemoteLock.verify_ownership()`; GC explicitly validates lock ownership and lease validity before deleting obsolete packfiles.
* **Pack Index (.idx) Verification & Auto-Regeneration (N-5):** Verify downloaded `.idx` files with `git verify-pack`; automatically regenerate missing or corrupted `.idx` files locally via `git index-pack`.
* **Non-UTF-8 Path Support (N-8):** Use `surrogateescape` error handling in `git_util.get_objects_to_push` to handle repository paths with non-UTF-8 byte sequences without decoding failures.
* **Ticket Cleanup Guarantee & Read-Only Status (N-9, N-10):** Ensure `RemoteLock.release()` cleans up the client's own ticket even if legacy lock state changed; pass `reap=False` during `get_status()` to keep `lock-status` purely non-mutating.

---

### Phase 4.2: v0.5.2 — Header Sanitization, Netloc Normalization, Safety Disambiguation & Connection Pool Scaling (Completed Deliverables)
* **Multipart Header Parameter Sanitization & Non-Zero Seek Offset (N-11):** Escape quotes and backslashes and strip CR/LF in `StreamingMultipartFile` header generation; record initial `tell()` to properly handle non-zero stream offsets.
* **Server Time Calibration Smoothing & Session Isolation (N-12):** Apply median filtering over moving window of HTTP `Date` samples to prevent jitter; isolate server time estimation strictly to API session.
* **Safety Library Matching Disambiguation (N-14):** Restrict folder-name fallback in `safety.py` strictly to cases where the server repository ID could not be resolved, preventing false collision rejections.
* **Pack & Ref Name Sanitization (N-15):** Validate packfile names against `^pack-[0-9a-zA-Z._-]+\.pack$` and sanitize ref names in `refs.py` and `helper.py`.
* **Credential Netloc Normalization (N-16):** Normalize network locations (lowercase hostname, strip default ports 80/443) across env variables, config, and SQLite tokens.
* **HTTP Connection Pool Scaling (N-17):** Scale `HTTPAdapter` pool sizes to `pool_connections=16, pool_maxsize=16` for thread safety under concurrent operations.
* **Library Exception Propagation (N-18):** Replace `SystemExit` in `seafile_paths.py` with `SeafileClientNotFoundError`.
* **Streaming Fetch Error Logging & Bounded Fallback (N-7):** Log streaming download exceptions to stderr and cap in-memory fallback at 16MB.

---

### Phase 4.3: v0.5.3 — Code Deduplication, Hardened Workflows & Protocol Hygiene (Completed Deliverables)
* **Pack Generation Consolidation & Deduplication (N-19):** Unified staged and temporary directory packfile generation in `git_util.py:create_packfile` into `_generate_pack_in_dir`.
* **SHA Regex Unification (N-19):** Consolidated SHA patterns across modules into a single exported `HEX_SHA_RE` in `git_util.py` supporting both SHA-1 (40-char) and SHA-256 (64-char).
* **Payload Type Safety & String Protection (N-19, L-4):** Distinguish `os.PathLike` from `str` in `client.py:upload_file`, encoding non-file strings directly to UTF-8 bytes to prevent unwanted filesystem reads.
* **Test Shim & Mock Cleanup (N-13):** Removed `@property def _known_dirs` hack in `client.py`, removed `hasattr(lock, "maybe_renew")` in `gc.py`, removed `except TypeError:` wrappers in `lfs.py`, and cleaned up `RemoteHelper.__init__`.
* **Exception Preservation on Preflight (L-13):** Removed redundant preflight safety call in `helper.py:RemoteHelper.__init__` exception handler to avoid masking true connection or authentication errors.
* **POSIX Line Discipline on Windows (L-7):** Reconfigured `sys.stdout` for LF newlines and UTF-8 in remote helper CLI mode.
* **Release Gate Hardening (N-20):** Integrated `ruff check .` and tag-to-version validation into `.github/workflows/release.yml`.
* **API Documentation Accuracy (N-6):** Clarified Seafile Web API v2 (`/api2/`) endpoints across user documentation.

---

### Phase 4.4: v0.5.4 — Pre-v0.6.0 Hardening, Config Security & Test Harness Realism (Completed Deliverables)
* **Configuration File Permission Warning (L-10):** Verified file permissions on POSIX for `~/.git-seafile.json` and issued warnings advising `chmod 600` if accessible by group or others.
* **URL Parsing Traversal Defense & Host Normalization (L-8):** Rejected relative path traversal segments (`.` and `..`), normalized hostnames to lowercase, and stripped default ports and embedded credentials.
* **CLI Argument Handling & Validation (L-11):** Enforced validation on `--min-packs` arguments with descriptive stderr warnings.
* **Robust Server Clock Synchronization:** Extracted RFC 2822 dates using robust regex and attached UTC timezone when naive datetimes are produced by multi-header proxy joins, preventing timezone offsets.
* **Test Harness Realism & Fault Injection (Sonnet §6):** Added realistic integer `mtime` timestamps and file `size` in `SeafileStub.list_dir`, served standard RFC 2822 `Date` HTTP response headers, and added per-path fault injection (`file_404_on` / `file_500_on`).

---

### Phase 4.5: v0.6.0 — Architectural Decoupling & Legacy Cleanup (Completed Deliverables)
* **Modular Helper Architecture (Sonnet §6 / Qwen §6):** Decomposed monolithic helper logic into focused single-responsibility modules:
  - `url.py`: URL parsing, normalization, and validation (`parse_seafile_url`, `SeafileURL`).
  - `config.py`: Git configuration reading and typed configuration dataclass (`RemoteConfig`).
  - `packs.py`: Packfile discovery, streaming downloads, integrity verification, and atomic installation (`fetch_and_install_pack`, `check_remote_has_packs`, `is_valid_pack_name`).
  - `helper.py`: Lean remote helper protocol implementation.
* **Legacy Single-File Lock Mirror Deprecation:** Marked v0.1–v0.3 single-file lock mirror (`.git-lock.json`) as deprecated (`.. deprecated:: 0.6.0`), preparing for full removal in v1.0.0. All modern clients coordinate exclusively via the distributed ticket queue in `.git-lock.d/`.
* **Workspace Cleanliness & Review Archival:** Moved all root `REVIEW*.md` files into the versioned `archive/` directory.

---

### Phase 4.6: v0.6.1 — Protocol Compliance, Fail-Closed Remote GC & Architectural Wiring (Completed Deliverables)
* **Git Remote Helper Object-Format Negotiation (N-1):** Advertised `option` + `object-format` capabilities, replied `ok` to `option object-format true`, and emitted `:object-format <alg>` during `cmd_list`, enabling native Git clones and pushes of SHA-256 repositories.
* **Fail-Closed Remote GC Compaction (N-2, N-3):** Aborted compaction immediately if any pack download or size check fails; Step 7 deletes only successfully compacted packs, preventing remote data loss.
* **Git LFS Progress Delta Reporting (N-4):** Emitted incremental `bytesSinceLast` in transfer progress events instead of total file size, restoring correct throughput metrics and ETA estimates in Git LFS.
* **Windows Stdio Normalization across All Subcommands (N-5):** Standardized UTF-8 and LF line endings at CLI entry point for all subcommands.
* **Ref Name Character Set Conformance (N-12):** Permitted legal `@` characters in ref names per `git check-ref-format` while strictly forbidding `@` components and `@{` reflog syntax.
* **Safety Typo Error Preservation (N-13):** Preserved distinct HTTP and duplicate library errors without misleading "Library not found" masking.
* **Push Lock Fencing (N-14):** Enforced `verify_ownership()` before writing remote ref files in `cmd_push`.
* **Case-Insensitive URL Scheme & URL Parsing Hardening (N-15, N-17):** Handled case-insensitive `seafile://` schemes and rejected empty hosts in explicit URLs.
* **Upload File String Safety (N-16):** Strictly encoded `str` content to UTF-8 without querying local filesystem paths.
* **Architectural Wiring & Performance Optimization (N-6, N-7, N-8, N-9):** Wired `RemoteConfig` dataclass into production runtime, avoided redundant fetch packfile copying with atomic rename (`move=True`), co-located LFS temporary scratch storage within repository git directory, and unified SQLite safe reading logic with `sqlite_read.py`.

### Phase 4.7: v0.6.2 → v0.6.4 — Protocol Completion, Lock-Protocol Soundness & Review Closure (Completed Deliverables)
* **v0.6.2:** Case-insensitive `seafile://` and embedded-scheme parsing; `@`-in-component ref-name conformance; `RemoteConfig` genuinely wired into production (`cmd_push` → lock + compaction); installer `move=` compat via `inspect.signature`; loud `error unsupported` reply for unknown helper commands.
* **v0.6.3 — Lock-Protocol Soundness:** `renew()` reads the ticket back before rewriting (a lapsed-and-taken-over lock is detected and refused, making the gc pre-deletion fence and the push pre-ref-write fence sound — reproduced with a stateful takeover test before the fix); lease renewal during gc pack downloads; non-zero `gc` exit on failed compaction; `order_ts`-based ticket ordering immune to renewal-induced mtime churn; gc deletion-failure reporting; env-credential mismatch named in the auth error.
* **v0.6.4 — Review Closure:** Trap-1 clone guard resolves the clone destination from `GIT_DIR`/`GIT_WORK_TREE` (measured against real git), blocking clone-into-synced-library from outside it; implausible `seafile.*` config falls back to documented defaults with a warning; stale library-id cache guard; unknown entry-type and malformed-JSON warnings; `set-head` reports the replaced HEAD; e2e stub DELETE endpoint fidelity. All findings from the October 2026 review cycle (glm/gemini/qwen/VERIFY/BACKLOG) verified fixed or dispositioned and the reviews archived.

---

### Phase 4.8: v0.7.0 — Major Cleanup (Completed Deliverables)
* **Test Suite Reorganization:** The 2,500-line `test_remote.py` monolith split into per-module files (`test_lock.py`, `test_gc.py`, `test_lfs.py`, `test_helper.py`), with client/config/url tests merged into their existing homes and duplicated coverage dropped.
* **Legacy Single-File Lock Removal:** The deprecated `.git-lock.json` mirror — writes, reads, and deletions across `acquire`/`renew`/`release`/`get_status`/`unlock` — is gone; `.git-lock.d/` tickets are the only lock protocol, and `acquire()` deletes an orphaned mirror so repositories self-clean. **Breaking:** do not mix v0.7.0+ with a v0.1–v0.3 helper on one repository.
* **Shared Pack-Download Path:** `packs.fetch_pack_artifact` owns the stream → capped-fallback → size-verify pattern previously duplicated across fetch and gc; gc's index downloads gain the size check fetch already had.
* **API Surface Cleanup:** `normalize_netloc` is public in `url.py` (dependency now points client → url); `SeafileURL.to_tuple()` and `RemoteHelper._parse_url()` shims removed; `HEX_SHA_RE` is the single canonical SHA-pattern name; unused `run_git(cwd=)` kwarg dropped.
* **Python 3.9 Dropped:** `requires-python >= 3.10`, CI matrix and classifiers updated (3.9 went EOL in October 2025).
* **Small Cleanups:** `cli test` renders the ref listing for humans; the Windows launcher fails with a clear message when python is absent; README install instructions match reality (GitHub-based; PyPI publication pending); the contrib Qt patch described as a proposal.

---

### Phase 4.9: v0.7.1 — Review Closure: CLI Crashes, SHA-256 Compaction & URL Fidelity (Completed Deliverables)
* **Fail-Loud CLI Error Paths (D1, D2):** `test` and `unlock` named their exception in an `except` clause whose import ran *inside* the `try` but after the first statement that could raise, so any earlier failure died with `UnboundLocalError` instead of the intended message — in the two commands a user reaches for when the remote is already broken. Both names are hoisted to module scope.
* **SHA-256 Remote Compaction (D18):** `gc` initialises its scratch repository with the remote's object format *and* runs `verify-pack`/`index-pack` with `-C <bare-repo>`; both halves are required, because those commands run with `GIT_DIR` scrubbed. Compaction previously failed on every SHA-256 remote, and `seafile.autogc` retried forever while packs grew unbounded.
* **URL Port & Scheme Fidelity (D4, D5):** An explicit port on the bare host form is honoured (`:80` → `http`, `:443` → `https`, any other port kept over `https`); a non-numeric port reports an invalid Seafile URL instead of escaping as a raw `ValueError`.
* **Ticket-Settlement Window (D21):** `acquire()` re-scans the queue after one `seafile.locksettle` interval (default 1s) before declaring a win, closing the partial-view double-grant. The added latency is documented rather than rounded away.
* **Windows Pack Re-Install (D25):** `install_packfile` makes both publish targets writable before `os.replace` — git writes pack/idx files `0444`, and a read-only destination fails on Windows where POSIX needs only directory write permission.
* **Fail-Loud Diagnostics (D3, D6, D7, D16):** Library-typo suggestions are built where the miss is detected; unguarded `list_dir` iterations raise a describable error; a transient renewal failure no longer reports as lost ownership; a malformed `~/.git-seafile.json` and an unreadable `seafile-ignore.txt` warn instead of failing silently.
* **Dead Branches Removed (D19, D20, D24):** Unreachable `get_repo_id` cache re-validation and `_detect_remote_object_format` fallback dropped; `install_packfile` passes `--git-dir`; `refs.__all__` exports `is_valid_ref_name`.
* **Docs & Guards (D8–D13, D15, D17):** DESIGN §7.1, USER_GUIDE §12/§14 and PROPOSALS corrected against the code; the no-path URL form documented; the missing `v0.6.2` release notes restored with a tag↔notes guard; per-subcommand `--help`; a magic deletion count in the gc tests replaced by a named assertion.
* **Coverage:** `gc.py` 76% → 100% via 21 in-process tests for the recovery and fail-closed paths (the e2e harness runs the helper in a subprocess, which the parent coverage run never records). Suite 383 → 454 tests.

---

### Phase 4.10: v0.7.2 — Helper Dry-Run Support & Clock-Resolution Test Hardening (Completed Deliverables)
* **Remote-Helper Dry-Run:** Git sends `option dry-run true` ahead of the push commands and *aborts* on `unsupported` — `fatal: helper seafile does not support dry-run`, exit 128 — before it writes a single push command, so accepting the option is what makes `git push --dry-run` usable at all. `cmd_push` routes it to `_push_dry_run`, which reproduces the real path's four decisions (namespace, local ref, remote ref, fast-forward) and emits the same `ok`/`error` lines while taking no lock, building no packfile, and writing no ref. The lock is skipped deliberately: it is itself a side effect, and holding it would park a real push behind the settlement window to answer a question nobody acts on. `_ALLOWED_REF_PREFIXES` was hoisted to a module constant so the real path and the preview cannot drift apart on what is a legal destination.
* **Windows Clock-Resolution Test Flake:** Four `test_lock` cases stamped a peer ticket with `order_ts = time.time()` and then asserted that an `acquire()` a few hundred microseconds later lost to it. Windows `time.time()` is backed by `GetSystemTimeAsFileTime` — **15.625 ms** granularity — through Python 3.12 (3.13 moved to `GetSystemTimePreciseAsFileTime`), so both stamps land in one tick and the queue falls through the tied `order_ts` and the always-tied integer mtime to the nonce, where the test's own random uuid4 sorts first about two runs in three. The protocol was never wrong — a tie is total and deterministic, so every contender agrees on the winner and mutual exclusion holds; only FIFO fairness degrades below the clock's resolution. The fixture now stamps strictly in the past, and a new test freezes the clock to pin the behaviour rather than leave it to chance.
* **Coverage:** Suite 454 → 463 tests.

---

### Phase 4.11: v0.8.0 — Whole-Tree Snapshots: The Vault (Completed Deliverables)
* **`tools/seafile_snapshot.py`:** Captures a working tree **including `.gitignore`d files** into versioned, byte-exact snapshots in a Seafile library — the one gap the helper cannot close, since an ignored file never enters a commit. It is a `tools/` script, not a subcommand: it does not extend the protocol and imports nothing from `git_remote_seafile` unconditionally.
* **The Vault:** A *separate* repository whose `--work-tree` is the source (`git --git-dir=<vault>/.git --work-tree=<source>`), so the source stays read-only. A branch in the source would publish the ignored files, and a subdirectory inside the source would place snapshot data inside the tree being snapshotted — hence a separate repository. See DESIGN §7.13 and SNAPSHOT §11.
* **Byte-Exactness:** Pins `core.autocrlf=false` **and** `* -text -filter -ident` in the vault's own `info/attributes`, which outranks a source `.gitattributes`. An attributes override must name every class it neutralises — `* -text` alone leaves `filter=lfs` active, which silently stored a 130-byte LFS pointer. Because `info/attributes` survives no clone, `--restore` clones `--no-checkout` and re-applies the settings.
* **Atomic, Idempotent Capture:** A vault-local index, `update-ref` with an expected old value, and `seafile-snapshot.lock`; an unchanged tree produces **no commit at all**, so a frequent schedule is cheap. Stale locks are reclaimed via a live-PID check rather than blocking every future run.
* **Restore Verification:** `--restore` verifies on every run with `git hash-object --no-filters` — the only one of Git's three hash checks that reads the bytes on disk (`git status` compares through the filters; plain `hash-object` applies the clean filter). `--verify` adds `git fsck`.
* **Guards:** Excludes use an fnmatch matcher instead of root-anchored pathspecs; pushing the vault to one of the source's own code remotes is refused unless `--allow-shared-remote`; credential-shaped paths are reported (not refused) at the end of every run.
* **Multi-Machine Fix:** The next snapshot bases on the **remote tip**, not the local branch — the original branched from the local branch, so a peer's push was rejected `(fetch first)` *and still moved the local branch*, failing every later run while the first looked successful. One retry re-parents on a lost race, and a failed push rolls the local branch back.
* **Documentation:** `SNAPSHOT.md` (17 sections) is the design document; `USER_GUIDE.md` §15 is the user-facing walkthrough; README lists the tool; DESIGN §7.13 records the architecture. `SNAPSHOT.md` joins the docs-consistency guard's document set.
* **Sensible Defaults & Vault Memory:** Every option has a default, so a bare `seafile_snapshot.py` is a complete run. Defaults resolve most-specific-first — the explicit flag, then the vault's own remembered config (`snapshot.*`), then the source repository (a single `seafile://` remote names the vault's destination), then the current directory. The recommended exclusions became `--default-excludes` and are **never applied silently**: the tool captures everything and names the reproducible bulk it noticed, because a backup that quietly omits a file is worse than a large one.
* **Mistake Prevention:** A vault records and enforces its one source tree; neither source nor vault may live in a Seafile-synced folder (checked at startup, the vault's only line of defence since the helper cannot see it); `--code-remote` must name a real remote; `--no-push` with `--remote` is refused; and flags that would be silently ignored (`--ref` without `--restore` above all) are errors.
* **Coverage:** Suite 463 → 587 tests across 92 targets; `tests/test_snapshot_tool.py` is 124 cases across five classes.

---

### Phase 5.0: v1.0.0 — Production Stability & Federation (Future)
* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation.
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.



