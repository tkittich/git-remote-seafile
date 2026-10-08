# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.4] - 2026-10-09

POSIX permissions warning on config file, URL parsing traversal defense and netloc normalization, CLI argument validation, robust server Date header parsing, and test harness realism.

### Fixed & Hardened

- **Configuration file permission warning (L-10).** On POSIX platforms, `client.py:_load_credentials` verifies file permissions for `~/.git-seafile.json` and emits a warning to `sys.stderr` if group- or world-accessible (mode & 0o077 != 0), advising `chmod 600 ~/.git-seafile.json` to protect credentials.
- **URL parsing traversal defense & host normalization (L-8).** In `helper.py:_parse_url`, reject path traversal segments (`.` and `..`), normalize hostnames to lowercase, and strip default HTTP/HTTPS ports (80/443) and credentials to ensure sanitized request targets.
- **CLI argument handling & validation (L-11).** In `cli.py`, validate `--min-packs` input arguments, emitting warnings to `sys.stderr` when invalid or missing values are provided before falling back to defaults.
- **Server clock synchronization & Date header parsing.** In `client.py:_record_server_date_header`, extract single RFC 2822 dates using robust regex and attach UTC timezone when naive datetimes are produced by multi-header proxy joins, preventing timezone offsets or clock skew across client environments.
- **Test harness realism & fault injection (Sonnet §6).** In `tests/e2e_harness.py`, enhanced `SeafileStub` to include realistic integer `mtime` timestamps and file `size` in directory listings, serve standard RFC 2822 `Date` HTTP response headers, and support per-path fault injection (`file_404_on` / `file_500_on`) to verify single-ref error propagation.

## [0.5.3] - 2026-10-09

Code deduplication, SHA regex unification, test double cleanup, release gate workflow hardening, stdio LF discipline, and payload type safety.

### Fixed & Hardened

- **Code deduplication & pack generation consolidation (N-19).** Unified staged and temporary directory packfile generation in `git_util.py:create_packfile` into a shared `_generate_pack_in_dir` helper.
- **SHA regex unification (N-19).** Replaced disparate SHA patterns with a single shared `HEX_SHA_RE` constant in `git_util.py` (matching 40-character SHA-1 and 64-character SHA-256) and used it in `helper.py:cmd_list`.
- **Payload type safety & string handling (N-19, L-4).** In `client.py:upload_file`, safely distinguish `os.PathLike` from `str`. If `content` is a string that does not point to an existing local file, encode it directly to UTF-8 bytes instead of erroneously attempting to open arbitrary strings as filesystem paths.
- **Test-shaped production shim cleanup (N-13).** Removed `@property def _known_dirs` hack in `client.py`, cleaned up `hasattr(lock, "maybe_renew")` in `gc.py`, removed `except TypeError:` fallback wrappers around progress callbacks in `lfs.py`, and allowed optional client injection in `RemoteHelper.__init__`.
- **Preflight error preservation (L-13).** In `helper.py:RemoteHelper.__init__`, removed redundant preflight safety call inside exception handler, ensuring true underlying connection and authentication errors propagate directly to caller without being masked.
- **Windows stdio LF discipline (L-7).** Reconfigured `sys.stdout` (newline LF, UTF-8 encoding) and `sys.stdin` (UTF-8) in remote helper CLI mode on Windows, ensuring strictly compliant POSIX line discipline over Git stdio pipes.
- **Release workflow hardening (N-20).** Added `ruff check .` linter execution and automated tag-to-package version verification (`${{ github.ref_name }} == "v" + __version__`) to the `release.yml` release gate test job.
- **Documentation API accuracy (N-6).** Updated references across guides from "Seafile Web API v2.1" to "Seafile Web API v2 (`/api2/`)".

## [0.5.2] - 2026-10-08

Multipart header sanitization, server clock calibration smoothing, safety repo ID disambiguation, pack and ref name sanitization, netloc normalization, connection pool scaling, library exception propagation, and streaming error logging.

### Fixed & Hardened

