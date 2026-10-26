# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.13] - 2026-10-26

The six-model review round's fixes: a lease that outlasts its phases, an upload that stops when its push is already doomed, a fetch that can no longer wedge itself, and payload objects that survive a real server's create-or-duplicate rename. Two refusals are new (shallow pushes, ref names the server would store differently), and `--rebuild-index` is the git-side audit for edits no timestamp can see.

### Added
- `--rebuild-index` drops the vault's index before staging, so every git-stored file is read and hashed again — the counterpart of `--payload-rehash` for an edit that leaves size and timestamp untouched (a memory-mapped writer on Windows, a volume with no change time).
- A filesystem with no change time of its own (FAT/exFAT) gets the one-hour trust margin an unprobed share already had, and the run says how many files that applies to.

### Fixed
- **A local phase longer than the lease forfeited the lock.** A pack build or repack past `seafile.locklease` (60 s) left the ticket expired mid-phase; a peer reaped it and the push failed late with an orphan pack. The lease is renewed from inside the phase now, and an upload whose lease is provably lost stops instead of streaming a doomed push.
- **A truncated pack stayed at its final name** and wedged every later fetch and `gc`. The push removes what it posted when the upload cannot be verified, and publishes the `.idx` before the `.pack`.
- **An interrupted multi-pack fetch wedged the clone**: the tip resolved, so every later fetch skipped the download git then failed for. The skip now requires the tips' history to be locally whole.
- **A damaged payload object blocked every later snapshot** on a real server, whose rename onto a taken name creates a `<sha> (1)` copy instead of failing; the stale object is now replaced and the copies swept.
- **A fetch that met an empty listing advertised a branchless repository.** A namespace that exists and answers empty is a read failure now, not "no refs".
- **A push from a shallow repository published a ref whose parents do not exist**; it is refused, naming `git fetch --unshallow`.
- **A directory inside an embedded repository that cannot be read left a hole with exit 0**; the run stops and names it.
- A push whose ref name the server would store differently (not NFC-normalised, a double quote, over 255 UTF-8 bytes) is refused instead of silently overwriting a twin branch.
- A concurrent mkdir's `<name> (1)` duplicate is detected and refused at creation.
- `--json` writes its document on an interrupt and on unexpected exceptions; a wrapped `Error:` keeps its listed paths; the push's own warnings now count for `--exit-warnings` and reach the `warnings` array.
- A OneDrive placeholder below the payload floor is refused by name, as one above it always was.
- `--payload-view-replace` reports a refused delete of the old view instead of building into its remains; the manifest's `snapshot` id is validated before it names a server-side directory.
- The vault pins `core.fsmonitor=false` and `core.untrackedCache=false`, so a global fsmonitor no longer leaves a daemon watching the source tree after the run.
- A post-push payload-verification failure is a warning and a non-zero exit; the vault still remembers its settings, so the next scheduled run is not refused.
- A renamed branch whose namespace became empty removes `refs/heads` itself, so an existing-but-empty directory stays the read-failure signal it now is.
- `verify-pack` runs without `-v`, whose per-object output reached ~0.6 GiB of captured text for a 10M-object pack.

### Changed
- Manual RUNBOOK §3.2 states the real repair for a remote with a deleted pack: delete the ref and push it again — a plain push re-uploads nothing.

## [0.9.12] - 2026-10-22

A backup can now be proven restorable, a scheduled run can report in a form a script reads, and every long phase says how much is left. Nothing about what is stored changes, and no run's exit code changes unless `--exit-warnings` is passed.

### Added
- `--restore-drill [N|all]`: restore the newest snapshot into a temporary directory, check it byte-for-byte and with `git fsck`, verify a random sample of payload files against their SHA-256, and delete it.
- `--json`: one JSON document on stdout (status, exit code, warnings, error, result); the human text moves to stderr.
- `--exit-warnings`: exit 3 when a run completed with a `Warning:`. Off by default; the exit codes are documented in SNAPSHOT.md.
- Hashing, payload restore and `--payload-verify` print the same `progress: ... MiB/s, about ... left` line an upload does.

### Fixed
- A hashing pass killed before it finished lost everything it had hashed; the cache is now saved about once a minute while it runs.

## [0.9.11] - 2026-10-21

A run that fails on one changed file no longer throws its hashes away or stops the other uploads, a long upload says how much is left, `--default-excludes` covers any virtualenv, and every message the tools print follows one style and one unit system, with tests that keep it so. Nothing about what is stored changes.

### Added
- **A long payload upload prints how much is left.** Once a minute: `progress: 41.2 GiB of 292.0 GiB uploaded (14%), 52 MiB/s, about 1h20m left`. A line per file said nothing about an hour-long run.

### Changed
- **Guards so the message style and the byte units cannot drift again.** `tests/test_message_style.py` reads the source's string literals (not comments or docstrings) and fails on a notification that is not `Warning:`/`Note:`/`Notice:`/`Hint:`/`Error:` in title case, on a number followed by `KB`/`MB`/`GB`, on two byte formatters that disagree, and on a new `Warning:`/`Note:`/`Notice:`/`Hint:`/`Error:` message that no test mentions (`tests/untested_messages.txt` lists the 14 older ones and may only shrink). The convention is now written down in AGENTS.md and CONTRIBUTING.md.
- **One byte formatter.** `git_remote_seafile.units.format_bytes` for the package and its copy `human_bytes` for the snapshot tool (which runs without the package), replacing three that differed: the heartbeat now prints `2.9 MiB` where it printed `3 MiB`, the helper's `Uploading packfile` line prints `13.2 MiB` instead of `13489 KiB`, and gc's `saved ~100.0 KiB` has a decimal.
- **The snapshot tool's fatal error is `Error: ...`**, as the helper's always was; it printed `error: ...`.
- **`--default-excludes` now covers every Python virtualenv, not just `venv` and `.venv`.** A run with the flag still printed "Leave it out next time with --default-excludes" for `.venv-audit`, which the flag did not know. A directory holding a `pyvenv.cfg` is found by name-independent detection and added to the excludes (and so remembered like any other).
- **The staging heartbeat says how much is expected.** `still staging the tree ... 8m01s (10.3 GiB written to objects; up to about 28.6 GiB expected, at most 12m left)`, from the free-space estimate the run already makes (an upper bound; left out when there is none or the step has passed it).
- **One message style, one unit system.** Notifications use the documented title case (`Warning:`, `Note:`, `Notice:`, `Hint:`) -- a later batch of messages had drifted to `WARNING:`, `NOTE:`, `NOTICE:` and the placement line's suggestion was `tip:`, now `Hint:`. Byte counts use binary labels everywhere (`KiB`, `MiB`, `GiB`): the snapshot tool printed `292.4 GB` where its own heartbeat printed `GiB`, and the helper's `Uploading packfile ... (13489 KB)` and `gc`'s `saved ~N KB` were also 1024-based. The `saved_kb` field in gc's JSON output keeps its name.
- **A virtualenv is suggested for exclusion under any name.** The "reproducible build output" warning only knew `venv` and `.venv`; a directory holding a `pyvenv.cfg` (`.venv-audit`, `env`, `py312`) is now named too, unless an exclude already covers it. On a real tree that was 11,000 files.
- **Installed packages no longer trigger the credentials warning.** `certifi/cacert.pem` is a public CA bundle; files under `site-packages` and `node_modules` are not matched against the credential patterns (everything else, `.env` and keys included, is).
- **Plainer placement and tuning lines.** They now say what a cut-off means ("files that deflate by less than 50% go to payload") instead of quoting a sample ratio, and `--dry-run`'s help says it stages the git-bound tree like a real run (it uploads nothing).

### Fixed
- **The link probe no longer reads three streams as slower than one.** Run against the real server, the probe (four 8 MiB files) measured 35 MiB/s for one stream and 34 MiB/s for three at once: a transfer of a quarter of a second is mostly request latency, and the three new connections were set up inside the timed window. The probe now uses 16 MiB files, connects its workers before timing, and never reports an aggregate below the single-stream rate. A new real-server test (`SEAFILE_TEST_LIBRARY`) pins that the probe measures and leaves nothing behind.
- **A failed upload no longer throws the hashes away, and one live file no longer stops the rest.** The hash cache was saved only after a fully successful publish, so a run that failed on one changed file (a database something keeps writing) hashed the whole tree again next time -- 292 GB on a real tree. It is now saved whenever hashing finished, with the same trust rule as after a clean run (size, mtime, inode, ctime and device must still match). A file that changed or vanished after it was hashed no longer stops the queue: every other file is uploaded and the run then fails naming each such file, with `--exclude` as the way out. A server failure still stops the queue as before.

## [0.9.10] - 2026-10-20

A snapshot with the `auto` payload rule now says where each file went, can be tuned with `--payload-auto-ratio`, and a new vault measures its own machine and link once to choose the cut-off. An existing vault changes nothing: nothing moves between git and payload unless you type the flag. The storage profile for a vault that outgrew its first verdict is now decided before staging instead of after.

### Added
- **`--payload-auto-ratio R` tunes the `auto` payload rule.** A file of 4–64 MiB goes to payload when a sample of it keeps at least *R* of its size under a fast deflate; the default stays 0.9 for an existing vault (nothing moves unless the flag is typed; a new vault measures, below). A lower value sends moderately compressible data to payload instead of deflating it through git, which pays when deflate is slow next to the link and gives up git's delta between snapshots. It is remembered in the vault like `--payload-min-size`; a remembered value that is not a ratio is the default, not an error.

- **A run with the `auto` payload rule says where its files went.** A `placement:` line (files and bytes to payload and in git, and why) is printed on every run and on `--dry-run`, from the sample the rule already takes. When a cut-off of 0.5 would save a minute or more at this machine's deflate speed and the link's upload rate (measured from the data itself; the rates are assumed until a run has measured them), a `tip:` line names the flag and what it would move. It changes nothing.

- **A new vault measures its machine and link once and remembers the payload cut-off.** On its first real run, a vault with no earlier snapshot and a gigabyte or more of compressible 4–64 MiB files times deflate on that data and uploads four 8 MiB probe files (one, then three at once; deleted again) to the payload store, takes the break-even between git's deflate-then-push and payload's hash-then-upload, keeps it between 0.5 and 0.9, and remembers it with the two rates (`snapshot.payloadratio`, `snapshot.uplink`, `snapshot.uplink1`). It never re-tunes an established vault or one whose remote already holds payload objects, `--dry-run` measures nothing, a failed probe leaves 0.9, and `--payload-auto-ratio measure` asks again. The `tip:` line then uses the measured rates instead of assumed ones.

### Fixed
- **A vault that outgrew its `default` storage profile now switches to `raw` before it stages, not after.** The doubling check ran after staging, so the run that grew the tree (on a real 30 GB git-bound tree, 8 minutes of deflating) still used the slow settings and only the next run got the verdict. The check now runs first, from the same stat walk the sample uses, and the post-staging block is gone.

## [0.9.9] - 2026-10-20

`v0.9.8` was tagged but never released: its release job failed on one test, which is fixed here. Everything in 0.9.8 is in this release, so a snapshot run from a subfolder no longer files a partial tree on the repository's vault. Nothing in how a snapshot or a push is stored changes.

### Fixed
- **The Windows access-denied pid test no longer depends on the runner's process list.** It stubs `OpenProcess` instead of probing a fixed pid against the real API, so a runner that happens to run a process with that pid cannot fail it (it failed the `v0.9.8` release job on windows-latest 3.12, twice). Test only; no tool behaviour changes.
- **Everything in 0.9.8** (a snapshot of a subfolder is of the whole repository); see that section below.

## [0.9.8] - 2026-10-20

`git seafile-snapshot` run from a subfolder of a repository no longer files a partial tree on the repository's vault. Nothing in how a snapshot or a push is stored changes.

### Fixed
- **`git seafile-snapshot` from a subfolder of a repository no longer snapshots the subfolder onto the repository's vault.** The source's `seafile://` remote was found from the subfolder, so the folder's files were committed on top of the whole repository's `-vault` chain (a diff of thousands of deleted files). A snapshot is now of the whole repository: from a subfolder the source becomes the repository root, with a note, and an explicit `--source` naming a subfolder is refused. A vault that already remembers a subfolder keeps working.

## [0.9.7] - 2026-10-16

Upload retries obtain a fresh single-use upload link, stale remote locks from recycled local PIDs are fast-reclaimed,
and snapshot vaults display UTF-8/Thai paths cleanly on Windows. Nothing in how a snapshot or a push is stored changes.

### Fixed
- **Upload retries in `client.py` refresh the upload link.** On transient connection failures and 5xx errors, rewindable uploads obtain a fresh upload link before retrying, preventing immediate `HTTP 400 Bad Request` failures on Seafile fileservers that consume tokens on interrupted connections.
- **`seafile_doctor.py errors` printed nothing with default `--days 0`.** The summary and error-group formatting logic was indented inside `if args.days:`, producing blank output when run with the default CLI setting.
- **Stale remote locks from recycled local PIDs are fast-reclaimed.** A local lock ticket records process creation timestamps on Windows and Linux; `_is_pid_alive` verifies that an active PID matches the ticket's start time, reclaiming abandoned locks immediately even if the operating system reused the PID.
- **Doctor SQLite reads survive concurrent client syncing.** Reading `repo.db` retries up to 3 times on torn WAL snapshots.
- **Snapshot tool outputs clean UTF-8 paths on Windows.** Staging repositories and restores pin `core.quotepath = false`, preventing multi-byte UTF-8 filenames (e.g., Thai characters) from being octal-escaped in Git command outputs.

### Changed
- **Consolidated `repo.db` SQLite schema reading (T-4).** Factored `read_repo_properties` into `sqlite_read.py` with shared retry logic for torn live WAL databases, eliminating schema drift across `safety.py` and `tools/seafile_doctor.py`.
- **HTTP connection pool scaled.** Configured HTTP client session with `pool_connections=64` and `pool_maxsize=64` to prevent pool exhaustion warnings during parallel payload transfers.
- **Remediation hints on HTTP 401/403.** When resolving repository IDs fails due to authentication or permission errors, client outputs an actionable suggestion pointing to `git-remote-seafile check-auth` and desktop client login status.
- **Diff stat truncation.** Passed `--stat-count=35` in snapshot diff summaries to prevent large file listings from flooding console logs.
- **Standardized notification prefixes.** Unified user-facing notifications across snapshot tools and CLI with title-case prefixes (`Warning:`, `Note:`, `Notice:`, `Hint:`).
- **Hardened lock test fixture hostname.** Defaulted `_ticket` machine name in test fixtures to `foreign-<hostname>` so CI hosts named `nodeA` cannot accidentally flip foreign-ticket assertions.

