# Architecture & Design Specification: git-remote-seafile

**Author**: Community Contribution  
**Target Organization**: [haiwen](https://github.com/haiwen) (Seafile Ecosystem)  
**Status**: Ready for Upstream Integration  

---

## 1. Executive Summary

`git-remote-seafile` introduces native Git remote support for Seafile servers using Git's standard Remote Helper specification (`gitremote-helpers(7)`).

It allows Seafile users and organizations to use existing Seafile libraries for private, TLS-secured, access-controlled Git repository hosting without:
1. Deploying a separate Git server (e.g. GitLab, Gitea), or
2. Experiencing the severe performance thrashing and data corruption that occurs when placing live `.git/` working trees inside desktop-synced folders.

---

## 2. Background & Problem Statement

Seafile's desktop synchronization client (`seaf-daemon`) is designed for document and file synchronization. When developers store active code repositories within synced libraries (e.g. `Documents/code/`), pathological failure modes emerge:

1. **Continuous Thrashing**: Git operations frequently write to `.git/` (`index.lock`, `objects/tmp_obj_*`, `COMMIT_EDITMSG`, `refs/heads/*`). Each write triggers full-library re-index cycles (hundreds of cycles daily).
2. **Lock Synchronization**: Ephemeral `.git/*.lock` files are synced across devices, locking Git on secondary machines.
3. **Conflict Storms**: When two machines edit code concurrently, the sync daemon generates `* (SFConflict ...).*` duplicate files rather than performing a 3-way line merge.

Attempting to fix this by modifying the desktop client to understand Git is technically fragile due to platform differences, stat cache discrepancies in `.git/index`, and concurrent worktree races.

### 2.1 Why Client-Side Daemon Patches Fail (Case Study: `vacaboja/seafile`)

Recent community efforts (such as the `vacaboja/seafile` fork and [PR #2988](https://github.com/haiwen/seafile/pull/2988) to `haiwen/seafile`) attempted to solve Git object loss by modifying the Linux sync daemon (`daemon/wt-monitor-linux.c`):
- **The specific bug addressed**: Upstream Seafile intentionally ignores `IN_CREATE` for regular files, assuming GUI copy tools will later fire `IN_CLOSE_WRITE`. When Git creates objects or packfiles via the `link(2)` syscall (hard links) or atomic link-renames, `IN_CREATE` fires on the directory, but **no file descriptor is opened or closed**. Upstream Seafile drops `IN_CREATE`, misses the file entirely, and silently omits the Git object from server uploads ([haiwen/seafile#2677](https://github.com/haiwen/seafile/issues/2677), [#2833](https://github.com/haiwen/seafile/issues/2833)).
- **The fork's workaround**: Queues `IN_CREATE` events in a 1-second timeout hash table (`recheck_accu`) and checks if nanosecond `st_mtim` remains unchanged, synthesising a deferred `WT_EVENT_CREATE_OR_UPDATE`.
- **Why client-side patches remain fundamentally insufficient**:
  1. **Platform-isolated**: The patch is strictly Linux-only (`wt-monitor-linux.c`), ignoring Windows and macOS.
  2. **Multi-file race conditions**: Commits write loose objects, `.git/index`, and `refs/heads/*` non-atomically. Asynchronous HTTP block syncing means peers can receive an updated branch ref before the underlying object blocks finish uploading, crashing commands with `fatal: bad object HEAD`.
  3. **Lock file propagation**: `.git/*.lock` files still sync across devices, freezing active repositories on secondary machines.
  4. **Merge conflicts**: Offline or simultaneous commits continue generating unresolvable `(SFConflict ...)` files rather than 3-way Git merges.

### 2.2 The Windows Dilemma: Same-Second Edits & Mandatory File Locks

On Windows systems, desktop syncing of active code introduces additional OS-level failure modes:
1. **1-Second `mtime` Truncation ("Racy Sync")**:
   Seafile's index format (`common/index/index.c`, adapted from Git's internal directory cache) tracks modification timestamps in integer seconds (`ce_mtime.sec`). Standard Windows CRT file stats truncate `st_mtime` to whole seconds. If an automated script, compiler, or Git command modifies a file multiple times within the **same second** without altering the byte length:
   ```text
   Disk mtime == Index mtime  AND  Disk size == Index size
   ```
   Seafile concludes the file is unchanged, silently skipping the update until a later touch or full library rescan.
2. **Mandatory File Locking (`ERROR_SHARING_VIOLATION` / Error 32)**:
   Unlike Linux advisory locks, Windows enforces mandatory sharing locks. When Seafile detects `FILE_NOTIFY_CHANGE_LAST_WRITE` via `ReadDirectoryChangesW` (`wt-monitor-win32.c`), it immediately opens the file for block hashing. Rapid atomic saves (`write temp` -> `rename`) or Git index updates collide with Seafile's open read handle, resulting in `Permission denied` errors in editors and compilers.
3. **`ReadDirectoryChangesW` Event Overflows**:
   Rapid bursts of filesystem operations (e.g. `git checkout` or `git rebase` touching dozens of files in milliseconds) easily overflow the client's 1 MB event buffer (`ERROR_NOTIFY_ENUM_DIR`), forcing costly full-library directory rescans after temporary Git lock files have already vanished.

### The Solution: API-Driven Git Remote Helper
Instead of synchronizing the local `.git/` directory, the developer's working tree remains completely outside Seafile's file watcher. Git uses Seafile Server as an authentic remote destination over the standard **Seafile Web API (`/api2`)**.

---

## 3. Remote Storage Architecture

Within the designated Seafile library (created via the Seafile Web UI, such as an unsynced dedicated library like `code` or inside a synced library's ignored subfolder like `seafile-git/`), each Git repository is stored in a clean, bare-like layout:

```text
/<repo-path>/
├── HEAD                        <- Symbolic ref ("ref: refs/heads/main\n")
├── .git-lock.json              <- Legacy cooperative lease lock mutex (deprecated v0.6.0)
├── .git-lock.d/                <- Distributed ticket directory (v0.4.3)
│   └── <nonce>.json            <- Deterministic client lock ticket
├── refs/
│   ├── heads/
│   │   ├── main                <- Commit SHA ("3a8f94b15c...\n")
│   │   └── feature-auth        <- Commit SHA
│   └── tags/
│       └── v1.0.0
└── objects/
    └── pack/
        ├── pack-9a8f4c....pack <- Git binary packfile containing objects
        └── pack-9a8f4c....idx  <- Corresponding packfile index
```

### Why Packfiles Instead of Loose Objects?
- A standard commit can involve dozens of loose objects. Transferring individual loose objects over HTTP would require dozens of requests per push/fetch.
- By packaging commits into Git packfiles using `git pack-objects`, every `git push` transfers **exactly two files** (`.pack` and `.idx`) regardless of commit size.
- Pushes complete in hundreds of milliseconds.
- Storage inside Seafile is content-addressed, deduplicated, and block-compressed according to standard Seafile server storage mechanisms.

---

## 4. Seafile Web API (`/api2`) Integration

`git-remote-seafile` uses exclusively standard, stable endpoints present in all modern Seafile servers (CE and Pro):

1. **Authentication**: Reuses existing desktop client tokens (`Accounts` table in `accounts.db`) or token headers (`Authorization: Token <token>`).
2. **Directory Listing**: `GET /api2/repos/{repo-id}/dir/?p=/{repo-path}/refs/heads/`
3. **Ref Reading**: `GET /api2/repos/{repo-id}/file/?p=/{repo-path}/refs/heads/{branch}`
4. **Packfile & Ref Upload**:
   - `GET /api2/repos/{repo-id}/upload-link/?p=/{repo-path}/objects/pack/`
   - `POST {upload-link}` with `multipart/form-data` and `replace=1`.
5. **Download Link**: `GET /api2/repos/{repo-id}/file/?p=/{repo-path}/{file}` returns a short-lived URL, which is then fetched directly.
6. **Branch Deletion**: `DELETE /api2/repos/{repo-id}/file/?p=/{repo-path}/refs/heads/{branch}`, falling back to `/dir/`. Seafile's `/file/` endpoint deletes both files and directories, while `/dir/` returns 404 for a file — and a ref can be either, since `refs/heads/feature/auth` is stored as a directory containing a file `auth`. In v0.4.3, deleting a nested ref automatically prunes newly-empty parent directories up to `refs/heads` on Seafile and evicts them from the client directory cache, preventing future directory/file (D/F) ref conflicts.

---

## 5. Protocol Flow (Git Core ↔ Helper)

```mermaid
sequenceDiagram
    autonumber
    participant Git as Git Client
    participant Helper as git-remote-seafile
    participant API as Seafile Server API

    Note over Git,Helper: Capability Handshake
    Git->>Helper: capabilities
    Helper->>Git: fetch<br/>push<br/>option<br/>object-format

    Note over Git,Helper: Option Negotiation
    Git->>Helper: option object-format true
    Helper->>Git: ok

    Note over Git,Helper: Ref Discovery
    Git->>Helper: list
    Helper->>API: GET /refs/heads/ and /refs/tags/
    API-->>Helper: JSON directory listing and ref SHAs
    Helper->>Git: :object-format [sha1|sha256]<br/>[sha] refs/heads/main<br/>@refs/heads/main HEAD

    Note over Git,Helper: Push Transaction
    Git->>Helper: push refs/heads/main:refs/heads/main
    Helper->>Git: (internal) run git pack-objects
    Helper->>API: GET upload-link
    API-->>Helper: temporary upload URL
    Helper->>API: POST pack-[sha].pack and pack-[sha].idx
    Helper->>API: POST /refs/heads/main (new commit SHA)
    Helper->>Git: ok refs/heads/main
```

---

---

## 6. Fast-Forward Safety & Conflict Guarantees

- **Post-lock ref verification**: While holding the active remote lock, `cmd_push` re-reads all destination refs from Seafile, bypassing the pre-lock `cmd_list` cache.
- **Fast-forward check**: The helper checks if the freshly re-read remote SHA is an ancestor of the local commit (`git merge-base --is-ancestor`) and uses fresh tips for pack object exclusion.
- **Optimistic compare-and-swap (CAS)**: Immediately before uploading ref files, the helper re-reads each ref one final time to verify it has not shifted since the fast-forward check. If another developer pushed to that branch in the interim, the helper outputs `error <dst> fetch first` or `error <dst> non-fast-forward`.
- Git aborts the push, prompting the user to `git pull` and merge using standard Git tools.
- **Zero file duplication**: No `(SFConflict ...)` files can ever be generated.

---

## 7. Advanced Storage & Concurrency Features

### 7.1 Distributed Ticket-Based Lease Locking
- In multi-developer teams, concurrent pushes could race during packfile uploads.
- `git-remote-seafile` implements a cooperative ticket-based distributed lock protocol stored at `/.git-lock.d/<nonce>.json` on the remote repository.
- Each client deposits an individual ticket containing owner hash, machine ID, holding PID, nonce, and expiration. Tickets are evaluated deterministically using Seafile server `mtime` and unique nonces (retrying on transient listing errors and failing closed). Because Seafile does not expose atomic server-side mutex primitives, the lock protocol is cooperative and advisory; write safety is reinforced with post-lock ref re-reads, optimistic compare-and-swap (CAS) verification before writing refs, and lease ownership fencing prior to GC deletions.
- Acquired locks are automatically mirrored to legacy `/.git-lock.json` for backward compatibility with older v0.1–v0.3 client versions (marked deprecated in v0.6.0, scheduled for removal in v1.0.0). Modern clients exclusively coordinate via the distributed ticket queue in `.git-lock.d/`.
- **Dead Local PID Fast-Reclaim**: When inspecting an unexpired lock held on the same machine (verifying both hostname and local machine hardware/container identifier), liveness checks (`OpenProcess` on Windows, `os.kill` on POSIX) immediately reclaim the lock if the holding process has terminated or crashed.
- **In-Transfer Progress Renewal**: Multi-gigabyte packfile uploads and downloads — including the pack downloads inside remote garbage collection — continuously refresh their lock lease every 20 seconds during active socket writes, without background daemon threads. Purely local compute phases (pack generation, `git repack`) renew before and after the phase rather than during it.
- **Sound Ownership Fencing**: Lease renewal reads the holder's ticket back before rewriting it. The nonce names the ticket file and only its creator writes it, so a ticket that is gone or carries a foreign nonce means the lease lapsed and was reaped — most plausibly by a new holder. Renewal fails closed in that case instead of resurrecting the ticket, which is what makes the pre-deletion fence in GC and the pre-ref-write fence in push able to actually detect a takeover.
- **CLI Management**: Operators can inspect active lock status via `git-remote-seafile lock-status <url>` and release or break locks via `git-remote-seafile unlock <url> [--force]`. Status inspection is strictly read-only and never reaps or mutates tickets.

### 7.2 Remote Packfile Compaction (`git-remote-seafile gc`)
- Over time, numerous pushes create multiple packfiles in `/objects/pack/`.
- The `gc` subcommand downloads all packs, invokes `git repack -ad -l` to consolidate and delta-compress them into a single unified packfile, uploads the result, and removes obsolete remote packs from Seafile. Obsolete pack deletion strictly re-validates lock ownership via fencing before removing any remote packs. Note that while obsolete packs are removed immediately from the repository, raw storage reclamation on the Seafile server backend requires the administrator to run `seaf-gc` after the library retention window has elapsed.

### 7.3 Git LFS Custom Transfer Agent Protocol
- Implements the official Git LFS line-based JSON custom transfer protocol.
- Handles `init`, `upload`, and `download` events over standard I/O.
- Allows large binary assets to be stored directly in Seafile `/lfs/` while maintaining small, agile Git commit histories.

### 7.4 Automated Pre-Flight Safety Guardrails (`safety.py`)
To prevent data loss and filesystem thrashing, `git-remote-seafile` enforces pre-flight safety verification before any push operation:
1. **Zero-Configuration Client Discovery**: Reads local Seafile desktop client SQLite configuration (`repo.db` discovered via `<ccnet>/seafile.ini`) to map all actively synced local libraries and their root directories.
2. **Trap 1 (Working Tree & Remote Collision)**: Detects if the current Git working tree resides inside a synced library and shares the exact path with the remote URL. Hard-aborts immediately before locking or uploading to prevent server packfiles from downloading over the active working tree. Recommends pushing to an unsynced library or an ignored subfolder (such as `seafile-git/`).
3. **Trap 2 (Download Reflection Loop)**: Detects pushes into synced libraries where the remote subfolder is not listed in `seafile-ignore.txt`. Aborts to prevent the desktop client from downloading server packfiles right back to the local drive.
4. **Library Typo Suggestions**: Uses fuzzy matching (`difflib.get_close_matches`) against `/api2/repos/` to provide helpful suggestions when library names are misspelled.
5. **Git Protocol Conformance**: Emits standard `error <dst> safety check failed: ...` protocol lines on `stdout` and full human-readable diagnostic guidance on `stderr`.
6. **Bypass Controls**: Supports non-interactive CI environments and automated testing via `SEAFILE_SKIP_SAFETY_CHECKS=1` or `git config seafile.skipsafetychecks true`.

### 7.5 High-Throughput Transfer & Scalability (v0.5.0)
- **Parallel Ref Enumeration (M-9)**: Concurrently resolves remote branch and tag hashes using `concurrent.futures.ThreadPoolExecutor(max_workers=8)` in `refs.py`, eliminating sequential latency overhead on repositories with large ref counts while strictly preserving deterministic alphabetical sort order for Git wire protocol stability.
- **Smart Pack Fetch Filtering (M-9)**: During `fetch`, the helper inspects requested commit objects against local Git object availability (`filter_existing_objects`). When all requested commits already exist locally, redundant remote packfile downloads are bypassed and protocol termination (`\n`) is emitted immediately. *(Heuristic Limitation)*: This optimization operates on tip commit object presence; in full clones, possessing the tip commit implies local object completeness. In shallow (`--depth 1`) or partial/promisor clones where tip commits exist while deep trees or blobs are missing, Git checkout may report missing objects if an un-deepened tree is accessed.
- **Disk-Staged Pack Streaming (H-1)**: During `push`, packfiles and index files generated by `git pack-objects` are staged on disk in temporary directories residing directly within `.git`, and streamed to Seafile via `StreamingMultipartFile` without buffering gigabyte payloads in RAM. Temporary staging directories are cleaned up immediately via `try...finally`.

### 7.6 Integrity & Concurrency Hardening (v0.5.1)
- **Post-Lock Ref Re-Reading & Optimistic CAS (N-1)**: Eliminates lost-update races by re-fetching destination ref tips after lock acquisition and verifying they haven't shifted immediately prior to writing ref files.
- **Fail-Closed Ref Discovery & GC Guard (N-2)**: Ref reading exceptions propagate instead of silently dropping branches; GC validates mirrored ref integrity before running repack.
- **Ownership Fencing (N-4)**: GC verifies active lock ownership and validity immediately before deleting obsolete remote packfiles.
- **Pack Index Verification & Local Regeneration (N-5)**: Downloaded `.idx` files are validated with `git verify-pack -v`; corrupted or truncated indexes are discarded and reconstructed locally via `git index-pack`.
- **Surrogateescape Path Handling (N-8)**: Safely handles non-UTF-8 repository filenames without crashing.

### 7.7 Robustness & Disambiguation (v0.5.2)
- **Header Injection & Streaming Offset Hardening (N-11)**: Sanitizes multipart form header parameters and preserves stream seek offsets in `StreamingMultipartFile`.
- **Clock Calibration Smoothing & API Isolation (N-12)**: Isolates server time estimation strictly to API responses and applies median filtering across samples to eliminate clock jitter.
- **Safety Library Matching Disambiguation (N-14)**: Distinguishes between local libraries sharing folder names across distinct repo IDs by restricting folder-name matching strictly to unknown target IDs.
- **Pack & Ref Name Sanitization (N-15)**: Strict regex validation for packfile names and control character / malformed syntax filtering for remote refs.
- **Credential Netloc Normalization (N-16)**: Standardizes hostnames and default ports across configuration and token lookups.
- **Connection Pool Scaling (N-17)**: Expands HTTP adapter connection pool capacity for thread-safe concurrent ref requests.
- **Structured Error Propagation (N-18)**: Replaces fatal system exits with `SeafileClientNotFoundError`.
- **Streaming Fetch Logging & Bounded Fallback (N-7)**: Surfaces streaming fetch errors to stderr and caps in-memory fallback to 16MB.

### 7.8 Modular Helper Architecture (v0.6.0)
- **Separation of Concerns**: Decomposed monolithic helper logic into single-responsibility modules:
  - **`url.py`**: Seafile URL parsing, scheme extraction, port normalization, and path validation (`parse_seafile_url`, `SeafileURL`).
  - **`config.py`**: Git configuration resolution with typed structured defaults (`RemoteConfig`).
  - **`packs.py`**: Remote packfile discovery, streaming downloads, integrity verification, and atomic installation (`fetch_and_install_pack`, `check_remote_has_packs`, `is_valid_pack_name`).
  - **`helper.py`**: Lean protocol handler focused strictly on Git remote helper commands (`capabilities`, `list`, `push`, `fetch`).

### 7.9 Protocol Compliance & Fail-Closed Hardening (v0.6.1)
- **Wire Object-Format Negotiation (N-1)**: Advertises `option` and `object-format` capabilities to Git core, replies `ok` to `option object-format true`, and emits `:object-format <alg>` during `list` by inspecting remote ref hash lengths, enabling native Git push and clone of SHA-256 repositories.
- **Fail-Closed Remote GC Compaction (N-2, N-3)**: Remote compaction aborts immediately if any remote pack fails to download or verify size against directory listings; Step 7 deletes only successfully compacted packs, preventing remote data loss.
- **Git LFS Incremental Delta Reporting (N-4)**: Sends incremental `bytesSinceLast` in LFS progress events instead of total file size, ensuring accurate throughput rates and ETAs in Git LFS.
- **Cross-Subcommand Stdio Discipline (N-5)**: Normalizes UTF-8 encoding and LF line discipline at the CLI entry point for all subcommands.
- **Ref Name Character Conformance (N-12)**: Permits legal `@` characters in ref names per `git check-ref-format` while strictly forbidding standalone `@` components and `@{` reflog sequences.
- **Push Lock Ownership Fencing (N-14)**: Enforces `lock.verify_ownership()` immediately before writing remote ref files in `cmd_push`.
- **Atomic Move Optimization (N-7)**: Supports `move=True` during packfile installation, renaming staged packs directly into local staging rather than copying across directories.
- **LFS Scratch Co-location (N-8)**: Places LFS transfer scratch storage under `.git/lfs/tmp` when available, eliminating cross-volume copies.

---

## 8. Upstream Integration Path for Haiwen / Seafile

To incorporate this capability into the official Seafile ecosystem:
1. **Repository Home**: Hosted at `tkittich/git-remote-seafile`; can be adopted upstream as `haiwen/git-remote-seafile` or bundled within official client distributions.
2. **Packaging**: Distributed via PyPI (`pip install git-remote-seafile`) and bundled into Seafile Windows/macOS client installers.
3. **Desktop Applet Integration**: A ready-to-merge Qt patch is provided in `contrib/seafile-client-context-menu.patch`, adding a *"Copy Git Remote URL"* action to the Seafile desktop applet's right-click context menu.

---

## 9. Acknowledgments & Prior Art

`git-remote-seafile` draws technical inspiration and design patterns from:
- [git-remote-dropbox](https://github.com/anishathalye/git-remote-dropbox) by Anish Athalye: Pioneered transparent Git packfile transport and cooperative lockfiles over cloud file storage APIs.
- [git-remote-gcrypt](https://spwhitton.name/tech/code/git-remote-gcrypt/) by Joey Hess: Pioneered decentralized, encrypted, push-to-anything Git remotes.
- [git-remote-s3](https://github.com/jason-riddle/git-remote-s3): Git remote helper for Amazon S3 object stores.
- [Git Remote Helpers Protocol Specification](https://git-scm.com/docs/git-remote-helpers): The bidirectional stdio protocol defined by core Git (`git-remote-helpers(7)`).
- [Git LFS Custom Transfer Agent Protocol](https://github.com/git-lfs/git-lfs/blob/main/docs/custom-transfers.md): The official specification for custom Git LFS transport agents.
- [Seafile Web API v2.1](https://manual.seafile.com/develop/web_api_v2.1/): The REST API provided by Seafile Ltd.

---

## 10. AI Disclosure & Authorship

This design specification and the underlying implementation were authored primarily with generative AI assistance from multiple AI systems, in collaboration with human architectural design, real-world forensic diagnostics, and verification on Windows by [@tkittich](https://github.com/tkittich). All code and protocol implementations are fully open source and licensed under the Apache License 2.0.