- **Multipart header sanitization & seek offset handling (N-11).** In `client.py:StreamingMultipartFile`, escape quote and backslash characters and strip CR/LF from `Content-Disposition` parameters to prevent header injection. Honor non-zero initial file `tell()` positions on file-like objects so partial or sought files stream correctly.
- **Server time offset median filtering & transfer session isolation (N-12).** Record HTTP `Date` headers exclusively from the primary API session (excluding file server transfer sessions that may reside on distinct hosts with desynchronized clocks) and maintain a moving window of samples with median filtering to eliminate jitter and clock flapping.
- **Safety library matching disambiguation (N-14).** In `safety.py`, restrict folder-name fallback strictly to cases where the server repository ID could not be resolved. Prevents false positive path collisions and reflection blocks when local and server libraries share identical folder names under distinct repository IDs.
- **Remote pack and ref name validation (N-15).** In `helper.py` and `gc.py`, validate remote packfile names against strict patterns (`^pack-[0-9a-zA-Z._-]+\.pack$`) and reject path separators or directory traversal sequences (`..`). In `refs.py` and `cmd_list`, filter out malformed ref names containing whitespace, control characters, or invalid Git ref syntax.
- **Credential netloc normalization & token scoping (N-16).** Normalize network locations across URLs (lowercasing hostnames and stripping default HTTP 80 / HTTPS 443 ports) when resolving credentials from environment variables (`SEAFILE_SERVER`), config files, and SQLite accounts databases.
- **Connection pool thread safety (N-17).** Increased HTTPAdapter connection pool capacity (`pool_connections=16, pool_maxsize=16`) in `client.py` to ensure thread-safe pooling across concurrent workers during parallel ref discovery in `iter_refs`.
- **Library exception propagation (N-18).** Replaced `SystemExit` in `seafile_paths.py:ccnet_dir` and `seafile_data` with `SeafileClientNotFoundError` (inheriting from `FileNotFoundError`), allowing calling modules and tools to catch missing client data directories gracefully as standard exceptions.
- **Streaming fetch failure logging & bounded fallback (N-7).** In `helper.py:cmd_fetch` and `gc.py:compact_repository`, log streaming download exceptions rather than silently swallowing them, and enforce a 16MB ceiling on the in-memory fallback to safeguard against RAM exhaustion on large packs.

## [0.5.1] - 2026-10-08

Post-lock ref verification to eliminate lost updates, ref read exception propagation, fail-closed lock acquisition and GC fencing, automatic pack index verification, and surrogateescape encoding.

### Fixed & Hardened

- **Post-lock ref verification & CAS protection (N-1).** In `helper.py:cmd_push`, re-read all destination refs from Seafile under the held lock rather than relying on stale pre-lock `cmd_list` cache. Fast-forward checks and pack object exclusion lists evaluate against fresh remote tips. Enforced an optimistic compare-and-swap (CAS) check immediately before uploading ref files, reporting `error <dst> fetch first` if the remote ref shifted. Added an end-to-end race condition test in `test_e2e.py`.
- **Ref enumeration exception propagation (N-2).** Removed blanket exception swallowing in `refs.py:iter_refs`. Genuine 404s still yield `None`, while transient network/HTTP 5xx failures propagate immediately to caller processes (`cmd_list`, `gc`), preventing silent ref omission and data loss during subsequent pack compaction.
- **Fail-closed lock acquisition on listing errors (N-3).** Hardened `lock.py:_scan_tickets` and `acquire()` so directory listing failures retry over the lease timeout and fail closed with `RepositoryLockedError` rather than defaulting to empty entries and erroneously claiming ownership. Replaced client timestamp tie-breaking with deterministic Seafile server `mtime` buckets and nonces.
- **Remote GC pack deletion fencing (N-4).** In `gc.py:compact_repository`, re-verify lock ownership via `RemoteLock.verify_ownership()` immediately before deleting obsolete remote packfiles in step 7. Aborts compaction without modifying old remote packs if the lease expired or ownership was lost.
- **Pack index (.idx) integrity verification & regeneration (N-5).** In `git_util.py:install_packfile`, validate downloaded `.idx` files against corresponding `.pack` files with `git verify-pack -v`. Corrupted or truncated index files are automatically discarded and regenerated locally using `git index-pack`. Added matching verification in `helper.py:cmd_fetch` and `gc.py`.
- **Non-UTF-8 path handling (N-8).** Configured `surrogateescape` error handling when decoding `git rev-list --objects` and encoding `git pack-objects` input in `git_util.py`, ensuring repos with legacy non-UTF-8 filenames push without raising `UnicodeDecodeError`.
- **Orphan ticket cleanup on release (N-9).** In `lock.py:release()`, ensure the client's own ticket in `.git-lock.d/<nonce>.json` is deleted even if the legacy `.git-lock.json` file is held by another host.
- **Read-only lock status inspection (N-10).** Added `reap=False` to `lock.py:_scan_tickets` when called from `get_status()`, ensuring read-only status checks never mutate remote repository state.

## [0.5.0] - 2026-10-08

High-throughput concurrent ref enumeration, smart pack fetch filtering, and zero-copy disk-staged pack push streaming.

### Added & Optimized