## [0.9.6] - 2026-10-15

A push of a repository with long non-ASCII paths no longer fails on Windows. Nothing in how a snapshot or a
push is stored changes.

### Fixed
- **A push failed with `git pack-objects failed: fatal: expected object ID, got garbage` when a path was long.** `pack-objects --stdin` on git for Windows reads each `<id> <path>` line into a 302-byte buffer and takes the overflow for the next line; a Thai path is three bytes a character, so a 100-character name overflowed it. The path is only a delta-compression hint, so a line over 300 bytes is now sent as the bare object id. The pack is the same set of objects.
- **A path containing U+0085, U+2028 or U+2029 (or `\x0b`, `\x0c`, `\x1c`-`\x1e`) was cut in two** by the object listing, because it was split with `str.splitlines()`. It is now split on `\n` only.

## [0.9.5] - 2026-10-15

A Seafile served under a path now works (checked against a real server through a prefix-only proxy), the
synced-folder check no longer goes silent on a torn database, and the test runner stops hanging. Nothing in
how a snapshot or a push is stored changes.

### Upgrading from 0.9.4

- **A server under a path is set with `SEAFILE_SERVER`, the credentials file's `server`, or the desktop client's account** (see USER_GUIDE 7.1). A `seafile://` URL still cannot carry the path. A server without a path behaves exactly as before.
- **The desktop client's account URL is adopted when the remote URL names the same host and port**, so a path stored in the account is used. Previously only its token was.
- **`tools/run_tests_parallel.py` kills a target after `--timeout` seconds (default 1800)**; pass `--timeout 0` to restore the old unlimited wait.

### Changed
- **The parallel test runner no longer hangs silently or leaves processes behind.** A worker past `--timeout` (30 minutes by default) is killed with what it started and reported by name; Ctrl-C stops every worker's whole process tree and exits 130; a failed spawn closes its log; a held temporary directory on Windows cannot turn a finished run into a traceback; a target that ran no tests is warned about.
- **Six suite gaps closed.** Credential branches (`username`, the permission warning, a database with no `Accounts` table, a token-less account); `cmd_fetch` is fed `<sha> <ref>` as git sends it; the "empty" pack outcome is produced; the concurrent ref walk is asserted with a barrier rather than a sleep; the gc "preserves all branches and tags" test inspects the mirror it computed.
- **The server-under-a-path support (below) was checked against the real server** through a local proxy that serves Seafile only under `/seafile` (push, clone, lock-status: 75 requests, none outside the prefix).

### Added
- **A Seafile served under a path** (`https://corp.example.com/seafile/`) works: `SEAFILE_SERVER`, the credentials file's `server` and the desktop client's account URL may carry the path. API calls and relative transfer links go under it; the token stays scoped to host and port. The desktop client's account URL is now adopted when the remote URL names the same authority (it used to supply only the token, which left the path off). `desktop-url` prints the short form and names the setting for such a server. USER_GUIDE 7.1 documents it and the no-code alternative (a proxy host of its own).

### Fixed
- **A synced-folder check that could not read the desktop client's `repo.db` said nothing.** A copy torn by a concurrent write read as "no libraries" and the guard did not run. The read is retried (three times, a quarter second apart) and, if the file is there and still unreadable, one warning names it.
- **A restore that failed after the clone left a partial tree and the retry stopped with "destination is not empty".** The failure now says where the partial restore was left and that it has to be deleted first; it is not deleted for you.

## [0.9.4] - 2026-10-15

The remaining items of the 0.9.3 review round, all of them about trusting what a server says. Checked on the
stub, on Linux, and against a real server (CE 13.0.28, scratch library): the opt-in test tier, a payload
snapshot with restore and verify, and a full `gc` followed by a clone and `fsck`.

### Upgrading from 0.9.3

- **`gc` and the tail merge take longer by one download of the new pack** (about 0.015 s per MiB) while the lock is held: they now read it back and compare SHA-256 before deleting anything.
- **A snapshot run lists its payload objects once more after the push.** An object a `--payload-delete` on another machine removed meanwhile is uploaded again when the source file is unchanged; otherwise the run exits 1 naming it (the snapshot is already pushed).

### Added
- **`gc` and the tail merge read the new pack back and compare SHA-256** before deleting anything. The listing only proves a size, and a same-length substitution passed it. Costs one more transfer of the new pack under the lock already held.
- **A snapshot that pushed lists its payload objects once more afterwards**, re-uploads any a `--payload-delete` on another machine removed meanwhile, and fails naming those it cannot restore.
- **A ref deletion is read back**: a server that answers 200 without deleting is reported as an error, not `ok`.
- The unreadable-directory guard and the forced English git messages have tests that run on Windows.

## [0.9.3] - 2026-10-15

A third independent review round (GLM-5.3, Gemini, DeepSeek) on 0.9.2, every finding reproduced or refuted
before anything was changed, plus the small snapshot-tool backlog and a release pre-flight. The two that
mattered: full `gc` deleted old packs without re-reading the refs, and on Windows the payload hash cache
could vouch for a same-size in-place edit.

### Decided
- **A lock-ignoring writer that loses a last-writer-wins race is not rescued by a `gc` grace period.** Both pushes report `ok` and the loser's commits are pruned by the next full `gc`; keeping unreachable objects would close that at the price of a store that never shrinks. It stays documented (DESIGN 4.1, USER_GUIDE limitations): every writer must use the helper.
- **The default conversion of an existing vault to payload mode is announced, not refused**, and is now said again as the last line of the run on stdout and stderr, so a scheduler's log ends with it.

### Added
- **`tools/release_check.py`** answers, before the tag exists, what the release job answers after it: version against tag, the release notes' shape and compare-link base, the CHANGELOG section and reference link, a clean tree (and a warning when HEAD is not on a remote branch).

### Upgrading from 0.9.2

- **Windows: every large payload file is hashed once more on the first run.** The hash cache's "ctime" was Windows' *creation* time, which never moves, so a same-size in-place edit with the modification time put back was trusted and the old hash recorded (reproduced on NTFS, Python 3.14). The key now holds NTFS's real change time; entries written before differ in that field and are re-hashed once. POSIX is unchanged.
- **`unlock` without `--force` refuses a ticket held by a live process on this machine** (it used to delete it, and that run failed at its next fence). A dead holder is released as before.
- **`--payload-gc --payload-delete` re-reads the remote's refs every 25 deletions or 10 seconds**, and a change stops the run (exit 1, the remainder stays marked).

### Fixed (review round 3)
- **Full `gc` deleted the old packs without re-reading the refs.** A writer that ignored the lock and pushed while `gc` was repacking had its pack deleted and its ref left naming nothing (reproduced). The refs are read again after the upload is verified; a change deletes nothing and says so.
- **A reaped-but-present expired lock ticket was resurrected by `renew()`**, putting the stalled holder back ahead of the peer that took over. An expired ticket with a live peer behind it is now `LEASE_LOST`; a slow holder with nobody waiting still renews.
- **`get_file_bytes` enforced its size cap only against a declared `Content-Length`**, so a chunked body of any size was buffered whole (every ref, HEAD and lock ticket is read this way). The body is bounded as it is read.
- **A ref file whose content is an option (`--stdin`) reached `git merge-base`'s argv.** Such a value is "fetch first" now.
- **Deleting a nested ref could remove a sibling created a moment earlier**: the empty-parent prune is recursive and listed once. It lists again immediately before the delete.
- **The unreadable-directory stop and the index-lock hint read git's English stderr**; under another locale they silently stopped firing. Git now runs with `LANGUAGE`/`LC_MESSAGES` set to `C`.
- **`_SHA256_RE` used `match`**, which accepts a trailing newline; `fullmatch` now.
- **A failed branch-delete rollback was reported as "(deleted)"**; it says the branch moved.
- **Documents named tests that did not exist** (PAYLOAD R7, R8) and nothing checked; a guard now fails on any `test_...` a document names that is not defined. The hash-while-sending guard, which the whole suite passed without, has a test that fails without it.
- **Documentation said more than the code does**: the read-back catches only a peer that writes after us; the pack upload is a size check, not a content proof; a token is warned about, not withheld, over plain HTTP; Trap 2's failure direction; LFS objects are never reclaimed; directory listings are unpaged (measured at 130 entries).

### Fixed
- **A snapshot of a tree holding a file name that is not UTF-8 stopped at `--exclude`** (`git add` was handed a replacement-character path). Names now round-trip as bytes; messages that print one escape it.
- **`--restore`'s byte verification refused a snapshot containing a name with a newline, and mis-read one starting with `"`.** Those names are hashed one at a time now.
- **A vault lock that could not be written** (full disk, failing share) leaked its descriptor and left an empty lock file that blocked the next run for the grace period. It is closed and removed, and the failure is reported.
- **`--code-remote` together with `--no-push` is an argument error** instead of being ignored.

### Removed
- **The warning for the retired `seafile.locksettle` / `seafile.gcmaxmib` keys** (retired in 0.8.8). A value still in your git config is ignored without a message, and the helper no longer runs a `git config --get-regexp` for it on every operation.

## [0.9.2] - 2026-10-14

Two more review rounds, this time on the snapshot tool (Opus twice, GLM and Gemini on its specification), and
the two things 0.9.1 listed as known gaps: modification times of git-stored files now survive a restore, and
`git push --force-with-lease` works. The most important fix: a directory the tool could not read used to be
silently missing from the snapshot.

### Upgrading from 0.9.1

- **A directory that cannot be read now stops the run** and is named (`git add` only warned and carried on, so everything under it was missing from the snapshot with exit 0). Make it readable, or `--exclude` it on purpose. A scheduled job that had been silently skipping one will start to fail.
- **`--payload-gc` is stricter**: its history fetch fails if the helper cannot read a ref (`SEAFILE_STRICT_REFS`), and the refs are read again just before the first deletion; a push that landed meanwhile stops the run with nothing deleted. Re-run it.
- **`--no-embedded-repos` means the same on every run**, and a nested repository with no commit is captured (or, with that flag, refused with a message naming `--exclude`).
- **Windows: paths behind a junction are left out and reported**, so git, the payload walk and the embedded-repository walk agree about what the tree is.
- **The hash cache gains the device and the inode change time.** An existing cache adopts them on its first run (no rehash); a file whose `ctime` later differs is hashed again.
- **A weekly report starts its clock on a vault's first run** and reads the vault in place; nothing is downloaded for it.

- **A snapshot that pushes to a `seafile://` remote now also records the modification time of every git-stored file** in `.seafile-payload/mtimes.json`, and `--restore` puts the times back. Your vault gets one more file in its tree on the next run (about 60 bytes per file, rewritten whenever any file's content changes; at 100,000 files it is about 5 MB raw and 1.5 MB compressed, and the helper uploads each push's pack whole, so that is what a changed snapshot costs). `--no-record-mtimes` turns it off and the vault remembers that. Snapshots made before this restore as they did.

### Added
- **Restoring modification times for git-stored files** (`--record-mtimes` / `--no-record-mtimes`). The times are the real ones; when nothing in the tree changed since the last snapshot its record is reused, so touching a file does not make a new snapshot. (A first-seen time carried per file was written and removed: a source reverted to older content kept its old time and could sort before an object built from the version it replaced.) A restore applies the record with the same path checks as a manifest and skips what does not fit.
- **`git push --force-with-lease` is supported.** The helper accepts git's `cas` option: a push over a lease may rewrite history, but only over the value the user last saw (an empty expectation means "must not exist"), and is refused as `stale info` otherwise, for updates and deletions, in `--dry-run` too. The compare-and-swap re-check under the lock still applies. A malformed `cas` is answered `unsupported`, never guessed at.

### Fixed
- **The clock probe's debris and noise.** A probe file left at the source root by a killed run is no longer captured as content and is removed by the next probe once it is old; "the clock could not be measured" is said once per vault, not on every run.

### Fixed (review of the 0.9.1 snapshot-tool code, Opus)
- **A restore deleted a git file named `<payload file>.part`.** The download staged as `<name>.part` beside the payload file, so a real file of that name that git had just restored was overwritten and renamed away, and the restore still reported "verified byte-exact". Downloads now stage under `.git/grs-restore/`.
- **A share whose clock was 2 to 5 s behind kept a stale hash.** The 5 s "noise" floor was wider than the 2 s racy window on the unsafe side. A share behind by more than 0.25 s is now applied; only a share ahead (which costs a rehash, not a stale hash) is ignored below 5 s.
- **A nested repository with no commit failed every run** ("does not have a commit checked out"). Its files are captured; with `--no-embedded-repos` the error says so and names `--exclude`.
- **`--no-embedded-repos` was silently ignored from the second run on**, and the note that a nested repository's history is not backed up was printed on the first run only. Both are decided from the directories that hold a `.git`, on every run.
- **Windows junctions.** Git for Windows follows them and the payload and embedded walks did not, so a large file behind a junction went into git. Paths behind a junction are left out and reported, so all three walks agree.
- **The weekly report downloaded the remote's whole history** into a temporary repository, was due on the very first run, and ran before the run's settings were remembered. It reads the local vault, and runs last.
- **A manifest path using a code point HFS+ ignores** (`.git<U+200C>/hooks/pre-commit`) was accepted; the set git strips is now stripped.
- **Flags that were accepted and ignored:** `--payload-report-days` with `--no-payload`, `--dry-run` or a run without a seafile:// remote; `--no-embedded-repos` with the gc, verify and view tools. A preview no longer writes a clock-probe file into a share.

