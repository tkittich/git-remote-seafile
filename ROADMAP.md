# Remaining Issues & Engineering Roadmap

**Baseline:** v0.4.3  
**Scope:** Reconciled backlog from `REVIEW.qwen.md` and `REVIEW.sonnet.md` after High/Medium resolutions in v0.4.0, patch enhancements in v0.4.1, optimization in v0.4.2, and distributed locking/pruning in v0.4.3.  
**Test Suite:** 324 tests across 72 targets (all green).

---

## 1. Inventory of Findings & Status

| ID / Source | Component | Description & Impact | Effort | Risk | Status |
|---|---|---|:---:|:---:|:---:|
| **L-8 / Qwen 5.6** | `helper.py` (`_parse_url`) | URL percent-encoding: `%20` in remote URL unquoted to prevent double-encoding on subsequent `quote()`. | XS | Low | **Implemented in v0.4.1** |
| **L-6 / Qwen 5.4** | `cli.py` (`set-head`) | Writes `HEAD` only after verifying target branch exists remotely. | XS | Low | **Implemented in v0.4.1** |
| **L-11 / Qwen 5.5** | `cli.py` (`gc`) | `gc --min-packs <invalid>` emits stderr warning on non-positive or non-integer value. | XS | Low | **Implemented in v0.4.1** |
| **L-17 / Qwen 5.3** | `gc.py` | If an individual pack download fails during compaction, emits a clear warning to stderr. | XS | Low | **Implemented in v0.4.1** |
| **CI-Gate / Sonnet §7** | `.github/workflows/release.yml` | Gated `build`, `publish-to-pypi`, and `github-release` jobs on a dedicated test suite execution. | XS | Low | **Implemented in v0.4.1** |
| **CI-Pin / Sonnet §7** | `.github/workflows/*.yml` | Third-party GitHub Actions pinned by immutable commit SHAs with semantic version comments. | XS | Low | **Implemented in v0.4.1** |
| **L-16 / Sonnet L-16** | `git_util.py` (`is_ancestor`) | Distinguishes exit code 1 (non-fast-forward) from >=128 (missing object), raising `GitError` to output "fetch first". | XS | Low | **Implemented in v0.4.1** |
| **Qwen 5.1 / Sonnet §10**| `helper.py` (`cmd_push`) | Multi-spec push (`git push origin b1 b2`) builds and uploads a separate `.pack` per spec instead of batching missing objects into one pack. | S | Low | **Implemented in v0.4.2** |
| **L-3 / Sonnet L-3** | `git_util.py` | `get_objects_to_push` discards path hints from `rev-list --objects`, passing bare SHAs to `pack-objects`, degrading delta compression window. | S | Low | **Implemented in v0.4.2** |
| **L-5 / Sonnet L-5** | `lfs.py` | LFS agent uploads always re-send without checking if remote object exists; missing hex validation on OID; missing transfer progress events. | S | Low | **Implemented in v0.4.2** |
| **L-9 / Sonnet L-9** | `client.py`, `safety.py`, `tools/` | Deduplicate Seafile desktop client SQLite/data directory discovery into a shared `seafile_paths.py`. | S | Low | **Implemented in v0.4.2** |
| **L-4 / Sonnet L-4** | `client.py`, `helper.py` | Remove mock-oriented guards (`getattr(self, "_known_dirs")`, `hasattr(client, "download_file_to")`) in favor of real test doubles. | XS | Low | **Implemented in v0.4.2** |
| **M-6 / Sonnet M-6** | `helper.py` / `refs.py` | Deleting nested ref `refs/heads/feature/auth` leaves directory `refs/heads/feature/` on Seafile, risking D/F conflict if branch `feature` pushed later. | S | Medium | **Implemented in v0.4.3** |
| **M-9 / Sonnet M-9** | `refs.py` (`iter_refs`) | Sequential O(refs) HTTP round-trips (2 per ref) during `list`. Benchmark and parallelize via `ThreadPoolExecutor` or cache via remote index file. | M | Medium | Planned for v0.5.0 |
| **M-9 / Sonnet M-9** | `helper.py` (`cmd_fetch`) | Post-compaction full history re-download: `cmd_fetch` checks packs by filename; if remote GC ran, clients re-download everything even if objects exist locally. | M | Medium | Planned for v0.5.0 |
| **Sonnet §6 / Qwen §6**| `helper.py` | Extract `url.py`, `packs.py`, and `config.py` from `helper.py` to keep the stdio protocol loop lean and modular. | M | Medium | Planned for v0.5.0 |
| **H-5 (Phase 3)** | `lock.py` | Evolution to ticket-based locking (`.git-lock.d/<nonce>.json` using server mtime/Date headers) + heartbeat/progress lease renewal for long transfers. | L | Medium | **Implemented in v0.4.3** |