- **Concurrent ref enumeration (M-9 / ARC-07 / ISSUE-11).** Parallelized remote branch and tag discovery in `refs.py:iter_refs` using `concurrent.futures.ThreadPoolExecutor(max_workers=8)`. Eliminates sequential HTTP round-trip latency on repositories with large ref counts while strictly preserving deterministic alphabetical sort order for Git wire protocol stability.
- **Smart pack fetch filtering (M-9).** During `cmd_fetch`, commit SHAs in `fetch_specs` are validated and inspected against local Git object availability via `filter_existing_objects`. When all requested commits already exist locally, redundant remote packfile downloads are bypassed and protocol termination (`\n`) is emitted immediately.
- **Zero-copy disk-staged pack push streaming (H-1).** In `cmd_push`, packfiles and index files generated by `git pack-objects` are staged directly in a temporary directory inside `.git` rather than buffering full pack payloads in RAM. Uploads stream directly to Seafile via `StreamingMultipartFile` and staging directories are cleanly pruned on exit.

## [0.4.6] - 2026-10-08

Pre-upload distributed lock renewal, container hostname PID isolation, unified GC lease renewal, owned library disambiguation hardening, and lazy directory cache initialization.

### Added & Optimized

- **Pre-upload lock lease renewal (L-3 / ISSUE-15).** Added `RemoteLock.maybe_renew()` and hooked renewals in `helper.py:cmd_push` before and after local packfile creation (`git pack-objects`), preventing lease expiration during heavy local commit traversals before socket streaming begins.
- **Container hostname PID isolation (L-2 / ISSUE-10).** Embedded hardware/container network machine identity (`_LOCAL_MACHINE_ID` via `uuid.getnode()`) into lock payloads and tickets (`.git-lock.d/<nonce>.json`). Dead-PID fast-reclaim verifies both hostname and machine identifier before reclaiming locks, preventing false lock theft across container fleets or VMs sharing hostnames.
- **Unified GC lease renewal (G-1 / ISSUE-15).** Streamlined remote repository garbage collection (`gc.compact_repository`) to use `RemoteLock.maybe_renew()`, guaranteeing continuous lease maintenance across multi-pack downloads, repack, upload, and cleanup.
- **Owned library disambiguation hardening (C-1 / ISSUE-09).** Enhanced duplicate library disambiguation in `client.py:get_repo_id` to verify repository `owner == username` alongside Seafile API `type in ("repo", "mine")`, accurately disambiguating owned libraries across diverse server API deployments.
- **Lazy directory cache initialization (C-2 / ISSUE-13).** Documented and preserved lazy `_known_dirs` set initialization on `SeafileClient` for unit testing instances initialized via `__new__`.
- **Design specification nuance update (D-1 / ISSUE-14).** Refined `DESIGN.md` §7.1 to accurately describe clock skew mitigation and the same-second client timestamp and nonce fallback ordering.

## [0.4.5] - 2026-10-08

In-transfer push streaming lock lease renewal, orphan candidate ticket teardown, remote GC lease heartbeat, Windows PID liveness access-denied handling, and incremental Git LFS transfer progress events.

### Added & Optimized

- **In-transfer push lease renewal (ARC-01 / H-1).** Restored and hardened in-transfer distributed lock renewal during packfile pushes via `StreamingMultipartFile` in `client._upload`, renewing the remote lock every 20 seconds of sustained upload progress without spawning background daemon threads.
- **Orphan candidate ticket teardown (ARC-02).** Wrapped `RemoteLock.acquire()` in robust `try...finally` teardown to immediately delete candidate ticket files (`.git-lock.d/<nonce>.json`) upon acquisition abort, failure, or timeout.
- **Remote GC lock lease heartbeat (ARC-03 / G-1).** Added periodic lock lease renewals (`lock.maybe_renew()`) throughout `compact_repository` in `gc.py`, preventing lock expiry during long packfile downloads, index generation, repack operations, and remote cleanup.
- **Windows process liveness permission handling (ARC-04).** Handled `ERROR_ACCESS_DENIED` (error code 5) in Windows `_is_pid_alive()`, ensuring system processes and processes across different user sessions are correctly treated as running rather than erroneously reclaimed.
- **Incremental Git LFS transfer events (ARC-06).** Added streaming chunk progress reporting to `client.download_file_to` and wired real-time progress callbacks into Git LFS transfer agent downloads and uploads.
- **Zero-copy staged packfile streaming.** Added optional `staged_dir` support to `git_util.create_packfile` allowing zero-copy direct streaming into multipart uploads without intermediate disk duplication, and ensured stateless upload progress tracking on stream rewinds.

## [0.4.4] - 2026-10-08

Documentation hardening, Mermaid diagram rich display fixes for GitHub, expanded CLI reference guide,
and engineering roadmap maintenance.

### Documentation & Maintenance

- **GitHub rich display fixes.** Resolved "Unable to render rich display" failures on GitHub by converting
  flowcharts to modern strict syntax (`flowchart TD`), quoting edge labels containing parentheses
  and special characters (`|"REST API (/api2)"|`), and replacing unclosed angle brackets in sequence diagrams.