### Fixed (second review round, Opus on the new code; and the two R4 specification reviews)
- **`--no-embedded-repos` after a capturing run wrote a gitlink naming the vault's own commit**, not the nested repository's (`git` was run through the vault's `--git-dir`). It now asks the nested repository, refuses a repository with no commit (naming `--exclude`), and records only the outermost repository of a nest (a repository inside another crashed the run).
- **A junction that is itself a repository** was followed out of the tree; the junction check now includes the path itself.
- **A `--no-push` run dropped the times record** (and made a commit) on a vault whose last snapshot had one; a run with nothing typed or remembered now follows the last snapshot.
- **The weekly report copied the whole vault into the temporary directory** to read it; it reads the vault in place, and the first run of a vault only starts the clock.
- **The payload gc could decide from a listing that had skipped a branch.** The helper's listing for a gc history fetch is strict (`SEAFILE_STRICT_REFS`), so a ref it cannot read stops the run instead of making that branch's objects look unreferenced; and the refs are read again immediately before the first deletion, so a push that landed while the run was deciding stops it.
- **A restore could be told to write through a link into `.git`:** a symlink `tool -> .git` plus a manifest path `tool/hooks/pre-commit` passed the check that the path stays inside the destination. Protected directories are refused after links are followed.
- **A hostile manifest could fill the disk before its sizes were compared.** A download is cut off the moment it passes the size the manifest declares.
- **A share that could not be probed was trusted by the machine's clock** (the rule 0.9.1 had fixed, on exactly the shares where it could not be applied); it now needs a margin of an hour, as does any file on a different device than the source root. The cache key also holds the device and the inode change time, so an in-place edit that restores the old mtime is a new file; and a whole-second mtime is trusted with a doubled margin instead of never (a FAT or HFS+ tree was re-read on every run).
- **A directory git could not open was silently missing from the snapshot** (`git add` prints a warning and exits 0). The run now stops and names it; `--exclude` leaves it out on purpose.
- **A pid recycled by an unrelated process blocked every later snapshot** ("another snapshot is running") on Windows. The lock records the process's start time and reclaims a live pid whose start time differs.
- **A vault whose destination was derived from the code remote** now says, once, that it is the same library as the code and who can read it.
- **A view warns before it builds** when the snapshot is larger than the library's remaining quota.
- **Documentation:** the claims that objects are never overwritten, that the source is never written to, that a payload vault can snapshot offline and that `--payload-verify` covers every snapshot are corrected; `--payload-verify` scope, tag pushing, the single-line manifest and the 0.8.8 restore are stated. A test now pins that a tag is a pin under `--payload-keep`.

### Documentation
- **A correction to 0.9.1.** Its changelog listed "the conflict-copy warning missed refs with a dot" as fixed. Measured on the real server (CE 13.0.28), Seafile names the loser of a concurrent write `<name> (SFConflict <user> <date>).<last extension>` (`v1.0.0` becomes `v1.0.0 (SFConflict ...).0.0`), which the original prefix match already caught; the report was an inference from two reviews and was wrong. The extra match of the name without its last extension is harmless, and a real-server test now pins the observed naming.

### Changed
- **Listing the remote for a fetch no longer advertises half a repository.** A refs directory that the parent listing showed and that then answers 404 is retried once and then an error, instead of a warning followed by an advertisement without those branches. A ref file with unusable content is still only a warning, so one planted file cannot make a repository unfetchable.
- **The clock probe recognises macOS network mounts** (`smbfs`, `nfs`, `afpfs`, `webdav`, read from `mount`).
- **The payload test class is split in five** so the suite's wall time is about 1.5 minutes shorter than a single 109-test target (no behaviour change).

## [0.9.1] - 2026-10-14

A review release: three independent code reviews of the Git remote helper, and every finding that was a
defect is fixed. Two could lose data silently (`gc` and `push`), three leaked or misdirected a token, and
the snapshot tool now captures nested repositories as their files.

### Upgrading from 0.9.0

- **`gc` is stricter, on purpose.** A ref that cannot be read, or a refs directory its parent lists but that now reports 404, stops `gc` with nothing deleted. It used to carry on without that ref, and its commits were pruned. A re-run after the transient failure is the remedy.
- **A push does one more listing and one more check.** The remote refs are read again under the lock, and the pack directory is listed after the upload so every `.pack` and `.idx` is seen at its local size before a ref names it. On a slow link that is two extra requests per push.
- **A nested git repository is now captured as its files** (the nested `.git` is not). A vault whose source holds one gets those files on its next snapshot, which can be large. `--no-embedded-repos` keeps the 0.9.0 behaviour.
- **After a run that pushes, the tool may print a payload store report** (at most every 7 days, report-only). `--payload-report-days 0` turns it off.
- **Pack names must be lowercase** and a `seafile://` path may not contain a backslash. Nothing this tool or git writes is affected; a library holding hand-made names is.
- **A remote named like a subcommand (`gc`, `test`, `unlock`, `doctor`) now works** as a remote. Nothing changes for a person running the subcommand.
- **Set the library's history to a few days** (USER_GUIDE 8.0.1). It is advice, not a requirement, but full history keeps every pack `gc` removes.
- No format changed: 0.9.0 vaults, manifests and remotes are read as before, and `PAYLOAD_MIN_TOOL` stays `0.9.0`.

### Added
- **Embedded git repositories are captured as their files.** A directory with its own `.git` used to be recorded as a commit id and restore empty; its tracked, untracked and ignored files now go into the snapshot (subject to `--exclude` and payload mode), at any depth, and the run lists them. The nested `.git` is not captured. `--no-embedded-repos` keeps the old behaviour; a vault holding a gitlink converts on its next run.
- **A report-only `--payload-gc` summary after a run that pushes**, at most every `--payload-report-days N` (default 7, `0` off). It deletes nothing and a failing report is a note, not a failed run.

### Fixed
- **`gc` could permanently delete the commits of a branch it did not see.** The compactor mirrored each remote ref as a loose file in its scratch repository, so refs whose names collide on the local filesystem (`Feature` and `feature` on Windows and macOS, a device name such as `NUL`, a trailing dot) became one file, and the branch that lost had its objects pruned and its packs deleted. The mirror is now a single `packed-refs` file, and git must read back exactly the refs written before anything is repacked.
- **A lock that could not be renewed warned on every 16 KiB upload block.** Renewal attempts are now paced by attempt, not by success, and a lease shorter than 60 s is renewed at a third of its length (a `seafile.locklease` under 20 s used to lapse mid-transfer).
- **The conflict-copy warning missed refs with a dot** (`v1.0.0`, `release-2.1`): Seafile puts its marker before the extension, so both forms are matched.
- **`git push --dry-run :ref` said `ok` for a ref that is not there**; it now answers as the real push does. DESIGN.md said a missing deletion was a no-op, which was never what the code did; the text now matches the code.
- **A bare host segment containing `?`** was reported as a server name that was not the one requests went to; it is refused.
- **`--force-with-lease`** is documented as unsupported (it was rejected as non-fast-forward with no explanation), and the lock-wait wording in the guide now says what the code does.
- **A remote named like a subcommand ran that subcommand.** git runs `git-remote-seafile <remote name> <url>`, so `git fetch gc` ran a full compaction (`test`, `unlock` and `doctor` broke fetch the same way). The call is routed to the helper when `GIT_DIR` is set, stdin is not a terminal and the protocol's first word is waiting on it.
- **Scratch directories were never removed on Windows.** Git marks pack files read-only, so `rmtree(ignore_errors=True)` left every `grs-push-*` directory behind and the stale sweeper still counted it as removed. They are removed (read-only included) and counted only when gone.
- **A listing entry without a name crashed `gc` with `KeyError: 'name'`.** `list_dir` drops such entries once.
- **A returned transfer link could pick who received the account token.** The check that decides whether the Seafile token rides along parsed the link with `urlparse`, while the connection is made by urllib3, and the two read `http://other\@api-host/` differently. The comparison now uses the connection's parser, and a link whose authority holds a backslash, userinfo or a control character is refused.
- **Transfer tokens leaked into error text.** A connection failure during an upload or download carried the link's path (and its short-lived token) into stderr and the protocol `error` line. Transfer failures are now `SeafileAPIError`s that name only the origin.
- **A URL password was printed verbatim** by four helper messages (the defaulted-path note, the `set-head` advice, the `gc` tips). They go through `mask_url` now.
- **A remote pack named as the uppercase twin of a local one could overwrite it on Windows.** Pack names are accepted only in lowercase.
- **A backslash in a `seafile://` path defeated the working-tree collision guard on Windows.** It is refused in the URL parser.
- **Ref, HEAD and lock files were read whole with no bound.** A ref whose listed size is over 4 KiB is not read, and text files declared over 16 MiB are refused.
- **A push could publish a ref naming objects the remote did not have.** The pack left out whatever the refs read *before* the lock reached, so a branch deleted (and `gc` run) in between took its unique commits away while `main` was written pointing at them. The refs are now read again under the lock; if they cannot be, the whole pack is sent.
- **A ref was written as soon as the pack upload answered 2xx.** The push now lists the pack directory and requires every uploaded `.pack` and `.idx` at its local size before any ref is written, as `gc` already does before it deletes.
- **A multi-ref push was fenced only once.** The lease is verified before each ref, as a deletion already did, so a push that outlives its lease stops instead of writing refs beside another holder.
- **A landed push could be reported `remote rejected`.** A failure of the default-HEAD bootstrap after the ref was written and verified is now a warning that names `set-head`.
- **A ref that could not be read, or a directory that listed as missing, was skipped silently.** `iter_refs` now names it; `gc` treats it as an error (nothing is deleted), and a fetch or `ls-remote` prints a warning instead of quietly advertising fewer branches.
- **The payload hash cache's racy rule used this machine's clock on a network share.** A share whose clock ran behind made a freshly edited file look old enough to trust, so a same-size edit within one timestamp granule kept its stale hash. On a network filesystem the share's clock is now measured with a probe file and cache entries are made in that clock; a local source tree is never written to.
- **Long Windows paths.** The vault and a restored clone set `core.longpaths=true`, and payload files are restored through extended-length paths.

### Documentation
- **Library history setting.** README quick start and USER_GUIDE 8.0.1 recommend a period of history (7 to 30 days) for a library that holds remotes, vaults or payload objects, and say why neither full nor no history is right.
- Manifest growth at 100,000 files is measured (PAYLOAD.md D4): about 10 kB per snapshot at 0.1% changed, 110 kB at 1%, 6 MB when every entry changes.
- SNAPSHOT.md no longer lists a snapshot-time `gc` step as future work: the helper's `seafile.autogc` already does it.

## [0.9.0] - 2026-10-14

The snapshot tool grows up: it installs with the package, and it can carry a 131 GiB tree.

### Upgrading from 0.8.8

- **The snapshot tool ships in the wheel.** `pip install git-remote-seafile` now provides `git-seafile-snapshot` (so `git seafile-snapshot` works) and `git-remote-seafile snapshot`. `python tools/seafile_snapshot.py` keeps working from a checkout (it is a wrapper). The generated module is `git_remote_seafile/snapshot_tool.py`; edit `tools/_snapshot/*.py` and run `tools/build_snapshot.py` as before.
- **`--help` is grouped.** The payload options are in two groups; nothing was renamed or removed.
- **Payload mode is on by default (`auto`).** A snapshot that pushes to a `seafile://` remote now sends files of 64 MiB or more, and 4 MiB or more that do not compress, to the server by hash instead of through git. A tree with no such file is snapshotted exactly as in 0.8.8 (no manifest, no server directory), and a run that cannot use it (no remote, a remote that is not `seafile://`, `--no-push`) simply does not. `--no-payload` (or `--payload-min-size 0`) turns it off and the vault remembers that.
- **An existing vault that holds large files in git changes on its next run.** Those files leave the snapshot's git tree and are uploaded as payload objects: expect one full hash-and-upload pass, the large files stored twice on the server (the old blobs stay in the vault's history, the new objects sit under `payload/`), and `git diff` showing them deleted. Snapshots taken before the switch restore as before; snapshots after it need the payload objects, and **0.8.8 cannot restore them completely** -- it has no manifest support, so it would restore the tree without the large files. A plain `git clone` of a payload vault does not contain the large files either (see `.seafile-payload/README.txt`). To keep an existing vault exactly as it was, run once with `--no-payload`.
- **After a complete real run the vault remembers the setting** (`snapshot.payload`, `auto` or `0`), so a later change of the default cannot alter it.

### Added
- **Snapshot tool, payload mode (`--payload-min-size SIZE`).** Files of at least SIZE bypass git: each is SHA-256-hashed and uploaded **by hash** to `<path>/payload/objects/<xx>/<sha256>` inside the vault's remote directory, and the vault commit carries `.seafile-payload/manifest.json`. `--restore` downloads every file and verifies size and SHA-256. Objects are never overwritten or deleted, so a snapshot costs nothing to keep, a rename or duplicate uploads nothing, and no server name rule applies. Removes the local object copy and the staging pass for large immutable data. On by default (see Upgrading); design and measurements in SNAPSHOT.md section 15.1.
- **`--payload-min-size auto`** (also a bare `--payload-min-size`): payload when a file is at least 64 MiB, or at least 4 MiB and a sample of it does not compress. Measured on a 131 GiB tree: 7 GiB of incompressible files between 4 and 64 MiB join the payload; compressible ones stay in git.
- **`--payload-gc [--payload-delete] [--payload-grace-days N]`**: report, and on request mark and later delete, stored payload objects no snapshot references. Two-phase (an object must stay unreferenced for the whole grace period, minimum 1 day), it reads every ref of the remote and the local vault, refuses a manifest it cannot read or whose format it does not know, refuses when a ref is a Seafile conflict copy, and deletes only exact 64-hex names.
- **`--payload-view [REF]`**: a browsable directory of real file names for a snapshot, built by server-side copy and rename of the stored objects (no data moves); `--payload-view-name`, `--payload-view-replace`. `SeafileClient.copy_entries` returns for it.
- **`--payload-verify [N|all]`**: download a sample (or all) of the newest snapshot's payload objects and check their SHA-256, to find an object damaged after it was stored.
- **`--payload-keep N`** (with `--payload-gc`): only the newest N snapshots of each branch and tag count as using objects, so older ones expire through the same two-phase deletion.
- **`--no-payload`**: payload mode off for this run and remembered.
- **`--payload-no-vault` and `--payload-accept-loss`** for `--payload-gc --payload-delete` (see Changed).
- **Payload mode remembers its setting** in the vault after a complete real run (`0` included); **`--payload-rehash`** ignores the hash cache for one run.
- **`--payload-jobs N`** uploads N payload files at once (default 3; measured 30 s -> 19.7 s for six 128 MiB files, no gain at 5).
- **`SeafileClient.copy_entries` and `rename_entry`**: server-side copy and in-place rename within a library.
- **The snapshot tool in the wheel**: console script `git-seafile-snapshot` and the `git-remote-seafile snapshot` subcommand.

