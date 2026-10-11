# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-10-11

The snapshot tool ships. `tools/seafile_snapshot.py` captures a working tree — **including the files `.gitignore` excludes** — into versioned, byte-exact snapshots stored in a Seafile library, using the same transport the helper already provides. It closes the one gap the helper structurally cannot: a `.gitignore`d file never enters a commit, so no remote helper can ever carry it.

### Added

- **`tools/seafile_snapshot.py` — whole-tree snapshots over Seafile.** Pointed at a source directory, it maintains a separate *vault* repository whose `--work-tree` **is** that source, so the source stays read-only and untouched; the vault commits the tree as it lies on disk, secret files and build output included. `--remote <url-or-path>` pins the vault's Seafile destination. Every run is atomic — a vault-local index, `update-ref` with an expected old value, and a `seafile-snapshot.lock` — and **an unchanged tree produces no commit at all**, so a scheduler can run it as often as it likes without growing the remote.
- **Byte-exactness is enforced, not assumed.** Git for Windows sets `core.autocrlf=true` in its *system* config, so a fresh `git init` inherits it while `git config --global core.autocrlf` reports nothing. The vault pins `core.autocrlf=false` **and** writes `* -text -filter -ident` into the vault's own `info/attributes`, which outranks any `.gitattributes` copied from the source. An attributes override must name **every** attribute class it neutralises: `* -text` alone does not disable `filter`, and a source `filter=lfs` attribute silently stored a 130-byte LFS *pointer* with the real bytes uploaded nowhere. A plain `git clone` of the vault is lossy for the same reason — `info/attributes` is not transferred by a clone — so `--restore` clones `--no-checkout` and re-applies the settings before checking out.
- **`--restore` and `--verify`.** `--restore` rebuilds the tree into a target directory and verifies byte-exactness on **every** run using `git hash-object --no-filters`, the one hash check that reads the bytes on disk rather than the bytes a filter would produce. `--verify` additionally runs `git fsck` over the vault.
- **`--exclude <glob>` (repeatable), `--dry-run`, `--code-remote <name>`.** Excludes use an fnmatch matcher, so `*/node_modules` and `*dist*` mean what they look like rather than being root-anchored pathspecs that match nothing below the root.
- **`--allow-shared-remote`.** Pushing the vault to one of the *source's own* code remotes is refused by default: mixing a whole-tree backup (which legitimately contains `.env`, keys and build artefacts) into a shared code library is almost never intended. The refusal names the colliding remote; the flag overrides it deliberately.
- **Credential-shaped paths are reported at the end of every run.** Not a refusal — a full backup *should* capture `.env` — but a warning, so the first push is not the moment you find out.
- **A vault belongs to one source tree.** It records the source directory it first saw and refuses a run pointing a *different* tree at it: one vault is one linear chain of snapshots of one tree, and mixing two is not something a later reader could undo.
- **Neither the source nor the vault may live in a Seafile-synced folder.** Checked at startup — before anything is written — via `safety.discover_local_synced_libraries()`. The sync client rewrites files underneath whatever lives in a synced folder and competes with Git for every path, which is the exact problem the tool exists to avoid. The helper cannot see the vault (it is told about the *source* and nothing else), so this check is the vault's only line of defence.
- **`--code-remote <name>` must name a real remote** of the source. Without it a typo failed *after* the snapshot was built, leaving a commit for a run the user reads as failed.
- **Flags that would otherwise be ignored are errors.** `--ref` without `--restore` is the sharp one: a run meant to restore an *old* snapshot would quietly take a *new* one instead. The snapshot-only and restore-only flag sets are now mutually rejected, and `--no-push` with `--remote` is refused as contradictory.

### Fixed

