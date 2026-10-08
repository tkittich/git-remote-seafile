# Engineering Roadmap & Backlog

**Baseline:** v0.5.4 (v0.5.4 release)  
**Scope:** Active architectural backlog and milestones following releases v0.4.0 through v0.5.4.  
**Test Suite:** 357 tests across 77 targets (all green).

> [!NOTE]
> All critical and high-severity findings from `REVIEW.gemini.md`, `REVIEW.qwen.md`, and `REVIEW.sonnet.md` (including ticket-based distributed locking, abandoned ticket cleanup, post-lock ref verification, exception propagation, GC lock fencing, pack index validation, surrogateescape paths, container PID isolation, D/F ref pruning, multi-spec pack batching, Git LFS transfer progress, safety guardrails, parallel ref enumeration, smart pack fetch filtering, and disk-staged streaming) have been completed. Minor or low-priority items remain tracked in the backlog below. See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

---

## 1. Active Backlog & Next Steps

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **Sonnet §6 / Qwen §6**| `helper.py` | Modular Helper Architecture: Split `helper.py` into `url.py`, `packs.py`, and `config.py` to keep the stdio protocol loop lean and modular. | M | Medium | v0.6.0 |
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

### Phase 5.0: v1.0.0 — Production Stability & Federation (Future)
* **Modular Helper Architecture:** Split `helper.py` into `url.py`, `packs.py`, and `config.py`.
* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation.
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.