### Changed
- **Safeguards around payload mode** (PAYLOAD.md D13): every snapshot commit carries a `Git-Remote-Seafile: <version>` trailer and a manifest carries `min_git_remote_seafile`, so a run warns when it builds on a newer tool's snapshot and an old tool refuses a manifest it cannot read; `--payload-delete` needs `--vault` (or `--payload-no-vault`) and stops with `--payload-accept-loss` when the remote's history was rewritten since the last delete run or `--payload-keep` would expire snapshots; a fresh vault is refused when the remote's manifests use another threshold than the default; a restore that would not fit the disk is refused before downloading; an upload that exceeds the quota or goes into a library this machine syncs is warned about first; `--payload-verify` leaves a marker that the store line and the gc report show; files hashed again are explained.
- **Payload mode is on by default** (see Upgrading). A fresh vault on another machine adopts `auto` rather than being refused; a vault that used payload mode is still refused a run that would put its large files into git by accident.
- **A conversion is announced before it starts.** When the default is about to move an existing vault's large files out of git, the run prints how many files and bytes, and how to keep the vault as it was, before hashing or uploading anything (a preview says "would move").
- **`--payload-jobs` no longer needs `--payload-min-size`**; it is refused only when payload mode is explicitly off.
- **The vault carries `.seafile-payload/README.txt`** beside the manifest, saying where the large files are and how to restore.
- **A run that uploaded payload objects prints the size of the store** (they are never deleted until `--payload-gc`).
- **`SeafileClient.delete_entry` forgets the directories under a deleted path**, so `mkdir_p` re-creates a directory that was deleted in the same session (found rebuilding a view in place).
- **`--help` groups the payload options** and says what each defaults to.

### Fixed
- **`--restore --ref <rev>` failed** (`a branch named 'snapshot' already exists`) against any remote whose HEAD is the vault branch, which is what the helper advertises.

## [0.8.8] - 2026-10-13

Everything here follows from the 2026-10-12 review and the decisions taken on it, then from measuring the result against a real Seafile server (CE 13.0.28): a 4 GiB merge, a 5 GiB single-file push, and a two-gigabyte request ceiling nobody had known about.
The theme is **less machinery, and the machinery that remains fails loudly** -- plus the size work that the real server made necessary.

### Upgrading from 0.8.7 or earlier

- **`seafile.autogc` is now on by default.** After a qualifying push the helper merges the small packs. `git config seafile.autogc false` restores the tip-only behaviour.
- **Subcommand usage errors exit 2 on stderr** (they were exit 1 on stdout). Results and reports stay on stdout; failures go to stderr. Scripts that parsed the old stream should be re-checked.
- **`seafile.locksettle` and `seafile.gcmaxmib` are gone** (reported once, then ignored). `seafile.gcmaxpackmib` now means "the largest pack the helper writes"; the automatic budget is 2x it.
- **Clients older than 0.8.4 must not share a remote with this version**: the legacy `.git-lock.json` probe was removed.

### Added
- **A "Known Limitations and What They Mean for You" section** in USER_GUIDE §14: concurrency, size and storage, server and library scope, Git feature gaps and the snapshot tool's caveats, each with what it means and what to do.
- **`gc --tail`** runs the automatic tail-merge tier on demand, so "what does autogc do" is a command you can run, test and schedule rather than a trigger you have to provoke.

- **`git-remote-seafile doctor [<seafile://url>]`**: login, how the URL is interpreted (server / library / path), safety guards and a write-and-delete probe in one run, stopping at the first failure and printing the next command.
- **An opt-in real-server test tier** (`tests/test_realserver.py`, `SEAFILE_TEST_LIBRARY=<scratch library>`): API semantics, the SHA-1/SHA-256 round trip, concurrent pushers through the lock, and the conflict-copy observation. First run against Seafile CE 13.0.28; see DESIGN §4.3.1.
- **Conflict copies are named, not swallowed.** After a ref write the helper lists the directory and warns when `<ref> (SFConflict ...)` appeared beside it; ref listing and `gc` identify such files as conflict copies instead of "malformed ref name". Measured on a real server: twelve simultaneous same-path uploads all returned success and left six copies.
- **Automatic tail merge** (`seafile.autogc true`): after a qualifying push the helper downloads and merges only the remote's *small* packs (below `seafile.gcmaxpackmib`, default 1 GiB) once their count reaches `seafile.gcthreshold`, within a bounded download budget (8 × `seafile.gcmaxpackmib`, 8 GiB by default). Large packs are frozen in place and never counted; the merged pack is verified on the remote before its originals are deleted. Manual full `gc` is unchanged.
- **Snapshot tool maintenance**: automatic incremental repack of the local vault's loose tier once it passes 256 MiB (`--no-repack` opts out), a manual whole-store reset (`--full-repack`), and a report-only mode (`--maintain`) that also checks free disk space.
- **Snapshot large-payload profile.** A first-run sample of the tree decides once whether the payload is large and incompressible; if so the vault gets `core.compression=0` and `core.bigFileThreshold=50m` (remembered as `snapshot.profile`; re-sampled once per doubling of a vault that began small). It replaces the earlier rolling compression advisory, which needed three runs before it spoke and fixed only half the problem.
- **`tools/build_snapshot.py` and `tools/_snapshot/`.** The snapshot tool's source is now cohesive parts, concatenated into the single `tools/seafile_snapshot.py` users run; `--check` (and `tests/test_snapshot_build.py`) fail when the generated file drifts. See CONTRIBUTING.
- **`seafile.gittimeout` / `SEAFILE_GIT_TIMEOUT`**, readable only from the system/global git config or the environment, raises the per-command git ceiling for large repositories. `SEAFILE_SNAPSHOT_STALL_LIMIT` does the same for the snapshot tool's no-progress limit.

### Changed
- **Snapshot tool: the raw-payload profile now starts at a sample ratio of 0.5, not 0.9.** Deflating on one core costs more time than the bytes it saves cost on the wire, so mixed data that shrinks only ~15% is staged faster uncompressed. Measured on a 131 GiB tree whose sample kept 85%: ~45 MiB/s (about 50 minutes) before, ~125-200 MiB/s after. A vault that already decided `default` is re-sampled only by the existing recheck rule; `git config snapshot.profile raw` + `core.compression 0` applies it by hand.
- **`seafile.autogc` now defaults to on.** The automatic tail merge is bounded (2 x `seafile.gcmaxpackmib` download budget, packs capped, deletes only after size verification, fenced per deletion), cost ~4 s on every 20th push on the measured server, and the alternative was a remote that grows without bound. `git config seafile.autogc false` restores the tip-only behaviour. The "run gc / enable autogc" tip now appears only for users who turned it off.
- **One output and exit-code convention for every subcommand**: results and reports on stdout, usage errors and failures on stderr; exit 0 done, 1 failed or "no", 2 usage error. Usage errors used to print to stdout and exit 1. `cli.main` is now a dispatch table of one function per command.

- **A URL with no path says where it points.** The first push of `seafile://<library>` into an empty location prints that the repository lives at `/git-repo` and how to write that explicitly.
- **The "run gc / enable autogc" tip is shown at most once a day per remote** (state in `~/.git-seafile-notices.json`), so it is not filtered out by being on every push.
- **The lock wait follows queue progress.** The timeout restarts whenever the holder changes or this ticket moves up (absolute cap `max(10 x timeout, 120 s)`), and a waiting ticket is re-sent only to keep its lease alive instead of on every 2 s poll. Measured: six simultaneous pushers used 13.4 s of a 15 s budget; nine now all succeed.
- **Push-time `pack-objects` no longer delta-searches big blobs.** The helper's per-push pack and the tail merge pass `-c core.bigFileThreshold=50m`. Measured on 1 GiB of 256 MiB random files plus 300 similar text files: 50.0 s → 5.1 s with an identical pack size (the text is still deltified). Compression level was never the bottleneck of a push (8-10 MB/s at every level); the delta search was.
- **The snapshot tool's watchdog is thread-free.** The no-progress kill is driven from the calling thread by retrying `communicate(timeout=...)`, which keeps stdin, stdout and stderr intact across timeouts: no monitor, pump or feeder threads. The push step is heartbeat-only (never killed for silence), and children inherit a helper ceiling sized to the watchdog's horizon.
- **The automatic repack is gated on bytes only.** An object-count trigger already exists in git (`git fetch` launches `gc --auto` at ~6,700 loose objects).
- **`seafile.locksettle` and `seafile.gcmaxmib` are no longer settings** (ten public keys become eight). The settlement window is a fixed one second (a race guard, not a tuning knob); the auto tail merge's budget is 8 × `seafile.gcmaxpackmib`. A value still set is reported once and ignored. The test suite moves the window with `SEAFILE_TEST_LOCK_SETTLE`.
- **`gc` errors carry git's words, not the command line.** A failed scratch-repo command used to surface as `CalledProcessError`'s repr.
- **Case folding in the synced-folder guard errs toward refusing on macOS.** The shared containment check now folds case on Darwin (and Windows, as before); Linux stays case-sensitive.
- **`acquire()` no longer probes for the long-gone `.git-lock.json`** (one request fewer per push), and ticket ordering is one key `(order_ts or timestamp or mtime, mtime, nonce)` applied per ticket instead of a queue-wide mode switch.
- **`RemoteHelper` carries no test-shaped defaults**: `dry_run` is set in `__init__`, `cmd_list` no longer `hasattr`-checks `_refs_cache`, and `RemoteLock` reads its own attributes directly. Tests build helpers with `tests/_helpers.make_helper()`.

