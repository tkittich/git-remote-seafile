# Engineering Roadmap & Backlog

**Baseline:** v0.5.1 (v0.5.1 release)  
**Scope:** Active architectural backlog and milestones following releases v0.4.0 through v0.5.1.  
**Test Suite:** 340 tests across 75 targets (all green).

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

### Phase 5.0: v1.0.0 — Production Stability & Federation (Future)
* **Modular Helper Architecture:** Split `helper.py` into `url.py`, `packs.py`, and `config.py`.
* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation.
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.


