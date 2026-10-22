# SNAPSHOT.md — full-tree backup to Seafile, `.gitignore`d files included

> **Status: shipped in v0.8.0; payload mode and the installed command in v0.9.0.**
> The implementation is `git_remote_seafile/snapshot_tool.py` (generated from
> `tools/_snapshot/`; `tools/seafile_snapshot.py` is a wrapper);
> the tests are `tests/test_snapshot_tool.py` (101 cases across 24 classes), and
> the tool is documented in `USER_GUIDE.md` §15, `DESIGN.md` §4.10 and
> `CHANGELOG.md`'s 0.8.0 entry. Everything below describes the tool **as it
> ships**; measurements were taken on this machine unless a claim says otherwise.
>
> This file is kept current **as part of the work** — see
> [Maintenance](#16-maintenance). Mechanical claims (config keys, environment
> variables, the corrected-claims list) are guarded by
> `tests/test_docs_consistency.py`; prose and measurements are not, so re-read
> them when the tool changes.

---

## 1. The gap

`git push` transfers **commits**. Anything matched by `.gitignore` never enters
a commit, so no Git remote helper — `git-remote-seafile` included — can carry
it. A repository backup and a *file* backup are therefore two different jobs,
and `git push` only does the first.

## 2. The approaches that do not work

**Renaming `.gitignore` aside.** The obvious idea — rename, `git add .`, push,
rename back — fails on both counts:

- **Unnecessary.** Renaming tracks *nothing*. Measured:

  ```
  $ mv .gitignore .gitignore.bak
  $ git ls-files            # still 2 files -- unchanged
  $ git status --porcelain  # the ignored files merely appear as "??"
  ```

  The ignored files only become *visible*; they still have to be added. And
  `git add -f` stages ignored files directly, with no rename at all — so the
  rename is pure ceremony.
- **Not atomic.** It mutates user-visible state — the working tree *and* the
  repository's own index — for the duration of the run. A crash, a Ctrl-C, or a
  concurrent IDE leaves a repository with no `.gitignore` and a fully-staged
  index. No `try`/`finally` fixes that, because the intermediate state is on
  disk and observable.

**`git stash`.** The only Git command whose stated purpose is "save the working
state", probed rather than reasoned about — and it fails on four counts:

```
$ git stash push -a -q -m probe
$ git rev-list --parents -n1 'stash@{0}'     # -> THREE parents
$ git ls-tree -r --name-only 'stash@{0}'     # -> .gitignore tracked.txt
$ git ls-tree -r --name-only 'stash@{0}^3'   # -> .env node_modules/dep.js
$ ls -A                                      # -> .git .gitignore tracked.txt
```

1. **Ignored files are not in the stash's tree** — they go in a *third parent*
   (`stash@{0}^3`), a side channel. A tree checkout materialises `.gitignore`
   and `tracked.txt`; `.env` and `node_modules/dep.js` are absent.
2. **`git stash push` mutates the source** — the listing above is the working
   directory *after* the command: the untracked and ignored files were
   **deleted** from it.
3. **The non-destructive form is a silent no-op.** `git stash create` leaves the
   tree alone and **silently ignores `-u`**: exit 0, nothing on stderr, no
   commit object. `git stash create -h` does not even list the flag.
4. It requires a HEAD, so it cannot snapshot a directory that is not yet a
   repository.

## 3. The design: a vault

A **vault** is a separate Git repository whose `--work-tree` points at the
source directory:

```
source dir  --(--work-tree)-->  vault repo  --git push-->  seafile://code/<name>-vault
(only ever read)                 own .git, own index
```