### Fixed
- **A snapshot dry run now stages at the speed of the run it previews.** It used git's defaults while the real run would use the raw-payload profile, so on the flood tree it deflated incompressible zips on a single core (measured 46 MiB/s against 186 MiB/s with `core.compression=0`) -- four times slower, on a machine that looked idle because one of 24 threads was busy. The profile's two settings now reach the preview through git's environment, writing nothing.
- **The snapshot tool refuses to stage a tree its volume cannot hold** (`--allow-low-disk` overrides). Found by pointing a dry run at the real flood tree: 130.7 GiB of raw data, a vault volume with 59.8 GiB free -- `git add` would have filled the disk, and a dry run stages exactly as much as a real run. The estimate is the tree size scaled by the sampled compressibility, minus what the vault already stores.
- **Leftover `tmp_pack_*` files from a killed run are swept** (under the vault lock, older than an hour) and reported by `--maintain`. Git leaves them read-only and Windows will not delete a read-only file, so the sweep clears the attribute first and says so when it still cannot remove one (found when the real 22.5 GiB file survived the first version). The flood vault held a 22.5 GiB one from 2026-10-12 that nothing mentioned; the tool had also read git's "garbage" count as unreachable objects.
- **A stale `index.lock` is explained**, not just quoted: the error now says a killed run probably left it and that the vault is not damaged.
- **A first-snapshot dry run no longer reports "no changes since the last snapshot".** A dry run never commits, so the report now compares the staged tree with the base's.
- **A long read timeout for uploads (H-17).** The 30 s request timeout also covered the server's post-upload assemble-and-commit phase, which grows with file size; uploads now wait up to 10 minutes for the reply (connect timeout unchanged).
- **A transient `HEAD` read failure no longer leaves git with half an advertisement (H-18).** `HEAD` is read before the first ref line is written, so the failure is raised while the listing is still empty.
- **Deleting the branch remote `HEAD` points at says so (H-19)**, with the `set-head` command that fixes it, instead of leaving a puzzling clone warning.
- **`stderr` is UTF-8 with replacement (H-22)**, so a non-ASCII library name can no longer raise from a diagnostic write in the middle of a push.
- **No pack the helper writes exceeds `seafile.gcmaxpackmib` (default 1 GiB).** A soak against a real server found that a single upload of 2 GiB is reset (1.75 GiB is accepted), which made a ~4 GiB tail merge fail at the upload and would equally fail any push of a 20 GB raw-data tree as one pack. Pushes, tail merges and full `gc` now pass `--max-pack-size`, so a large push arrives as several self-contained packs. A single object larger than the cap cannot be split; it becomes one larger pack, uploaded in chunks (next entry). The failure was safe -- nothing was deleted -- but the work was lost.
- **Files at or above 1 GiB are uploaded in `Content-Range` chunks** (128 MiB each, three attempts per chunk), so a single object larger than the cap -- or than the server's single-request ceiling -- no longer makes a push impossible. Measured on the real server: 2.5 GiB and 5 GiB files assembled from chunks and downloaded byte-identical, and a 5 GiB single-file repository pushed through the helper in 181 s. After the last chunk the size is checked against the listing, so a server or proxy that ignores `Content-Range` (and would keep only the final piece) is reported, not trusted.
- **A warning before a checkout git for Windows cannot do.** With `core.autocrlf=true` a 4 GiB+ blob is silently truncated by `git add` and stalls a `git clone` (measured: 5 GiB, 240 s timeout; with `core.autocrlf=false` the same file clones in 11 s, byte-identical). The helper sees pack sizes first and now says so, with the one-flag workaround. The helper itself pushed a 5 GiB single-file repository in 181 s and fetched it back.
- **Tail-merge sizing follows from that cap**: packs at least half the cap count as full and are frozen (a full pack is always a little *under* the cap, so the cap itself could never be the freeze line); the automatic budget is 2 x the cap instead of 8 x, because the merge holds the push lock for about 0.05 s per MiB (measured: 64 MiB/s down, 32 MiB/s up) and 8 GiB was a six-minute hold during which other pushers see "locked".
- **`gc --tail` ignores the download budget** (the budget keeps an *automatic* merge short; someone who ran it by hand accepted the time).
- **Encrypted libraries are refused with a clear message** instead of failing later with a bare status code (`get_repo_id` reads the listing's `encrypted` flag). SNAPSHOT.md no longer recommends one.
- **`is_valid_ref_name` now agrees with `git check-ref-format`** on trailing and leading slashes and one-level names, found by a new differential test that runs both over ~300 awkward names.
- **Snapshot tool:** `is_ancestor` no longer reads a git failure as "diverged" (which steered users at `--reset-to-remote`); settings are remembered only after a run succeeds, so a typo'd `--remote` does not stick; `--code-remote` is pushed after the vault's own checks rather than before; `--restore` without `--branch` uses the remote's only branch (or lists several) instead of insisting on `snapshot`.
- **Trusted config scopes follow git's precedence** (global before system).
- `lock-status` no longer prints "Reclaiming stale lock" while only inspecting.
- LFS progress is throttled (1 % or 250 ms) instead of one JSON line per 64 KiB chunk.
- Scratch directories (`grs-push-*`, `grs-fetch-*`, `grs-staging-*`) left under `.git` by a crashed run are swept after 24 h.
- The helper resolves the library only when first needed, so `capabilities` no longer touches the network; protocol commands are matched on the first token, not a prefix.
- **Manual `gc` on a SHA-256 remote failed once the tail merge had written its packs.** The format detector measured what remained after stripping `pack-`, so `pack-tail-<64 hex>` read as a 69-character hash and the scratch repository came out SHA-1. The detector now reads the hash from the *end* of the name, in one function both paths use.
- **A fetch could fail after a local `gc`** (`did not send all necessary objects`): the "already fetched" hint is now discarded when the requested tips are not actually present.
- **`gc` no longer passes a fence it cannot check.** The `hasattr(lock, "verify_ownership")` fallbacks, including the final `return True`, are gone.
- **`unlock --force` no longer reports success when nothing was deleted**, and a malformed `HEAD` on the remote is skipped with a warning instead of being echoed to git.
- **`verify_docs_guards.py` exited 1** because its drift scenario targeted prose that had been reworded.

## [0.8.7] - 2026-10-12

A patch release: every change in it is a defect rather than a capability. The theme is the one the
fail-closed principle has always stated and the code did not always follow — **a check that cannot
answer must not be read as an answer, and a command that never finished must not be read as one that
did.**

Three of the four groups below are that sentence in different clothes. Nothing bounded a subprocess, so
a wedged `git repack` held the remote lock past its own expiry while another client lawfully took over.
A download compared nothing against its `Content-Length`, so a truncated packfile was reported as a
success. A `dir_exists()` answered "does not exist" for every non-200, including the ones that mean "I
could not tell". And the shipped source cited twenty-three documents that are not in the repository, so
no reader could follow a single one of them.

The fourth is the two halves of `D6` that the v0.8.6 pass deliberately left undone — and the reason it
left them is the reason they were worth doing properly: a type check that is red on arrival and a
coverage report nothing produces are both worse than nothing, because they look like enforcement.

### Added

- **A `typecheck` CI job (D6 residual).** mypy 2.4.0, pinned, run bare so that the scope and the
  strictness come from `[tool.mypy]` in `pyproject.toml` rather than from the workflow. The package
  and `tools/` are clean; `tests/` is deliberately out of scope, because it is a namespace package
  (mypy resolves each module under two names and stops before checking anything) and its test doubles
  are `MagicMock`s no annotation can describe. `warn_unused_ignores` is on, so an ignore that has
  stopped being necessary fails the job instead of absorbing the next real error on its line.
- **A `coverage` CI job (D6 residual).** It runs the whole suite under `coverage` and publishes the
  per-file table to the job summary, because a workflow log needs admin rights to download and a job
  summary does not. `[tool.coverage.*]` holds the configuration, and `parallel` plus
  `patch = ["subprocess"]` are what make the number mean anything: the suite runs in worker processes
  and the end-to-end tests exercise the helper as a child of `git`, so measuring the driver alone would
  report a figure that looks like coverage and is not. There is deliberately **no threshold** — a
  `fail_under` chosen to make today's build green is one that gets raised for the wrong reason or
  lowered for a worse one. At this release the suite covers **91%** of the package (2944 statements,
  264 missed), recorded as a dated observation rather than a target.
- **Twelve regression tests for everything below**, in four classes. Suite: 681 → 693 tests /
  134 → 138 targets.

### Changed

- **Every `git` invocation is bounded (H-24).** The tree had timeouts on its HTTP calls and none at
  all on its twenty-five `subprocess` sites. `git_util.run_git`, `gc`'s runner and the snapshot tool's
  runner now bound every call and fail loudly, naming the command and the limit. The bound is a
  constant rather than a configuration key, on purpose: a repository must not be able to raise the
  ceiling that protects its own concurrency guarantee, because the failure it prevents is a `gc` that
  holds the remote lease open past expiry while another client lawfully takes over.
- **`filter_existing_objects` degrades instead of failing.** It is the one deliberate exception: a
  timeout there warns on stderr and answers "assume no objects are present", because the cost of a
  false negative is a re-upload rather than a lost object.
- **`dir_exists()` no longer answers "does not exist" for a transient error (H-7).** It returned
  `False` for *every* non-200 while `list_dir()` beside it raised for anything that was not 200 or 404,
  so the two disagreed about the same state. Only an explicit 404 is a `False` now. The ref-pruning
  loop already caught the raise and stopped; `mkdir_p` now aborts with the client's own message instead
  of proceeding to a creation it could not confirm.
- **`.gitignore` gained `.coverage.*`.** Parallel mode — which `pyproject.toml` turns on for the whole
  suite, so that a worker cannot overwrite the driver's data — writes one data file per process, and
  the plain `.coverage` rule matched none of them.

### Fixed

- **A truncated download is a failure, not a success (A12.10).** `download_file_to` read
  `Content-Length` into the progress callback and never compared it against what it had written, so a
  short body returned `True`. A packfile that is quietly incomplete is a corrupt object store found
  later and far from the cause; a mismatch now raises.
- **A server time that was never learned is announced (H-8).** `get_server_time()` fell back to the
  *local* clock whenever no `Date` header had been seen, which rested the lock's ordering on a clock
  the protocol does not trust. It still falls back — a server that sends no date must not make the lock
  unusable — but it now says so once, on stderr.
- **The shipped source stops citing documents a reader cannot open (C8).** Twenty-three comments
  across seven files named `REVIEW.*.md`, `archive/…`, `(N-16)` and `(Qwen …)`; every one of them lives
  in the gitignored `archive/`, so none could be followed from a checkout. The reasoning is inlined, and
  an identifier survives only where it names a fact about the review rather than standing in for the
  explanation.
- **`ROADMAP.md` re-targeted.** The six rows a patch release could carry are closed and recorded in §2;
  every row that is left still names v0.9.0, v1.0.0 or `won't do`.
- **`CONTRIBUTING.md` §Tagging says what to name the tag, and why.** Its `-m` example was a bare
  `v<x.y.z>` while every real tag in the history carries the release-notes title, and it did not say
  that a bare `git push origin` leaves the tag behind — which is how v0.8.6 came to be reported as
  pushed with no tag. The procedure now names the tag explicitly, names `mirror` as a remote that
  needs it too, and `ROADMAP.md` §1.3 tracks the two follow-ups (`tools/release_check.py`, and
  `push.followTags` as a maintainer convenience).

## [0.8.6] - 2026-10-12

Two tranches, and neither of them is code — which is the point. A workflow with no token scope, no
concurrency group and a worker count copied from an assumption about the runner produces exactly the
same green pipeline as one without those problems. A dependency floor nothing installs is a number in
a metadata file. A release whose artifacts nothing attested looks identical to one whose artifacts
were. None of it is visible from a passing test run, so each piece is pinned by a guard rather than
left to a checklist.

The dependency floor is the half with teeth. `requests>=2.25.0` predated three CVEs in requests' own
credential handling, and that matters more here than in most projects: this helper holds a Seafile
token and runs inside every `git` invocation, so the transport library's credential handling is part
of its attack surface. The floor is now 2.32.4 — and, more to the point, a new CI job pins exactly
that version and runs the whole suite against it. Every other leg installs the *latest* release, so
until now the number this package declared was never exercised at all.

### Added

- **Release artifacts carry build provenance and a checksum (D4).** The build job generates a
  build-provenance attestation signed against the workflow's own OIDC identity and writes a
  `SHA256SUMS` file; both ride to the Release alongside the wheel and sdist. A checksum answers "is
  this the file that was published" — and cannot answer more, since whoever can replace the file can
  replace the checksum beside it. The attestation records the repository, workflow, commit and
  digest, so it answers the question worth asking about a program you are about to hand a token to:
  who built it, and from where. `SECURITY.md` documents both checks, `gh attestation verify` included.
- **A `min-deps` CI job (D5)** that installs `requests` at exactly the version `pyproject.toml`
  declares as its floor, with `--no-deps` so the editable install cannot quietly resolve past it,
  asserts the pin survived, and runs the full suite against it.
- **Dependabot for the pinned actions (D9).** Every `uses:` is pinned to a full commit SHA with the
  release in a trailing comment, and the cost of that default is that the pins go stale silently.
  The configuration covers `github-actions` only: a `pip` entry would open pull requests raising the
  dependency floor, and a bot moving a security floor is not a security review.
- **A bug-report issue form, a pull-request template and `CODEOWNERS` (D9).** The issue form asks for
  the version, platform, interpreter and git version the maintainer would otherwise have to request,
  routes a vulnerability to the private channel rather than into a public issue, and carries a
  redaction checkbox — a Seafile token is a bearer credential for every library the account can see.
  The pull-request template is the checklist a release actually runs.
- **`CODE_OF_CONDUCT.md` (D9)**, adopting the Contributor Covenant 2.1 by reference rather than
  restating it, so the text in force is the published text.

### Changed

- **`ci.yml` scopes its token (D1).** `permissions: contents: read` is now stated rather than
  inherited. The repository default is read/write on older settings, and this workflow only reads.
  `release.yml` has carried the equivalent block since it was written.
- **A superseded run is cancelled (D6).** A `concurrency` group keyed on the workflow and ref, with
  `cancel-in-progress: true`. The matrix is fifteen jobs per push; without this, a series of pushes
  to one branch queues fifteen more behind each previous push instead of superseding them. The
  cancelled run's logs stay available — what is lost is a verdict on a commit that has already been
  replaced.
- **The matrix caches pip (D6)**, keyed explicitly on `pyproject.toml` rather than left to the
  action's file search, so the cache is invalidated exactly when the declared dependencies change.
- **The worker count is derived, not pinned (D7).** Both workflows ran `tools/run_tests_parallel.py`
  with a literal `-j 4`, justified by "the runner has four vCPUs" — a number that is not the same on
  every OS in the matrix and changes whenever GitHub resizes a runner image. Both now pass `-j 0`,
  which is one worker per CPU the runner reports; the runner already clamps the count to the number
  of targets, so a large self-hosted box cannot start more workers than there is work to hand them.
- **The dependency floor is a security floor (D5): `requests>=2.32.4`.** Each version in that line
  closed a credential-handling leak: 2.31.0 fixed CVE-2023-32681 (the `Proxy-Authorization` header
  followed a redirect to a different host), 2.32.0 fixed CVE-2024-35195 (`verify=False` set on a
  `Session` persisted into later requests made through it), and 2.32.4 fixed CVE-2024-47081 (a crafted
  URL could leak `.netrc` credentials to a third party). The previous floor was chosen for a feature
  this code does not use.
- **`.gitignore` names `.review-venv/` and `.ruff_cache/` (D10)** instead of relying on the
  `.gitignore` files inside those directories, which are not rules this repository controls.
- **`ROADMAP.md` §1.2 carries the archive backlog.** The Tier 3 list from the v0.8.2 verification and
  the findings the `-r2` reviews left open now live in a tracked file rather than in `archive/`,
  which is gitignored — and each row records whether it has been re-derived against this release
  (**probed**) or is still only transcribed from the review that raised it.

### Fixed

- **`HEX_SHA_RE` is asked with `fullmatch` at both call sites (A12.2).** `$` also matches immediately
  before a trailing newline, so `HEX_SHA_RE.match("a" * 40 + "\n")` was `True` while the same string is
  not a SHA. A caller handing this a line read from a file would have produced a candidate that
  `git cat-file` rejects, surfacing as a missing object rather than as bad input.
- **`USER_GUIDE` §9 no longer promises that no `SFConflict` file is ever created (A11).** The
  unconditional form was unproven: it is a claim about the paths this tool writes — the guardrails
  keep them out of a synced folder — not about what the Seafile *server* would do with two
  simultaneous writes to one path, which has never been exercised against a real instance. The step
  now says exactly that, and points at `DESIGN.md` §3.3.
- **The README's LFS memory figure is measured rather than asserted (C6).** "0.15 MB, not the
  142.8 MB a buffered implementation needs" was a decimal with no reproduction. It is now 0.16 MB
  against 128 MB on a 64 MB payload, with the method named (`tracemalloc` over 16 KiB reads) and the
  test that pins the property cited.

### Tests & Tooling

- Ten new guards in `tests/test_release_workflow.py` covering the properties above: the token scope,
  the concurrency group, the pip cache, the derived worker count, the `min-deps`/`pyproject` floor
  agreement, the floor's own security floor, the build job's attestation permissions and step, the
  checksum-after-attestation ordering, and the two `.gitignore` entries. The `-j 0` guard strips
  comments before scanning, because the replacement is *explained* by a comment quoting the old value.
- `CODE_OF_CONDUCT.md` joins the doc-guard set, which is now ten documents rather than nine; the
  guards still catch 12/12 injected drifts.
- Suite: 671 → 681 tests / 130 → 134 targets, all green; ruff clean.

## [0.8.5] - 2026-10-12

Two halves, and neither changes the protocol — which is why this is a patch release rather than the
minor the documentation work was briefly planned as.

The larger half is the documentation's *shape*, which had stopped doing its job. The smaller is the
six items the v0.8.2 verification left in its Tier 2 list: a URL-parsing ambiguity, a snapshot-tool
ordering defect that put excluded bytes into the object store, a repo-wide LFS setting that misroutes
objects in a dual-remote setup, a release gate that tested one interpreter and checked its tag too
late, a build with no install check, and three stale timings carried in CI. Every fix is fail-loud or
fail-closed; none is a protocol change.

`DESIGN.md` had become a changelog wearing a spec's clothes. Its second half was nine subsections
titled `(v0.5.0)` through `(v0.8.0)`, each listing fixes under their review identifiers, with later
entries silently superseding earlier ones — so a reader could not answer "what does the protocol
guarantee *today*?" without replaying the release history, and the same material was already in this
file in more detail. Worse, the parts that *were* normative had drifted: the storage diagram still
tagged a directory with the release that introduced it, the sequence diagram showed a ref being
uploaded to a path it is never uploaded to, and the credential section omitted the precedence rule
that changed in v0.8.3.

### Changed

- **`DESIGN.md` rebuilt as a specification.** The document is now organised by what the system must
  do rather than by when it changed: **Invariants** (transport, storage, concurrency, data safety),
  **Protocol** (wire, on-server layout, API surface, lock, compaction, LFS, safety, configuration,
  modules), a **Failure Model** mapping every detectable failure to fail-closed, fail-loud or warn,
  a **Security & Threat Model**, and **Non-Goals**. Each old §7.x subsection was split into its
  invariant — which moved into the new structure — and its release narrative, which did not, because
  it is already in this file. Nothing normative was dropped: the split was checked identifier by
  identifier against the previous revision.
- **The stale parts are corrected.** The storage diagram no longer carries release tags; the
  sequence diagram now shows the settle window, the post-lock re-read, the final compare-and-swap
  read and the read-back, and shows the ref upload going to its parent directory; the credential
  section states the precedence rule; and the header no longer claims a status ("Ready for Upstream
  Integration") that nothing had established.

### Added

- **`SECURITY.md`** — credential handling and precedence, the token-scoping rule and why it exists,
  plain-HTTP and file-permission behaviour, TLS and proxy configuration, the threat model from an
  operator's angle, what the lock tickets disclose, and how to report a vulnerability.
- **`RUNBOOK.md`** — recovery procedures with symptom, cause and fix: a lock held by a machine that
  no longer exists, stray or conflicting refs, a ref pointing at a missing object, half-uploaded
  packfiles, conflict artifacts inside `refs/`, and a failed compaction after a successful push.
- **A supported-versions and compatibility matrix** (`DESIGN.md` §4.3.1). The documents claimed "all
  modern Seafile servers (CE and Pro)" while the suite validates against a stub; the matrix states
  what is claimed and marks what has not been exercised.
- **TLS and proxy guidance** (`USER_GUIDE` §6): `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE`,
  `HTTPS_PROXY` / `NO_PROXY`, and the note that **Git's own `http.sslCAInfo` is not read** — the
  helper is a separate process, which is why a self-hosted instance can fail with a certificate
  error in a setup where a GitHub remote works.
- **Upgrade and compatibility notes** (`USER_GUIDE` §5): the v0.7.0 lock removal, the v0.8.4 grant
  marker's fleet-wide requirement, and the fact that no protocol marker file exists to make old and
  new clients refuse each other.
- **The dual-remote LFS trap is documented** (`USER_GUIDE` §11.1, pointed at from §8.5 and
  `DESIGN.md` §4.6). `lfs.standalonetransferagent` is *repo-wide*, so with it set to `seafile`, LFS
  objects route to Seafile even when the remote being pushed is GitHub — leaving pointers on GitHub
  with no objects behind them. The scoped `lfs.https://seafile.example.com/.standalonetransferagent`
  form, the second-setting trap in `lfs.customtransfer.seafile.args` (which names the destination a
  second time), and the `concurrent` default are all spelled out.

### Fixed

- **`USER_GUIDE` §6 documented the credential behaviour that v0.8.3 had already replaced.** It said
  a bare `SEAFILE_TOKEN` "is adopted for *any* server the remote URL names" — the confused-deputy
  defect A2 fixed, not the shipped behaviour. The code requires `SEAFILE_SERVER` alongside the token
  and ignores the token otherwise; the guide now says so, and a troubleshooting row names the error
  a user actually sees. A prose contradiction the doc guards cannot see, found by re-reading the
  section while writing `SECURITY.md`.

- **Two overclaims inherited from the old §7.x were caught while rebuilding, and are now corrected rather than carried forward.** The document asserted that a non-numeric port is "reported as an invalid Seafile URL rather than escaping as a raw `ValueError`" — true of `parse_seafile_url`, false of `normalize_netloc()`, which read `urlparse(...).port` unguarded. Rather than write that down as a limitation, the guard was added (A7.4, under *Fixed* below). It also described ref-name validation as following measured `git check-ref-format` behaviour without saying that the rule judges *components* and not the separator, so `refs/heads/` satisfies it; the destination check that actually stops `git push origin :refs/heads/` deleting every branch is a second, separate check. Both statements were verified against the code rather than re-read.

- **A dotted library name with a nested path was parsed as a server (A7).** `seafile://notes.v2/team/repo`
  has no marker between the library and the path, and the old rule read *any* dotted first segment as
  a host — so `notes.v2` became the server and `team` the library. The test is now conservative: a
  port or userinfo is honoured outright, and a bare dot counts only when the segment has the shape of
  a real hostname, meaning dot-separated labels ending in an *alphabetic* label. `notes.v2` and
  `proj.2026` end in a numeric label, which no hostname can have, so they stay libraries. A name that
  is genuinely either way (`my.lib`, `files.local`) is still read as a server, and the explicit-scheme
  form names the server outright; the ambiguity is stated in `DESIGN.md` §4.3 and `USER_GUIDE` §7
  rather than left to be discovered.
- **`%2F` could smuggle a path separator past the traversal refusal (A7).** Segments were
  percent-decoded *after* being split, so `a%2F..%2F..%2Fb` remained the single component spelled
  `a/../../b` and never met the `.`/`..` check. Decoding now happens before the split, so `%2F` is a
  real separator and every component it produces is held to the same rule.
- **A malformed port escaped as urllib's bare `ValueError` (A7.4).** `normalize_netloc()` read
  `urlparse(...).port` unguarded, so `seafile://https://host:abc/lib` failed with a message naming
  neither the URL nor the fact that the fault was a *URL* fault — which sends the reader to the
  server. The read is guarded and re-raised as `Invalid Seafile URL format: invalid port in '<host>'`,
  and `DESIGN.md`'s failure model records the guarantee in place of the gap it used to describe.
- **The shipped context-menu patch generated a URL the helper misreads (A7).**
  `contrib/seafile-client-context-menu.patch` emitted a bare-host URL with an unencoded library and
  folder, so a library whose name contained a dot was read as a server and a folder containing a
  space produced a remote the helper could not parse. It now builds an explicit-scheme URL and
  percent-encodes each path component, and its header says why.
- **An excluded path was read, hashed and written to the object store before being dropped (B2).**
  `build_snapshot` staged the whole tree with `git add -A -f .` and removed the excluded paths from
  the index afterwards. The index is a *cache*, so a path that never reaches it cannot be skipped by
  it: every excluded byte was read from disk, hashed, and written as a loose object, then left
  unreferenced — and it could not be caught earlier, because excluded paths are exactly the ones the
  stat cache has nothing for. Excludes are now applied *before* staging (the post-filter stays as a
  safety net), so a large excluded tree costs nothing, and a test asserts that an excluded file
  produces no new object.
- **A local `gc` made the helper re-download packs it already had (A9).** The skip test compared the
  remote's pack *names* against the local pack directory, and a local `gc` repacks and renames them,
  so the comparison silently stopped matching. The installed set is now recorded per repository id
  and per destination in `.git/seafile/<repo-id>/packs.json`, written after each fetch, so the
  decision survives a repack; an unreadable or unknown-version state file costs a re-download, never
  a missing object.

### Tests & Tooling

- `tests/test_docs_consistency.py` gains `TestDesignIsASpecNotAChangelog`: the normative sections
  must be present, and no heading may name a release. The guards check *tokens* — config keys,
  environment variables, subcommands — not structure, so without this a future release could add
  `(v1.1)` headings back one at a time with every guard staying green.
- `tools/verify_docs_guards.py` gains two scenarios proving that guard catches drift: a renamed
  normative section, and a reintroduced release-stamped heading. 10 → 12 injected drifts.
- `SECURITY.md` and `RUNBOOK.md` joined `_DOC_NAMES` and `MANIFEST.in`, so the existing guards cover
  them and they reach the sdist. Both were written to satisfy those guards, which is how the
  dotted-name counter-example in an early draft of the runbook was caught before it shipped.
- **The release gate now tests the matrix it claims, and checks the tag first.** It ran a single
  `ubuntu-latest` / 3.11 combination, and the tag-versus-`__version__` assertion ran *after* the
  suite, so a mis-tagged release did all the work before failing. A `verify-tag` job now runs first,
  the `test` job `needs` it, and the gate carries the same `os` / `python-version` matrix with
  `fail-fast: false` as `ci.yml`.
- **The build now installs what it built.** `python -m build` was followed by an upload with no
  `twine check`, no wheel install, and nothing asserting that `tools/` stayed out of the wheel.
  `.github/smoke_install_wheel.sh` checks both artifacts with `twine check`, installs the wheel into
  a fresh venv, runs `git-remote-seafile version`, asserts the wheel has no top-level `tools/`, and
  asserts the sdist *does* ship `tools/seafile_snapshot.py`.
- **The stale timing curve in `ci.yml` is gone, and the doc guards run in CI.** `ci.yml` still quoted
  a curve measured at 268 tests, and `tools/verify_docs_guards.py` — which proves each guard catches
  drift — had no caller under `.github/` or `tests/`, so the proof never ran. The lint job now runs
  the guard tests and the drift scenarios, and the curve is a pointer to `CONTRIBUTING.md`.
- Suite: 640 → 671 tests / 123 → 130 targets, all green; docs guards 12/12; ruff clean.

## [0.8.4] - 2026-10-12

Two findings from `REVIEW.sonnet.md` that the v0.8.3 pass deferred, both of them
cases where the code was trusted for something it had not checked: the lock
believed a clock estimate about who owned it, and `gc` believed an upload
returned `True`. Neither is a protocol change — the ticket gains one field and
the pack verification adds no format — and both now fail closed.

### Fixed

- **A granted lock can no longer be preempted by a later arrival's clock.** The
  ticket queue ordered on `order_ts`, a *client-side estimate* of server time
  (the local clock plus the offset implied by the `Date:` header, whose
  resolution is one second). That estimate is fine for deciding who *waits* —
  approximate shared time is all an expiry check needs — but it was also
  deciding who *holds*, and two estimates half a second apart are
  indistinguishable from a genuinely earlier arrival. A newcomer whose estimate
  came out behind an active holder's therefore sorted ahead of it and acquired
  the lock while the holder was mid-push. Reproduced against a stateful store:
  with the old ordering, both clients end up marked as holders of one
  repository. A winning client now publishes `"holding": true` on its ticket,
  and the queue sorts every granted ticket ahead of every ticket that has merely
  asked, whatever the timestamps say; `order_ts` is demoted to the tie-break
  among peers. The marker is written in a second pass, after the queue has
  agreed the client won, so `holding` always means "granted" and never "asked" —
  a marker written on the first upload would claim the lock before it was
  awarded. Effective once every client pushing to the repository is on v0.8.4+:
  a newer client will not preempt an older holder, but an older client ignores
  the marker. The lock contract and its limits are now spelled out in
  `USER_GUIDE.md` §12 and `DESIGN.md` §7.1.
- **The ownership fence now looks at the queue, not only at our own ticket.**
  `verify_ownership()` was `return self.renew()`, which proves the *lease* — the
  ticket is gone or foreign once another client reaps it — and nothing else. It
  could not see the failure above, because in that failure our own ticket is
  still there, still ours, still unexpired: it reads back perfectly while the
  queue has already given the lock away. The fence now also scans the queue and
  fails when an active ticket sorts ahead of ours, reported as a third state,
  `another client was granted the lock while this ticket was still held`, kept
  distinct from a takeover because the two send an operator to different places.
  The scan is read-only (a verification step that reaped tickets would be doing
  acquisition's job) and rate-limited to one listing per second, so fencing once
  per deleted ref does not list the queue once per ref; the lease is still
  re-proven on every call.
- **`gc` no longer deletes the old packfiles on the strength of an unverified
  upload.** After `repack -a -d` the consolidated pack and index were uploaded
  and *all* superseded packs were then deleted, with only two checks between: that
  `upload_file` returned `True` and that the lease was still held. Nothing
  confirmed the new pack was actually there and intact — a truncated write, a
  quota error a proxy answered `2xx`, or an `.idx` that never landed would pass
  and the old packs would be removed. For a tool whose product is "your
  repository's only copy", that is the step that deserves the most paranoia. The
  remote pack directory is now listed again and every new `.pack`/`.idx` must
  appear at the size it has locally; anything else refuses, names what could not
  be verified, and leaves every old packfile in place. The lock is also
  re-validated before *each* deletion rather than once before the loop: the
  result of `maybe_renew()` was being discarded there, and since its `False` also
  means "the renewal interval has not elapsed" — which is what most of its calls
  return — a lease lost mid-loop read as an ordinary no-op and the loop kept
  deleting inside another client's push. Losing ownership part-way through now
  stops the cleanup and says so, without failing the run: the consolidated
  packfiles are verified by that point, so the leftovers are only clutter.

### Documentation

- `USER_GUIDE.md` §12 said ticket ordering "prevent[ed] write-write collision
  races and client clock drift". It did not, for the reason above; the section
  now separates what the ordering guarantees from what the `Date:` header can
  support, and states the mixed-version limit of the marker. The fence
  description gained the two-part check and the distinct failure messages.
- `USER_GUIDE.md` §10 and `DESIGN.md` §7.2 now describe what actually gates the
  pack deletions, and §10 records that compaction's scratch space comes from the
  system temporary directory — worth knowing before compacting a large library
  on a small `tmpfs`, where the run fails late and after the lock is taken.
- `lock-status` prints a `Granted` line when the winning ticket carries no marker
  — a pre-v0.8.4 client, or an abandoned queue `--force` would clear.

### Tests & Tooling

- `tests/test_lock.py` gains `TestHoldingMarkerOutranksTheServerTimeEstimate`:
  the skew preemption above, the marker surviving a renewal (renewal rewrites
  the whole ticket body, so a marker added to only some of the writers would be
  dropped on the first refresh and the ticket would still look well-formed), the
  `LEASE_PREEMPTED` state, the reaping of a crashed holder that the marker must
  not turn into a permanent lock, the scan rate limit, and the sort key putting a
  grant ahead of an earlier estimate. Verified to fail with the marker removed
  from the ordering.
- `tests/test_gc.py` gains
  `TestGcVerifiesTheUploadBeforeDeletingAnything`: a truncated pack, a pack
  absent from the listing, a listing failure during verification, and a lease
  lost mid-cleanup. Each asserts that no packfile is deleted. The gc fixtures now
  model a remote whose pack listing grows with what it was sent — a listing
  frozen at its pre-upload state fails the new check, correctly, but for a reason
  that has nothing to do with the test.

## [0.8.3] - 2026-10-11

The Tier 1 pass from the verification of `REVIEW.sonnet.md`, archived with a
per-finding table for all 35 items as `archive/VERDICT.sonnet-v0.8.2.md`. Six
defects are closed, three of them security-relevant or destructive: a credential
that followed an attacker-chosen host, a launcher that executed a file from the
repository it ran in, and a `git push` that could delete every branch. No
protocol changes; every fix is fail-loud or fail-closed, and the three
documentation claims the review called false were corrected rather than
restated.

### Fixed

- **A Seafile token is only sent to the server it was declared for.** A bare
  `SEAFILE_TOKEN`, and a `~/.git-seafile.json` token with no `"server"` beside
  it, used to be adopted for whatever host the `seafile://` URL named — and a URL
  is something a user is far more likely to have been *handed* than to have
  written. `git clone seafile://somewhere.example/lib` therefore posted the
  user's Seafile token to `somewhere.example`: a confused deputy, where the URL
  is attacker-chosen and the credential is not. The token is now scoped to a
  matching `SEAFILE_SERVER` (or the config file's own `"server"`), and the
  failure message names the omission instead of reporting "no credentials". A
  token about to travel to a non-loopback host over plain HTTP now warns on
  stderr, on every adoption — the alternative is a bearer credential leaving the
  machine readable without a word.
- **`git push origin :refs/heads/` can no longer delete every branch.** The push
  paths checked only that a destination began with `refs/heads/` or
  `refs/tags/`, and `is_valid_ref_name` — like `git check-ref-format` — judges
  *components* and says nothing about a trailing separator. `refs/heads/`
  satisfied both, `_full_path` stripped the slash, and `delete_entry` removes
  directories through the same `/file/` endpoint it uses for files, so the push
  deleted the whole namespace. Destinations are now checked against git's own
  rule as well as the prefix, and a name with an empty or trailing component is
  refused.
- **Deleting a ref is read first, and fenced.** The delete path wrote nothing
  back, so a deletion that removed nothing still reported `ok` — a push that
  vanished silently. It now reads the ref before removing it, which turns a no-op
  into git's own `remote ref does not exist`, proves the target is a file rather
  than a directory, and gives the lease check something to run *before*: only ref
  *writes* were fenced by `lock.verify_ownership()`, though a deletion is just as
  destructive.
- **Every `error` line goes through one sanitizer.** Git reads a helper's stdout
  a line at a time, so an exception message carrying a newline becomes a second,
  invented protocol line. Six call sites open-coded the flattening and the delete
  path wrote the raw exception, so `_proto_error()` now owns it and the property
  belongs to the protocol layer rather than to each site's memory. The v0.8.1
  entry below described this sanitizer as already existing; a new source-level
  guard enforces it, and the guard immediately found one more direct writer —
  `cmd_push`'s malformed-refspec branch, the twin of a branch already converted
  in the dry-run path and the one site hand-conversion missed.
- **The launchers no longer execute code from the repository they run in.** A
  remote helper's working directory is the repository git is operating on, and
  `python -m` puts that directory first on `sys.path` — so a repository
  containing a `requests.py` ran it as the user. Both `bin/git-remote-seafile`
  and the Windows `.bat` now bootstrap with `-I` (isolated: no cwd, no user
  site-packages, `PYTHONPATH` ignored) and insert the package directory
  explicitly. `-P`/`PYTHONSAFEPATH` would say this more directly but need Python
  3.11, and this project supports 3.10.
- **`seafile_doctor.py` no longer answers "not synced" when it could not tell.**
  `where` documents exit 1 as a *verified* negative and exit 2 as "could not
  check", but an unresolvable client data directory — and any unexpected
  exception — escaped as a traceback, which also exits 1, so a script gating on
  the code was told "safe" by a crash. Both now report exit 2 and say so, and
  exit 1 is reserved for a definite answer. v0.8.1 claimed this class closed for
  the `repo.db` case; the crash path was missed.
- **The snapshot tool meets its documented Git floor.** `ensure_vault` created
  the vault with `git init -b <branch>`, which is Git 2.28, while `USER_GUIDE` §5
  promises Git 2.20+. The initial branch is only an unborn-HEAD symref, so it is
  now named with `symbolic-ref`, the same operation on every version: the floor
  is met rather than raised, and `init.defaultBranch` cannot leak in either.

### Documentation

- **`USER_GUIDE` §8.2 called `--depth` and `--filter` "supported".** They are
  not. The helper implements no `depth` or `filter` option, answers
  `unsupported`, and Git then falls back to a full clone *without a word* — so
  the note now says so plainly: nothing is broken, and nothing is smaller either.
  The part that is true is kept, that the fetch-skip is withheld in a repository
  which is shallow or partial for some *other* reason, and `DESIGN.md` §7.9 and
  `SNAPSHOT.md` §12/§14 carry the same correction, with the snapshot tool's
  shallow-restore idea marked blocked on helper support rather than merely
  unused. The v0.8.2 heading below, "Shallow and partial clones now work", reads
  as an overstatement in hindsight: that release made the helper *tolerate* such
  a repository, not *create* one.
- **`DESIGN.md` no longer describes a `get_repo_id` revalidation that cannot
  run.** The v0.6.4 entry below, and its release note, describe a cached id being
  checked against a fresh listing — which it was, until v0.7.1 removed the check
  as unreachable behind the typo suggestion and as a listing fetch on every
  lookup. A cache hit now returns immediately, so a *stale* cached id is not
  detected; §7.10 and §7.11 record the reversal, and the published v0.6.4 wording
  is left as the record of what that release shipped.
- **`README` documents `where`'s exit 2.** The command line quoted exit 1 alone,
  so a script author had no way to know that "could not check" was a distinct
  answer.

### Tests & Tooling

- New cases: credential scoping and the plain-HTTP warning (`test_client.py`);
  push-destination validation, the fenced delete, and a delete that must not
  report `ok` for a ref that is not there (`test_helper.py`); the launcher's
  isolation, including a hostile working directory (`test_launcher.py`); the
  doctor's exit 2 on an unresolvable data directory (`test_seafile_doctor.py`);
  and the vault's initial branch, a slashed branch name, and idempotence
  (`test_snapshot_tool.py`). `ProtocolErrorDisciplineTests` reads the helper's
  AST and fails if any function other than `_proto_error` writes an `error` line.
- Two tests that failed on the fixes were corrected rather than accommodated,
  because both had been passing for the wrong reason:
  `test_cmd_push_tag_creation_and_deletion` modelled a repository where the tag
  had never been pushed (so the old blind delete reported `ok` for a no-op), and
  `test_the_launcher_resolves_its_own_directory` read `PYTHONPATH`, which the
  isolated launcher no longer sets.

## [0.8.2] - 2026-10-11

The symlink-restore fix, plus the two documentation gaps the same-day audit left open. One of
those gaps turned out to hide a real defect: writing a note about how symlinks survive a
restore showed that a snapshot containing one could not be restored at all. No protocol changes.

### Added

- **Shallow and partial clones now work.** The fetch-skip optimisation reasons from tip-commit presence, which implies a complete object store only in a full clone: in a `--depth` or `--filter` clone the tip can be present while its trees and blobs are not, so the skip left `git checkout` reporting missing objects. The helper now withholds the skip when the repository is shallow (`git rev-parse --is-shallow-repository`) or carries a promisor remote (`remote.<name>.promisor`), and fetches the pack instead. The probe fails safe — if it cannot answer, the pack is fetched.

### Changed

- **The tools' acquisition is now documented, and the test runner's curve re-measured.** No wheel contains `tools/`, so the README's `pip install git+…` followed by `python tools/seafile_snapshot.py` asked the reader to run a file that was not there; the README's snapshot section and `USER_GUIDE` §15 now say the tools come with the source checkout and the sdist, and the README's diagnostics section says so for `seafile_doctor.py`. Separately, `tools/run_tests_parallel.py`'s docstring published a **52.7 s** headline measured against 268 tests — before the snapshot tool and its tests existed — which read as a sub-minute suite for one that takes about four minutes; it now carries a curve measured at 612 tests / 118 targets, names the critical path (`test_snapshot_tool.ByteExactnessTests`, 115.6 s against a 238.6 s wall at `-j 8`), and no longer quotes a single-threaded baseline at all rather than quote a stale one. `CONTRIBUTING.md` carried a second copy of the same curve and was corrected with it. Symlink behaviour also gained user-facing notes: `USER_GUIDE` §8.2 (the helper side) and §15.4 (the restore side), the README's snapshot warning, and a rewritten `SNAPSHOT.md` "File modes and symlinks" paragraph, whose old claim that a restore "may not reproduce permissions or symlinks" was wrong in the other direction — a link is captured faithfully even when the source has `core.symlinks=false`.

### Fixed

- **A snapshot holding a symlink could not be restored, and one holding nothing but symlinks crashed the restore.** `verify_restore` gathers symlink entries (mode `120000`) into a list deliberately kept apart from the regular files', but looked each link up by its *position* in that other list. A correctly restored link was therefore compared against an unrelated file's blob and reported as a mismatch — and a mismatch aborts the restore, so any tree containing a symlink failed to restore on a machine where links work. A tree whose only entries were symlinks raised `IndexError` outright. Links are now looked up by path, and each is still judged by reading the link and comparing it to its own blob. The `0.8.1` note below that a correct restore "no longer spuriously fails" on POSIX described the intent rather than the behaviour that shipped; this is the fix that makes it true.
- **A push that lost a ref race is no longer reported as successful.** Seafile exposes no atomic server-side compare-and-swap, so the pre-write CAS check and the ref write are separate round trips, and a peer that ignores the advisory lock can land between them: the ref then holds the other client's commit while the first client is told `ok`, and that push vanishes silently. Each ref is now read back immediately after it is written, and a value differing from the SHA just written is reported as `error <dst> fetch first`. A read-back that cannot settle the question — the ref is absent, or the read fails — is reported as an unverified write (a warning), never as a failed push.

## [0.8.1] - 2026-10-11

The review-cycle fix pass. Two commits' worth of findings from the same-day full-tree review (REVIEW.glm.md, cross-checked against the gemini and qwen peer reviews) and the three snapshot-tool reviews (REVIEW.snap{,1,2}.md, archived): 41 findings closed, 595 tests across 113 targets, all green. No protocol changes; every fix is fail-loud or fail-closed. Only one entry is user-visible at the default settings on day one: a repository can no longer smuggle `[seafile] skipsafetychecks = true` past its own push.

### Fixed

Snapshot tool (`tools/seafile_snapshot.py`):

- **Symlinks no longer defeat the byte-exact certification.** `verify_restore` verifies symlink entries (mode 120000) by reading the link against the stored blob; on Windows a link checked out as a text file is now a reported mismatch instead of a false "verified", and on POSIX a *correct* restore no longer spuriously fails.
- **A restore warns about embedded repositories (gitlinks) in the restored tree.** The snapshot-time warning used to be the only one; a restore years later printed "verified byte-exact" over empty directories.
- **`git()` scrubs the ambient Git environment and detaches stdin.** `GIT_INDEX_FILE` has no command-line override, so a run launched from a git hook or alias could write the whole tree (ignored files included) into the launching repository's index; `GIT_TERMINAL_PROMPT=0` turns a scheduler credential prompt into a fast failure instead of a hang holding the vault lock.
- **Multi-segment excludes work** (`--exclude packages/web/build` excludes everything under it — the precise spelling used to match nothing), patterns accept Windows backslashes, and bare-name matching is case-insensitive so one command line means one snapshot on every platform.
- **Smaller hardening:** `delete_branch` honors its compare-and-swap (a silently-undeleted branch no longer masquerades as rolled back); the push-rollback message no longer claims the vault "matches the remote" when the remote moved during the retry; stale-lock reclaim survives Windows sharing violations; `verify_restore`'s mismatch error names the path git could not open; `--dry-run` writes nothing and no longer even builds a preview commit (which needed an identity the preview deliberately did not write — this is what broke CI runners with no global git identity; identity is still validated, read-only); the Win32 pid-probe prototypes are built once; repeated `--exclude` flags persist once.

Helper:


- **Branch-deletion push errors can no longer corrupt the protocol stream.** Every `error <ref>` line now goes through one sanitizer that flattens `\r`/`\n`; the branch-deletion path was the one site still writing a raw exception message, whose second line git would have parsed as a helper command.
- **Packfiles are published pack-first, index-second.** Git discovers packs by scanning for `*.idx` and assumes the sibling `.pack` exists; installing the index first exposed a window in which a concurrent `git status` could hit "cannot open packfile". A pack without its index is simply ignored, so the new order has no observable broken state.
- **Repo-local git config can no longer disarm the safety guardrails.** `seafile.skipsafetychecks` is read through a system/global-scoped reader: a repository shipping `[seafile] skipsafetychecks = true` in its own `.git/config` silently disabled Trap 1/Trap 2 for every operation launched from that checkout.
- **Credentials in a user-typed URL no longer reach the terminal.** A new `mask_url()` strips `user:password@` before the gc tip and every CLI status line interpolates a URL.
- **An unknown helper command fails loudly.** It used to write `error unsupported` (a line git's helper reader does not know) to stdout and keep reading; it now diagnoses on stderr and exits non-zero.
- **The LFS unknown-event reply carries `event: "complete"`.** A bare `{"error": ...}` matches no dispatch state of git-lfs's wait loop and could hang the transfer.
- **`lock-status` and `unlock` no longer treat "could not inspect the lock queue" as "unlocked".** A listing failure reports a distinct error state (`locked: None`): `lock-status` exits 1 with a diagnosis, and non-force `unlock` refuses to act on a queue it could not read.
- **Non-force `unlock` deletes only the winning ticket.** It used to sweep every ticket in `.git-lock.d`, destroying other clients' (even waiters') tickets; the queue sweep is now `--force`'s documented job.

Tools:

- **`seafile_snapshot.py` decides "branch absent on remote" by asking the remote** (`ls-remote`), not by matching English git error text — the old sniff was locale-dependent and broke first-run on localized git.
- **The snapshot tool's synced-folder guard works from a bare checkout and warns when it cannot tell.** A missing `sys.path` made the guard a silent no-op for the documented `python tools/seafile_snapshot.py` invocation, and read failures returned "not synced" without a word.
- **The snapshot tool's Windows PID probe no longer misreads a live lock holder as dead.** `OpenProcess` now goes through a properly prototyped `WinDLL("kernel32", use_last_error=True)`: `GetLastError()` through `ctypes.windll` can return a stale value and an unprototyped HANDLE truncates — either defect could let a concurrent run steal a lock. The helper's own `_is_pid_alive` had the same defect and was fixed the same way.
- **`seafile_doctor.py` hardening:** the sync-error histogram guards absurd timestamps like the rest of the file; the churn window sorts on a real date key (the `MM/DD/YY` string sort picked the wrong days across a year boundary) and `--days 0` means "no window"; database-read diagnoses go to stderr, not stdout; and `where` answers exit 2 "could not read repo.db" so a script never receives a verified negative for an unknown state.

Docs:

- Three places advertised a `gc --aggressive` flag that does not exist; they now reference `git-remote-seafile gc`. The acknowledgment links say "Seafile Web API v2" like the body text and v0.5.3 changelog entry already did. `SNAPSHOT.md`'s gc-notice wording matches the actual threshold behavior; the README command table lists `version`.

## [0.8.0] - 2026-10-11

The snapshot tool ships. `tools/seafile_snapshot.py` captures a working tree — **including the files `.gitignore` excludes** — into versioned, byte-exact snapshots stored in a Seafile library, using the same transport the helper already provides. It closes the one gap the helper structurally cannot: a `.gitignore`d file never enters a commit, so no remote helper can ever carry it. The release also folds every fix from the four-way review cycle (GLM, qwen, gemini, deepseek — `archive/REVIEW.*-v0.8.0.md`); the tag originally pointed at a commit whose release build failed, so the review fixes joined the release rather than following it.

### Added

- **`tools/seafile_snapshot.py` — whole-tree snapshots over Seafile.** Pointed at a source directory, it maintains a separate *vault* repository whose `--work-tree` **is** that source, so the source stays read-only and untouched; the vault commits the tree as it lies on disk, secret files and build output included. `--remote <url-or-path>` pins the vault's Seafile destination. Every run is atomic — a vault-local index, `update-ref` with an expected old value, and a `seafile-snapshot.lock` — and **an unchanged tree produces no commit at all**, so a scheduler can run it as often as it likes without growing the remote.
- **Byte-exactness is enforced, not assumed.** Git for Windows sets `core.autocrlf=true` in its *system* config, so a fresh `git init` inherits it while `git config --global core.autocrlf` reports nothing. The vault pins `core.autocrlf=false` **and** writes `* -text -filter -ident` into the vault's own `info/attributes`, which outranks any `.gitattributes` copied from the source. An attributes override must name **every** attribute class it neutralises: `* -text` alone does not disable `filter`, and a source `filter=lfs` attribute silently stored a 130-byte LFS *pointer* with the real bytes uploaded nowhere. A plain `git clone` of the vault is lossy for the same reason — `info/attributes` is not transferred by a clone — so `--restore` clones `--no-checkout` and re-applies the settings before checking out.
- **`--restore` and `--verify`.** `--restore` rebuilds the tree into a target directory and verifies byte-exactness on **every** run using `git hash-object --no-filters`, the one hash check that reads the bytes on disk rather than the bytes a filter would produce. `--verify` additionally runs `git fsck` over the vault.
- **`--exclude <glob>` (repeatable), `--dry-run`, `--code-remote <name>`.** Excludes use an fnmatch matcher, so `*/node_modules` and `*dist*` mean what they look like rather than being root-anchored pathspecs that match nothing below the root.
- **`--message <text>` and `--reset-to-remote`.** `--message` overrides the snapshot commit message. `--reset-to-remote` is the recovery path for a vault whose local and remote chains have diverged with no common tip: the run stops and explains, and this flag adopts the remote chain deliberately, dropping the local-only snapshots. That is the one case in which a snapshot can be lost, so it is never taken implicitly — the tool names the flag and the cost, and waits to be asked.
- **`--allow-shared-remote`.** Pushing the vault to one of the *source's own* code remotes is refused by default: mixing a whole-tree backup (which legitimately contains `.env`, keys and build artefacts) into a shared code library is almost never intended. The refusal names the colliding remote; the flag overrides it deliberately.
- **Credential-shaped paths are reported at the end of every run.** Not a refusal — a full backup *should* capture `.env` — but a warning, so the first push is not the moment you find out.
- **A vault belongs to one source tree.** It records the source directory it first saw and refuses a run pointing a *different* tree at it: one vault is one linear chain of snapshots of one tree, and mixing two is not something a later reader could undo.
- **Neither the source nor the vault may live in a Seafile-synced folder.** Checked at startup — before anything is written — via `safety.discover_local_synced_libraries()`. The sync client rewrites files underneath whatever lives in a synced folder and competes with Git for every path, which is the exact problem the tool exists to avoid. The helper cannot see the vault (it is told about the *source* and nothing else), so this check is the vault's only line of defence.
- **`--code-remote <name>` must name a real remote** of the source. Without it a typo failed *after* the snapshot was built, leaving a commit for a run the user reads as failed.
- **Flags that would otherwise be ignored are errors.** `--ref` without `--restore` is the sharp one: a run meant to restore an *old* snapshot would quietly take a *new* one instead. The snapshot-only and restore-only flag sets are now mutually rejected, and `--no-push` with `--remote` is refused as contradictory.

### Fixed

Snapshot tool:

- **Multi-machine snapshots were permanently broken.** The vault's next snapshot branched from the *local* branch rather than the remote tip, so a second machine's push was rejected `(fetch first)` **and still moved its local branch** — which made every subsequent run fail too, while the first run *looked* like it had worked. The run now fetches the remote tip and bases on it, retries once by re-parenting on a lost race, and rolls the local branch back to what the remote holds on failure.
- **`--no-push` no longer erases the vault's remembered remote.** It is a per-run override; it used to `--unset-all snapshot.remote`, so one local run silently degraded a scheduled job to local-only snapshots. A remembered `--code-remote` is suppressed for the run too, making "pushes nothing" literally true.
- **The push rollback survives a failure in the retry itself.** A fetch dying mid-re-parent (exactly when the network is flaky) used to leave the vault branch advanced past a commit the server refused. The rollback now runs on every failure path, and "did the remote move?" is decided by re-fetching the tip rather than matching words in git's (locale-dependent) output.
- **`--restore` refuses a destination inside a Seafile-synced folder** — the desktop client would upload the restored tree (secrets included, by design) straight back into its original library — and refuses `--source`/positional `SOURCE` instead of silently ignoring them.
- **`ensure_identity` read the wrong config scope.** It used `git config --get`, which reads through to the *global* config, so the source repository's own identity was never consulted — and the test for it was green for the wrong reason. Now `--local`.
- **Nested repositories are detected and named.** A nested repo appears to Git as a submodule gitlink; the tool warns prominently on every run — recording only the gitlink, not the contents, so a restore would produce a broken directory — and says to back it up separately. (Capturing the contents via `update-index --cacheinfo` is measured and written up in SNAPSHOT.md §8 (future work).)
- **Stale locks are reclaimed** via `pid_is_alive` — which now also treats an out-of-range pid (beyond POSIX `os.kill`'s pid_t) as dead instead of crashing, and Windows `OpenProcess` ACCESS_DENIED as *alive* instead of stealing a live elevated process's lock; `VaultLock` fsyncs its identity write, retries a transient Windows sharing-violation on unlink, and reclaims an unparseable lock file past a grace window.
- **A shrunken upload source now fails the upload instead of silently sending a short body.** `StreamingMultipartFile.read` returned `b""` at the source's premature end, which urllib3 reads as EOF — so `requests` sent the declared `Content-Length` with fewer bytes and nothing raised anywhere.
- **A failing vault-config write is an error**, not a silent divergence between what the run believes it remembered and what the vault holds; the vault config is read once per run (was four times), backslash normalisation is scoped to the path-valued key, and the byte-verification of a restore hashes every file in one process.

Helper:

- **gc refuses to compact when refs outside `refs/heads`/`refs/tags` exist** (or a ref name git could not honour): the mirror cannot carry them, so their objects would have been deleted permanently while the ref file survived, broken.
- **Download-error messages no longer leak the transfer token.** The upload path already redacted it; `get_file_bytes` and `download_file_to` now name the origin and the repo-side path instead of the whole `seafhttp` link.
- **`_adjust_url` compares authorities case-insensitively** through `normalize_netloc`, so `SEAFILE_SERVER=https://MyServer.com` still gets the http→https upgrade whose absence turned every push into a redirected-POST "HTTP 400"; `SeafileClient` grows `close()` and the context-manager protocol.
- **`option object-format` accepts the documented algorithm form** (`{'true'|sha1|sha256}`) instead of answering "unsupported" to a legitimate pin.
- **`seafile://[::1]/lib/repo` parses** — the bracketed IPv6 bare-host form (optional `:port`) is one address; only the unbracketed colon-bearing form is refused, with the bracketed spelling named.
- **`lock-status` flags an all-expired ticket queue as STALE** instead of presenting a dead lock as one that "expires in 0s"; gc's `check_remote_has_packs` propagates listing failures instead of failing open; `_prune_empty_ref_parents` deletes a parent only on a positively-confirmed empty listing; an LFS download whose size disagrees with the request fails with an explicit 502; a non-numeric `seafile.*` integer warns instead of silently using the default.
- **`unlock` on an unlocked repository says "not locked; nothing to release"** instead of reporting a successful unlock that did nothing (`RemoteLock.unlock` now returns whether a lock existed).
- **A transient error resolving the target library is warned about** during the safety checks instead of silently degrading Trap 1/2 to folder-name matching, which on a renamed sync folder read exactly like "no collision possible".
- The doctor names `repo.db` (not a `sync_error.db` it never opens), `cmd_where` exits 1 for "not synced" so scripts can gate on it, and an unexpected log shape or client schema no longer kills a whole report.

### Changed

- **Every option now has a default, and the vault remembers the rest.** A bare `python tools/seafile_snapshot.py` is a complete run: the source is the current directory, the vault is `<source>-vault` beside it, and the destination, branch and exclusions are written into the vault's own config (`snapshot.*`) at the end of the first run, so a scheduled job needs no arguments. Defaults resolve most-specific-first — the flag you passed, then what the vault remembered, then the source repository (a single `seafile://` remote names the vault's destination: `seafile://code/app` → `seafile://code/app-vault`), then the current directory. A positional `SOURCE` argument is accepted as well as `--source`; `--no-push` overrides the remembered or derived remote *for that run* and leaves the vault's memory alone.
- **The recommended exclusions became `--default-excludes`, and are never silent.** Previously they were printed as advice in `--help` only. They are still not applied by default: a snapshot is a backup, and the failure that cannot be forgiven is a backup that quietly omitted the file that mattered — `node_modules` is reproducible, `.env` is not, and the tool cannot tell them apart. It now captures everything and then **names the reproducible bulk it noticed**, with the exact `--exclude` flags to drop it. `--default-excludes` adds the set on top of any `--exclude`; `--no-default-excludes` is its explicit negation.
- **The docs were re-anchored to the shipped tool.** SNAPSHOT.md's status header, synced-library guard, shared-remote check and next steps now describe what shipped; USER_GUIDE §15.4's restore examples use the real flag set (`--restore --remote --into`); the per-machine `refs/heads/snapshot-<host>` claim is corrected — the default is one shared chain, with `--branch` for per-machine chains; the CHANGELOG/release-notes claim that nested repositories are captured was false and is corrected (they are detected and warned about).

### Documentation

- **`SNAPSHOT.md`** — the snapshot tool's design document, in 16 sections: the gap, the approaches that do not work, the vault design, usage and defaults, the cost model, multi-machine semantics, restore and its verification, caveats and inherent limits, large files and LFS, the separate-repository argument, the restic/Borg comparison, what Git gives, prior art, what the helper should not grow, future work and non-goals, and maintenance.
- **`USER_GUIDE.md` §15** documents the snapshot tool for users, and **`README.md`** lists it alongside the other tools.
- `SNAPSHOT.md` joins the docs-consistency guard's document set, and the guard pins the corrected claims (per-machine chains, the synced-guard narrative, nested-repo warning, shared-remote check) as absent from every shipped document.

### Tests & Tooling

- **`tests/test_snapshot_tool.py`** — 92 cases across 22 classes, covering the byte-exactness traps, the exclude matcher, the restore verification, the multi-machine race and push-race recovery, identity resolution, stale-lock reclamation, the synced-folder guard wiring, the config-plumbing contract, the shared-remote refusal, the defaults and vault memory, and the argument validation.
- **Test suite ran each snapshot case twice, until a fixture split: 525 s → 128 s for the module, ~600 s → 206 s for the suite.** The parallel runner schedules one unit of work per *class*, and `unittest` inherits test methods — so when a test-bearing class was used as a base, its cases ran once under the base's own name and once under the subclass's. In `test_snapshot_tool.py` that was 49 of 124 cases duplicated. The fixture now carries no tests, the module is split into single-responsibility classes, and `tests/test_suite_shape.py` guards the whole suite against ever reintroducing a test-bearing base class (across module imports too).
- **The snapshot tool spent over half its subprocesses on config plumbing, now batched: a steady-state run is 28 → 14 git calls (12 without a push).** Every `git config` is a process, and on Windows each costs ~125 ms, almost all of it start-up. The remembered settings were read one key at a time and rewritten unconditionally on every run; they are now read in a single `--null --list` (once per run, not four times) and written only when a value actually changed, and the byte-exactness pairs and the committer identity likewise.
- **The four-way review's second pass fixed four tests that were green for the wrong reason**: the gc streaming-fallback tests actually disable streaming now (`del` on a spec-less MagicMock regenerates the attribute); the machine-behind fast-forward test's clone actually succeeds into its own vault (it used to fail silently into machine A's, so the reconcile path never ran); the explicit-worktree safety test's ids match, so Trap 1 really evaluates; the sqlite temp-copy test asserts its precondition. The suite-shape guard resolves base classes across modules, the e2e harness bounds every subprocess with a timeout, and the paths the release build hit (a POSIX `OverflowError` on a deliberately-huge stale-lock pid; the same-second commit-collision flake that fired on CI; the non-hermetic derivation test) are fixed and pinned.
- Suite: 463 → 576 tests across 112 targets, all green; docs guards 9/9.

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

[0.9.13]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.12...v0.9.13
[0.9.12]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.11...v0.9.12
[0.9.11]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.10...v0.9.11
[0.9.10]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.9...v0.9.10
[0.9.9]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.8...v0.9.9
[0.9.8]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.7...v0.9.8
[0.9.7]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.6...v0.9.7
[0.9.6]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.5...v0.9.6
[0.9.5]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/tkittich/git-remote-seafile/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.8...v0.9.0
[0.8.8]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.7...v0.8.8
[0.8.7]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.6...v0.8.7
[0.8.6]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.5...v0.8.6
[0.8.5]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.4...v0.8.5
[0.8.4]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/tkittich/git-remote-seafile/compare/v0.8.0...v0.8.1
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