- **Multi-machine snapshots were permanently broken.** The vault's next snapshot branched from the *local* branch rather than the remote tip, so a second machine's push was rejected `(fetch first)` **and still moved its local branch** — which made every subsequent run fail too, while the first run *looked* like it had worked. The run now fetches the remote tip and bases on it, retries once by re-parenting on a lost race, and rolls the local branch back to what the remote holds on failure.
- **`ensure_identity` read the wrong config scope.** It used `git config --get`, which reads through to the *global* config, so the source repository's own identity was never consulted — and the test for it was green for the wrong reason. Now `--local`.
- **Stale locks are reclaimed.** A `seafile-snapshot.lock` left by a killed process is detected via `pid_is_alive` rather than blocking every future run.
- **Nested repositories are detected and named.** A nested repo appears to Git as a submodule gitlink; the tool warns prominently on every run — recording only the gitlink, not the contents, so a restore would produce a broken directory — and says to back it up separately. (Capturing the contents via `update-index --cacheinfo` is measured and written up in SNAPSHOT.md §8 as future work.)
- **A shrunken upload source now fails the upload instead of silently sending a short body.** `StreamingMultipartFile.read` returned `b""` at the source's premature end, which urllib3 reads as EOF — so `requests` sent the declared `Content-Length` with fewer bytes and nothing raised anywhere.
- **`seafile://[::1]/lib/repo` parses.** The bracketed IPv6 bare-host form (with optional `:port`) is one address; only the unbracketed colon-bearing form is refused, with the bracketed spelling named.
- **A transient error resolving the target library is warned about** during the safety checks instead of silently degrading Trap 1/2 to folder-name matching, which on a renamed sync folder read exactly like "no collision possible".
- **`unlock` on an unlocked repository says "not locked; nothing to release"** instead of reporting a successful unlock that did nothing (`RemoteLock.unlock` now returns whether a lock existed).

- **A shrunken upload source now fails the upload instead of silently sending a short body.** `StreamingMultipartFile.read` returned `b""` at the source's premature end, which urllib3 reads as EOF — so `requests` sent the declared `Content-Length` with fewer bytes and nothing raised anywhere.
- **`seafile://[::1]/lib/repo` parses.** The bracketed IPv6 bare-host form (with optional `:port`) is one address; only the unbracketed colon-bearing form is refused, with the bracketed spelling named.
- **A transient error resolving the target library is warned about** during the safety checks instead of silently degrading Trap 1/2 to folder-name matching, which on a renamed sync folder read exactly like "no collision possible".
- **`unlock` on an unlocked repository says "not locked; nothing to release"** instead of reporting a successful unlock that did nothing (`RemoteLock.unlock` now returns whether a lock existed).

### Changed