- **CLI subcommands quick-reference table.** Added a top-level CLI table to `README.md` and expanded
  terminal output walkthroughs in `USER_GUIDE.md` for `lock-status` and `unlock [--force]`.
- **Remote storage specification alignment.** Updated `DESIGN.md` directory layout with `.git-lock.json`
  and `.git-lock.d/<nonce>.json`.
- **Roadmap grooming.** Streamlined `ROADMAP.md` by clearing completed deliverables (v0.4.1–v0.4.3) and
  focusing exclusively on the active architectural backlog.

## [0.4.3] - 2026-10-08

Ticket-based distributed lock protocol, in-transfer lease renewal without daemon threads,
stale lock fast-reclamation for dead local processes, remote D/F ref conflict directory pruning,
and lock management CLI subcommands (`lock-status` and `unlock`).

### Added & Optimized

- **Ticket-based distributed locking (H-5 Phase 3).** Evolution of the cooperative lock protocol
  to deterministic ticket files (`.git-lock.d/<nonce>.json`). Tickets are ordered by Seafile server
  `mtime` and HTTP `Date:` response headers to eliminate client clock drift and write-write collision races.
  Maintains mirrored `.git-lock.json` for full backward compatibility with older clients.
- **In-transfer lock lease renewal.** Streaming multipart file uploads hook progress updates directly
  into `RemoteLock.renew()`, refreshing the lock lease every 20 seconds of sustained upload progress
  during multi-gigabyte packfile transfers without spawning background daemon threads.
- **Dead local PID fast-reclaim.** Stale locks left by crashed processes on the same machine are verified
  via cross-platform PID liveness checks (`OpenProcess` on Windows, `os.kill` on Unix) and immediately
  reclaimed without waiting for the lease timeout.
- **Remote D/F ref conflict prevention (M-6).** Deleting nested refs (e.g. `refs/heads/feature/auth`)
  automatically and recursively prunes now-empty parent directories up to `refs/heads` on Seafile,
  evicting them from `client._known_dirs` and preventing directory/file ref collision errors.
- **Lock inspection and management CLI.** Added `git-remote-seafile lock-status <url>` to inspect active
  lock holder metadata and expiration, and `git-remote-seafile unlock <url> [--force]` to release
  or forcibly break locks.

## [0.4.2] - 2026-10-08

Multi-spec packfile batching, delta compression path hint preservation, Git LFS transfer agent
hardening, unified Seafile client path discovery, and code hygiene.

### Added & Optimized

- **Multi-spec packfile batching.** Multi-branch pushes (`git push origin b1 b2`) now batch all
  missing objects across all push specs into a single `.pack` and `.idx` upload rather than
  creating and uploading separate packfiles per branch.
- **Delta compression path hint preservation.** `get_objects_to_push` preserves full `<sha> <path>`
  lines from `rev-list --objects` when feeding `git pack-objects`, restoring path-name hashing
  for optimal delta compression windowing.
- **Git LFS transfer agent hardening.** Validates OID strings against strict patterns to prevent path
  traversal (`..`), checks remote object existence and byte size to skip redundant uploads, and
  reports standard `progress` events during transfers.

### Refactored & Code Hygiene

- **Unified client path discovery.** Extracted desktop client configuration and SQLite database
  path resolution into a shared `seafile_paths.py` module, deduplicating search logic across
  `client.py`, `safety.py`, and `tools/seafile_doctor.py`.
- **Mock-guard cleanup.** Replaced defensive `getattr`/`hasattr` guards on client and helper
  instances with proper default class attributes and property descriptors.

## [0.4.1] - 2026-10-08

URL percent-decoding, remote branch verification on `set-head`, compaction and CLI diagnostics,
fast-forward error clarity, CI release test gating, and action SHA pinning.

### Fixed

- **URL percent-decoding.** Added `unquote()` to path segments in `_parse_url`, preventing
  double-encoding of escaped characters (e.g. `%20` for spaces) on subsequent API requests.
- **Remote branch verification in `set-head`.** Verifies that the destination branch exists
  in remote refs before uploading the `HEAD` pointer, preventing dangling references.
- **Compaction pack download failure logging.** Emits a stderr warning naming any packfile
  whose download fails during compaction in `gc.py`.
- **CLI `--min-packs` argument validation.** Emits a stderr warning on non-positive or
  non-integer `--min-packs` inputs, gracefully falling back to default.
- **Fast-forward error clarity.** Distinguishes git merge-base exit code 1 (diverged history)
  from exit code >=128 (unknown remote tip locally), reporting `fetch first` rather than
  `non-fast-forward`.

### CI & Tooling

- **Release workflow test gate.** Gated `build`, `publish-to-pypi`, and `github-release`
  jobs in `release.yml` on a dedicated unit test suite run.