**Two words, two different things.** A **vault** is the *repository*: one
directory, one `.git`, one remote. A **snapshot** is a single *commit* inside
it. The command is `git seafile-snapshot` because that is the verb ("take a
snapshot"); the flag is `--vault` because that is the object the user points at.

Snapshots are ordinary commits built in the vault's **own** index. The source is
never written to: not `.gitignore`, not `.git`, not the index -- with one
exception, a probe file at the source root that is created and removed within a
moment, **only** when the source is on a network share, to read that share's clock
(PAYLOAD.md D6). The code
repository stays small, so `git clone` on a second machine stays fast — the
snapshot never enters its history.

**Atomicity, concretely:**

| Property | How |
| :--- | :--- |
| Interrupted run | Leaves at worst a stale vault index; the source is untouched. The next run overwrites it. |
| Concurrent snapshots | A vault-local `seafile-snapshot.lock` (created `O_EXCL`); records `hostname pid`, `fsync`ed; reclaimed when the holder is dead *on the same host*, or the file is unparseable and older than a grace window. |
| Torn branch update | `git update-ref` with an **expected old value** — a racing run loses loudly rather than clobbering. |
| Push | A single ref update, guarded by the helper's distributed lease lock. |
| Failed push | The local branch is **rolled back** to what the remote actually has — on every failure path, including a failure inside the retry. |
| Idempotence | An unchanged tree produces **no commit at all**. |

## 4. Usage, defaults, and refusals

Every option has a default, so a bare run is a complete run:

```bash
# Snapshot C:/code/myproject into C:/code/myproject-vault and push it to
# Seafile, inferring the destination from the source repo's own remote
cd C:/code/myproject
git seafile-snapshot

# From elsewhere; the vault defaults to '<source>-vault'
git seafile-snapshot C:/code/myproject

# Preview: nothing is pushed, the branch does not move, no setting changes
git seafile-snapshot C:/code/myproject --dry-run

# Snapshot + push, and push the code repo first in the same command
git seafile-snapshot --source C:/code/myproject \
    --vault C:/code/myproject-vault \
    --code-remote origin \
    --remote seafile://code/myproject-vault \
    --default-excludes
```

### Defaults, and where they come from

Resolved most specific first:

1. **the flag you passed** — always wins;
2. **what the vault remembered** from its last run — written into the vault's
   own config under `snapshot.*` at the end of every non-dry run;
3. **the source repository** — a single `seafile://` remote names the vault's
   destination (`seafile://code/app` → `seafile://code/app-vault`); two such
   remotes are ambiguous and derive nothing;
4. **the current directory** for the source, `'<source>-vault'` for the vault.

A snapshot is of a whole repository, as `git push` is. Run from a subfolder of a
git repository, the source becomes the repository root (with a note), so the
vault and its remote are the repository's, never a second chain of a partial
tree. An explicit `--source` that is a subfolder is refused, naming the root. A
vault that already remembers a subfolder as its source keeps working.

So a scheduled job is the bare command, and an explicit flag is written back for
next time. Two overrides are deliberately *per-run* and leave the vault's memory
alone:

- **`--no-push`** — this run pushes nothing (the vault *and* the code repo) and
  changes no remembered setting. A one-off local run does not unhook a
  scheduled job.
- **`--dry-run`** — previews the run: the vault is initialised if missing and
  the snapshot is built into its index (so the reported diff is real), but the
  branch does not move, nothing is pushed, and the remembered settings are
  untouched.

The vault is the bottom of the chain *and* the place the chain is stored, which
is what makes the second run argument-free. It also means the vault remembers
which source it belongs to: a run pointing a **different** tree at an existing
vault is refused, because one vault is one linear chain of snapshots of one tree
and a later reader cannot undo a mix.

### Exclusions are never silent

**There is no default exclude list.** "All files" is the point of a snapshot,
and the failure that cannot be forgiven is a backup that quietly omitted the
file that mattered — `node_modules` is reproducible, `.env` is not, and the tool
cannot tell them apart. Instead it captures everything and then names the
reproducible bulk it noticed, with the flags to drop it:

```
Warning: the snapshot includes reproducible build output that is usually not worth
backing up.  Leave it out next time with --default-excludes, or explicitly:
  --exclude 'build' --exclude 'dist' --exclude 'node_modules' --exclude '*.pyc'
```

`--default-excludes` adds the recommended set (`node_modules`, `venv`, `.venv`,
`__pycache__`, `*.pyc`, `dist`, `build`) on top of anything passed to
`--exclude`. Excludes are remembered with everything else, so one
`--default-excludes` run keeps them until they are overridden explicitly.

> **A pattern with no slash matches that name at *any* depth.** `--exclude
> node_modules` drops `packages/api/node_modules/` as well as the top-level
> one — what a monorepo needs. Wildcards work across separators (`*.pyc`
> matches `a/b/c.pyc`), and a trailing slash is ignored. **Quote a wildcard**
> so your own shell does not expand it first. `*dist*` is the trap in the other
> direction: it also matches `distance.txt`. Prefer the bare name, or `*dist/*`
> if a wildcard is genuinely wanted.

Excludes are applied **before** the tree is staged, not by deleting paths from
the index afterwards. The order is the point: the index is a stat cache, and a
path it was never told about has no entry to skip, so an excluded path that is
staged and then removed has already been read from disk, hashed, and written
into the object store. Filtering first means an excluded directory is never
opened at all — see §5 for what that is worth.

That mechanism stages the survivors through `git add --pathspec-from-file`,
which arrived in **Git 2.25**. Without `--exclude`/`--default-excludes` the tool
stages with a plain `git add`, where the package's documented Git 2.20 floor is
enough; with them, 2.25 is the floor. There is deliberately **no fallback** to a
plain `add` on older Git: staging first and dropping the excluded paths
afterwards is exactly the read-then-discard order the paragraph above exists to
avoid, so the fallback would quietly undo the guarantee rather than degrade it.
The paths are handed to git as **literal** pathspecs (`GIT_LITERAL_PATHSPECS=1`),
so a name like `a[b].txt` or `*.log` is staged as itself and never as a glob.

### Refusals, not guesses

The failures this tool can cause are quiet ones, so wherever a guess would be
silent it refuses instead:

- **Neither the source nor the vault may live in a Seafile-synced folder.**
  Checked at startup — before anything is written — via a best-effort import of
  the helper's `discover_local_synced_libraries()`. A machine without the
  Seafile client simply sees no synced libraries and the guard is idle. The
  vault needs its own check because the helper can never see it: the helper is
  told about the *source* (`GIT_WORK_TREE`) and nothing else.
- **A restore destination may not live in a synced folder either.** A restored
  tree — secrets included, by design — inside a synced library would be
  uploaded straight back into its original library.
- **A vault belongs to one source** — records it, and refuses a different one.
- **`--code-remote <name>` must name a real remote** of the source, so a typo
  fails before the snapshot instead of after it.
- **`--no-push` with `--remote`** is contradictory and refused.
- **Flags that would be ignored are errors.** `--ref` without `--restore` is
  the sharp one: a run meant to restore an *old* snapshot would otherwise
  quietly take a *new* one. The snapshot-only and restore-only flag sets are
  mutually rejected — including `--source`/positional `SOURCE` under
  `--restore`.
- **The vault's remote may not be one of the source's own code remotes.**
  A whole-tree backup legitimately contains `.env`, keys and build artefacts;
  pushing it where the code goes is almost never intended. The refusal names
  the colliding remote; `--allow-shared-remote` overrides it deliberately.
- **A failing vault-config write is an error**, not a silent divergence between
  what the run believes it remembered and what the vault holds.

### Exit codes and `--json`

| Exit | Meaning |
| :--- | :--- |
| `0` | Done (a warning may have been printed), or there was nothing to do. |
| `1` | The run failed; `Error:` on stderr names why. |
| `2` | The command line was wrong. |
| `3` | The run completed but printed a `Warning:`. **Only with `--exit-warnings`**: without it the same run exits `0`, as it always did. |

A scheduled job that must alert on anything unusual runs with `--json
--exit-warnings`: stdout is then exactly one JSON line, so `jq -e '.status == "ok"'`
is the whole check. The document names `mode` (`snapshot`, `restore`,
`restore-drill`, `payload-verify`, ...), `status`, `exit_code`, `warnings` (the
`Warning:` lines), `error` (the last `Error:` line) and `result`, whose keys depend
on the mode (a snapshot: `files`, `bytes`, `commit`, `pushed`,
`payload_objects_to_upload`; `payload-verify`: `checked`, `objects`, `damaged`). The
keys of a mode are only ever added to.

## 5. What a run costs

### Transferred

Git is content-addressed, so only changed blobs are uploaded. Measured on a
486 KB, 62-file tree:

| Scenario | Pushed |
| :--- | ---: |
| First snapshot | 639 KB remote in total |
| One 4 KB tracked file + one 10 KB ignored file changed | **31 KB** |
| Nothing changed | **0 KB — and no commit is created** |

The vault's index keeps a stat cache, so unchanged files are not re-hashed
either. Because the vault is ordinary Git history, "roll back to last Tuesday"
is `git show`, `git diff`, or a checkout of an older commit — not a restore
from a proprietary archive format.

**An excluded path costs nothing, and that follows from *when* the filter
runs.** Excludes are applied before staging rather than by dropping paths from
the index afterwards, so an excluded directory is never opened: no read, no
hash, no loose object. Staging first and filtering second paid all three and
then discarded the result, leaving the excluded bytes unreferenced in the
object store until a `gc`. The exclusion list is therefore a lever on the
*run's* cost, not only on the snapshot's size.

### Subprocesses

Every `git` invocation is a separate process, and on Windows that is about
**125 ms** of pure start-up regardless of how little the command does — measured
with `git --version` in a loop. The count therefore matters more than the work.
A settled vault (the second and later runs) used to make **28** calls, of which
**16 were config plumbing**: five remembered keys read one at a time, five keys
rewritten unconditionally, the two byte-exactness pairs reset every run — and
the vault's config was re-read four times over by different functions.

All of that is now batched, idempotent, and read once:

| | Before | After |
| :--- | ---: | ---: |
| Config reads (remembered + identity + byte-exactness) | 5 | **3** (2 vault + 1 source) |
| Remembered settings written | 5 keys, ~8 calls | **0 when nothing changed** |
| Byte-exactness (`autocrlf`, `safecrlf`) | 2 calls, always | **0 when already set** |
| Identity (`user.name`, `user.email`) | 6 calls | **1 read, 0 writes** once inherited |
| **Whole run, unchanged tree (then pushed)** | **28 calls** | **14 calls** |
| Whole run, unchanged tree, `--no-push` | 25 calls | **12 calls** |

There is a floor here — a snapshot inherently needs `fetch`, `add`,
`write-tree`, `rev-parse`, `ls-tree`, `ls-files` and the push, and none of those
can be avoided without re-implementing Git's index. Fourteen calls is close to
it.

The maintenance pass adds on top of that floor, and only when it fires: the
first run that has not yet decided the storage profile samples the tree (stat
calls plus about 24 MiB of reads, no git processes) and, for a large raw
payload, writes two config values (a vault decided `default` repeats the stat
walk each run to see whether its tree has doubled); a *new* vault with a
gigabyte or more of compressible 4--64 MiB files also measures its link once
(four 16 MiB uploads, deleted again; PAYLOAD.md D1); and a push whose loose tier
has passed 256 MiB takes an incremental `repack -d` + prune pair.

> **Watch the remote's packfile count.** The helper counts only *small* packs (below half of seafile.gcmaxpackmib, default 1 GiB) and warns once their number reaches the threshold (seafile.gcthreshold, default 20). With `seafile.autogc` on (the default), a bounded tail merge consolidates them inline after each qualifying push — large packs are frozen where they stand; with it off, run `git-remote-seafile gc <url>` deliberately. A remote that is never compacted grows in bytes without bound — see the
> large-files section below for how fast that can be.

## 6. Several machines, one vault

A vault can be shared. Each machine fetches the remote tip **before** building,
so its snapshot lands on top of the shared chain rather than beside it. The
reconciliation:

| Local vs remote tip | Action |
| :--- | :--- |
| Remote has no branch yet | Build on the local branch (first push). |
| Local absent, or equal | Build on the remote tip. |
| Local is an **ancestor** (behind) | Fast-forward, then build on the remote tip. |
| Remote tip is an **ancestor** (ahead, snapshots taken offline) | Keep the local chain; the push fast-forwards. |
| **Diverged** | Stop, change nothing, explain. `--reset-to-remote` adopts the remote chain instead. |

Two machines can also land between one run's fetch and its push. The loser sees
a non-fast-forward rejection; because a snapshot is just a tree, it is
re-committed on the new tip and pushed again — one automatic retry, chain still
linear. Measured with a synthetic race: the remote ends up
`A -> rival -> B`, three commits, no loss. Whether the remote actually moved is
decided by **re-fetching the tip**, not by matching words in git's
(locale-dependent) rejection output — so the retry works in any locale, and if
the re-fetch itself fails the rollback still runs.

**Divergence is refused rather than resolved silently.** Merging two snapshot
chains is meaningless — they are two different working trees, not two edits to
one file — and quietly dropping a machine's snapshots is the kind of silent
data loss this project treats as a bug. The refusal names the remedy. Per-machine
chains are available with `--branch snapshot-<host>`; the default is the shared
chain, because that is what "one backup" means.

### Which machine took which snapshot

A shared chain is one log written by several machines, and the commit *author*
is the same person on all of them — the identity is inherited from the source
repository's **own** config (the same repository cloned everywhere), never from
the global one. So the default snapshot message carries the **hostname** and a
UTC offset:

```
snapshot 2026-10-10T21:45:42+07:00 on workstation-a
snapshot 2026-10-10T21:45:46+02:00 on laptop-b
```

## 7. Restore

```bash
git seafile-snapshot --restore \
    --remote seafile://code/myproject-vault \
    --into C:/restore/myproject

# roll back: any revision works
... --ref snapshot~3
```

### Restore drill

```bash
git seafile-snapshot --restore-drill --remote seafile://code/myproject-vault
```

A backup nobody has restored is a hope. The drill restores the newest snapshot
(or `--ref`) into a temporary directory, checks every file byte-for-byte, runs
`git fsck`, downloads a random sample of the payload (default 10 files, `all`
for every one) and compares each with its SHA-256, then deletes the directory. It
costs the vault's disk and download once, so run it monthly, not nightly; a
failure keeps the directory for inspection and exits 1.

An interrupted run resumes: objects already uploaded are skipped, and the hash
cache is saved about once a minute while a tree is hashed (and when an upload then
fails), so a killed run re-reads only what it had not reached.

The snapshot holds the **whole** tree, tracked files included, so this single
command reconstitutes the working directory. Clone the *code* repository
separately only if you want its branches and history — see the caveats.

### Why `--restore` exists instead of a plain `git clone`

A plain `git clone` of the vault is **lossy on Windows**, and this was found by
running the round trip rather than by reading the code.

The vault stored the correct blob — `git cat-file` showed `a\nb\nc\n` — but the
checkout wrote `a\r\nb\r\nc\r\n`. Cause: **Git for Windows ships
`core.autocrlf=true` in its *system* config**, so `git config --global
core.autocrlf` reports *nothing* while the effective value is `true`, and a
freshly initialised vault inherits it. A backup whose restore differs from the
original is not a backup.

Two fixes, both needed:

1. The vault is pinned byte-exact on **every** run: `core.autocrlf=false` plus
   an `info/attributes` override carrying `* -text -filter -ident`.
2. `info/attributes` has the **highest precedence** in Git's attribute chain, so
   it also outranks a `.gitattributes` in the source tree — a source
   `* text=auto` cannot re-enable conversion. Confirmed with `git check-attr`,
   which reports `text: unset` and `filter: unset` for every path.

`--restore` is still required, because **`info/attributes` is not transferred by
a clone** while the source's `.gitattributes` is. A naive clone re-inherits the
system autocrlf and re-converts. So `--restore` clones `--no-checkout`, applies
the byte-exact settings to the clone, materialises the branch ref *without
touching the worktree* when a `--ref` is requested, and checks out exactly once.

### Verification, on every restore

Every restore re-hashes every checked-out file with `git hash-object
--no-filters` — batched into a single process, bytes end to end — and fails
loudly on any difference. The check is byte-level on purpose, because **on a
naive `git clone` of the vault both of the obvious checks pass a mangled
restore**:

| Check | On a CRLF-mangled checkout |
| :--- | :--- |
| `git status --porcelain` | **empty** — it compares *through* `core.autocrlf`, so the mangling is normalised away |
| `git hash-object <path>` | **matches** — it applies the clean filter, re-normalising the very difference being looked for |
| `git hash-object --no-filters <path>` | **mismatches** — it hashes the bytes as they lie on disk |

All three measured on one clone, in one run. A verification step that cannot
fail is worse than no verification, because it gets believed. `--verify`
additionally runs `git fsck`.

**Symlink entries (mode `120000`) never go through `hash-object`**, because it
cannot judge one: on POSIX it follows the link and hashes the *target*, so a
correct restore would be reported as a mismatch, and on Windows a link checked
out as a text file would hash equal to the blob, so a restore that lost the link
would pass. Each link is read with `os.readlink` and compared to its own blob
instead, and a path that does not come back as a link at all is a mismatch.

Verified byte-identical after restore: LF text, CRLF text, a 256-byte binary
blob, a file with no trailing newline, a symlink (with `core.symlinks` enabled,
which is the default on Linux and macOS), `.gitattributes`, `.gitignore`, and
ignored files under `node_modules/`. The naive clone differed — which is what
makes the contrast real rather than assumed.

## 8. Caveats and inherent limits

**Nested Git repositories are captured as their files, not their history.**
`git add` records a directory containing its own `.git` as a single commit
reference (mode `160000`), and that commit is not copied into the vault.
Measured: `git cat-file -t` on the recorded object fails with "could not get
object info", and `nested/inner.txt` is absent from a plain `git add` snapshot.
Since 0.9.1 the tool replaces each such entry by the files under it (tracked,
untracked and ignored, subject to `--exclude` and payload mode), at any depth,
and prints which paths it did this for. The nested `.git` itself (history,
branches, stashes) is **not** captured: back it up separately if you need it.
Submodules take the same road. `--no-embedded-repos` restores the old
behaviour (the commit id only, with a warning), and a vault that already holds
a gitlink converts on its next run. The plumbing is measured below.

**The source's `.git` is not captured.** The vault records the *working tree*,
not the code repository's history — verified as 0 `.git/` entries in the
snapshot. Code history needs `git push` to the code remote; the vault is not a
substitute for it.

**A machine that snapshotted offline while another pushed cannot merge.** The
run stops and explains; `--reset-to-remote` drops the local-only snapshots and
continues from the remote chain. See
[section 6](#6-several-machines-one-vault).

**Large files cost one full copy per snapshot until the remote is compacted.**
Measured with a 32 MB incompressible file, 1 KB changed per snapshot:

| Snapshot | Remote size |
| :--- | ---: |
| 1 | 33 MB |
| 2 | 65 MB |
| 3 | 97 MB |
| after `gc` | **33 MB** |

The three near-identical revisions *do* delta-compress to essentially one copy —
but only once something consolidates them. With `seafile.autogc` on (the default) that happens
automatically: each qualifying push tail-merges the small packs (re-delta included),
so a 32 MB file snapshotted daily settles back toward one copy at a bounded download
cost per cycle. With it off — where the helper does not compact on push —
the same file grows the remote by 32 MB **per day** until someone runs `git-remote-seafile gc`. This is
the most important operational caveat for large files, and it is a compaction
problem, not a Git one.

**A snapshot is not a transaction.** The tree is read file by file. A file
changing mid-scan yields a snapshot that is internally consistent but not a
point-in-time image of the whole tree.

**Secrets are permanent.** The vault will happily record `.env`, keys and
credentials, and Git history is forever. Every run lists the credential-shaped
paths it captured, so the choice of remote is made with that in view. Push it
only to a library only you can read.

**File modes and symlinks** — Git for Windows does not reliably record the
executable bit (`core.fileMode`), so a restore elsewhere may not reproduce file
permissions. Symlinks are captured faithfully — a `120000` tree entry whose blob
is the link target, recorded even when the source checkout has
`core.symlinks=false` — and a restore on Linux or macOS rebuilds real links.
Windows is the exception: without `core.symlinks` enabled (Developer Mode, or an
elevated process) `git checkout` writes a regular text file containing the
target path instead. The restore's byte verification names that as a mismatch,
and because the check runs on every restore — not only under `--verify` — the
run stops there rather than reporting success over a tree of text files. The
vault is intact: enabling `core.symlinks` and restoring again reproduces the
links.

**Case-insensitive filesystems** — Windows and macOS can collapse `Foo.txt` and
`foo.txt`; Git will warn, but the snapshot may not round-trip both.

**Path length** — a deep source tree can exceed Windows' 260-character limit,
and a restore into a deeper destination is where it surfaces.

**File timestamps are recorded in a side file, not by git.** Git records
content, not mtime, so since 0.9.2 a run that pushes to a `seafile://` remote (or
that is given `--record-mtimes`) also writes `.seafile-payload/mtimes.json`, and
a restore sets each file's time from it. The times are the real ones at the
snapshot that changed anything; when nothing in the tree changed, the previous
record is reused, so touching a file without changing it neither changes the
record nor makes a new snapshot. Snapshots made before 0.9.2, or with
`--no-record-mtimes`, restore every file with the checkout time, so a build
system may rebuild everything once. Directory times are not recorded.

**Empty directories are not captured.** Git does not track them.

**Touching a file without changing its content produces no commit** — the vault
is content-addressed, not timestamped. If you want a heartbeat record of "a
backup ran", this tool will not give you one.

**There is no retention policy.** Every snapshot is kept forever; nothing
expires, and pruning would mean rewriting history, which the vault does not do.
Retention is a real feature of restic and Borg — see
[section 11](#11-how-this-compares-to-restic-borg-and-filesystem-snapshots).

### The nested-repository capture, measured for future work

*Shipped in 0.9.1; the measurements below are what it is built on. The
implementation uses `update-index --add` with paths, after dropping the
gitlink, which behaves the same as `--cacheinfo` here and needs no
`hash-object` pass of its own.*

Caveat 1 is not a Git limitation. It is a limitation of `git add`, and the
plumbing underneath `git add` does not have it. Measured, in this order:

```
$ git add -A -f .                  # what the tool does today
warning: adding embedded git repository: nested
$ git ls-files -s -- nested
160000 a75b13a8058b529ec3ccce95d963295dec54ca1a 0    nested   # a commit id, no contents

$ git add -f nested/inner.txt      # the obvious fix
fatal: Pathspec 'nested/inner.txt' is in submodule 'nested'

$ git update-index --force-remove nested
$ git update-index --add --cacheinfo 100644,"$sha",nested/inner.txt
$ git write-tree                   # -> nested/inner.txt IS in the tree
```

The rule is that `update-index --cacheinfo` cannot insert a path *underneath* an
existing gitlink — it fails with "appears as both a file and as a directory" —
so the gitlink entry has to be dropped first, and the nested repository's files
then inserted one at a time with `hash-object -w` + `update-index --cacheinfo`.

Probed end to end on a three-level tree: `nested/` is a repository, and
`nested/deep/` is another one inside it. The resulting snapshot restored **5 of
5 files** — including `nested/loose.txt`, which is untracked *inside* the nested
repository, and `nested/deep/deeper.txt`, two repositories down. Nothing in the
source was touched. It captures the nested repository's **working tree** —
tracked *and* untracked files — but not its history, and it does **not** produce
a submodule: a restore gives plain directories and files, which is right for a
backup and wrong for a checkout.

## 9. Large files and Git LFS

**The vault does not use Git LFS, and it actively disables it rather than merely
omitting it.** The vault's `info/attributes` carries `* -text -filter -ident`,
which outranks any `.gitattributes` in the source tree and unsets every
`filter=` attribute — including LFS. This is deliberate, and it exists because
of a real, measured failure: a source `.gitattributes` containing
`*.bin filter=lfs` made `git add` store a **130-byte LFS pointer** in place of
the 4 KB file, and the real bytes went nowhere, because the vault has no LFS
remote. The snapshot looked correct and was worthless — silent data loss in a
tool whose job is a full backup. Vaults written before the fix are upgraded
automatically on the next run, and the tool keeps the warning current for any
newly-added filter.

**How large binaries are handled instead:** as ordinary Git blobs, whole and
byte-exact. The transfer itself streams (the helper stages packs on disk and
streams uploads), so memory is not the constraint — **remote growth** is. See
the measured table in the caveats above: a changed large file costs a full new
blob per snapshot until `gc` runs.

**When that is the wrong trade:** for large, *frequently changing* binaries,
chunk-level dedup is the right answer, and Git does not do it. The
[comparison section](#11-how-this-compares-to-restic-borg-and-filesystem-snapshots)
says where to put that job instead. For files that are large but rarely change,
the vault is fine — an unchanged file costs nothing, and `gc` collapses the
occasional change to near one copy. The helper itself *does* ship a full Git LFS
custom transfer agent (`lfs-transfer`) for your regular `git push` workflow,
where LFS is appropriate; the vault is a separate, deliberately LFS-free
design.

## 10. Why a separate repository, not a branch or a subdirectory

Measured, and the reason is structural rather than stylistic.

**A branch in the source repository would publish the ignored files.** The
snapshot's tree is the *whole* working tree: `.env`, keys, credentials,
`node_modules`. In the same repository, one `git push --all` — or a mirror
remote whose refspec happens to cover `refs/heads/*` — sends all of it to the
code remote. A separate repository removes that failure mode entirely.

**But be precise about what a separate repository buys.** It separates the
*object stores*, which is what stops an ordinary `git push --all` from carrying
the snapshot along. It does **not**, by itself, keep the ignored files off the
code remote's *server*: point the vault's `--remote` at the same library the
code lives in and the snapshot is published there on purpose. The tool
*enforces* the deliberate part — `--remote` colliding with one of the source's
own remotes is refused, and `--allow-shared-remote` overrides it.

**A branch also entangles the source's object database.** You would have to
avoid the source's index (a temporary index via `GIT_INDEX_FILE`) — but the
snapshot's *objects* still land in the source repository. An interrupted run
leaves dangling objects there, and any later `git gc` in the source repo now has
to reason about the snapshot chain. The vault writes nothing to the source at
all: measured, the source's `.git` is **2125 KB before and 2125 KB after three
snapshots**, while the vault holds 4188 KB separately. A clone of the code
repository therefore carries 2125 KB no matter how many snapshots exist.

**It works on a directory that is not a repository.** Measured: a plain
directory containing `a.txt` and `.env` and no `.git` at all snapshotted fine.
A branch-based design has nowhere to put the branch.

**And the lifecycles are different.** The vault can be deleted, recreated, given
its own remote, its own permissions (a separate library with its own sharing) and its own
retention. The code repository's history is the thing you least want to touch.

**A subdirectory inside the source (`.vault/`, `snapshots/`) is worse, and the
tool refuses it.** Two independent reasons, either of which is disqualifying:

1. `git add -A -f .` runs with `--work-tree=<source>`, so a vault under the
   source is *inside its own snapshot*. Because it contains a `.git`, it would
   be captured like any nested repository (its files, since 0.9.1; a bare
   gitlink and a broken directory on restore before that) -- the vault, copied
   into itself on every run.
2. If the source sits in a synced Seafile library, the client would sync the
   vault's `.git` — precisely the corruption the project's Golden Rule exists to
   prevent.

A hidden directory is not a loophole; `-f` does not care about dot-prefixes.

## 11. How this compares to restic, Borg, and filesystem snapshots

| | Filesystem snapshot (ZFS/btrfs, VSS, Time Machine) | restic / Borg | The vault |
| :--- | :--- | :--- | :--- |
| Storage granularity | filesystem blocks, copy-on-write | content-defined chunks | whole-file blobs in a Git object DB |
| Cost of a 1-byte edit to a big file | ≈ zero | only the changed chunks | **a full new blob until `gc`** — measured: 8 MB file, 1-byte edit → **+8202 KB**, then `git-remote-seafile gc` → back to 8284 KB |
| Identical files stored once | yes (blocks) | yes (chunks, globally) | yes, exactly — measured: two identical 1 MB files share one blob SHA, 1099 KB of object store for 2 MB of content |
| Portable across filesystems and OSes | no | yes | yes |
| Reaches Seafile through the existing helper | no | only via WebDAV/rclone | **yes — plain `git push`** |
| History / diff semantics | a list of snapshots | a list of snapshots | **a real Git DAG**: `git log`, `git diff`, `git show`, `git bisect` |
| Restore granularity | whole dataset | whole file | whole file, and **the whole tree in one command** |
| Encryption at rest | filesystem-level | built in | none — encrypt the disk/volume, or use restic / Borg (Seafile encrypted libraries are not supported by this tool) |
| Retention / expiry | usually built in | built in | **none** |
| Integrity check | filesystem-level | content checksums | Git's own object hashing |
| Needs the tree to be a Git repo | no | no | no |

Two axes carry the whole comparison:

- **Cost model.** Filesystem snapshots are near-free per change (block COW);
  restic and Borg are cheap per change (chunk dedup); Git is cheap **on the
  wire** (delta compression inside a pack) but expensive **at rest** until `gc`
  runs. That is the measured 8 MB → +8202 KB → 8284 KB above.
- **What you get back.** Only the vault hands you a Git DAG, so "what changed
  between Tuesday and Friday, ignored files included" is a `git diff`, and a
  rollback is a checkout rather than a restore from a proprietary archive.

**restic and Borg are whole backup *systems*.** They own the storage format, the
deduplication, the encryption, the retention policy, and the scheduling. They
are the right answer to "I want a backup system."

**The vault is not competing with them.** It does one thing they cannot do here:
it puts the *ignored* files into the Git history that already flows to Seafile
through this project's helper, so that one `--restore` reconstitutes a working
tree and `git log`/`git diff` answer "what changed" over the whole tree —
ignored files included. That is a **complement to a Git remote, not a
replacement for a backup engine.**

The jobs to give to restic/Borg rather than build here:

| Missing capability | Hand it to | Why not here |
| :--- | :--- | :--- |
| Deduplication across *different* files, compression | restic / Borg | Git dedups blobs, not content; a copied file is stored twice. |
| Encryption at rest | restic / Borg, or an encrypted volume (Seafile encrypted libraries are **not supported**: the helper refuses them) | `git-crypt` works on the vault but encrypts per-repo, not per-file-set. |
| **Retention and expiry** | restic / Borg | The clearest gap: the vault keeps everything and cannot prune without rewriting history. |
| Efficient large, volatile binaries | restic / Borg via rclone, or keep them out of the vault | See the large-files section — the cost is real; a bounded automatic tail merge now exists (§5), but full delta-compaction remains a manual `gc`. |

The honest summary: **if you want a backup system, use restic pointed at a
Seafile-backed rclone target. If you want your ignored files to travel with the
repository you already push to Seafile, use the vault.** They can coexist, and
the vault is the smaller, sharper tool.

## 12. What Git gives us, and what stays unused

**Taken advantage of today:**

- **Content-addressed dedup.** Two identical files are one blob — measured:
  `big1.bin` and `big2.bin` share a blob SHA, and 2 MB of content costs 1099 KB
  of object store.
- **Packfile delta compression.** Near-identical revisions collapse — but only
  when `gc` runs (the 8 MB measurement in the caveats).
- **The commit DAG.** History, `diff`, and rollback for free.
- **Object integrity.** Every object is hash-verified when read, so a corrupt
  restore fails loudly instead of silently.
- **Plumbing**: `write-tree`, `commit-tree`, and `update-ref` with a
  compare-and-swap expected value — which is what makes the branch move safe.
- **`merge-base --is-ancestor`** — the fast-forward decision in the
  multi-machine reconciliation.
- **`info/attributes`** — the byte-exact override, at the top of Git's
  attribute-precedence chain.
- **`add -f`** — the lever that makes the `.gitignore`-rename trick unnecessary.
- **`fetch`** — reading the remote tip before building.
- **`git fsck`** — under `--restore --verify`; the byte-level check runs on
  every restore regardless.

**Available and unused — and two are *blocked* by the helper, worth knowing
before designing around them:**

| Git feature | Status |
| :--- | :--- |
| `git notes` (per-snapshot metadata) | **Blocked, deliberately.** The helper writes only `refs/heads/` and `refs/tags/` (`REF_NAMESPACES` in `refs.py`), so notes under `refs/notes/` could not be pushed to a `seafile://` remote. The allowlist is shared with the ref advert *and* with compaction's reachability mirroring, so widening it is not a one-line change — see [section 14](#14-hooks-resticborg-bridges-and-what-the-helper-should-not-grow). |
| Custom ref namespaces (`refs/snapshots/<host>`) | **Blocked** by the same allowlist. A per-machine chain is one `--branch snapshot-<host>` run away — an ordinary branch name, no custom namespace needed. |
| Tags (`refs/tags/*`) | **Allowed, unused.** The natural way to mark a milestone snapshot. |
| Shallow / partial clone | **Blocked by the helper.** `--restore` fetches the whole history, and `--depth` or `--filter=blob:none` would speed up restoring a large vault — but the helper does not implement Git's `depth`/`filter` options, so Git silently falls back to a full clone (USER_GUIDE §8.2). Making this useful needs helper support first. |
| `git count-objects` | **Unused.** The tool reports the tree's size but not the remote's growth. |
| `git bundle` | **Unused.** A portable single-file snapshot, if one is ever needed offline. |
| Commit signing, `git replace`, grafts | **Unused.** No need. |

## 13. Prior art

| Project | Relationship |
| :--- | :--- |
| **[`git-store-file`](https://blog.iany.me/2025/12/backup-ignored-files-with-git-remote-branch/)** ([source](https://github.com/doitian/dotfiles-public/blob/master/default/bin/git-store-file)) | Uses the *same plumbing* — a temporary index via `GIT_INDEX_FILE`, `git add -f`, `git commit-tree`, a direct push — but stores to a **branch in the same repository** (`origin/_store`) and for a **named file list**, not the whole tree. |
| **[`safe-gitignore`](https://github.com/sstraus/safe.gitignore)** | A post-commit hook that copies `# safe`-marked ignored files into a **separate private repository**, preserving directory structure, with optional `git-crypt` encryption. Closest in spirit to the vault, but allowlist-driven rather than whole-tree. |
| **Dotfiles bare-repository idiom** ([write-up](https://www.atlassian.com/git/tutorials/dotfiles)) | `git --git-dir=$HOME/.dotfiles --work-tree=$HOME` is the direct ancestor of the vault pattern. |
| **[`git-remote-dropbox`](https://github.com/anishathalye/git-remote-dropbox)** | The landmark demonstration of transparent Git packfile transport plus distributed locking over a cloud storage API — the model `git-remote-seafile` itself follows. |
| **[git-annex](https://git-annex.branchable.com/)**, **[BorgBackup](https://www.borgbackup.org/)**, **[restic](https://restic.net/)** | Occupy the same "back up everything, incrementally" space with purpose-built formats. They deduplicate and compress better than Git and handle large binaries properly; they do not give you Git's history, diff and merge semantics. See [section 11](#11-how-this-compares-to-restic-borg-and-filesystem-snapshots). |

Reference documentation: [`gitignore(5)`](https://git-scm.com/docs/gitignore) —
why renaming the file cannot track anything;
[`gitattributes(5)`](https://git-scm.com/docs/gitattributes) — the attribute
precedence that `info/attributes` sits at the top of;
[Seafile: excluding files](https://help.seafile.com/syncing_client/excluding_files/)
— the `seafile-ignore.txt` semantics the helper's Trap 2 depends on.

Notably, none of the prior art mentions the `core.autocrlf` trap. It is specific
to Git for Windows, and it only surfaces when you actually run a restore.

**Placement: this project, not its own repository.** The tool shells out to
plain `git`; its only tie to the package is one *best-effort, optional* import
(`discover_local_synced_libraries` for the synced-folder guard), and it still
works standalone when the package is absent. The audience overlap is near-total,
`tools/seafile_doctor.py` is the precedent, and a separate repository means its
own CI matrix, docs guards and release process for very little isolation. It
should also **not** be promoted to a `git-remote-seafile` subcommand: that CLI
is about *the remote*, and a local-filesystem backup command dilutes it. The
trigger to reconsider is concrete: an *unconditional* import from
`git_remote_seafile`, or a user interface.

## 14. Hooks, restic/Borg bridges, and what the helper should not grow

**Hooks: no, because Git already has the one that matters.** The helper is a
*transport* invoked by `git push` with the user's credentials. A hook mechanism
inside it would be a code-execution surface and a new config surface — and
`git push` already runs `.git/hooks/pre-push` before any remote helper is
invoked. The one genuine gap, "run something *after* a successful push", belongs
in a wrapper script or a scheduler, not in the transport.

**restic and Borg: no bridge, because the helper is the wrong layer.** restic
and Borg want a *dumb blob store*; a remote helper speaks `list`/`fetch`/`push`
and moves packfiles and refs. Bridging them is a second product — a Seafile
backend for restic — and that work already has an owner: **rclone ships a
native Seafile backend** (`backend/seafile/`) that speaks the `/api2` REST API
directly, no WebDAV involved:

```
restic -r rclone:seafile:backups/restic-repo init
restic -r rclone:seafile:backups/restic-repo backup ~/code/myproject
```

Two real caveats on that route: rclone's Seafile backend authenticates with
**username and password** (plus 2FA) and explicitly does not support the Library
API Token this helper prefers; and it is **not fast** — restic's many small
pack/index objects mean many per-file API calls. WebDAV (`ENABLE_SEAFDAV = true`,
set by the server administrator) adds only a `davfs2`-style mount route for
Borg, and changes nothing for the helper or the vault.

**What the helper could usefully add — and should not:**

| Candidate | Verdict |
| :--- | :--- |
| A `webdav-url` subcommand printing the SeafDAV URL | **Plausible, deliberately not built.** Only useful where the admin enabled WebDAV, which the helper cannot discover. |
| Widening `REF_NAMESPACES` to `refs/notes/*` or custom namespaces | **No.** The allowlist is not only a push guard — it also decides what compaction mirrors for reachability, and a ref the compactor cannot see makes its objects look like garbage for `repack -a -d` to delete permanently. (Since v0.8.0, compaction *refuses* when such refs exist, rather than deleting.) Widening a least-privilege boundary three subsystems share, to enable metadata the commit message already carries, is a bad trade. |

## 15. Future work and deliberate non-goals

**The short list, in order:**

1. **Shallow/partial restore** — `--restore` fetches the whole history; for a
   vault with years of snapshots, `--depth` or `--filter=blob:none` would help.
   This needs the *helper* to implement Git's `depth`/`filter` options first:
   today it answers `unsupported`, and Git silently does a full clone, so the
   flag is accepted and ignored (section 12, USER_GUIDE §8.2).

*Done since this list was written: nested-repository capture (0.9.1) and payload
mode (0.9.0, [section 15.1](#151-payload-mode-large-files-by-hash-behind---payload-min-size)).
The helper already tail-merges small packs after a push (`seafile.autogc`, on by
default, deferred with a notice above the download budget), so the snapshot tool
needs no `gc` step of its own.*

**Open questions, deliberately unresolved:**

- **Per-machine vault vs one shared vault.** Sharing is safe (section 6), but it
  makes the remote a single linear log of several machines' trees, and a
  divergence stops the run. Per-machine vaults (`...-vault-<host>`) avoid
  divergence entirely at the cost of N remotes to restore from. The shared model
  ships as the default; `--branch` is the escape hatch.
- **Retry depth.** One automatic retry covers a two-machine race. A fleet of
  machines pushing on the same schedule could need more, or a backoff.
- **Snapshot tagging.** `refs/tags/*` is allowed by the helper and unused — the
  natural way to mark a milestone snapshot if wanted.

**Deliberate non-goals** — do not add these:

- **No dedup, compression, encryption, or retention in code.** restic and Borg
  own those problems; encrypt the volume the vault lives on (Seafile encrypted libraries are refused by the helper).
- **No merge of diverged chains.** Refusing is correct.
- **Keep `--exclude` opt-in.** "All files" is the point.
- **Keep the secrets report a warning, not a refusal.** A backup that refuses to
  back up is a backup that gets switched off.

### 15.1 Payload mode: large files by hash (`--payload-min-size`, on by default)

**Status.** Shipped in v0.9.0, **on by default** (`auto`) for runs that push to a
`seafile://` remote; `--no-payload` turns it off. The contract the
implementation is held to -- requirements, decisions, failure model and an
adversarial review -- is [PAYLOAD.md](PAYLOAD.md); this section keeps the
measurements and the reasoning for the layout. Files at or above the
given size are hashed, stored on the server under their SHA-256, and listed in a
manifest the vault commit carries; `--restore` downloads and verifies every one.
The selection rule (`auto`), a retention helper (`--payload-gc`), a remembered
setting, a force-rehash switch and named, browsable *views* of a snapshot are
implemented too. **Not implemented:** a payload location in a different library
from the vault.

**The problem it answers.** A vault carries every file *through git*: each blob
is hashed, written as a loose object, packed, uploaded as a pack. For a tree
that is 99.8% large immutable data (measured: 118 GiB of a 131 GiB tree in 352
files) that is pure overhead -- a second local copy of the payload (loose
objects, then temporary packs), a staging pass of 12-20 minutes, a restore that
needs the objects *and* the checked-out files on disk at once, and the 1 GiB
pack cap and 2 GiB request ceiling worked around with chunking. Git's
strengths -- history of small text, metadata, integrity of a tree -- are not
used on that data at all.

**The design.** Split the tree at a size threshold. Small files go through the
vault exactly as before. Files at or above the threshold are *payload*: they
never enter git. Each is stream-hashed (SHA-256, no local copy) and uploaded to

    <vault path>/payload/objects/<first two hex digits>/<sha256>

inside the vault's own remote directory -- one URL names both, and the helper
ignores what it does not know (measured on the real server: push, clone,
`doctor`, `gc --tail` and a full `gc` all behaved and left the directory
alone). The vault commit carries `.seafile-payload/manifest.json`: path, mode,
size, SHA-256 and mtime of every payload file, plus a snapshot id derived from
them. Git keeps what git is good at -- the file list, the checksums, the history
of both -- and Seafile keeps what it is good at: big files and shared blocks.

A run hashes (reusing the previous hash where size and mtime are unchanged and
not racily close to the last commit), lists only the fan-out directories the
hashes fall in, uploads the objects that are missing -- largest first, three at
a time -- verifies them with a listing, and only then commits and pushes the
vault. **Nothing is ever overwritten or deleted by a run.**

**Why by hash, and not a mirror of the tree.** The first implementation mirrored
the tree into `current/` and made each snapshot a server-side copy of it. That
worked and was measured end to end (below), but a named mirror has costs that a
hash-named store does not:

- an object is never replaced, so a **snapshot is a manifest and costs nothing
  to keep** -- no per-snapshot quota charge, no history entry, no copy
  operation;
- a **rename or a duplicate uploads nothing**: the same bytes are already there
  under the same name (measured by test: rename = 0 objects; two identical files
  = one object);
- **no name rules apply**: what the server holds is a hex string, so NFC
  normalisation, `"` becoming `%22` and the 255-byte limit (all measured below)
  cannot touch a payload file, and a path of any length restores;
- **concurrent writers cannot conflict**: two machines storing the same object
  write the same content under the same name, where two writers of one named
  path made `(SFConflict ...)` copies (measured);
- **retention is ours**: the tool can compute which objects no snapshot
  references, and deleting them frees quota at once.

The price is that the server's own file view is unreadable -- `ab/ab3f...` --
and that only the tool (or a script that reads the manifest) can rebuild a
tree. The answer to "I want to look" is a **view**, planned: one directory of
real names built on demand by server-side copy and rename of objects (about two
calls per file, no data transferred -- the copy was measured at 20-30 ms for
1,500 entries), deleted when no longer wanted. A view is a convenience, never
the source of truth.

**Restore** clones the vault, reads the manifest, downloads each object to its
real path, verifies size and SHA-256, and sets mode and mtime. Restoring an old
snapshot works for as long as its objects exist. Disk need is the payload once,
not twice.

**What of DESIGN.md section 2 applies?** That section explains why a `.git`
*directory* cannot live in a synced library: the desktop client's index keeps
whole-second mtimes (a same-second, same-size edit looks unchanged), Windows
holds mandatory locks on files the client is hashing, event buffers overflow
under bursts, and two machines produce conflict copies. All of those are
properties of **the desktop client watching a working tree**. The payload step
involves no desktop client: it reads the source with ordinary file reads and
writes with the REST API, and the source and the vault are refused if they sit
in a synced folder. The *same class* of problem does exist in the tool's own
change detection, and is handled the same way git handles it:

- a cached hash is trusted only when size and mtime match **and** the mtime is
  older than the last commit by a margin (git's "racily clean" rule), so a
  same-size edit inside the timestamp granularity is rehashed, not missed;
- a file is compared with its recorded size and mtime both before and after it
  is uploaded, so bytes that changed after hashing cannot sit under another
  content's name; the run stops and says which file;
- a file another process holds open exclusively (a Windows database or GIS
  project) stops the run naming it, never a quiet omission;
- there is no switch yet to force a full rehash; a cache the racy rule cannot
  vouch for (a file whose mtime was restored by a copy tool) is the case that
  would want one.

**Seafile's own file history.** Library history is not the snapshot mechanism
and must not be relied on as one: it is per library commit, retention-limited,
and subject to the server's GC. What it does do:

- *Replaced files:* because objects are never overwritten, file history has
  nothing to keep -- a hash name has one version. (In the mirror design every
  replacement was a history version.)
- *Deleted files:* a deleted object stays in history until the library's history
  expires and GC runs. Measured: deleting a folder freed **no** disk for 1 h
  30 min; setting the library to *no history* and running the server's online GC
  for that library freed 33.8 GiB within seconds. Quota is different -- it
  dropped the moment the folder was deleted.
- *A safety net:* with a short history (days, not "none"), an object the tool
  deleted by mistake -- or that a person did -- can be restored from Seafile's
  trash. That is worth the little disk it costs, and is the recommended setting
  for the payload library.
- *One entry per operation:* each upload or delete is one library commit
  (measured: 8 entries for 2 uploads, 2 copies and 4 `mkdir`s). A first run of
  ~350 files writes ~350 entries.

**Measured on the real server (Seafile CE 13.0.28 in Docker on OpenZFS,
2026-10-13).** Kept here because they decided the design:

- a single upload ran at 32 MiB/s; three of *different* files at once reached
  53 MiB/s aggregate, and through the tool six 128 MiB files took 30 s with one
  worker, 19.7 s with three, 19.6 s with five (the default is three);
- a 1.1 GiB file went through the chunked `Content-Range` path and restored
  byte-identical; a restore ran at about 85 MiB/s;
- a server-side directory copy of four 256 MiB files, and of a 1,500-entry tree
  three levels deep, took 20-30 ms and added **0 bytes** to the block store
  (`du -sb` of `storage/blocks`, sampled every 10 s: +1,073,741,824 for the
  upload, +0 for the copy, +1,073,741,824 for a replacement, +0 for the
  delete); a server-side rename took about 30 ms;
- quota and library size count a copy **at full size** (a 200 MiB file and its
  copy read as 400 MiB), and a desktop client that syncs a library holding
  copies downloads each one in full (3 GiB for three 1 GiB files, 43 s) -- so a
  payload library is best one nobody syncs whole;
- what the server does to names: stored **NFC-normalised**; a `"` becomes
  `%22`; a name over **255 bytes of UTF-8** is refused (255 ASCII and 85 Thai
  characters pass, 256 and 86 fail); `: * ? < > |`, a trailing space or dot,
  reserved Windows names, emoji, Thai and a 4,212-character path were stored as
  given. These rules apply to *views*, not to payload objects.

**Not measured:** GC on a payload library that also holds data worth keeping
(the test library held only scratch data, so the 33.8 GiB is an upper bound);
a view of a large snapshot (the per-file copy-and-rename cost); the desktop
client's behaviour when a person edits files in a library the tool also writes.

**Failure model.** Objects are written before the vault commit that names them,
and the commit is pushed after, so a vault never references an object that was
not verified present. An interrupted run leaves objects that no snapshot names;
they are harmless, reused by the next run, and found by the retention helper.
The server cannot report a content SHA-256, so upload correctness rests on
size checks; **restore always verifies SHA-256**, which is where corruption
would surface.

**Why not the alternatives.**

- *A mirror of the tree plus a server-side copy per snapshot* -- built and
  measured, and kept as the reference for what a view does. Rejected as the
  default for the reasons above: per-snapshot quota and history cost, renames
  re-uploaded, name rules, in-place replacement.
- *Git LFS (the helper ships a custom transfer agent).* It keeps a second copy
  under `.git/lfs`, depends on `.gitattributes` filters (which the vault
  disables to stay byte-exact) and tracks by pattern, so it does not reach
  ignored files -- the reason the snapshot tool exists.
- *The desktop client or `seaf-cli`.* The sync protocol (commit and fs objects
  plus content-defined blocks, client-side dedup, resumable block transfer) is a
  different protocol from the REST API the helper speaks, is not documented as a
  stable interface, and -- as far as is known -- `seaf-cli` is packaged for
  Linux and drives a daemon. Syncing `data/` with the desktop client remains
  the right answer when no consistent code-plus-data snapshot is needed.
- *Library history as the snapshot.* Retention-limited and GC-dependent.

**Cost for the 131 GiB case.** First run: hashing at disk speed, then 118 GiB at
32-53 MiB/s (about 40-60 minutes), no local duplicate, no pack cap. A run with
nothing changed is a directory walk and one listing per fan-out directory.

**Next.** Views; a retention helper (list the objects no manifest in the
vault's history references, and delete them on request); a remembered flag;
optionally a payload library separate from the vault's, for quota or client-sync
reasons.

## 16. Maintenance

This file is kept current **as part of the work**, not by a robot: when a change
alters the tool, its tests, or the thinking recorded here, this file is updated
in the same pass.

Two backstops exist:

- **Mechanical claims** — config keys, environment variables, subcommands, the
  `seafile-ignore.txt` name — are guarded by `tests/test_docs_consistency.py`,
  which scans this file because it is listed in `_DOC_NAMES`. Drift fails the
  suite. The same test pins the *corrected* claims (per-machine chains, the
  synced-guard narrative, the nested-repo warning, the shared-remote check) as
  absent from every shipped document, so a superseded statement cannot silently
  return.
- **The suite-shape guard** (`tests/test_suite_shape.py`) keeps the snapshot
  tests from ever running twice under the parallel runner.

The prose and the measurements are **not** guarded, and deliberately so: prose
drift can survive a green suite for releases at a time, so the numbers here are
marked as measured-on-this-machine rather than presented as invariants. Re-read
them when the tool changes.