- **Every option now has a default, and the vault remembers the rest.** A bare `python tools/seafile_snapshot.py` is a complete run: the source is the current directory, the vault is `<source>-vault` beside it, and the destination, branch and exclusions are written into the vault's own config (`snapshot.*`) at the end of the first run, so a scheduled job needs no arguments. Defaults resolve most-specific-first — the flag you passed, then what the vault remembered, then the source repository (a single `seafile://` remote names the vault's destination: `seafile://code/app` → `seafile://code/app-vault`), then the current directory. A positional `SOURCE` argument is accepted as well as `--source`; `--no-push` overrides the remembered or derived remote *for that run* (and, fixed after release, no longer erases it from the vault's memory).
- **The recommended exclusions became `--default-excludes`, and are never silent.** Previously they were printed as advice in `--help` only. They are still not applied by default: a snapshot is a backup, and the failure that cannot be forgiven is a backup that quietly omitted the file that mattered — `node_modules` is reproducible, `.env` is not, and the tool cannot tell them apart. It now captures everything and then **names the reproducible bulk it noticed**, with the exact `--exclude` flags to drop it. `--default-excludes` adds the set on top of any `--exclude`; `--no-default-excludes` is its explicit negation.

- **SNAPSHOT.md re-anchored to the shipped tool** (status header, the synced-library guard, the shared-remote check, nested-repo status, next steps); USER_GUIDE §15.4's restore examples now use the real flag set (`--restore --remote --into`), and the per-machine `refs/heads/snapshot-<host>` claim is corrected — the default is one shared chain, with `--branch` for per-machine chains.
- **A failing vault-config write is an error**, not a silent divergence between what the run believes it remembered and what the vault holds.

### Documentation

- **`SNAPSHOT.md`** — the snapshot tool's design document, in 17 sections: the gap, the approach that does not work (renaming `.gitignore`), the vault design, usage, defaults, multi-machine semantics, restore, caveats, the cost model measured on real pushes, prior art, an own-review section with the six issues found and fixed, and an analysis of whether the helper should grow hooks or support restic/Borg.
- **`USER_GUIDE.md` §15** documents the snapshot tool for users, and **`README.md`** lists it alongside the other tools.
- `SNAPSHOT.md` joins the docs-consistency guard's document set.

### Tests & Tooling

- **`tests/test_snapshot_tool.py`** — 75 cases across twenty classes, covering the byte-exactness traps, the exclude matcher, the restore verification, the multi-machine race, identity resolution, stale-lock reclamation, the shared-remote refusal, the new defaults and vault memory, and the argument validation.
- **Test suite ran each snapshot case twice, until a fixture split: 587 s → 206 s.** The parallel runner schedules one unit of work per *class*, and `unittest` inherits test methods — so when a test-bearing class was used as a base, its cases ran once under the base's own name and once under the subclass's. In `test_snapshot_tool.py` that was 49 of 124 cases duplicated (the module alone took 525 s instead of 151 s). The fixture now carries no tests, the module is split into twenty single-responsibility classes, and `tests/test_suite_shape.py` guards the whole suite against ever reintroducing a test-bearing base class.
- **The snapshot tool spent over half its subprocesses on config plumbing, now batched: a steady-state run is 28 → 16 git calls.** Every `git config` is a process, and on Windows each costs ~125 ms, almost all of it start-up. The remembered settings were read one key at a time and rewritten unconditionally on every run; they are now read in a single `--null --list` and written only when a value actually changed, and the byte-exactness pairs and the committer identity likewise. The parallel module time fell 167 s → 128 s on this alone.
- Suite: 463 → 576 tests across 112 targets, all green; docs guards 9/9. (539/110 was the state the tag originally pointed at; the release was cut after the four-way review's fixes joined — the release workflow had failed at the earlier commit and never published.)
- **`tests/test_suite_shape.py`** — a suite-shape guard: no class that defines a test may be subclassed.
- **Suite grew 539 → 576 tests across 112 targets** (the v0.8.0 figures predate the post-release fixes): the gc streaming-fallback tests actually disable streaming now (`del` on a spec-less MagicMock regenerates the attribute, so they passed via `dest.is_file()==False`, not the fallback); the machine-behind fast-forward test's clone actually succeeds into its own vault (it used to fail silently into machine A's, so the reconcile path never ran); the explicit-worktree safety test's ids now match so Trap 1 really evaluates; the sqlite temp-copy test asserts its precondition; the suite-shape guard resolves base classes across modules; and the corrected v0.8.0 claims (per-machine chains, synced-guard, nested-repo warning, shared-remote check) are pinned as absent from every shipped doc.

## [0.7.2] - 2026-10-10

Remote-helper dry-run support, plus the test-suite fix that had been reddening CI on the Windows 3.10–3.12 legs.

### Added

- **`git push --dry-run` now works.** Git sends `option dry-run true` ahead of the push commands and **aborts with `fatal: helper seafile does not support dry-run` (exit 128) if the helper answers `unsupported`** — before it writes a single push command — so the flag was simply unusable against a Seafile remote. The option is now accepted, and a dry run answers exactly as a push would by making the same four decisions (namespace, local ref, remote ref, fast-forward) while taking **no lock, building no packfile, and writing no ref**. Skipping the lock is deliberate: it is itself a side effect, and holding it would park a real push behind the settlement window to answer a question nobody acts on. Verified against a live remote — a dry run that reported a new tag left the tag list and `refs/heads/main` unchanged.

### Tests & Tooling

- **The lock-ordering tests no longer depend on the wall clock's resolution.** Four tests in `test_lock.py` stamped a peer ticket with `order_ts = time.time()` and then asserted that an `acquire()` a few hundred microseconds later lost to it. On Windows, `time.time()` is backed by `GetSystemTimeAsFileTime` with a **15.625 ms** granularity through Python 3.12 — 3.13 moved to `GetSystemTimePreciseAsFileTime` — so the two stamps landed in the *same* tick, the queue fell through the tied `order_ts` and the always-tied integer mtime to the **nonce**, and the test's own random `uuid4` sorted before the peer's literal nonce in roughly two runs out of three. This is what had been intermittently reddening CI on the `windows-latest` 3.10/3.11/3.12 legs, and only those. The fixture now stamps a ticket that models "already on the remote" strictly in the past, and a new test freezes the clock so the behaviour is pinned rather than left to chance. **No production change was needed**: a tie is still broken deterministically, so every contender agrees on the winner and mutual exclusion holds; only FIFO fairness degrades below the clock's resolution, which no ordering rule can recover.

## [0.7.1] - 2026-10-10

Review-closure patch: every finding from the October 2026 review cycle is closed, including two `UnboundLocalError` crashes in the commands a user reaches for when something is already wrong, and remote `gc` being broken outright on SHA-256 repositories.

### Fixed

- **`git-remote-seafile test` and `unlock` no longer crash on failure.** Both named `SafetyError` / `RepositoryLockedError` in an `except` clause whose `from … import` ran *inside* the `try` but after the first statement that can raise, so any earlier failure — a bad URL, missing credentials, the network — died with `UnboundLocalError` instead of the intended message. The names are now imported at module scope. These are precisely the two commands a user runs when the remote is already misbehaving, which is what made the crash worth a patch release.
- **Remote `gc` works on SHA-256 repositories.** The scratch repository was initialised SHA-1, so `index-pack` rejected the 64-hex packs and `repack` exited 128: `gc` was unusable on a SHA-256 remote, and with `seafile.autogc` on it retried forever while the pack directory grew without bound. The fix needs **both** halves — initialise the scratch repo with the remote's object format, *and* run `verify-pack`/`index-pack` with `-C <bare-repo>` so they inherit it (they run with `GIT_DIR` scrubbed). Either alone still fails.
- **Re-installing a pack no longer dies on Windows.** git writes pack and index files read-only (`0444`), and `os.replace` onto a read-only *destination* fails with `WinError 5` on Windows (POSIX only needs directory write permission). Both targets are now made writable before publishing.
- **`seafile://host:80/…` keeps its port and its scheme.** The port was dropped and the scheme forced to HTTPS, so `seafile://host:80/lib/repo` became `https://host` — redirecting to `:443` and disagreeing with the explicit `seafile://http://host:80/…` form. A port now decides the scheme: `:80` is `http`, `:443` is `https`, and any other port keeps its number over `https`.
- **A non-numeric port fails cleanly.** `seafile://my:lib/a/b` escaped as a raw `ValueError: Port could not be cast to integer value as 'lib'` — and, through the `test`/`unlock` bug above, as a traceback. It is now `Invalid Seafile URL format: invalid port 'lib' in host 'my:lib'`, naming the host it could not parse.
- **`unlock --force <url>` reads the URL, not the flag.** A leading flag was taken as the URL, producing a confusing "library not found: '--force'". Flags are now skipped before the URL is read, as in every other subcommand.
- **Library-typo suggestions can actually fire.** `RemoteHelper.__init__` resolved the repo id first and raised, so `check_preflight_safety`'s fuzzy-match block was unreachable and its unit test passed vacuously. The suggestion is now built where the miss is detected (`SeafileClient.get_repo_id`, from the listing it already fetched), which also removes a duplicate repo listing request.
- **A malformed `~/.git-seafile.json` and an unreadable `seafile-ignore.txt` now warn** on stderr instead of failing silently — the two `except Exception: pass` sites that could genuinely confuse.
- **Odd server responses are diagnosed rather than crashed on.** Three `list_dir` iterations lacked the `isinstance(entry, dict)` guard their two sibling call sites already had, turning a non-array response into `AttributeError`/`KeyError`; they now raise a describable error.
- **"Lost lock ownership" no longer means two different things.** `verify_ownership()` conflated "the lease lapsed and another client took over" with "the renewal upload failed transiently"; a transient failure no longer aborts the push with the takeover message.

### Changed

- **`acquire()` settles the ticket queue before claiming the lock.** A partial directory view could let two clients both read themselves as the winner. A short settlement window — new `seafile.locksettle`, default `1` — re-scans before committing. The cost is real: roughly 0.85 s per push per unit, so the README's and USER_GUIDE's "<1 s push" claims were corrected rather than left to mislead.
- **Dead branches removed.** `get_repo_id`'s cache re-validation could never run (an early return guaranteed the cache lookup was `None`), and `_detect_remote_object_format`'s pack-inspection fallback was unreachable (the loop over validated 40/64-hex SHAs always returned on its first element).
- **`refs.__all__` now exports `is_valid_ref_name`**, which `helper.py` imports — cosmetic, but it tripped static analysis.
- **`install_packfile` passes `--git-dir`** to its `verify-pack`/`index-pack` calls, matching `filter_existing_objects`.
- **`tools/seafile_doctor.py` uses the public `open_live_sqlite_ro`** context manager instead of the private `_copy_with_sidecars`; it previously closed its connection by hand at two of three exit paths.

### Removed

- **`publish-to-pypi` workflow job.** PyPI publication is deferred to the official Seafile repository once this code is merged upstream; this repository's releases ship wheel and sdist assets on GitHub Releases only. The job had never run (no credentials configured), so no behavior changes — the workflow just no longer carries a perpetually-skipped step.

### Documentation

- **`USER_GUIDE` §7 documents the URL scheme/port rule** behind the `seafile://host:80/…` fix, and the no-path form (`seafile://code` → the library root's `git-repo` subfolder) is documented for the first time.
- **`USER_GUIDE` §12 and §14 corrected.** The `unlock` example output now matches the real string (`Unlocked repository at seafile://…`), and the troubleshooting table no longer quotes a `fatal: remote locked by …` message that appears nowhere in the code.
- **`DESIGN` §7.1** now says ticket ordering prefers `order_ts` and falls back to `mtime`, matching §7.10 and the implementation.
- **`PROPOSALS.md`** no longer describes the `.git-lock.json` lock removed in v0.7.0, and spells the ignore file `seafile-ignore.txt`.
- README and DESIGN state the distribution decision explicitly: GitHub Release assets here, PyPI via the official Seafile repository upstream.
- **`CONTRIBUTING.md` gains a "Releasing" section** describing the tag-push process, which previously existed only in the workflow and its guards.

### Tests & Tooling

- **`v0.6.2` release notes restored.** The tag existed with no curated notes — the single gap in the v0.3.0-and-up series, invisible from both ends. A new guard fails the suite when a merged tag has no notes file, so it cannot reopen silently.
- **`gc.py` is fully covered** (76% → 100%) by 21 in-process tests for the recovery and fail-closed paths: truncated- and corrupt-index regeneration, the oversized-index abort, both lock-fence fallbacks, the verbose summary, and every refusal (no refs readable, failed repack, repack produced nothing, untrusted pack name, empty ref SHA). The e2e harness had been hiding them — it runs the helper through `subprocess.run`, which the parent coverage run never records.
- `install_packfile`'s staged-path branches, the `python -m` entry point, and `lock.acquire()`'s retry and failure branches are now tested; the gc test names the deleted packs instead of asserting a bare count.
- The Seafile ignore-file name is guarded in the docs-consistency suite, which must now name exactly one file and spell it canonically.
- Suite: 383 → 454 tests, all green; docs guards 7 → 9.

## [0.7.0] - 2026-10-09

Major cleanup: the legacy single-file lock is removed, the test monolith is split per module, the pack-download path is deduplicated, and Python 3.9 (EOL) is dropped.

### Removed

- **Legacy Single-File Lock (`.git-lock.json`).** The v0.1–v0.3 mirror — its writes in `acquire()`/`renew()`, its reads in `acquire()`/`release()`/`get_status()`, and its deletions in `release()`/`unlock()` — is gone; the ticket queue in `.git-lock.d/` is the only lock protocol. `acquire()` deletes an orphaned mirror found on the remote so existing repositories self-clean. **Upgrade note:** do not mix v0.7.0+ with a v0.1–v0.3 helper on the same repository — those clients predate the ticket protocol and cannot see its locks. Also removed with it: `SeafileURL.to_tuple()` and `RemoteHelper._parse_url()` (compatibility shims whose only callers were tests), the unused `run_git(cwd=)` kwarg, and **support for Python 3.9** (EOL October 2025 — `requires-python` is now `>=3.10`, the CI matrix and classifiers drop 3.9).

### Changed & Refactored

- **Test Suite Reorganized.** The `test_remote.py` monolith split into `test_lock.py`, `test_gc.py`, `test_lfs.py`, and `test_helper.py`; client-ops, config-reader, and url-parsing tests merged into their existing per-module files; duplicate set-head and url coverage dropped in favor of the per-module originals.
- **Shared Pack-Download Path.** New `packs.fetch_pack_artifact` owns the stream → capped-fallback → size-verify pattern that `fetch_and_install_pack` and `gc.compact_repository` each duplicated; gc's index downloads gain the size check fetch already had. Failure messages are unchanged.
- **`normalize_netloc` Public in `url.py`.** The netloc normalizer moves from `client.py` (imported privately by `url.py`) to the URL module as public API; the dependency now points client → url.
- **Small Cleanups.** `git-remote-seafile test` renders the ref listing for humans instead of leaking the wire protocol; `HEX_SHA_RE` is the single canonical name; the Windows launcher exits 127 with a clear message when python is missing; README install instructions match reality (GitHub-based — PyPI publication pending); the contrib Qt patch is described as a proposal needing a rebase.

## [0.6.4] - 2026-10-09

Review-closure polish: the Trap-1 clone guard now sees the actual clone destination, and the remaining verified LOW findings from the October 2026 review cycle are fixed or dispositioned.

### Fixed & Hardened

- **Trap-1 Clone Guard Resolves the Real Destination via `GIT_DIR`.** In `safety.py:check_preflight_safety`, when Git cannot name a work tree (clone), the fallback now resolves `GIT_DIR`/`GIT_WORK_TREE` before falling back to the process cwd. Measured against real git: during `git clone` the helper inherits `GIT_DIR` pointing at `<destination>/.git`, so `git clone seafile://Documents/code/x D:\Seafile\Documents\code\x` run from *outside* the synced library is now blocked — the case the cwd fallback could never see. GIT_WORK_TREE wins when set.
- **Implausible `seafile.*` Config Falls Back to Documented Defaults.** `RemoteConfig.load` refuses to honor a negative `seafile.locktimeout`, a zero/negative `seafile.locklease` (which would expire every lock immediately), or a sub-1 `seafile.gcthreshold`, warning on stderr and using the documented defaults instead.
- **Stale Library Cache No Longer Masks Deletion.** `SeafileClient.get_repo_id` verifies a cached id against the fresh `/api2/repos/` listing; a library deleted or unshared since the last lookup now raises a clean "library not found" instead of returning a dead id whose 404s surface downstream.
- **Unknown Directory-Entry Types Warn.** `refs.py:iter_refs` names entries whose Seafile type is neither `dir` nor `file` instead of dropping them silently — a silently skipped entry is how silent ref loss starts.
- **Git LFS Malformed JSON Lines Warn.** The LFS transfer loop writes the offending line to stderr instead of skipping silently, so a protocol desync is diagnosable rather than looking like a hang.
- **`set-head` Reports the Previous HEAD.** The success message now includes the value it replaced (or `unset`), an aid when recovering a remote that points at the wrong default branch.
- **Documentation & Contract Notes.** `download_file_to` documents that *dest* holds a partial file after a mid-stream exception; `is_path_ignored` documents its two deliberate leniencies (platform-wide case-insensitivity, wildcards crossing separators); `open_live_sqlite_ro` documents the torn-snapshot residual and the caller pattern that contains it; the lock deprecation note records the ticket/mirror divergence on partial renewal failure; USER_GUIDE notes the MAC-derived machine identifier's container caveat.

### Tests & Tooling

- **E2E Stub DELETE Fidelity.** `SeafileStub` now dispatches `/file/` (exact files only) vs `/dir/` (directories only, 404 for files) like the real server, so a future swap of `delete_entry`'s endpoint order cannot pass silently; `SeafileStub.packs()` takes the repo id instead of hardcoding it.
- New tests: GIT_DIR/GIT_WORK_TREE guard (blocked, unblocked, precedence) and config fallback behavior — suite at 398.

## [0.6.3] - 2026-10-09

Sound lease-renewal fencing for the distributed lock, lease renewal during gc pack downloads, non-zero `gc` exit on failed compaction, acquisition-time ticket ordering, and deletion-failure reporting.

### Fixed & Hardened

- **Lease Renewal Verifies Ownership Before Rewriting (fencing soundness).** In `lock.py:renew`, read the holder's ticket back before overwriting it: a ticket that is gone, unreadable, or carries a foreign nonce fails the renewal instead of resurrecting it. Previously `renew()` blindly re-uploaded the ticket and the legacy `.git-lock.json` mirror, so after a lease lapse and takeover by another client, `verify_ownership()` returned `true` for a lock the caller no longer held — letting gc delete remote packfiles (and push write refs) inside another client's push. The takeover is now detected and refused, making the pre-deletion fence in gc and the pre-ref-write fence in push sound. Covered by a new stateful-store takeover test.
- **Lease Renewal During GC Pack Downloads.** In `gc.py:compact_repository`, both `download_file_to` calls now receive a progress callback wired to `lock.maybe_renew`, so the lease is refreshed during multi-gigabyte pack downloads — previously the longest locked phase ran with no renewal at all.
- **`git-remote-seafile gc` Exits Non-Zero on Failed Compaction.** In `cli.py`, a `status: "error"` result (download failure, size mismatch, invalid pack name) now exits 1; `"ok"` and `"skipped"` still exit 0. Scripts and CI can finally tell an aborted compaction from a skipped one.
- **Ticket Ordering Uses the Recorded Acquisition Time.** Tickets now carry `order_ts` (captured once at acquire, preserved by renewal) and ordering prefers it when all tickets in a scan carry it. Previously the order key was the directory `mtime`, which renewal bumps — every lease refresh moved the healthy holder to the back of the queue, letting a later waiter win the scan. Scans mixing ticket formats fall back to mtime for all of them, preserving behavior against older clients (whose legacy mirror gates contention regardless).
- **GC Reports Deletions It Could Not Perform.** `compact_repository` collects `delete_entry` failures instead of ignoring them, warns on stderr, and returns `deleted_packs` / `deletion_failures` in the result dict.
- **`gc` Subcommand Resolves Config Through `RemoteConfig`.** The manual `gc` CLI path now passes `config=RemoteConfig.load()` to `compact_repository`, matching the push path (v0.6.2 wired only auto-compaction).
- **Env Credential Mismatch No Longer Silent.** When `SEAFILE_SERVER`/`SEAFILE_TOKEN` name a different server than the remote URL, the `SeafileAuthError` now says so instead of reading as "no credentials exist".

### Changed & Refactored

- **Public Directory-Cache Eviction.** `SeafileClient.evict_known_dir()` replaces the helper's reach into the private `_known_dirs` set after pruning empty ref-parent directories.
- **Server Time via Public API.** `RemoteLock._get_server_time` uses `SeafileClient.get_server_time()` instead of reading the private `_server_time_offset` attribute.
- **Dead Code Removal.** Dropped the unused `PACK_NAME_RE` imports and `_PACK_NAME_RE` aliases from `helper.py` and `gc.py` (both modules validate via `is_valid_pack_name`).
- **Documentation Accuracy.** USER_GUIDE/DESIGN now describe lease renewal as covering uploads *and* downloads, note the un-renewed local compute phases and the fence that covers them, scope the Trap-1 clone guard to the directory the clone is run from, and show the real 8-hex hash in the `lock-status` owner example.

## [0.6.2] - 2026-10-09

URL scheme case-insensitivity at the parser level, ref-name `@` conformance correction, real production wiring of `RemoteConfig`, installer-compat hardening without swallowed errors, and loud failure for unknown helper commands.

### Fixed & Hardened

- **URL Scheme Case-Insensitivity Completion (N-15 carryover).** In `url.py:parse_seafile_url`, match both the `seafile://` prefix and the embedded explicit http(s) scheme case-insensitively per RFC 3986; previously only the argument inspection in `cli.py` was lowercased, so user-typed `SEAFILE://...` or mixed-case URLs were silently mis-parsed (e.g. an uppercase host-looking segment became the server URL). The `desktop-url` subcommand now also classifies mixed-case server URLs as http vs https correctly.
- **Ref Name Conformance Correction (N-12 refinement).** In `refs.py:is_valid_ref_name`, permit `@` as a path component — including `refs/heads/@`, which real git accepts per measured `git check-ref-format` output — and reject only the bare refname `@` (the HEAD alias) and `@{` sequences.
- **RemoteConfig Production Wiring Completion (N-6 carryover).** The optional `config=` parameters added in v0.6.1 were never supplied by any production caller; `cmd_push` now loads `RemoteConfig.load()` once per push and passes it to both the push lock (`RemoteLock`) and auto-compaction (`compact_repository`), so these values resolve through one documented dataclass for the first time.
- **Pack Installer Compat Without Swallowed Errors (N-7 hardening).** In `packs.py:fetch_and_install_pack`, custom installer callbacks are probed with `inspect.signature` instead of a retry-on-`TypeError` fallback; the old pattern could mask a genuine failure occurring after the staged files had already been moved into place.
- **Loud Failure for Unknown Helper Commands.** The helper command loop now replies `error unsupported` on stdout and writes a diagnostic to stderr for unrecognized top-level commands, instead of an empty line that git would read as a successful no-op.

### Documentation

- USER_GUIDE: documented the scoping behavior of `SEAFILE_TOKEN` when `SEAFILE_SERVER` is unset (the token is adopted for any server the remote URL names); the idle lock-status example now matches actual code output (`Repository at <url> is UNLOCKED.`).

## [0.6.1] - 2026-10-09

Wire-level Git remote helper `object-format` negotiation, fail-closed remote garbage collection, Git LFS progress delta calculation, Windows CLI stdio discipline, legal `@` ref names, and architectural wiring.

### Fixed & Hardened

- **Git Remote Helper Object-Format Protocol Negotiation (N-1).** In `helper.py`, advertise `option` and `object-format` capabilities in `cmd_capabilities`, reply `ok` to `option object-format true` in the command loop, and emit `:object-format <algorithm>` during `cmd_list` by detecting hash length on remote refs. Enables native Git push and clone of SHA-256 repositories without pack corruption errors.
- **Fail-Closed Remote GC Compaction (N-2, N-3).** In `gc.py:compact_repository`, make pack download loop strictly fail-closed: immediately abort compaction if any remote pack fails to download or verify size against directory listings. Step 7 only deletes packs recorded in `downloaded_packs`. Enforced 16MB buffer ceiling on in-memory downloads.
- **Git LFS Custom Transfer Agent Progress Delta (N-4).** In `lfs.py`, track `last_bytes` per transfer and emit incremental `bytesSinceLast = transferred - last_bytes` in `progress` events, preventing Git LFS throughput and ETA calculation errors.
- **Windows CLI Stdio Normalization across All Subcommands (N-5).** In `cli.py:main`, reconfigure `sys.stdout` (UTF-8, LF) and `sys.stdin` (UTF-8) unconditionally at the start of `main()`, ensuring `lfs-transfer` and other subcommands operate safely on Windows without `UnicodeEncodeError` or CRLF corruption.
- **Ref Name Validation Conformance (N-12).** In `refs.py:is_valid_ref_name`, permit legal `@` characters in ref names per `git check-ref-format` (e.g. `feature@v2`) while rejecting single-component `@` and `@{` reflog sequences.
- **Safety Typo Error Preservation (N-13).** In `safety.py`, gate typo suggestion queries strictly on 404 / not-found errors, preserving genuine API errors (HTTP 500, timeouts, duplicate libraries) rather than misleadingly reporting "Library not found".
- **Push Lock Ownership Fencing (N-14).** In `helper.py:cmd_push`, verify lock ownership lease (`lock.verify_ownership()`) immediately before writing remote ref files to prevent split-brain pushes if renewal failed during upload.
- **Case-Insensitive URL Scheme (N-15).** In `cli.py`, inspect `arg.lower().startswith("seafile://")` so uppercase schemes (`SEAFILE://`) are handled correctly.
- **Upload File String Content Safety (N-16).** In `client.py:upload_file`, treat `str` payloads strictly as text content and encode to UTF-8 without querying local filesystem paths.
- **Empty Host Validation in Explicit URLs (N-17).** In `url.py:parse_seafile_url`, reject explicit URLs with empty host components (e.g. `seafile://https:///lib/repo`).
- **LFS Scratch Storage Co-location (N-8).** In `lfs.py`, place temporary scratch directory under `.git/lfs/tmp` when `git_dir` is provided, preventing cross-volume moves on multi-drive setups.
- **Packfile Install Atomic Move Optimization (N-7).** In `git_util.py:install_packfile` and `packs.py:fetch_and_install_pack`, support `move=True` to move staged packfiles into staging atomically with `shutil.move` / `os.replace`, eliminating redundant disk copies.
- **Production Architectural Wiring (N-6).** Wired `RemoteConfig` dataclass from `config.py` into `RemoteHelper`, `RemoteLock`, and `compact_repository`.
- **SQLite Safe Reading Consolidation (N-9).** Reused `_copy_with_sidecars` and `_SIDECAR_SUFFIXES` from `sqlite_read.py` in `tools/seafile_doctor.py`.

## [0.6.0] - 2026-10-09

Architectural decoupling of the remote helper into modular subsystems (`url.py`, `config.py`, `packs.py`), deprecation of legacy v0.1–v0.3 single-file lock mirror, object-format capability negotiation, and review workspace archival.

### Changed & Refactored

- **Modular Helper Architecture (Sonnet §6 / Qwen §6).** Decoupled monolithic `helper.py` into dedicated, single-responsibility modules:
  - `url.py`: Seafile URL parsing, scheme extraction, port normalization, and path traversal validation (`parse_seafile_url`, `SeafileURL`).
  - `config.py`: Git configuration resolution with typed structured defaults (`RemoteConfig`).
  - `packs.py`: Remote packfile discovery, streaming downloads, integrity verification, and atomic installation (`fetch_and_install_pack`, `check_remote_has_packs`, `is_valid_pack_name`).
  - `helper.py`: Lean remote helper protocol implementation focusing strictly on stdio commands (`capabilities`, `list`, `push`, `fetch`).
- **Object-Format Negotiation & Capability Advertisement.** Supported `object-format` capability advertisement and negotiation in `helper.py` for sha1 and sha256.
- **Pack Name Validation Consolidation.** Unified remote packfile naming checks across `helper.py` and `gc.py` using `is_valid_pack_name`.
- **Deprecated Legacy Single-File Lock Mirror.** Marked the v0.1–v0.3 single-file lock mirror (`.git-lock.json`) in `RemoteLock` as deprecated (`.. deprecated:: 0.6.0`), scheduled for full removal in v1.0.0. All modern clients coordinate exclusively via the distributed ticket queue in `.git-lock.d/`.
- **Review Document Archival.** Archived all root `REVIEW*.md` files into the versioned `archive/` directory to maintain root workspace cleanliness.

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

[0.8.0]: https://github.com/tkittich/git-remote-seafile/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/tkittich/git-remote-seafile/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/tkittich/git-remote-seafile/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/tkittich/git-remote-seafile/compare/v0.6.4...v0.7.0
[0.6.4]: https://github.com/tkittich/git-remote-seafile/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/tkittich/git-remote-seafile/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/tkittich/git-remote-seafile/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/tkittich/git-remote-seafile/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/tkittich/git-remote-seafile/compare/v0.5.4...v0.6.0
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