- **Supply chain action pinning.** Pinned all GitHub Actions across `ci.yml` and `release.yml`
  to immutable commit SHAs with semantic version comments.
- **Published roadmap.** Published comprehensive project backlog and release milestones
  in `ROADMAP.md` at repository root.

## [0.4.0] - 2026-10-08

Pure-Python streaming multipart transfers, streamed packfile downloads with integrity
verification, configurable distributed locking, Git revision walker exclusion fix,
credential scoping hardening, and automated preflight guardrails.

### Fixed

- **Multi-branch push exclusion inversion.** Replaced alternating `--not` revision
  walker flags with `^<sha>` syntax, ensuring merge base exclusions are correctly
  applied when pushing multiple branches.
- **Credential scoping to look-alike domains.** Replaced substring SQL `LIKE`
  matching in `accounts.db` discovery with exact parsed hostname and netloc
  comparison, eliminating potential token exfiltration to look-alike hosts.
- **Safety preflight guardrail bypasses.** Identified local synced libraries by stable
  `repo_id` rather than local folder name, and treated worktrees placed directly at the
  library root as Trap 1 collisions.
- **Restricted push ref namespaces.** Enforced strict validation preventing pushes or
  deletions outside `refs/heads/` and `refs/tags/` to protect references from
  garbage-collection purging.
- **Contradictory protocol output.** Narrowed error handling in `cmd_push` to a
  per-ref scope and tracked reported specs, preventing duplicate `error <dst>` lines
  for refs already reported `ok`.
- **Empty repository push lockout.** Permitted empty repository listings during
  `list for-push` so pushes to freshly initialized remote repositories succeed without
  error.
- **Ref SHA validation.** Enforced 40-character hex regex validation on all remote
  ref payloads, rejecting corrupt responses or HTML error pages.
- **Direct session timeouts.** Added explicit timeouts to all raw `session.get()`
  calls in `cli.py` and `safety.py`.
- **Deterministic library name disambiguation.** Resolved duplicate library names by
  prioritizing owned libraries (`type="repo"` / `"mine"`) over shared libraries, and
  failing with actionable diagnostics naming candidate UUIDs on true ties.

### Performance

- **Pure-Python streaming multipart uploads.** Built an in-memory streaming
  multipart generator (`StreamingMultipartFile`) calculating exact `Content-Length`
  without buffer copies. Measured peak memory on 64 MB transfers dropped from
  142.8 MB to 0.15 MB.
- **Streaming packfile downloads with integrity verification.** Converted `cmd_fetch`
  to stream packfiles directly to temporary staging files on disk via `download_file_to`,
  verifying byte lengths against server metadata before calling `git index-pack`.

### Changed

- **Configurable distributed lock lease & timeout.** Introduced `seafile.locklease`
  (default: 60s) and `seafile.locktimeout` (default: 15s) Git configuration keys,
  transitioned internal retry loops to monotonic clocks, added PID to lock payloads,
  and implemented post-write acquisition verification to catch concurrent races.
- **Desktop URL explicit scheme generation.** Emitted explicit `seafile://http://...`
  URLs for local/LAN HTTP servers in `desktop-url` to prevent forced HTTPS redirection.

## [0.3.3] - 2026-10-08

Distributed locking nonce to prevent same-host lock theft, server URL credential
scoping, and streamed packfile transfers during garbage collection.

### Fixed

- **Cooperative lock theft on same machine.** `RemoteLock` now writes a unique
  acquisition nonce (UUID) alongside the machine identifier. When a process whose
  lease expired subsequently releases the lock, it verifies that both the machine
  identifier *and* the acquisition nonce match, preventing it from inadvertently
  deleting a newer lock acquired by another process on the same machine.
- **Server URL credential scope in `SeafileClient`.** When an explicit `server_url`
  is configured, credential resolution no longer falls back to unrelated server
  accounts found in `accounts.db`, `~/.git-seafile.json`, or environment variables.
- **Truncated quote stripping on URL responses.** Seafile upload and download
  links with trailing newlines (e.g. `"<url>"\n`) are now stripped of trailing
  whitespace before removing surrounding quotes, preventing literal quotation
  marks from corrupting outbound transfer URLs.
- **SQLite WAL sidecar copying in `seafile_doctor.py`.** `open_ro` now copies
  `-wal`, `-shm`, and `-journal` sidecar files to temporary storage alongside
  the main database, preventing missing tables or empty queries when inspecting
  active desktop client databases.

### Performance

- **Packfiles stream during garbage collection.** Consolidated packfiles in
  `gc.py` are streamed to the server using file path descriptors, and downloads
  stream chunk-by-chunk to disk via `download_file_to`, preventing multi-gigabyte
  packfiles from being buffered entirely in memory.