---

## 2. Release Milestones & Roadmap

### Phase 3.1: v0.4.1 — Patch Release (Ergonomics, Safety & CI Hardening) — COMPLETED
* **CI Test Gating:** Release workflow directly runs test suite on `ubuntu-latest` before packaging and publishing.
* **Action Pinning:** All GitHub Actions pinned by immutable commit SHA with version comments.
* **URL Percent-Decoding (L-8):** `unquote()` added to URL path segments in `helper._parse_url`.
* **Branch Verification on `set-head` (L-6):** Target branch verified against remote refs prior to writing `HEAD`.
* **Compaction & CLI Diagnostics (L-11, L-17):** Stderr warnings for invalid `--min-packs` and skipped pack downloads.
* **Error Code Distinction on Fast-Forward (L-16):** `is_ancestor` raises `GitError` on missing remote objects, reporting `fetch first`.

---

### Phase 3.2: v0.4.2 — Transport Polish & Push Optimization — COMPLETED
* **Multi-Spec Pack Batching (Qwen 5.1 / Sonnet §10):** Aggregate missing commit/tree/blob objects across all push specs into a single `.pack` and `.idx` upload.
* **Delta Compression Hint Preservation (L-3):** Preserve `<sha> <path>` lines from `rev-list --objects` when invoking `pack-objects`.
* **Git LFS Transfer Polish (L-5):**
  * Validate OID format avoiding directory traversal.
  * Check remote object existence and byte size before upload to eliminate duplicate uploads.
  * Emit standard Git LFS progress messages for asset transfers.
* **Code Hygiene (L-4 & L-9):**
  * Extracted shared `seafile_paths.py` for client discovery across `client.py`, `safety.py`, and `seafile_doctor.py`.
  * Cleaned up mock-oriented `getattr`/`hasattr` guards.

---

### Phase 3.3: v0.4.3 — Distributed Locking & Long Transfers — COMPLETED
* **Ticket-Based Distributed Lock Protocol (H-5 Phase 3):** Deterministic ticket files in `.git-lock.d/<nonce>.json` ordered by server timestamps and HTTP `Date:` response headers, preventing write-write collision races and client clock drift.
* **Legacy Lock Mirroring:** Mirrored `.git-lock.json` ensures full backward compatibility with older client versions.
* **Progress-Driven Lease Renewal:** In-transfer progress hook refreshing lock lease every 20s of sustained upload progress without background daemon threads.
* **Dead Local PID Fast-Reclaim:** Instant stale lock recovery on same-machine crashes via cross-platform PID liveness checks (`ctypes/OpenProcess` on Windows, `os.kill` on POSIX).
* **Remote D/F Ref Conflict Prevention (M-6):** Recursive pruning of empty parent directories upon branch or tag deletion on Seafile.
* **Lock Management CLI:** `lock-status` and `unlock [--force]` subcommands for manual inspection and operator recovery.

---

### Phase 4.0: v0.5.0 — Scalability & Architecture (Planned)
* **Parallel Ref Enumeration (M-9):** Use a worker pool (`ThreadPoolExecutor`) in `iter_refs` to fetch ref links and SHAs concurrently.
* **Smart Pack Fetch Filtering (M-9):** Skip downloading packs if all requested commit objects already exist locally.
* **Modular Architecture:** Split `helper.py` into `url.py`, `packs.py`, and `config.py`.

---

### Phase 5.0: v1.0.0 — Production Stability & Federation (Future)
* **Comprehensive End-to-End Fault Injection:** Clustered Seafile tests and high-latency simulation.
* **Enterprise Credential Store:** Windows Credential Manager / macOS Keychain integration.
