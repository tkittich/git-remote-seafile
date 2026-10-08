# Engineering Roadmap & Backlog

**Baseline:** v0.5.0-dev (v0.4.6 release)  
**Scope:** Active architectural backlog and milestones following releases v0.4.0 through v0.4.6.  
**Test Suite:** 337 tests across 74 targets (all green).

> [!NOTE]
> All earlier findings from `REVIEW.gemini.md`, `REVIEW.qwen.md`, and `REVIEW.sonnet.md` (including ticket-based distributed locking, abandoned ticket cleanup, pre-upload renewal, container PID isolation, D/F ref pruning, multi-spec pack batching, Git LFS transfer progress, safety guardrails, parallel ref enumeration, smart pack fetch filtering, and zero-copy pack push disk staging) have been completed. See [CHANGELOG.md](CHANGELOG.md) for historical release notes.

---

## 1. Active Backlog & Next Steps

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **Sonnet §6 / Qwen §6**| `helper.py` | Modular Helper Architecture: Split `helper.py` into `url.py`, `packs.py`, and `config.py` to keep the stdio protocol loop lean and modular. | M | Medium | v0.5.1 / v0.6.0 |
| **Fault Injection** | `tests/` | Comprehensive End-to-End Fault Injection: Clustered Seafile tests and high-latency simulation. | L | Low | v1.0.0 |
| **Credential Store**| `client.py` | Enterprise Credential Store: Windows Credential Manager and macOS Keychain integration. | M | Medium | v1.0.0 |

---

## 2. Upcoming Release Milestones

### Phase 4.0: v0.5.0 — Scalability & Architecture (Completed Deliverables)
* **Parallel Ref Enumeration (M-9):** Concurrently fetch ref links and SHAs using `ThreadPoolExecutor(max_workers=8)` in `iter_refs` while preserving deterministic sorted output.
* **Smart Pack Fetch Filtering (M-9):** Inspect requested commit objects in `cmd_fetch` and bypass redundant remote packfile downloads when objects already exist locally.
* **Zero-Copy Pack Push Disk Staging (H-1):** Stage packfile generation on disk inside `.git` scratch directory in `cmd_push`, streaming directly via `StreamingMultipartFile` and cleaning up staging files on completion.

---

### Phase 5.0: v1.0.0 — Production Stability & Federation (Future)
* **Modular Helper Architecture:** Split `helper.py` into `url.py`, `packs.py`, and `config.py`.
* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation.
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.