- **Multi-branch push exclusion optimization.** `cmd_push` now includes all
  known remote ref SHAs from the cached ref table in the pack exclusion list,
  preventing duplicate object packing when pushing multiple merged branches.

### Added

- **Direct module execution entrypoint.** Added `git_remote_seafile/__main__.py`
  to allow running the tool directly via `python -m git_remote_seafile`.
- **URL path quoting.** Path query strings (`?p=...`) across API endpoints are
  safely quoted with `quote(..., safe="/")`.

### Changed

- **Deduplicated `desktop-url` discovery.** `desktop-url` now delegates to
  `safety.discover_local_synced_libraries`, providing unified candidate path
  discovery and support for `~/.config/seafile/repo.db`.

## [0.3.2] - 2026-10-08

CI lints now, and the first lint run found real defects — including a test that
had never run.

### Fixed

- **A test that had never run.** `test_remote.py` defined
  `test_cmd_push_non_fast_forward_rejected` **twice** in the same class; the
  second definition silently replaced the first, so the first was dead rather
  than merely redundant. Discovery yields exactly *one* test id for that name —
  two definitions, one test — which is why a green suite never noticed. This was
  the cross-review's item #9, recorded as fixed and not. The two bodies differed
  only in the fixture SHAs they set, so deleting the shadowed copy is
  behaviour-neutral, which is precisely what made it invisible.
- **Six unused imports, two unread loop variables, and a dropped exception
  cause.** `ruff` turned up `shutil`/`tempfile` in `tests/e2e_harness.py`,
  `print_help` in `tests/test_cli.py`, `MagicMock` in `tests/test_git_util.py`
  and `sys` in `tests/test_safety.py`; `rid` in `cli.py` and `name` in
  `tools/verify_docs_guards.py` were iterated but never read — both fixed by
  *using* them rather than renaming them to `_`, since a name that is there to
  be read should be read; and `lock.py` re-raised inside `except ... as ex`
  without `from ex`, dropping the original cause from the chain.

### Added

- **CI runs `ruff` as its own job.** Pyflakes, syntax errors and bugbear, with
  the rule selection in `pyproject.toml` under `[tool.ruff.lint]` so a local
  `ruff check .` and CI cannot disagree about what is being checked, and with
  `target-version = "py39"` enforcing the declared interpreter floor rather than
  merely documenting it. The version is pinned, so a new ruff release cannot
  redden a green branch without anything in this repository having changed.

### Testing

- **`TestLintIsWired` keeps the lint job load-bearing.** A deleted lint job looks
  exactly like a passing one, so four tests pin the wiring: the job exists, it
  invokes `ruff check`, its version is pinned, and the rules live in
  `pyproject.toml`.

### Documentation

- `CONTRIBUTING.md` documents the lint step and how to reproduce it locally.

## [0.3.1] - 2026-10-08

Fixes from a cross-review of v0.2.1 by three independent reviewers, landing after
the v0.3.0 release. Each one is covered by a regression test that was watched to
fail before the fix.

### Fixed

- **Clone and fetch now run the pre-flight guardrails.** They previously ran only
  on push, so `git clone seafile://Documents/code/myproject` executed from inside
  the synced `Documents/` library was not blocked — it wrote a whole repository
  into the synced tree and started the download-reflection cycle the guardrails
  exist to prevent. Trap 1 blocks fetch/clone; Trap 2 stays push-only by design,
  since a fetch uploads nothing. During `git clone` the helper runs in the parent
  of the directory being created, which is not yet a repository, so Git cannot
  name a working tree — the check falls back to its own working directory.
- **A failed fetch fails instead of reporting success.** `cmd_fetch` caught every
  exception, wrote the protocol terminator and returned, so a transport error
  looked like a successful fetch of nothing: `git clone` finished with an *empty*
  repository and exited 0. It now exits non-zero with a diagnostic naming the
  pack and the failure.
- **Transfers go through the session and retry transient failures.** Download and
  upload used bare `requests.get`/`post`, bypassing connection reuse, TLS/proxy
  settings and retries, so a dropped connection part-way through a packfile
  download failed the whole fetch. Retries cover `429/500/502/503/504`; uploads
  are never re-sent, because a retried `POST` can duplicate work we cannot undo.
- **LFS payloads stream instead of being buffered in RAM.** Upload read the whole
  object with `f.read()` and download fetched it whole with `get_file_bytes`,
  putting roughly twice the object size in memory — the one documented claim
  ("multi-gigabyte models and datasets") the code contradicted outright. Objects
  are now streamed in both directions.
- **Returned file-link hosts are no longer forced onto the API server.** The
  rewrite fixed a reverse proxy reporting an internal host, but broke
  split/clustered deployments, where the file server genuinely lives elsewhere
  and the rewritten URL 404s. See `seafile.forcefilehost` under Added. Completing
  a *relative* link against the server still always happens, and a link naming the
  *same host and port* as the server over plain `http` is still corrected — see
  the next entry.
