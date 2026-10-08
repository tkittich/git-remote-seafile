# Engineering Roadmap & Backlog

**Baseline:** v0.4.4  
**Scope:** Active architectural backlog and future milestones following releases v0.4.0 through v0.4.3.  
**Test Suite:** 324 tests across 72 targets (all green).

> [!NOTE]
> All earlier findings from `REVIEW.qwen.md` and `REVIEW.sonnet.md` (including ticket-based distributed locking H-5, D/F ref pruning M-6, multi-spec pack batching, Git LFS transfer hardening, and safety guardrails) have been completed and shipped in v0.4.0–v0.4.3. See [CHANGELOG.md](CHANGELOG.md) for full historical release notes.

---

## 1. Active Backlog & Next Steps

| ID / Source | Component | Description & Impact | Effort | Risk | Planned Target |
|---|---|---|:---:|:---:|:---:|
| **M-9 / Sonnet M-9** | `refs.py` (`iter_refs`) | Sequential O(refs) HTTP round-trips (2 per ref) during `list`. Parallelize via `ThreadPoolExecutor` or cache via remote index file. | M | Medium | v0.5.0 |
| **M-9 / Sonnet M-9** | `helper.py` (`cmd_fetch`) | Smart Pack Fetch Filtering: If all requested commit objects already exist locally, skip downloading remote packs even after remote GC. | M | Medium | v0.5.0 |
| **Sonnet §6 / Qwen §6**| `helper.py` | Modular Helper Architecture: Split `helper.py` into `url.py`, `packs.py`, and `config.py` to keep the stdio protocol loop lean and modular. | M | Medium | v0.5.0 |
| **Fault Injection** | `tests/` | Comprehensive End-to-End Fault Injection: Clustered Seafile tests and high-latency simulation. | L | Low | v1.0.0 |
| **Credential Store**| `client.py` | Enterprise Credential Store: Windows Credential Manager and macOS Keychain integration. | M | Medium | v1.0.0 |

---

## 2. Upcoming Release Milestones

### Phase 4.0: v0.5.0 — Scalability & Architecture (Planned)
* **Parallel Ref Enumeration (M-9):** Use a worker pool (`ThreadPoolExecutor`) in `iter_refs` to fetch ref links and SHAs concurrently.
* **Smart Pack Fetch Filtering (M-9):** Skip downloading packs if all requested commit objects already exist locally.
* **Modular Helper Architecture:** Split `helper.py` into `url.py`, `packs.py`, and `config.py`.

---

### Phase 5.0: v1.0.0 — Production Stability & Federation (Future)
* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation.
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.
