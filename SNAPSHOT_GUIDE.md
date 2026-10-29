# git seafile-snapshot: User's Guide

How to take, restore and verify whole-tree snapshots of a working directory, ignored files included, with `git seafile-snapshot`. This was chapter 15 of the [User Guide](USER_GUIDE.md); the design, the measurements and the reasoning behind it are in [SNAPSHOT.md](SNAPSHOT.md), the large-file mode is specified in [PAYLOAD.md](PAYLOAD.md), and what changed between releases is in [UPGRADING.md](UPGRADING.md).

## Contents

- [Overview](#overview)
- [1. The vault](#1-the-vault)
- [2. Capturing a snapshot](#2-capturing-a-snapshot)
  - [2.1 What is *not* excluded by default](#21-what-is-not-excluded-by-default)
- [3. Byte-exactness, and why it needs enforcing](#3-byte-exactness-and-why-it-needs-enforcing)
- [4. Restoring and verifying](#4-restoring-and-verifying)
- [5. What the tool refuses to do](#5-what-the-tool-refuses-to-do)
- [6. Several machines, one vault](#6-several-machines-one-vault)
- [7. What it costs](#7-what-it-costs)
- [8. Keeping the vault small](#8-keeping-the-vault-small)
- [9. Large payloads: payload mode](#9-large-payloads-payload-mode)

---

## Overview

The remote helper (see the [User Guide](USER_GUIDE.md)) moves *commits*. That leaves one gap the helper cannot close by construction: **a file your `.gitignore` excludes never enters a commit, so no remote helper can ever carry it.** `.env`, a local database, an editor's state directory, build output you would rather not rebuild — none of it reaches Seafile through a push, however many remotes you add.

`git seafile-snapshot` closes that gap. It captures the working tree **as it lies on disk** — ignored files and all — into versioned, byte-exact snapshots stored in a Seafile library, over the same transport the helper already uses. It is its own command (installed with the package; `git-remote-seafile snapshot` is an alias), and it needs no credentials of its own: it reaches Seafile the way `git push` does.

> [!NOTE]
> **Installed with the package.** `pip install git-remote-seafile` provides the helper (`git-remote-seafile`) *and* the snapshot tool as `git-seafile-snapshot` — so `git seafile-snapshot` works anywhere git finds it on `PATH` — and as `git-remote-seafile snapshot`. From a source checkout, `git seafile-snapshot` runs the same program (the file is a thin wrapper around `git_remote_seafile/snapshot_tool.py`, so existing commands and scheduled jobs keep working). (`git seafile-snapshot --help` is intercepted by git, which looks for a man page: ask with `-h`, or run `git-seafile-snapshot --help`.) The diagnostics script `tools/seafile_doctor.py` (README, *Diagnostics for the Seafile desktop client*) still ships with the source and the sdist only.

> [!NOTE]
> **Git 2.25+ if you use exclusions.** The tool stages the surviving paths through
> `--pathspec-from-file`, which arrived in Git 2.25. Without `--exclude` or
> `--default-excludes` it stages with a plain `git add`, where the package's
> documented Git 2.20 floor ([User Guide §5](USER_GUIDE.md#5-installation)) is enough; with them you need 2.25. It does
> **not** fall back to a plain `add` on older Git, and that is deliberate:
> staging first and dropping the excluded paths from the index afterwards would
> read exactly the files the exclude list exists to keep out of the vault.

> [!IMPORTANT]
> **Keep the Golden Rule ([User Guide §2](USER_GUIDE.md#2-the-golden-rule-working-tree-placement)).** The tool does not write to a snapshot's *source* (the one exception is a probe file at the root of a source on a network share, created and removed within a moment to read the share's clock), but **neither the source nor the vault may live in a Seafile-synced folder**: the sync client rewrites files under whatever lives there, and the run refuses it unless you pass `--allow-synced` ([section 5](#5-what-the-tool-refuses-to-do)). The snapshot's *destination* is a Seafile library, and like every other remote of this tool (see the [User Guide](USER_GUIDE.md)) it should be an unsynced one.

## 1. The vault

Pointed at a source directory, the tool maintains a **separate repository — the vault — whose `--work-tree` is that source**:

```bash
git --git-dir=<vault>/.git --work-tree=<source> …
```

The source stays read-only and untouched: nothing is committed into it, no branch is created in it, and its own index and refs are never consulted. The vault does the commiting, and the vault is what you push to Seafile.

Why a separate repository rather than a branch or a subdirectory in the source? Because either alternative would *publish* the ignored files into the source's own history — and, in the subdirectory case, would place snapshot data inside the tree it is trying to snapshot. The reasoning is worked through in `SNAPSHOT.md` §11.

## 2. Capturing a snapshot

Every option has a default, so the first run is the only one that needs an argument — and even that can be just the directory:

```bash
# The whole thing: snapshot C:/code/myproject into C:/code/myproject-vault
# and push it to Seafile, inferring the destination from the source repo
cd C:/code/myproject
git seafile-snapshot

# From elsewhere, naming the tree. The vault defaults to '<source>-vault'.
git seafile-snapshot C:/code/myproject

# What that would do, without writing anything
git seafile-snapshot C:/code/myproject --dry-run

# The same, said explicitly
git seafile-snapshot --source C:/code/myproject \
    --vault C:/code/myproject-vault \
    --remote seafile://code/myproject-vault
```

**The first run teaches the vault what you chose, and later runs remember it.** The destination, branch and exclusions are written into the vault's own config, so a scheduled job can be nothing more than the bare command — and an explicit flag always wins and is written back for next time.

Defaults are resolved most-specific-first:

| Priority | Source | Example |
| :--- | :--- | :--- |
| 1 | The flag you passed | `--remote seafile://backups/app` |
| 2 | What the vault remembered | the remote from the last run |
| 3 | The source repository | `seafile://code/app` → `seafile://code/app-vault` |
| 4 | The current directory | source = `.` (inside a git repository: the repository root), vault = `<source>-vault` |

`--no-push` overrides the remembered or derived remote *for that run* — it pushes nothing (neither the vault nor the code repo) and leaves the vault's memory alone, so a one-off local run does not unhook a scheduled job. `--dry-run` previews the run — the vault is initialised if missing and the snapshot is built into its index, so the diff is real — without moving the branch, pushing, or touching the remembered settings.

| Flag | Effect |
| :--- | :--- |
| `SOURCE` (positional) or `--source <dir>` | The tree to snapshot. Default: the current directory, or the repository root when that is inside a git repository. A subfolder of a repository is refused: a snapshot is of the whole repository. |
| `--vault <dir>` | Where the vault repository lives. Default: `<source>-vault` beside the source. |
| `--remote <url>` | The Seafile destination. Default: remembered, else derived from the source's own `seafile://` remote. |
| `--branch <branch>` | The vault branch holding the snapshots. Default: `snapshot` — one shared chain across machines ([section 6](#6-several-machines-one-vault)). `--branch snapshot-<host>` gives per-machine chains instead. |
| `--exclude <glob>` | Repeatable. Skip matching paths, e.g. `--exclude node_modules`. |
| `--default-excludes` / `--no-default-excludes` | Add the recommended junk exclusions ([section 2.1](#21-what-is-not-excluded-by-default)) and every Python virtualenv found (a directory holding `pyvenv.cfg`, whatever it is called), or explicitly not. A recommended pattern is kept out of the list for a run when the source's own git repository tracks a file it matches (`build` would otherwise drop a committed `tools/build/make.py`); the run says so, and an explicit `--exclude` is always honoured. |
| `--no-embedded-repos` | Record a nested git repository only as its commit id (the 0.9.0 behaviour). By default its files are captured, its `.git` is not. |
| `--record-mtimes` / `--no-record-mtimes` | Record (or not) the modification time of git-stored files in `.seafile-payload/mtimes.json`. On by default for a run that pushes to a `seafile://` remote; an explicit choice is remembered by the vault. |
| `--payload-report-days N` | Payload mode: print the report-only `--payload-gc` summary when none was printed in N days (default 7, `0` off). |
| `--message <text>` | Override the snapshot commit message. |
| `--code-remote <name>` | Also `git push <name>` in the source repo first; the name is validated up front. |
| `--no-push` | This run pushes nothing and changes no remembered setting. |
| `--dry-run` | Report what would be committed and pushed, and do neither. |
| `--reset-to-remote` | Adopt the remote chain when the vault has diverged and the two have no common tip, dropping the local-only snapshots ([section 6](#6-several-machines-one-vault)). Never taken implicitly — this is the one way a snapshot can be lost. |
| `--allow-shared-remote` | Override the shared-remote guard ([section 5](#5-what-the-tool-refuses-to-do)) deliberately. |
| `--allow-synced` | Proceed even when the source, the vault or a restore destination is inside a Seafile-synced folder ([section 5](#5-what-the-tool-refuses-to-do)). The sync client rewrites files under Git, so this is for a library you have made inert (auto-sync off, or `seafile-ignore.txt` covering it). |
| `--payload-min-size [<size>\|auto]` | **Payload mode.** Selected files are not put through git: each is SHA-256-hashed and uploaded **by hash** to `<path>/payload/objects/` inside the vault's remote directory, and the commit records a manifest. With no value, or `auto`: files of 64 MiB or more, plus files of 4 MiB or more that do not compress. With a size (`64M`, `1G`): files of at least that size. A restore downloads and verifies them. Objects are never overwritten or deleted by a run, so a snapshot costs nothing to keep and a rename uploads nothing. Needs a remote; cannot be combined with `--no-push`; `0` turns it off. A vault that has used it needs the flag on every run. Put SOURCE first, or write `--payload-min-size=auto`. See PAYLOAD.md. |
| `--payload-auto-ratio <R>\|measure` | With the `auto` payload rule, a file of 4–64 MiB goes to payload when a sample of it keeps at least *R* of its size under a fast deflate (default `0.9`: only data that barely compresses). A lower value (e.g. `0.5`) sends moderately compressible data to payload instead of deflating it through git, which is faster when deflate is slow next to your link and gives up git's delta compression between snapshots. `0 < R <= 1`; remembered by a complete real run. A **new vault** with a gigabyte or more of such data measures your deflate speed and your link once on its first real run (four 16 MiB probe files, uploaded and deleted) and remembers a cut-off between `0.5` and `0.9`; an existing vault, or one whose remote already holds payload objects, keeps `0.9` until you set it, and `measure` measures again. A vault that moves files in or out of git by changing it does so once, on the next run. Every run (and `--dry-run`) with the `auto` rule prints a `placement:` line (how many files and bytes go to payload and stay in git) and, when a lower cut-off would plainly be faster on your machine and link, a `Hint:` line saying how many files it would move and the time it would save; the tip changes nothing. |
| `--payload-skip-changing` | With payload mode, a payload file that changes while it is being read or uploaded (a database, a mailbox, a VM disk) is **left out of this snapshot**, named in a `Warning:`, instead of failing the run and snapshotting nothing. The previous snapshot keeps its last version and the next quiet run captures the file again. Not remembered; the default is an error. |
| `--payload-rehash` | With payload mode, ignore the hash cache for this run and hash every payload file again (a tree whose timestamps were restored by a copy tool, or a periodic audit). A real run **remembers** `--payload-min-size` in the vault, so a later bare run keeps using it (`0` is remembered too). |
| `--payload-gc` | **Payload retention.** Report the stored objects no snapshot references (in any ref of `--remote`, or the local `--vault`, which must be a vault). With `--payload-delete` it marks them, and a later run deletes those that stayed unreferenced for `--payload-grace-days` (default 7, minimum 1). Refuses to act when it finds no manifest, an unreadable one, or a Seafile conflict copy of a ref. |
| `--payload-verify [<n>\|all]` | **Payload audit.** Download *n* randomly chosen objects (default 20, or `all`) of the newest snapshot of `--remote` and check each against its SHA-256; exit non-zero and name the damaged ones. The server cannot report a content hash, so this is the only way to find an object damaged after it was stored. About 85 MiB/s: all of a 111 GB payload takes roughly 25 minutes. |
| `--status` | **Is my backup current?** Reports, without staging, hashing or uploading anything: the last snapshot's time and size, what changed in the source since it (a size-and-mtime comparison against the records that snapshot wrote — the manifest for payload files, `mtimes.json` for the git-stored ones), the settings the next run would use, and whether the vault is locked. The comparison is a heuristic and the report says which way it errs: a change that keeps the stats its record holds hides (size and mtime for a payload file and for a git-stored one whose snapshot recorded sizes; mtime alone for an older snapshot's git-stored files), and a file whose time changed without its content shows as changed though the next run would find nothing new. The library's quota is not reported, and it never contacts the remote: its `pushed` line is what this vault recorded at its last push (`yes`, `NO -- N snapshot(s) were taken after the last push`, or `not recorded` for a vault that has not pushed since it began keeping the record), not a look at the server. It does not combine with `--no-push` or `--dry-run`. `--json` carries the counts in `result.status`. |
| `--restore-drill [<n>\|all]` | **Restore drill.** Prove the newest snapshot of `--remote` restores: clone it into a temporary directory, check every file byte-for-byte, run `git fsck`, download *n* randomly chosen payload files (default 10) and check their SHA-256, then delete the directory. It needs the disk for the whole vault; set `TMP` / `TMPDIR` to use a bigger volume. A failed drill keeps the directory and names it. `--ref snapshot~3` drills an older snapshot. |
| `--json` | One JSON document on stdout (`schema`, `mode`, `status` of `ok`/`warnings`/`failed`, `exit_code`, `seconds`, `warnings`, `error`, `result`); the human text moves to stderr. A usage error exits 2 before any document is written. |
| `--exit-warnings` | Exit `3` instead of `0` when the run completed but printed a `Warning:`. Off by default, so a job that works today does not start failing. |
| `--payload-view [<ref>]` | **Payload view.** Build a browsable directory of real file names for a snapshot of `--remote` (a branch, or `snapshot~2`; default `snapshot`) at `<path>-view/<name>` in the same library, by server-side copy and rename of the stored objects: no data moves and no extra disk is used, but **quota and library size count each copy at its full size**, so a view of a large snapshot needs that much headroom (the run warns when the account's quota is short; delete the view when you are done with it). A convenience, not a restore: no mode or mtime, and the server's name rules apply (names it cannot hold are reported). `--payload-view-name <name>` picks the directory (default: the snapshot id); `--payload-view-replace` rebuilds an existing one, so `current` can be refreshed. Write the ref as `--payload-view=<ref>`. |
| `--payload-keep <n>` | With `--payload-gc`, count only the newest *n* snapshots of each branch and tag as using objects, so older snapshots expire: their exclusive objects are removed by `--payload-delete` after the grace period and those snapshots can no longer be restored. Default: keep all. |
| `--payload-delete` | With `--payload-gc`, act (mark, then delete after the grace period); default: report only. |
| `--payload-grace-days <n>` | With `--payload-gc`, how long an object must stay unreferenced before it is deleted. |
| `--restore` | Restore a snapshot instead of taking one; requires `--remote` and `--into` ([section 4](#4-restoring-and-verifying)). |
| `--into <dir>` | Destination directory for `--restore`. |
| `--ref <rev>` | Which snapshot to restore. Default: the vault branch tip. Only meaningful with `--restore` — passing it without one is an error, not a no-op. |
| `--verify` | With `--restore`, also run `git fsck` over the clone. |

**Scheduled runs: exit codes and `--json`.** `0` is done (a warning may have been printed), `1` failed (`Error:` on stderr), `2` the command line was wrong, and `3` — only with `--exit-warnings` — completed but printed a `Warning:`. With `--json` stdout is exactly one JSON line (`status` is `ok`, `warnings` or `failed`), so a job can alert with `jq -e '.status == "ok"'` and attach `.warnings` or `.error` to the notification. Run `--status` nightly (it reads local state only) and `--restore-drill` now and then (monthly, not nightly: it downloads the vault once) so the first restore is not the day you need it. Details: SNAPSHOT.md, *Exit codes and `--json`* and *Restore drill*.

**An unchanged tree commits nothing.** The tool compares the new tree against the previous snapshot and stops if they match, so a scheduled job — hourly, say — costs a directory walk and a push that transfers nothing. This is what makes it safe to run far more often than you would run a backup.

### 2.1 What is *not* excluded by default

Nothing, unless you say so. **On your first run at a terminal** (no vault yet, no `--exclude`, `--default-excludes` or `--no-default-excludes` given, not `--json` or `--exit-warnings`) the tool lists the reproducible directories it found, each with its size, and asks `Leave them out of this and later snapshots? [Y/n]`. A yes is the same as `--default-excludes` (committed source is kept, virtualenvs are found by `pyvenv.cfg`, the vault remembers it); a no captures everything. It never asks again, and an unattended run never asks. Without that answer the tool captures the whole tree, including `node_modules/`, `dist/` and `__pycache__/`, and then **tells you** what it noticed:

```
Warning: the snapshot includes reproducible build output that is usually not worth
backing up.  Leave it out next time with --default-excludes, or explicitly:
  --exclude 'build' --exclude 'dist' --exclude 'node_modules' --exclude '*.pyc'
```

This is deliberate. A snapshot is a backup, and the one failure that cannot be forgiven is a backup that quietly omitted the file that mattered. `node_modules` is reproducible; `.env` is not; the tool cannot tell them apart, so it does not try. `--default-excludes` adds exactly the set above, on top of anything you passed to `--exclude`.

## 3. Byte-exactness, and why it needs enforcing

A snapshot tool that quietly changes your files is worse than none. Git will rewrite line endings on checkout if `core.autocrlf` says so, and on Windows the trap is sharp: **Git for Windows sets `core.autocrlf=true` in its *system* config**, so `git config --global core.autocrlf` reports nothing while the effective value is `true`, and a fresh `git init` inherits it.

The vault therefore pins, for itself:

```
core.autocrlf=false
core.safecrlf=false
core.longpaths=true
core.quotepath=false
core.fsmonitor=false
core.untrackedCache=false
```

and writes this into its own `info/attributes`:

```
* -text -filter -ident -working-tree-encoding
```

`info/attributes` **outranks** any `.gitattributes` copied from the source, which is what makes the pin hold. Three details worth knowing, because all were learned the hard way:

- **An attributes override must name *every* attribute class it means to neutralise.** `* -text` alone does **not** disable `filter`: a source `.gitattributes` carrying `filter=lfs` still applies, and the vault silently stored a **130-byte LFS pointer** while the real bytes went nowhere.
- **Paths over 260 characters and non-ASCII names are pinned.** `core.longpaths=true` allows deep directory structures on Windows, while `core.quotepath=false` ensures multi-byte international filenames (such as Thai, CJK, or accented Latin) remain legible in snapshot diffs and status output instead of being C-quoted as octal escapes (e.g. `\340\270...`). (If your source repository's own `git status` shows octal escapes, set `git config core.quotepath false` or `git config --global core.quotepath false`).
- **A plain `git clone` of the vault is lossy.** `info/attributes` is not carried by a clone; the source's `.gitattributes` is. Restoring by cloning the vault by hand can therefore hand you the wrong bytes. That is why `--restore` exists.

## 4. Restoring and verifying

```bash
# Rebuild a tree into a target directory, verifying byte-exactness
git seafile-snapshot --restore \
    --remote seafile://backups/myproject \
    --into ~/restored

# Any revision works -- e.g. roll back three snapshots
git seafile-snapshot --restore \
    --remote seafile://backups/myproject --into ~/restored --ref snapshot~3

# Additionally run `git fsck` over the vault
git seafile-snapshot --restore \
    --remote seafile://backups/myproject --into ~/restored --verify
```

`--restore` takes `--remote` (the vault to restore from) and `--into` (a new,
empty directory); `--source` and `--vault` do not apply — the tree comes from
the remote, and the guard refuses flags that would be ignored. `--restore`
clones `--no-checkout`, re-applies the settings above, then checks out — and
verifies the result on **every** run with

```bash
git hash-object --no-filters -- <path>
```

which hashes the bytes as they lie on disk. That is the only one of Git's three hash checks that cannot be fooled:

| Check | What it hashes | Fooled by a mangled checkout? |
| :--- | :--- | :--- |
| `git status --porcelain` | the file *through* the filters | Yes — compares filtered bytes to filtered bytes |
| `git hash-object` | the file after the clean filter | Yes — the filter can normalise the difference away |
| `git hash-object --no-filters` | the raw bytes on disk | **No** — and it holds for *any* checkout, including one you made by hand |

> [!NOTE]
> **A symlink restored onto Windows without `core.symlinks` fails that check, and the restore aborts.** Git checks a `120000` entry out as a plain text file whose contents are the target path, so the verification finds no link to read and names the path as a mismatch. Because the check runs on **every** restore — not only under `--verify` — the run stops there rather than quietly handing back a tree of files where the source had links. The blobs are intact: enabling `core.symlinks` (Developer Mode, or an elevated process) and restoring again reproduces the links. Symlinks are verified by reading the link itself, never by `hash-object`, which on POSIX would follow the link and hash its target.

## 5. What the tool refuses to do

- **It will not stage a tree the vault's volume cannot hold.** Before the first byte is staged the tool estimates the new object storage (tree size scaled by the sampled compressibility, minus what the vault already holds) and refuses when that exceeds the free space less 1 GiB; `--allow-low-disk` overrides. A 130 GiB tree on a volume with 60 GiB free is refused with the numbers rather than filling the disk; narrow the tree with `--exclude` or point `--vault` at a bigger volume.
- **It will not push the vault to one of the source's own code remotes.** A whole-tree backup legitimately contains `.env`, keys and build artefacts; pouring that into a shared code library is almost never what anyone wants. If the vault's destination collides with a URL in the source's `remote.*.url` / `remote.*.pushurl`, the push is refused and the colliding remote named. `--allow-shared-remote` overrides it.
- **It reports credential-shaped paths when that set is new.** The first run says so, and a later run says so again only when the set of paths changes (a new key file, a `.env` that was not there); the vault remembers a digest of what it last warned about. Said on every run it would be noise, and it would make `--exit-warnings` exit 3 for ever on any tree with a `.env`. A dry run never uses the warning up. This is a *warning, not a refusal* — a full backup *should* capture `.env`; the point is to find out before the first push, not after.
- **It will not run on a stale lock.** A `seafile-snapshot.lock` left behind by a killed process is detected by checking whether the owning PID is still alive -- and, since 0.9.2, whether it is still the *same process* (the lock records the process's start time, because Windows recycles pids and a crashed nightly run's number soon belongs to something long-lived) -- and reclaimed rather than blocking every future run.
- **It will not leave a hole for a directory it cannot read.** `git add` only prints a warning for a directory it cannot open and carries on, so everything under it would be missing from the snapshot with exit 0. The run stops and names it; make it readable, or `--exclude` it on purpose.
- **A vault belongs to one source tree.** The vault records the source directory it first saw and refuses a run that points a *different* tree at it — mixing two trees into one linear chain of snapshots is not something a later reader could undo. Give each tree its own vault.
- **Neither the source nor the vault may live in a Seafile-synced folder.** Checked at startup, before anything is written. The sync client rewrites files underneath whatever lives in a synced folder and competes with Git for every path — the exact problem this tool exists to avoid.
- **`--code-remote <name>` must name a real remote** of the source. Without this check a typo would fail *after* the snapshot was built, leaving a commit for a run the user reads as failed.
- **Flags that would otherwise be ignored are errors.** `--ref` without `--restore` is the sharp one: a run meant to restore an *old* snapshot would quietly take a *new* one instead. `--no-push` together with `--remote` is contradictory and also refused.

## 6. Several machines, one vault

A vault is a Git repository, so it can be pushed to from more than one machine — but each machine needs to branch from the **remote tip**, not from its own local branch. Getting this wrong is subtle: the second machine's push is rejected `(fetch first)` **and its local branch still moves**, so the first run looks like it worked while every later run fails. The tool fetches the remote tip and bases on it, retries once by re-parenting if it loses a race, and rolls its local branch back to what the remote holds if a push fails.

By default all machines append to **one** shared chain on `refs/heads/snapshot`; the hostname in each snapshot's commit message is how `git log` says which machine saw which tree (`SNAPSHOT.md` §6 documents the reading). If you would rather keep per-machine chains, pass an explicit `--branch snapshot-<hostname>` — supported, but never the default, and the divergence handling above is what makes the shared chain safe.

## 7. What it costs

Measured on real pushes, not estimated (`SNAPSHOT.md` §8):

| Situation | Transferred |
| :--- | :--- |
| A 486 KB tree with one 4 KB and one 10 KB file changed | 31 KB |
| Nothing changed | **0 KB — and no commit** |
| A 1-byte edit to an 8 MB file | +8202 KB, until `git-remote-seafile gc` |
| A 32 MB file snapshotted three times | remote grows 33 → 65 → 97 MB |

The last row is the one to watch: a large file that changes daily adds its own size to the Seafile library every day. `seafile.autogc` (on by default) tail-merges those small packs between full gcs and bounds the growth; if you turned it off, run `git-remote-seafile gc` ([User Guide §10](USER_GUIDE.md#10-remote-packfile-compaction--auto-gc-git-remote-seafile-gc)) on the *vault's* remote periodically — or keep large build artefacts out with `--default-excludes` ([section 2.1](#21-what-is-not-excluded-by-default)).

## 8. Keeping the vault small

Every snapshot commit writes new objects into the vault's local object store, and like any repository they accumulate in tiers: loose objects (one file per object — what a fresh clone or first commits produce), incremental packs, and full packs after consolidation. The tool maintains the first two tiers itself; the remote tier is the helper's job ([User Guide §10](USER_GUIDE.md#10-remote-packfile-compaction--auto-gc-git-remote-seafile-gc)).

- **Automatic incremental repack.** Before each push that has a destination, the tool measures its own loose tier, and once it passes 256 MiB it runs `git repack -d` + prune — consolidating *loose objects only*, never touching existing packs. (An object-*count* trigger already exists in git itself: the `git fetch` this tool runs launches `gc --auto` once about 6,700 loose objects exist.) It is best-effort: a failure is reported and the push proceeds anyway. `--no-repack` skips it for one run.
- **Manual full reset.** `--full-repack` runs `git repack -ad` + prune over the whole store, collapsing everything into one pack. Use it after large deletions or when the local pack count has grown high; nothing runs it automatically.
- **Report.** `--maintain` changes nothing: loose/object/pack counts, object-store size against free disk space, and the storage profile (below).

**Large raw payloads get a storage profile, decided once from a sample.** Two git costs scale with raw bytes and buy nothing on data that neither compresses nor deltifies: staging deflates every blob (42 MB/s on incompressible data, 131 MB/s with `core.compression=0`), and `pack-objects` — the helper's push and the vault's own background `gc --auto` — runs a delta search over every blob below `core.bigFileThreshold` (default 512 MiB), which measured 8–10 MB/s regardless of the compression level. On a 20 GB+ tree that is the difference between minutes and a timeout.

Before the first staging the tool samples the tree (a few chunks from each of up to 32 of the largest files — about 24 MiB of reads however large the tree is). When the tree is at least 2 GiB, big files (≥ 1 MiB) hold at least half of it, and the sample keeps ≥ 50 % of its size under a fast deflate (compressing on one core costs more time than the bytes it saves cost on the wire, so only data that shrinks to under half is left on the slow path), the vault is switched to the **raw profile**: `core.compression=0` and `core.bigFileThreshold=50m` in the vault's own config, with one line saying so and how to undo it. Nothing about the *content* changes — only storage encoding and packing strategy, which are safe to flip at any time.

- The decision is remembered (`snapshot.profile`) and not repeated. A vault that began small and grew past 2 GiB is sampled again once per doubling of the tree, before the run stages (so the run that grew the tree already uses the verdict).
- A vault where you have set `core.compression` yourself is never second-guessed.
- `--no-object-compression` applies the raw profile unconditionally.
- `--dry-run` takes the sample and reports what a real run would do, without writing it.

**Long steps show a heartbeat and are killed only when nothing is happening.** Staging, repacking and cloning print "still staging the tree ... 4m12s (9.0 GiB written to objects; up to about 28.6 GiB expected, at most 9m30s left)" once a minute (the expectation is an upper bound from the free-space estimate, and is left out when there is none or the step has passed it). A step is killed only if its object directory stops growing for 30 minutes (`SEAFILE_SNAPSHOT_STALL_LIMIT` raises that, in seconds) — a *sliding* limit, so a slow-but-real run survives and a dead disk does not. The push step is never killed for silence (its upload is silent by construction, and a dead link fails on the helper's own HTTP timeout), and children inherit `SEAFILE_GIT_TIMEOUT` = the same horizon unless you set one. While payload objects upload, a `progress:` line once a minute says how much is done, the average rate and about how long is left (`progress: 41.2 GiB of 292.0 GiB uploaded (14%), 52 MiB/s, about 1h20m left`).

**The vault's remote is a remote.** Every push adds one small packfile there, so the [User Guide §10](USER_GUIDE.md#10-remote-packfile-compaction--auto-gc-git-remote-seafile-gc) threshold warning and tail merge apply to it exactly as to any other — and SNAPSHOT.md §8's large-files caveat is the reason to watch it.

The full design — the alternatives that do not work, the nested-repository analysis (the measured capture shipped in 0.9.1), the own-review findings, and a comparison with restic and Borg — is in [SNAPSHOT.md](SNAPSHOT.md).

## 9. Large payloads: payload mode

> [!WARNING]
> **Upgrading to 0.9.0: payload mode is on by default.** A vault that holds files of 64 MiB or more (or 4 MiB or more that do not compress) in git moves them out of git on its **next** snapshot: one full hash-and-upload pass, the large files stored twice on the server (the old blobs stay in the vault's history, the new objects sit under `payload/`), and they show as deleted in `git diff`. Snapshots taken before the switch restore as before; later ones need the payload, **and 0.8.8 cannot restore them completely** -- it has no manifest support and restores the tree without the large files. A plain `git clone` of a payload vault does not contain the large files either. A vault with no such file is unaffected. To keep a vault exactly as it was, run once with `--no-payload`; it is remembered.

A vault carries every file *through git*: each is hashed, written as a loose object,
packed, uploaded. For a tree that is mostly large, immutable, already-compressed
data -- rasters, archives, arrays, video -- that is pure overhead: a second local
copy, a long staging pass, and a pack cap. **Payload mode** keeps such files out of
git. Each is hashed (SHA-256) and stored on the server under that hash, inside the
vault's own remote directory (`<path>/payload/objects/`); the snapshot records a
manifest (path, mode, size, hash, mtime) instead. Measured on a real server: a 131 GiB
tree (633 payload files, 124.6 GB) snapshotted in 38 minutes and restored
byte-exact.

```bash
# Take a snapshot. Payload mode is on by default ('auto'): files of 64 MiB or more,
# plus files of 4 MiB or more that do not compress, go to the server by hash. A size
# works too (--payload-min-size=100M); put the source first, or use the = form.
git seafile-snapshot C:/code/flood --default-excludes

# The vault remembers the setting; a bare run keeps using it.
git seafile-snapshot C:/code/flood

# Turn it off (remembered): every file goes through git, as in 0.8.8.
git seafile-snapshot C:/code/flood --no-payload

# Restore is the same command as always; it downloads and verifies the payload.
git seafile-snapshot --restore --remote seafile://code/flood-vault --into D:/restored --verify
```

What to know:

- **Each run says where its files went, and the cut-off is tunable.** With the `auto`
  rule, files of 4 MiB or more are decided by a sample of how well they deflate, and the
  run prints a `placement:` line (files and bytes to payload and in git) and, when a
  lower cut-off would plainly be faster on your machine and link, a `Hint:` line. The
  default cut-off is `0.9` (only data that barely compresses leaves git);
  `--payload-auto-ratio R` changes it, and is remembered. A **new** vault with a
  gigabyte or more of compressible 4--64 MiB files measures your deflate speed and
  link once on its first real run (four 16 MiB probe files, uploaded and deleted;
  a crash can leave one, named `probe-*` directly under `payload/objects/`, which is safe
  to delete) and remembers a cut-off between `0.5` and `0.9`. An existing vault is
  never re-tuned on its own; `--payload-auto-ratio measure` measures again.
- **It is on by default, and remembered.** With a `seafile://` remote and a push, a
  run uses `auto` unless the vault remembered something else. A tree with no file
  that big is snapshotted exactly as before: no manifest, no server directory. A run
  that cannot use it (no remote, a remote that is not `seafile://`, `--no-push`)
  simply does not. A complete real run stores the setting in the vault
  (`--no-payload`, or `--payload-min-size 0`, turns it off and is remembered too); a
  preview never writes it. A vault whose last snapshot used payload mode refuses a
  run that would put its large files back into git by accident. When the default is
  about to move an *existing* vault's large files out of git, the run prints a
  `NOTICE` saying how many files and bytes, **before** it hashes or uploads anything
  (a preview says "would move"); interrupt the run and use `--no-payload` to keep
  the vault as it was.
- **A plain `git clone` of a payload vault does not contain the large files.** It
  holds the manifest and `.seafile-payload/README.txt`; get the tree back with
  `--restore`.
- **The store only grows** until you run `--payload-gc` (and `--payload-keep`); a run
  that uploaded objects prints how many and how large the store is.
- **How it meets git.** The vault is still an ordinary repository; payload files are simply never staged.
  - *Inside git* a payload snapshot is the small files plus two ordinary blobs, `.seafile-payload/manifest.json` (one line of JSON, sorted keys) and `README.txt`. `git log`, `git show`, `git fsck`, `git gc`, `git repack` and `git clone` work as on any vault; a manifest change shows in `git diff` as one long line, so read it with `git show snapshot:.seafile-payload/manifest.json | python -m json.tool`.
  - *Outside git* the large files are plain files in `<vault path>/payload/objects/` on the server. Git never sees them, so `git gc` and the helper's `gc`, `gc --tail` and `doctor` neither read nor delete them (the helper ignores a foreign `payload/` directory); only `--payload-gc` removes an object, and only one no snapshot references.
  - *A checkout is not a restore.* `git clone`, `git checkout` or `git switch` gives you the manifest, not the files; `--restore` (or `--payload-view` for a browsable copy made on the server) is what produces them, and verifies each by SHA-256.
  - *History rewrites need care.* The manifests are what keep objects alive: `git push --force`, `git filter-repo` or deleting a snapshot branch or tag make older manifests unreachable, and a later `--payload-gc --payload-delete` will then treat their objects as unused.
- **A snapshot costs nothing to keep.** Objects are not overwritten (the one exception
  is an object found with the wrong size, which is replaced), so a snapshot is a
  manifest; a renamed or duplicated file uploads nothing.
- **A payload vault cannot take a snapshot without pushing.** The objects must be on the
  server before a commit names them, so `--no-push` is refused on a vault that uses
  payload mode (and `--no-push --no-payload` stages the large files into git, which is
  the cost payload mode exists to avoid). `--dry-run` previews without pushing.
- **`--payload-verify` audits the newest snapshot only.** Objects that only older or
  tagged snapshots reference are found damaged at restore time, not before.
- **Tags are pins, but only if they are on the remote.** `git tag` in the vault is local;
  push it (`git -C <vault> push <remote> <tag>`) or a retention run on another machine
  does not see it. A pinned snapshot's own objects are kept whatever `--payload-keep`
  says.
- **The manifest is one line of JSON** (so no text filter can alter it): `git diff` and
  `git show` of it are not readable. Use `--payload-view` for names, or `jq` on the blob.
- **A machine on 0.8.8 or earlier restores a payload vault's git tree only**, without the
  large files and without a warning (it does not know manifests). Restore with 0.9.0 or
  later.
- **A weekly report, never a deletion.** After a run that pushes, the tool prints the
  report-only `--payload-gc` summary (stored, in use, reclaimable) when none was printed in
  the last 7 days; `--payload-report-days N` changes the period and `0` turns it off. A
  report that fails is a note, not a failed run, and nothing is deleted without
  `--payload-gc --payload-delete`.
- **Uploads are parallel and verified.** `--payload-jobs N` (default 3). Each object
  goes up under a temporary name, is hashed as it is sent, and is renamed to its hash
  only if the bytes match; the whole set is re-checked just before the vault is pushed.
- **Look at it, or audit it.** `--payload-view` builds a browsable directory of real
  names by server-side copy (no data moves); `--payload-verify [N|all]` downloads and
  hashes a sample, which is the only way to find an object damaged after it was stored.
- **Retention is explicit.** `--payload-gc` reports what no snapshot references;
  `--payload-delete` marks it and a later run deletes it after `--payload-grace-days`;
  `--payload-keep N` lets older snapshots expire (they then cannot be restored). Run it
  from one machine, and set the library's history to keep a few days as a safety net.
  Deleting needs `--vault <your long-lived vault>` (or `--payload-no-vault`, if the
  remote really is the only copy), because a snapshot that was never pushed would
  otherwise look unused. If the remote's history was rewritten since the last delete
  run -- a force push, a deleted branch or tag -- or `--payload-keep` would expire
  snapshots, the run refuses until you pass `--payload-accept-loss`. **A tag is a
  pin:** `git tag keep-2026-q3 <snapshot>` keeps that snapshot's objects whatever
  happens to the branch.
- **What it checks before it starts.** A restore that would not fit the free disk is
  refused before the first download (`--allow-low-disk` overrides); an upload larger
  than your remaining quota, or into a library this machine syncs, is warned about
  first; a fresh vault is refused when the remote's snapshots use another threshold
  than the default, or a different auto cut-off (repeat that flag, or pass
  `--payload-min-size auto` / `--payload-auto-ratio <theirs>` to switch).
  Every snapshot commit records the tool's version in a `Git-Remote-Seafile:`
  trailer, and a run warns when it builds on a snapshot written by a newer tool.
  The store line and the gc report say when `--payload-verify` last ran.
- **What it does not do:** no encryption, no delta between versions of one file (a
  changed file is a whole new object; the server shares unchanged blocks), and a
  file's name must be restorable on the platform you restore to (Windows names it
  cannot hold are skipped and reported, git-stored files and payload files alike;
  the rest of the snapshot is restored and verified).

The specification, the requirements and the review that shaped it are
[PAYLOAD.md](PAYLOAD.md); the measurements are in
[SNAPSHOT.md section 15.1](SNAPSHOT.md).