- **A plain-`http` upload link no longer breaks every push.** Seafile can answer
  the upload-link call with `http://<your-server>/seafhttp/upload-api/...` while
  your server is `https`. A `POST` to that URL is answered with a `301`, and
  `requests` re-issues a redirected `POST` as a `GET`, which the upload endpoint
  rejects with `HTTP 400` — so the push died at lock acquisition, before a single
  object moved, as `Failed to upload .git-lock.json to /seafile: HTTP 400`.
  Downloads were unaffected, because a redirected `GET` is still a `GET`, which
  is why only pushes failed. The scheme is now corrected whenever the link names
  the same host and port as the server, and never downgraded in the other
  direction. Upload errors also name the endpoint that was hit, minus the
  token-bearing path.
- **`desktop-url` no longer requires a token.** It only needs to know which
  server to name, so it failed for users with the desktop client installed but no
  reachable token. It now reports "cannot determine the Seafile server" instead of
  a credential error when the server itself is missing.
- **A dotted library name is no longer read as a host.** `seafile://my.lib/repo`
  was parsed as server `https://my.lib` with library `repo`, making the library
  `my.lib` unreachable via the short URL. The host form is now only recognised
  when a library *and* a path follow it.
- **URL parsing rejects a server with no library.** `seafile://seafile.example.com`
  is now reported as a missing library immediately, rather than after a round trip
  as a baffling "library not found".
- **An unknown subcommand reports itself.** A typo such as
  `git-remote-seafile chck-auth` was passed to the helper as the *remote URL* and
  surfaced as an auth or library error. It now prints the help text and exits 2.
- **The declared build floor could not actually build the project.**
  `requires = ["setuptools>=61"]` alongside `license = "Apache-2.0"` — a PEP 639
  SPDX expression that setuptools only learned to read in 77. Nothing noticed
  because an isolated build installs the *newest* setuptools satisfying the floor,
  so only a distribution building from the sdist sees it: with 76.1.0,
  `python -m build --no-isolation` stops at `invalid pyproject.toml config:
  project.license`. The floor is now 77, and a test pins it to the feature in use
  rather than to a number someone has to remember to bump.

### Added

- **`seafile_doctor.py` ships in the sdist, and is documented.** It is the
  recovery tool for the synced-library trap the guardrails call a *fatal failure*:
  it reads the desktop client's `ccnet/seafile.ini` and `repo.db` to say which
  local library a working tree sits in, what state its sync is in, and which
  files a conflict left behind. It was previously kept out of the package and
  referenced by nothing — shipping it untested would have been worse, so it
  arrived with the 27 tests that cover all seven subcommands.
- **`seafile.forcefilehost`** (git config) / **`SEAFILE_FORCE_FILE_HOST`**
  (environment) — opt in to forcing download/upload links onto your configured
  server host, for a reverse proxy that returns an unreachable internal host.
  Default off; an explicit `0`/`false`/`off` in the environment overrides a
  `true` config.
- **`SeafileClient.download_file_to(repo_id, file_path, dest)`** — streams a
  remote file to disk instead of returning it as `bytes`.
- **`SeafileClient.upload_file`** now accepts a path or an open binary file
  object as well as `bytes`, and streams it. Because a file object is seekable,
  the request still carries a `Content-Length` rather than falling back to
  chunked encoding.
- **A `CHANGELOG.md`**, which this project should have had from the start.

### Changed

- **The version is declared once.** `pyproject.toml` no longer repeats it as a
  literal kept in step with `git_remote_seafile.__version__` by a test; the build
  reads the package attribute through `[tool.setuptools.dynamic]`. A test whose
  only job is to reconcile two copies of a fact reports the drift *after* someone
  has already shipped it, so the copy was removed rather than guarded.

### Testing

- **The suite runs across processes.** `python -m unittest discover tests` is
  single-threaded and `test_e2e` dominates it — every case shells out to a real
  `git` and starts a stub HTTP server — so CI and the edit/test loop use
  `tools/run_tests_parallel.py`, one worker per test class. Measured here: 204 s
  serial against 57 s at twelve workers. The runner has no dependencies and
  travels in the sdist.
- **The workflow actions run on Node 24.** `checkout` and `setup-python` still
  declared Node 20 and drew a deprecation annotation on every CI job; both, plus
  the artifact pair in the release workflow, moved to the lowest major whose
  `action.yml` declares `node24`. GitHub was already forcing them onto Node 24,
  so this changes the declaration and not the behaviour.

### Documentation

- `seafile.forcefilehost` and the remaining fetch memory ceiling are documented,
  along with the fact that the guardrails now cover clone and fetch.
- The DESIGN delete-endpoint description matched neither the code nor the reason
  for it: `refs/heads/feature/auth` is stored as a *directory*, and Seafile's
  `/file/` endpoint deletes files and directories while `/dir/` returns 404 for a
  file.

## [0.3.0] - 2026-10-08

Data-loss and silent-failure fixes, and the Python floor raised to 3.9.

### Fixed

- **Nested branch references are discovered recursively.** Refs with a slash in
  the name (`refs/heads/feature/auth`, `bugfix/*`, …) live on Seafile as a
  *directory* containing a file, and single-level listings silently omitted them.
  `clone`/`fetch` reported success while omitting whole branches, and `gc` then
  treated their commits as unreachable and **deleted them**. This was the only
  unrecoverable remote data-loss bug, and it was triggered by ordinary branch
  names.
- **`gc` refuses unsafe compaction** when no refs could be read or a repack
  produced no packs. A transient failure on the refs listing made a populated
  repository look ref-less, after which every object looked unreachable.
- **Failed operations no longer report success.** A 404 on the refs listing used
  to make `clone`, `fetch` and `ls-remote` exit 0 with an empty result.
- **Atomic packfile installation.** Packfiles are staged and published with
  `os.replace`, so a truncated or interrupted download can no longer poison the
  local repository.
- **Auto-compaction actually runs.** `seafile.autogc` self-deadlocked on the
  remote lock, then ran its scratch-repo commands against the caller's repository;
  failures were swallowed.
- **`mkdir_p` surfaces failure** instead of reporting success for a directory it
  did not create.
- **Remote lock hardening.** `release()` no longer deletes a lock another machine
  has taken over, and the lock payload no longer leaks the first characters of the
  API token.
- **Force-push over diverged history** no longer fails with `bad object` when the
  remote tip is a commit this clone has never fetched.

### Changed

- **Python 3.9+ is now required and enforced** at import, with a message naming
  the version found. The code had always used `str.removeprefix`,
  `str.removesuffix` and `pathlib.Path.is_relative_to` at runtime.
- **Live database reads.** The desktop client's `repo.db`/`accounts.db` are copied
  before being read, including SQLite's WAL sidecar files. Reading in place risked
  blocking the running client, and a main-file-only copy of a WAL database returns
  **no rows at all**, which silently disabled the pre-flight safety checks.

### Added

- **Offline end-to-end test harness**: a stub Seafile API plus a real `git`
  subprocess, exercising `list → push → fetch → gc` over a real socket, with fault
  injection for the failure paths a healthy server never produces. The bugs above
  were found with it, not with mocks.
- Authorship is disclosed as generative AI assistance from multiple AI systems.

### Testing

- 146 tests. CI reports every failing job instead of cancelling at the first one,
  and surfaces failing test names as annotations.

## [0.2.1] - 2026-10-07

### Added

- Comprehensive unit-test suites for `git_util`, `client`, `cli`, and protocol
  edge cases.

## [0.2.0] - 2026-10-07

### Added

- **Pre-flight safety guardrails**: Trap 1 (working tree / remote path collision)
  and Trap 2 (download reflection in a synced library), plus library-root
  pollution protection and typo suggestions.
- The `seafile-git/` subfolder convention for pushing inside a synced library.

### Fixed

- `delete_entry` now deletes both files and directories (Seafile's `/dir/`
  endpoint returns 404 when deleting a file).
- `mkdir_p` checks for existing directories, preventing duplicate numbered
  folders.
- Cross-volume packfile creation no longer fails with an "Improper link" error,
  and multi-line push errors are sanitised before reaching the protocol stream.

## [0.1.0] - 2026-10-07

### Added

- Initial release: a transparent Git remote helper over the Seafile Web API v2.1,
  with zero-config desktop-client token discovery, distributed locking, remote
  packfile compaction, and a Git LFS custom transfer agent.

[Unreleased]: https://github.com/tkittich/git-remote-seafile/compare/v0.5.4...HEAD
[0.5.4]: https://github.com/tkittich/git-remote-seafile/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/tkittich/git-remote-seafile/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/tkittich/git-remote-seafile/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/tkittich/git-remote-seafile/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/tkittich/git-remote-seafile/compare/v0.4.6...v0.5.0
[0.4.6]: https://github.com/tkittich/git-remote-seafile/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/tkittich/git-remote-seafile/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/tkittich/git-remote-seafile/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/tkittich/git-remote-seafile/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/tkittich/git-remote-seafile/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/tkittich/git-remote-seafile/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/tkittich/git-remote-seafile/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/tkittich/git-remote-seafile/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/tkittich/git-remote-seafile/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/tkittich/git-remote-seafile/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/tkittich/git-remote-seafile/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/tkittich/git-remote-seafile/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/tkittich/git-remote-seafile/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/tkittich/git-remote-seafile/releases/tag/v0.1.0
