# SNAPSHOT.md — full-tree backup to Seafile, `.gitignore`d files included

> **Status: shipped in v0.8.0.** The implementation is `tools/seafile_snapshot.py`;
> the tests are `tests/test_snapshot_tool.py` (92 cases across 22 classes), and
> the tool is documented in `USER_GUIDE.md` §15, `DESIGN.md` §7.13 and
> `CHANGELOG.md`'s 0.8.0 entry. Sections written *before* the ship are kept as
> design history — where one has been overtaken by the shipped tool, a note says
> so. Everything below was measured on this machine unless a claim is explicitly
> marked otherwise.
>
> This file is kept current **as part of the work** — see
> [Maintenance](#15-maintenance). Defects found in review are in
> [section 16](#16-own-review-issues-found); the helper/hooks question is in
> [section 17](#17-should-the-helper-grow-hooks-or-support-restic-and-borg).

---

## 1. The gap

`git push` transfers **commits**. Anything matched by `.gitignore` never enters
a commit, so no Git remote helper — `git-remote-seafile` included — can carry
it. A repository backup and a *file* backup are therefore two different jobs,
and `git push` only does the first.

## 2. The approach that does not work: renaming `.gitignore`

The obvious idea is: rename `.gitignore` aside, `git add .`, push, rename it
back. It fails on both counts, and the first failure is the one people miss.

**It is unnecessary.** Renaming tracks *nothing*. Measured:

```
$ mv .gitignore .gitignore.bak
$ git ls-files            # still 2 files -- unchanged
$ git status --porcelain  # the ignored files merely appear as "??"
```

The ignored files only become *visible*; they still have to be added. And
`git add -f` stages ignored files directly, with no rename at all — so the
rename is pure ceremony.

**It cannot be made atomic.** It mutates user-visible state — the working tree
*and* the repository's own index — for the duration of the push. A crash, a
Ctrl-C, or a concurrent editor/IDE leaves a repository with no `.gitignore` and
a fully-staged index. No `try`/`finally` fixes that, because the intermediate
state is on disk and observable.

## 3. The design: a vault

A **vault** is a separate Git repository whose `--work-tree` points at the
source directory:

```
source dir  --(--work-tree)-->  vault repo  --git push-->  seafile://code/<name>-vault
(only ever read)                 own .git, own index
```

**Two words, two different things — and they are not interchangeable.** A
**vault** is the *repository*: one directory, one `.git`, one remote, something
you create, point at and eventually delete. A **snapshot** is a single *commit*
inside it. "The vault holds forty snapshots" is a sentence; "the vault is a
snapshot" is not. The tool keeps the same split — `--vault <path>` names the
repository, while `--message`, `--ref`, `--dry-run` and `--restore` are all about
snapshots.

So neither word is being retired, because neither is a synonym for the other.
What *was* wrong is that earlier drafts of this file used them loosely, which is
what made the design read as if it had two names. The script is called
`seafile_snapshot.py` because that is the verb the user wants ("take a
snapshot"), and the flag is `--vault` because that is the object the user points
at. See [section 16](#16-own-review-issues-found) for the full answer.

Snapshots are ordinary commits built in the vault's **own** index. The source
is never written to: not `.gitignore`, not `.git`, not the index. The code
repository stays small, so `git clone` on a second machine stays fast — the
snapshot never enters its history.

**Atomicity, concretely:**

| Property | How |
| :--- | :--- |
| Interrupted run | Leaves at worst a stale vault index; the source is untouched. The next run overwrites it. |
| Concurrent snapshots | A vault-local `seafile-snapshot.lock` (created `O_EXCL`). |
| Torn branch update | `git update-ref` with an **expected old value** — a racing run loses loudly rather than clobbering. |
| Push | A single ref update, guarded by the helper's distributed lease lock. |
| Failed push | The local branch is **rolled back** to what the remote actually has, so the vault never claims a snapshot the remote never took. |
| Idempotence | An unchanged tree produces **no commit at all**. |

## 4. Usage

Every option has a default, so a bare run is a complete run. The first run is the
only one that needs an argument, and even that can be just a directory:

```bash
# Snapshot C:/code/myproject into C:/code/myproject-vault and push it to
# Seafile, inferring the destination from the source repo's own remote
cd C:/code/myproject
python tools/seafile_snapshot.py

# From elsewhere; the vault defaults to '<source>-vault'
python tools/seafile_snapshot.py C:/code/myproject

# see what would be captured, write nothing
python tools/seafile_snapshot.py C:/code/myproject --dry-run

# snapshot + push, and push the code repo first in the same command
python tools/seafile_snapshot.py --source C:/code/myproject \
    --vault C:/code/myproject-vault \
    --code-remote origin \
    --remote seafile://code/myproject-vault \
    --default-excludes
```

### Defaults, and where they come from

Resolved most specific first:

1. **the flag you passed** — always wins;
2. **what the vault remembered** from its last run — written into the vault's own
   config under `snapshot.*` at the end of every non-dry run;
3. **the source repository** — a single `seafile://` remote names the vault's
   destination (`seafile://code/app` → `seafile://code/app-vault`); two such
   remotes are ambiguous and derive nothing;
4. **the current directory** for the source, `'<source>-vault'` for the vault.

So a scheduled job is the bare command, and an explicit flag is written back for
next time. `--no-push` overrides the remembered or derived remote *for that
run* — it pushes nothing (the vault and the code repo alike) and leaves the
vault's memory alone, so a one-off local run does not unhook a scheduled job;
`--dry-run` previews the run without moving the branch, pushing, or touching
the remembered settings.

The vault is the bottom of the chain *and* the place the chain is stored, which is
what makes the second run argument-free. It also means the vault remembers which
source it belongs to: a run pointing a **different** tree at an existing vault is
refused, because one vault is one linear chain of snapshots of one tree and a
later reader cannot undo a mix.

### Exclusions are never silent

**There is no default exclude list.** "All files" is the point of a snapshot, and
the failure that cannot be forgiven is a backup that quietly omitted the file that
mattered — `node_modules` is reproducible, `.env` is not, and the tool cannot tell
them apart. Instead it captures everything and then names the reproducible bulk it
noticed, with the flags to drop it:

```
WARNING: the snapshot includes reproducible build output that is usually not worth
backing up.  Leave it out next time with --default-excludes, or explicitly:
  --exclude 'build' --exclude 'dist' --exclude 'node_modules' --exclude '*.pyc'
```

`--default-excludes` adds the recommended set (`node_modules`, `venv`, `.venv`,
`__pycache__`, `*.pyc`, `dist`, `build`) on top of anything passed to `--exclude`.

> **A pattern with no slash matches that name at *any* depth.** `--exclude
> node_modules` drops `packages/api/node_modules/` as well as the top-level one —
> what the name suggests, and what a monorepo needs. Wildcards still work across
> separators (`*.pyc` matches `a/b/c.pyc`), and a trailing slash is ignored.
> **Quote a wildcard** so your own shell does not expand it first.
>
> This needs saying because the tool used to hand the pattern to `git rm` as a
> pathspec, where a *literal* path is anchored at the tree root. Measured on a
> tree holding `node_modules/x.js`, `pkg/node_modules/y.js` and
> `a/b/node_modules/z.js`, `--exclude node_modules` then removed **one of the
> three**. Matching now happens in the tool — see
> [section 16](#16-own-review-issues-found) — so the plain name behaves.
>
> `*dist*` is the trap in the other direction: it also matches `distance.txt`.
> Prefer the bare name, or `*dist/*` if a wildcard is genuinely wanted.

### Refusals, not guesses

The failures this tool can cause are quiet ones, so wherever a guess would be
silent it refuses instead (see [section 16](#16-own-review-issues-found)):

- **Neither the source nor the vault may live in a Seafile-synced folder.**
  Checked at startup via `safety.discover_local_synced_libraries()`, before
  anything is written. The sync client rewrites files underneath it and competes
  with git for every path — the problem this tool exists to avoid.
- **A vault belongs to one source** — records it, and refuses a different one.
- **`--code-remote <name>` must name a real remote** of the source, so a typo
  fails before the snapshot instead of after it.
- **`--no-push` with `--remote`** is contradictory and refused.
- **Flags that would be ignored are errors.** `--ref` without `--restore` is the
  sharp one: a run meant to restore an *old* snapshot would otherwise quietly take
  a *new* one. `--into`, `--verify`, `--message`, `--code-remote`, `--exclude`,
  `--dry-run` and the rest are rejected against `--restore` and vice versa.

## 5. Updates

Each run appends one commit to a linear chain on `refs/heads/snapshot`. Git is
content-addressed, so only changed blobs are uploaded. Measured on a 486 KB,
62-file tree:

| Scenario | Pushed |
| :--- | ---: |
| First snapshot | 639 KB remote in total |
| One 4 KB tracked file + one 10 KB ignored file changed | **31 KB** |
| Nothing changed | **0 KB — and no commit is created** |

The vault's index keeps a stat cache, so unchanged files are not re-hashed
either. Because the vault is ordinary Git history, "roll back to last Tuesday"
is `git show`, `git diff`, or a checkout of an older commit — not a restore
from a proprietary archive format.

### What a run costs in subprocesses

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

The saving is why the test module's parallel time fell from 167 s to 128 s:
nearly half of what the suite spent was process start-up, not Git. There is a
floor here — a snapshot inherently needs `fetch`, `add`, `write-tree`,
`rev-parse`, `ls-tree`, `ls-files` and the push, and none of those can be
avoided without re-implementing Git's index. Fourteen calls is close to it.

> **Watch the remote's packfile count.** The helper warns on every push and
> `seafile.autogc` is off by default, so a frequent snapshot regime needs
> `git-remote-seafile gc <url>` run deliberately (or `seafile.autogc` turned
> on). A remote that is never compacted grows without bound.
>
> For small text trees this is a slow leak. For large binaries it is not — a
> 32 MB file grows the remote by 32 MB per snapshot until it is compacted. See
> caveat 6 in [section 8](#8-caveats).

## 6. Several machines, one vault

A vault can be shared. Each machine fetches the remote tip **before** building,
so its snapshot lands on top of the shared chain rather than beside it.

**This was broken, and the failure mode was permanent.** Measured with two
sources and one remote, before the fix:

```
machine A: snapshot + push              -> remote 53ab8d3
machine B: own fresh vault, same remote -> ! [rejected] snapshot -> snapshot (fetch first)
                                           B's local branch advanced to 3d1f0d9 anyway
machine B: second run                   -> "no changes since the last snapshot"
                                           then the same rejected push, forever
```

Two distinct defects, both now fixed and covered by tests:

1. **The base was the local branch, never the remote.** A second machine built
   on its own unrelated genesis, so its push could never fast-forward.
2. **A rejected push still moved the local branch.** The rejection therefore
   never healed: every later run rebuilt on the advanced branch and was refused
   again. This is the more dangerous of the two, because the first run *looks*
   like it worked — the vault reports "moved refs/heads/snapshot".

After the fix, machine B appends to machine A's commit and the chain is linear.
The reconciliation is:

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
`A -> rival -> B`, three commits, no loss.

**Divergence is refused rather than resolved silently.** Merging two snapshot
chains is meaningless — they are two different working trees, not two edits to
one file — and quietly dropping a machine's snapshots is the kind of silent
data loss this project treats as a bug. The refusal names the remedy.

### Which machine took which snapshot

A shared chain is one log written by several machines, and the commit *author*
is the same person on all of them — the identity is inherited from the source
repository, which is the same repository cloned everywhere. So the default
snapshot message carries the **hostname**:

```
snapshot 2026-10-10T21:45:42 on workstation-a
snapshot 2026-10-10T21:45:46 on laptop-b
```

**Found while probing this, and fixed:** the vault was taking its identity from
the *global* git config rather than from the source repository. `git config
--get` reads through to the global file, so the check "does the vault have an
identity of its own?" was answered *yes* by the global one, and the source
repository was never consulted. The existing inheritance test only passed
because it cleared the global config — green for the wrong reason. Inheritance
now reads each repository's **own** config only, and there is a test that keeps
a global identity present.

## 7. Restore

```bash
python tools/seafile_snapshot.py --restore \
    --remote seafile://code/myproject-vault \
    --into C:/restore/myproject

# roll back: any revision works
... --ref snapshot~3
```

The snapshot holds the **whole** tree, tracked files included, so this single
command reconstitutes the working directory. Clone the *code* repository
separately only if you want its branches and history — see caveat 4 below.

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
the byte-exact settings to the clone, and only then checks out.

Verified byte-identical after restore: LF text, CRLF text, a 256-byte binary
blob, a file with no trailing newline, `.gitattributes`, `.gitignore`, and
ignored files under `node_modules/`. The naive clone differed — which is what
makes the contrast real rather than assumed.

## 8. Caveats

Fifteen caveats, and taken as a flat list they look alarming. They are long
because they were **unsorted**: the list mixed three different kinds of
statement, and only one of the three is a to-do list.

| Kind | Caveats | What it means |
| :--- | :--- | :--- |
| **Fixed already** — the tool handles it, and a test holds it | 2, 5, and the byte-exactness and multi-machine work in [section 6](#6-several-machines-one-vault) and [section 7](#7-restore) | Described here only so the *failure* is on record. |
| **Fixable in the tool, not yet done** — the mechanism exists, and where marked it has been measured | 1 (nested repos), 6 (compaction) | Real work, but no new ideas needed. Two of the original four were fixed since this table was written: the `--exclude` pathspec in [section 16](#16-own-review-issues-found), and the synced-library guard (caveat 9) in v0.8.0 — see the note under caveat 9. |
| **Inherent** — a property of Git, the filesystem or Windows, not of this design | 3, 4, 7, 8, 10, 11, 12, 13 | restic and Borg have most of these too. |
| **Deliberate policy** — a choice, not a limitation | 14, 15 | Two of these are worth *keeping*: "a snapshot is not a transaction" and "there is no retention policy" are the honest limits of a Git-backed backup. |

So the honest answer to "are the caveats fixable?" is: **two of the fifteen are
already fixed, three are fixable and unfixed, and the remaining ten should not be
fixed** — they should be *stated*, which is what this section is for. The
unnumbered `--exclude` pathspec defect that sat alongside them is fixed as well;
[section 16](#16-own-review-issues-found) records it and the five other defects
found in the same pass.

### Verified on this machine

1. **Nested Git repositories are recorded as gitlinks — their contents are
   NOT captured.** `git add` records a directory containing its own `.git` as a
   single commit reference (mode `160000`), and that commit is not copied into
   the vault. Measured: `git cat-file -t` on the recorded object fails with
   "could not get object info", and `nested/inner.txt` is absent from the
   snapshot. A restore would produce a broken directory. The tool prints a
   prominent warning naming each such path — **back those up separately**.
   Submodules are the same mechanism and are warned about identically.
   **This is fixable, and the fix has been measured** — see
   [the nested-repository fix](#the-nested-repository-fix-measured) at the end
   of this section. Until it is implemented, the warning is the whole of the
   protection.
2. **A source `filter=` attribute is neutralised — without that, Git LFS
   silently destroys the backup.** This was a real bug, found by probing, not a
   theoretical worry. A source `.gitattributes` containing `*.bin filter=lfs`
   made `git add` store a **130-byte LFS pointer** in place of the 4 KB file,
   and the real bytes went nowhere, because the vault has no LFS remote. The
   snapshot looked correct and was worthless. `info/attributes` now carries
   `* -text -filter -ident`; `git check-attr` confirms `filter: unset`.
   Verified byte-identical afterwards, both with real `git-lfs` 3.7.1 and with
   a deliberately content-mangling custom filter. Vaults written before the fix
   are upgraded automatically on the next run.
   **The generalisable lesson: an attributes override must name every attribute
   class you need to neutralise**, not just the one you were thinking about.
3. **Empty directories are not captured.** Git does not track them.
4. **The source's `.git` is not captured.** The vault records the *working
   tree*, not the code repository's history — verified as 0 `.git/` entries in
   the snapshot. Code history needs `git push` to the code remote; the vault is
   not a substitute for it.
5. **A machine that snapshot offline while another pushed cannot merge.** The
   run stops and explains; `--reset-to-remote` drops the local-only snapshots
   and continues from the remote chain. See [section 6](#6-several-machines-one-vault).
6. **Large files cost one full copy per snapshot until the remote is
   compacted.** Measured with a 32 MB incompressible file, 1 KB changed per
   snapshot:

   | Snapshot | Remote size |
   | :--- | ---: |
   | 1 | 33 MB |
   | 2 | 65 MB |
   | 3 | 97 MB |
   | after `gc --aggressive` | **33 MB** |

   The three near-identical revisions *do* delta-compress to essentially one
   copy — but only once something runs `gc`. Since `seafile.autogc` is off by
   default and the helper does not compact on push, a 32 MB file snapshotted
   daily grows the remote by 32 MB **per day** until someone runs
   `git-remote-seafile gc`. This is the most important operational caveat for
   large files, and it is a compaction problem, not a Git one.
7. **A snapshot is not a transaction.** The tree is read file by file. A file
   changing mid-scan yields a snapshot that is internally consistent but not a
   point-in-time image of the whole tree.
8. **Secrets are permanent.** The vault will happily record `.env`, keys and
   credentials, and Git history is forever. Push it only to a library only you
   can read.
9. **The vault must live outside the source, and outside any synced library.**
   The source/vault relationship is enforced by two path guards, and the
   synced-library rule is enforced too, as of v0.8.0: the tool imports the
   helper's `discover_local_synced_libraries()` (best-effort — a machine
   without the client simply sees no synced libraries) and refuses the run
   when the source **or the vault** sits inside one. The note below is kept
   as design history: it is what prompted the fix, and its probe of what the
   helper can and cannot see is still the reason the vault needs its own
   guard.

### The synced-library rule, and what the helper does and does not cover

An earlier version of this file said the synced-library rule was undetectable.
**That was wrong**, and it is worth recording how, because the detail matters.

The helper ships `discover_local_synced_libraries()`, which reads the Seafile
desktop client's database and returns every synced library on the machine.
Measured here: **4 libraries**, including `Documents -> D:\theera\Documents` —
which is the folder this very project lives in. So the condition is detectable —
**and since v0.8.0 the tool calls it**: `check_paths_are_unsynced()` runs at
startup, before anything is written, for the source *and* the vault. What
follows is the pre-fix analysis, kept because the helper-visibility probe is
still the reason the vault needs its own guard.

What the tool *does* get, for free, is the helper's own pre-flight checks.
Probed rather than assumed — a stub remote helper was installed and the
environment git hands it was dumped:

```
git --git-dir=<vault>/.git --work-tree=<source> push <url>
  -> helper sees GIT_DIR=<vault>/.git, GIT_WORK_TREE=<source>
```

Because `GIT_WORK_TREE` is the **source**, the helper's Trap 1 / Trap 2 checks
evaluate the source tree — so a source sitting un-ignored inside a synced
library is caught on push. What is **not** checked is the *vault*: the helper
never sees it, because the work tree it is told about is the source. A vault
placed inside a synced library is therefore silently unprotected, and that is
the specific case the tool should detect itself.

### Known Git-on-Windows limitations, NOT measured in this session

10. **File modes and symlinks.** Git for Windows does not reliably record the
    executable bit (`core.fileMode`), and symlink support depends on developer
    mode and privileges. A restore performed on Linux may not reproduce
    permissions or symlinks. If that matters, verify before relying on it.
11. **Case-insensitive filesystems.** Windows and macOS can collapse `Foo.txt`
    and `foo.txt`; Git will warn, but the snapshot may not round-trip both.
12. **Path length.** A deep source tree can exceed Windows' 260-character path
    limit, and a restore into a deeper destination is where it surfaces.
13. **File timestamps are not preserved.** Git records content, not mtime. A
    restore gives every file the checkout time, so a build system may rebuild
    everything once. Harmless for a backup, surprising if you restore in place.

### Not a caveat, but worth knowing

14. **Touching a file without changing its content produces no commit** — the
    vault is content-addressed, not timestamped. If you want a heartbeat record
    of "a backup ran", this tool will not give you one.
15. **There is no retention policy.** Every snapshot is kept forever; nothing
    expires, and pruning would mean rewriting history, which the vault does not
    do. Retention is a real feature of restic and Borg — see
    [section 12](#12-how-this-compares-to-a-filesystem-snapshot-tool-restic-or-borg).

### The nested-repository fix, measured

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
source was touched.

What this buys, and what it does not:

- It captures the nested repository's **working tree** — tracked *and* untracked
  files — which is what a file backup wants. It is still not that repository's
  *history*; that needs a `git push` of its own.
- It composes, because the walk is recursive.
- It does **not** produce a submodule. A restore gives plain directories and
  files, not a repository you can `git fetch` in. That is right for a backup and
  wrong for a checkout — so the tool should keep warning, but describe the result
  accurately instead of calling it broken.

## 9. What Seafile and `git-remote-seafile` already give you

The tool deliberately reuses the transport rather than reimplementing it. What
is inherited, and what is left on the table:

| Capability | Status | Notes |
| :--- | :--- | :--- |
| Packfile transport over the Seafile API | **Used** | The tool only ever runs plain `git push`; every byte moves through the helper. |
| Distributed lease lock | **Used** | Taken by the helper for the duration of the push, so two machines cannot interleave ref writes. |
| Fast-forward guard | **Used, and now relied on** | The helper refuses a non-fast-forward rather than clobbering. That refusal is what the retry and rollback logic is built around. |
| Trap 1 / Trap 2 pre-flight safety | **Used, for the source only** | Verified by probe: the helper sees `GIT_WORK_TREE=<source>`. The *vault* is never checked. |
| `check-auth`, `test`, `lock-status`, `unlock`, `desktop-url` | **Available** | Useful for diagnosing a stuck push; not wired into the tool. |
| `gc` / `seafile.autogc` | **Not used** | The tool never compacts. The helper warns on every push, and nothing acts on the warning. This is the clearest operational gap — see caveat 6. |
| `lfs-transfer` | **Deliberately bypassed** | The vault pins `-filter`, because a vault with no LFS remote would otherwise store pointers instead of files. |
| `set-head` | **Not used** | The remote's default branch is left unset, so a plain `git clone` of the vault warns. `--restore` sidesteps it. |
| Synced-library discovery | **Used** (best-effort import, v0.8.0) | Closes the vault-inside-a-synced-library gap: the run is refused at startup. |

**Seafile-side capabilities** that bear on the gaps this tool leaves open:

- **Encrypted libraries.** Seafile supports client-side encrypted libraries.
  Pointing the vault at one gives encryption at rest without `git-crypt` and
  without the tool knowing anything about it — the strongest argument for
  putting the vault in its own library rather than reusing the `code` one.
- **Server-side file history and trash.** These operate on the *packfiles*, so
  they are opaque here; they do not substitute for the vault's own Git history.
- **Library permissions.** The natural control for the "secrets are permanent"
  caveat: push the vault to a library only you can read.

## 10. Corner cases worth thinking about

Grouped by how much is actually known about each.

**Handled, with a test:**

- A second machine sharing one vault, and a machine that is behind.
- Divergence, and the `--reset-to-remote` escape hatch.
- A push that loses a race with another machine.
- A push the remote refuses outright (rollback).
- Vault inside the source, source inside the vault.
- A concurrent local run (the lock).
- The source or the vault sitting inside a synced library (refused at startup).
- A source `filter=`, an older vault needing upgrade.

**Known, documented, not handled:**

- Nested repositories' *contents* (detected, and named loudly in the run's
  output — the measured capture is still future work).
- Packfile growth on the remote with no compaction.
- No retention or expiry.
- Empty directories, file modes, symlinks, timestamps, case collisions.

**Open questions, deliberately unresolved:**

- **Per-machine vault vs one shared vault.** Sharing is now safe, but it makes
  the remote a single linear log of several machines' trees, and a divergence
  stops the run. Per-machine vaults (`...-vault-<host>`) avoid divergence
  entirely at the cost of N remotes to restore from. Neither is obviously
  right; the shared model is the default because it is what "one backup" means.
- **Whether the vault should ever fetch-and-merge.** It should not: two trees
  are not two edits, and a merge would fabricate a state neither machine saw.
- **Retry depth.** One automatic retry covers a two-machine race. A fleet of
  machines pushing on the same schedule could need more, or a backoff.
- **`--restore` fetches the whole history.** For a vault with years of
  snapshots that is slow, and a shallow or partial restore is not implemented.

## 11. Why a separate repository, not a branch or a subdirectory

Measured, and the reason is structural rather than stylistic.

**A branch in the source repository would publish the ignored files.** The
snapshot's tree is the *whole* working tree: `.env`, keys, credentials,
`node_modules`. In the same repository, one `git push --all` — or a mirror
remote whose refspec happens to cover `refs/heads/*`, which is exactly what this
project's own `mirror` does — sends all of it to the code remote. A separate
repository removes that failure mode entirely. This is the strongest reason, and
it is not hypothetical.

**But be precise about what a separate repository buys.** It separates the
*object stores*, which is what stops an ordinary `git push --all` from carrying
the snapshot along. It does **not**, by itself, keep the ignored files off the
code remote's *server*: point the vault's `--remote` at the same library the code
lives in and the snapshot is published there on purpose. Since v0.8.0 the tool
*does* check — `--remote` colliding with one of the source's own remotes is
refused, and `--allow-shared-remote` overrides it deliberately (issue 3 in
[section 16](#16-own-review-issues-found)). So the accurate claim is that a
separate repository turns publishing the ignored files from an accident into a
deliberate act, and the tool enforces the deliberate part.

**A branch also entangles the source's object database.** You would have to
avoid the source's index (a temporary index via `GIT_INDEX_FILE`, as
`git-store-file` does) — but the snapshot's *objects* still land in the source
repository. An interrupted run leaves dangling objects there, and any later
`git gc` in the source repo now has to reason about the snapshot chain. The
vault writes nothing to the source at all: measured, the source's `.git` is
**2125 KB before and 2125 KB after three snapshots**, while the vault holds
4188 KB separately. A clone of the code repository therefore carries 2125 KB no
matter how many snapshots exist.

**It works on a directory that is not a repository.** Measured: a plain
directory containing `a.txt` and `.env` and no `.git` at all snapshotted fine
(it uses the global git identity). A branch-based design has nowhere to put the
branch.

**And the lifecycles are different.** The vault can be deleted, recreated,
given its own remote, its own permissions (an encrypted Seafile library) and its
own retention. The code repository's history is the thing you least want to
touch, and mixing them means pruning the snapshot chain would mean touching it.

**A subdirectory inside the source (`.vault/`, `snapshots/`) is worse, and the
tool refuses it.** Two independent reasons, either of which is disqualifying:

1. `git add -A -f .` runs with `--work-tree=<source>`, so a vault under the
   source is *inside its own snapshot*. Because it contains a `.git`, it is
   recorded as a gitlink — a broken directory on restore, plus a permanent
   warning.
2. If the source sits in a synced Seafile library, the client would sync the
   vault's `.git` — precisely the corruption the project's Golden Rule exists to
   prevent.

The guard is tested (`test_vault_inside_the_source_is_refused`). A hidden
directory is not a loophole; `-f` does not care about dot-prefixes.

## 12. How this compares to a filesystem snapshot tool, restic or Borg

An earlier version of this file said "hand it to restic / Borg" in a table and
left it there, which read as a contradiction — they are backup tools, and so is
this. The distinction is narrower than it looked, and worth stating plainly.

| | Filesystem snapshot (ZFS/btrfs, VSS, Time Machine) | restic / Borg | The vault |
| :--- | :--- | :--- | :--- |
| Storage granularity | filesystem blocks, copy-on-write | content-defined chunks | whole-file blobs in a Git object DB |
| Cost of a 1-byte edit to a big file | ≈ zero | only the changed chunks | **a full new blob until `gc`** — measured: 8 MB file, 1-byte edit → **+8202 KB**, then `gc --aggressive` → back to 8284 KB |
| Identical files stored once | yes (blocks) | yes (chunks, globally) | yes, exactly — measured: two identical 1 MB files share one blob SHA, 1099 KB of object store for 2 MB of content |
| Portable across filesystems and OSes | no | yes | yes |
| Reaches Seafile through the existing helper | no | only via WebDAV/rclone | **yes — plain `git push`** |
| History / diff semantics | a list of snapshots | a list of snapshots | **a real Git DAG**: `git log`, `git diff`, `git show`, `git bisect` |
| Restore granularity | whole dataset | whole file | whole file, and **the whole tree in one command** |
| Encryption at rest | filesystem-level | built in | none — use a Seafile encrypted library |
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

**restic and Borg are whole backup *systems*.** They own the storage format,
the deduplication, the encryption, the retention policy, and the scheduling.
They are the right answer to "I want a backup system."

**The vault is not competing with them.** It does one thing they cannot do
here: it puts the *ignored* files into the Git history that already flows to
Seafile through this project's helper, so that one `--restore` reconstitutes a
working tree and `git log`/`git diff` answer "what changed" over the whole tree
— ignored files included. That is a **complement to a Git remote, not a
replacement for a backup engine.**

So "hand it to restic or Borg" means: **do not grow the vault into a backup
engine.** Concretely, these are the jobs to give to them, not to this tool:

| Missing capability | Hand it to | Why not here |
| :--- | :--- | :--- |
| Deduplication across *different* files, compression | restic / Borg | Git dedups blobs, not content; a copied file is stored twice. |
| Encryption at rest | restic / Borg, or a Seafile **encrypted library** | `git-crypt` works on the vault but encrypts per-repo, not per-file-set. |
| **Retention and expiry** | restic / Borg | The clearest gap: the vault keeps everything and cannot prune without rewriting history. |
| Efficient large, volatile binaries | Git LFS (this project ships a transfer agent), or keep them out of the vault | See caveat 6 — the cost is real and compaction is manual. |

The honest summary: **if you want a backup system, use restic pointed at a
Seafile-backed WebDAV or rclone target. If you want your ignored files to travel
with the repository you already push to Seafile, use the vault.** They can
coexist, and the vault is the smaller, sharper tool.

### Should we borrow their features?

**Not their storage format — their behaviours.** Taking them one at a time:

| Feature | Verdict |
| :--- | :--- |
| Chunk-level dedup / compression | **No.** That is restic's and Borg's whole reason for existing; reimplementing it inside a Git object database is not possible without abandoning Git, and abandoning Git abandons the reason to have this tool. |
| Encryption at rest | **Borrow, but not in code.** Point the vault at a **Seafile encrypted library**. That is one setting, works today, and needs no change to the tool. `git-crypt` is the alternative if the encryption must be per-repository. |
| Retention / expiry | **Borrow the concept, not the implementation.** This is the one genuine gap: the vault keeps everything. Pruning would mean rewriting the chain, which the vault deliberately does not do. If you need retention, run restic alongside — do not add a prune to a backup that is meant to be append-only. |
| Compaction on a schedule | **Already available.** Git has `gc`; the helper exposes it as `git-remote-seafile gc`. The vault just does not call it — see the recommended next steps. |
| Snapshot naming / tagging | **Cheap to borrow.** restic has `--tag`; Git's equivalent is `refs/tags/*`, which this helper *does* allow. Not needed yet, but it is the natural way to mark a milestone snapshot. |
| Progress reporting, dry-run, verification | **Already present** (`--dry-run`), and see `git fsck` in [section 13](#13-what-git-already-gives-us-and-what-we-leave-unused). |
| Scheduling | **Out of scope for the tool.** Use the OS scheduler, or the agent's automations. |

### Why a new tool rather than an existing one

**Nothing else does this.** The prior art below is close in *mechanism* and
different in *job*:

- `git-store-file` stores a **named file list** to a branch in the **same**
  repository — a way to version a few dotfiles, not to capture a working tree.
- `safe-gitignore` is **allowlist-driven** (`# safe` markers), so it captures
  what you remembered to mark rather than everything.
- Neither pins **byte-exactness**, and neither handles the Windows
  `core.autocrlf` trap or the LFS-pointer trap, because neither was written for
  "restore this exactly on another machine".

Those three properties — whole tree, byte-exact restore, safe on a shared
remote — are what this tool exists for, and none of them is a configuration
flag on anything else.

### Placement: this project, or its own?

The tool has **zero code dependency** on the package — it imports nothing from
`git_remote_seafile` and shells out to plain `git`. Its only tie is a *runtime*
one: it pushes to a `seafile://` URL, so it needs the helper installed.

That means it *could* be separate. It should not be:

- The audience overlap is near-total. Anyone who wants a Seafile-backed Git
  remote is a candidate for a Seafile-backed file backup, and vice versa.
- It is ~600 lines that mostly invoke `git`. A separate repository means its own
  CI matrix, its own docs guards, its own release process and version numbers —
  real, recurring cost for very little isolation.
- `tools/seafile_doctor.py` is the precedent: adjacent tooling already lives
  here.

It should also **not** be promoted to a `git-remote-seafile` subcommand. That
CLI is about *the remote*; a local-filesystem backup command dilutes it, and the
docs guard would then require it to be documented as a first-class subcommand of
a released package.

The trigger to reconsider is concrete: **if it ever imports from
`git_remote_seafile` unconditionally, or grows a user interface, it has become
its own thing.** A *best-effort, optional* import for the synced-library check
(see [section 9](#9-what-seafile-and-git-remote-seafile-already-give-you)) would
not cross that line, because the tool still works standalone.

### Recommended next steps, in order

The first two of the original list are **done** — see
[section 16](#16-own-review-issues-found): the vault can no longer be pushed to a
code remote, and `--exclude` matches at any depth. The third was done in v0.8.0:
the vault inside a synced library is no longer merely warned about, it is
**refused** (best-effort import of the helper's discovery, silent when the
helper is absent). What remains:

1. **Capture nested repositories** instead of only warning about them — the
   plumbing is measured and written up at the end of [section 8](#8-caveats).
2. **Surface `gc`** — either run `git-remote-seafile gc` after a snapshot when
   the helper reports enough packfiles, or document a schedule. Today the
   warning is printed and ignored.
3. **The shared-vs-per-machine vault question was decided for v0.8.0**: the
   shared chain ships, with divergence handling and the `--branch` escape
   hatch. Per-machine vaults remain the fallback for genuinely divergent
   workflows.
4. **Do not** add dedup, encryption, or retention. Point at restic — and note
   that on a Seafile instance with WebDAV disabled there may be no restic route
   at all; see
   [section 17](#17-should-the-helper-grow-hooks-or-support-restic-and-borg).

## 13. What Git already gives us, and what we leave unused

### First: Git has no snapshot *feature* — only primitives

Asked directly: does Git already do this? **No.** There is no command whose job
is "record this working tree, ignored files included, somewhere I can restore it
from". What Git has is the four plumbing commands this tool is built from —
`add -f`, `write-tree`, `commit-tree`, `update-ref` — which are a *vocabulary*,
not a feature.

The one candidate that looks like a feature is `git stash`, and it is the only
command in Git whose stated purpose is "save the working state". It was probed
rather than reasoned about, and it fails on four counts:

```
$ git stash push -a -q -m probe
$ git rev-list --parents -n1 'stash@{0}'     # -> THREE parents
$ git ls-tree -r --name-only 'stash@{0}'     # -> .gitignore tracked.txt
$ git ls-tree -r --name-only 'stash@{0}^3'   # -> .env node_modules/dep.js
$ ls -A                                      # -> .git .gitignore tracked.txt
```

1. **Ignored files do not go in the stash's tree.** They go in a *third parent*
   (`stash@{0}^3`), a side channel for untracked files. The stash's own tree holds
   the tracked files only.
2. **Restoring the tree therefore does not restore them.** A tree checkout
   materialised `.gitignore` and `tracked.txt`; `.env` and `node_modules/dep.js`
   were absent. `git archive stash@{0}` agrees — two files.
3. **`git stash push` mutates the source.** That last listing is the working
   directory *after* the command: the untracked and ignored files were **deleted**
   from it. A backup tool whose first promise is "your source is read-only" cannot
   be built on that.
4. **The non-destructive form is a silent no-op.** `git stash create` leaves the
   working tree alone — and **silently ignores `-u`**: exit status 0, nothing on
   stderr, and no commit object produced. `git stash create -h` does not even list
   the flag. For a backup, a silent no-op is the worst possible failure mode. It
   also requires a HEAD ("You do not have the initial commit yet"), so it cannot
   snapshot a directory that is not yet a repository.

The vault differs from `git stash` in exactly those four places: everything goes
in **one** tree, the source is never written to, an unchanged tree is *reported*
rather than silently skipped, and a directory with no `.git` works fine.

The wider survey tells the same story — every other candidate exports something
that already exists, rather than recording something that does not:

| Candidate | Why it is not a snapshot |
| :--- | :--- |
| `git archive <commit>` | Exports a *tree*; untracked files are in no tree. |
| `git bundle` | Packages refs and objects for transport, not working-tree files. |
| `git worktree add` | Another checkout of the same repository — same tracked content. |
| `git add -N` / `--intent-to-add` | Still bound by `.gitignore`; needs a second `add` anyway. |
| GitHub/GitLab "download source" | A tarball of a commit. Untracked files are not in it. |

So Git supplies the *storage model* and the *plumbing*; the tool supplies the
feature. That is a small amount of code — but it is not zero, and it is not a
configuration flag on anything else.

**Taken advantage of today:**

- **Content-addressed dedup.** Two identical files are one blob — measured:
  `big1.bin` and `big2.bin` share a blob SHA, and 2 MB of content costs 1099 KB
  of object store.
- **Packfile delta compression.** Near-identical revisions collapse — but only
  when `gc` runs (the 8 MB measurement in [section 12](#12-how-this-compares-to-a-filesystem-snapshot-tool-restic-or-borg)).
- **The commit DAG.** History, `diff`, and rollback for free.
- **Object integrity.** Every object is hash-verified when read, so a corrupt
  restore fails loudly instead of silently.
- **Plumbing**: `write-tree`, `commit-tree`, and `update-ref` with a
  compare-and-swap expected value — which is what makes the branch move safe.
- **`merge-base --is-ancestor`** — the fast-forward decision in the
  multi-machine reconciliation.
- **`info/attributes`** — the byte-exact override, at the top of Git's
  attribute-precedence chain.
- **`add -f`** — the actual lever that makes the `.gitignore`-rename trick
  unnecessary.
- **`fetch`** — reading the remote tip before building.

**Available and unused — and two of these are *blocked*, which is worth knowing
before designing around them:**

| Git feature | Status |
| :--- | :--- |
| `git notes` (per-snapshot metadata) | **Blocked, deliberately.** The helper writes only `refs/heads/` and `refs/tags/` (`REF_NAMESPACES` in `refs.py`), so notes under `refs/notes/` could not be pushed to a `seafile://` remote. That allowlist is shared with the ref advert *and* with compaction's reachability mirroring, so widening it is not a one-line change — see [section 17](#17-should-the-helper-grow-hooks-or-support-restic-and-borg). |
| Custom ref namespaces (`refs/snapshots/<host>`) | **Blocked** by the same allowlist, for the same reason. A per-machine chain is one `--branch snapshot-<host>` run away — an ordinary branch name, no custom namespace needed. |
| Tags (`refs/tags/*`) | **Allowed, unused.** The natural way to mark a milestone snapshot. |
| `git fsck` | **Used**, under `--restore --verify`. The byte-level check runs on every restore; `fsck` is the optional second pass. |
| Shallow / partial clone | **Unused.** `--restore` fetches the whole history; `--depth` or `--filter=blob:none` would speed up restoring a large vault. |
| `git count-objects` | **Unused.** The tool reports the tree's size but not the remote's growth. |
| `git bundle` | **Unused.** A portable single-file snapshot, if one is ever needed offline. |
| Commit signing, `git replace`, grafts | **Unused.** No need. |

So the honest answer to "are we taking advantage of what Git offers?" is: **the
storage and history model, yes; the metadata and maintenance surface, mostly.**
Two of the obvious ideas — `git notes`, custom refs — are unavailable through
this helper rather than merely unbuilt, and deliberately so. `git fsck` has since
moved from this table to the one above, as `--restore --verify`. What is still
unbuilt: shallow/partial clone, `git count-objects` in the report, and
`git bundle`.

## 14. Prior art

The design is well-trodden. The closest relatives:

| Project | Relationship |
| :--- | :--- |
| **[`git-store-file`](https://blog.iany.me/2025/12/backup-ignored-files-with-git-remote-branch/)** ([source](https://github.com/doitian/dotfiles-public/blob/master/default/bin/git-store-file)) | Uses the *same plumbing* — a temporary index via `GIT_INDEX_FILE`, `git add -f`, `git commit-tree`, a direct push — but stores to a **branch in the same repository** (`origin/_store`) and for a **named file list**, not the whole tree. Its author's write-up lists the "separate repository + symlinks" approach as the thing it replaced. |
| **[`safe-gitignore`](https://github.com/sstraus/safe.gitignore)** | A post-commit hook that copies `# safe`-marked ignored files into a **separate private repository**, preserving directory structure, with optional `git-crypt` encryption. Closest in spirit to the vault, but allowlist-driven rather than whole-tree. |
| **Dotfiles bare-repository idiom** ([write-up](https://www.atlassian.com/git/tutorials/dotfiles)) | `git --git-dir=$HOME/.dotfiles --work-tree=$HOME` is the direct ancestor of the vault pattern: a Git directory whose work-tree is somewhere else. |
| **[`git-remote-dropbox`](https://github.com/anishathalye/git-remote-dropbox)** | The landmark demonstration of transparent Git packfile transport plus distributed locking over a cloud storage API — the model `git-remote-seafile` itself follows. |
| **[git-annex](https://git-annex.branchable.com/)**, **[BorgBackup](https://www.borgbackup.org/)**, **[restic](https://restic.net/)** | Occupy the same "back up everything, incrementally" space with purpose-built formats. They deduplicate and compress better than Git and handle large binaries properly; they do not give you Git's history, diff and merge semantics. See [section 12](#12-how-this-compares-to-a-filesystem-snapshot-tool-restic-or-borg). |

Reference documentation:

- [`gitignore(5)`](https://git-scm.com/docs/gitignore) — why renaming the file
  cannot track anything.
- [`gitattributes(5)`](https://git-scm.com/docs/gitattributes) — the attribute
  precedence that `info/attributes` sits at the top of.
- [Seafile: excluding files](https://help.seafile.com/syncing_client/excluding_files/)
  — the `seafile-ignore.txt` semantics this project's Trap 2 depends on.

Notably, none of the prior art mentions the `core.autocrlf` trap. It is
specific to Git for Windows, and it only surfaces when you actually run a
restore.

## 15. Maintenance

This file is kept current **as part of the work**, not by a robot: when a
discussion changes the tool, the tests, or the thinking in here, this file is
updated in the same pass. That is the convention.

One backstop exists: **mechanical claims** — config keys, environment
variables, subcommands, the `seafile-ignore.txt` name — are guarded by
`tests/test_docs_consistency.py`, which scans this file because it is listed in
`_DOC_NAMES`. Drift fails the suite.

The prose and the measurements are **not** guarded, and deliberately so: the
project's own experience is that prose drift survives a green suite for
releases at a time, so the numbers here are marked as measured-on-this-machine
rather than presented as invariants. Re-read them when the tool changes.

If you change the tool: change the tool, change its tests, run the suite, and
update this file. The guard will tell you if a mechanical claim went stale.

## 16. Own review: issues found, and what was fixed

A pass over the tool looking for defects rather than features, ordered by how
much each one matters. Everything here was measured or read out of the code, not
guessed at. **All six are now fixed**, each with a test. Every entry keeps the
measurement that found it, because the measurement is the part worth keeping.

| # | Issue | State |
| :--- | :--- | :--- |
| 1 | `--exclude` was anchored at the tree root | **Fixed** — matching moved into the tool |
| 2 | `--dry-run` did not suppress `--code-remote` | **Fixed** |
| 3 | Nothing checked the vault's remote against the code remote | **Fixed** — refused by default |
| 4 | The lock had no stale recovery | **Fixed** — pid and host recorded, reclaimed when dead |
| 5 | No integrity check on a restore | **Fixed** — byte-level, on every restore |
| 6 | Secrets warned about in prose only | **Fixed** — reported at snapshot time |

### 1. `--exclude` is root-anchored, so the documented invocation under-excludes

**Silent, and it hits monorepos hardest.** `--exclude node_modules` excludes the
root `node_modules/` and nothing else. Measured on a tree holding
`node_modules/x.js`, `pkg/node_modules/y.js` and `a/b/node_modules/z.js`: **one of
the three** was removed. The same holds for `dist` and `build` — precisely the
directories a monorepo keeps under `packages/*/`. The user's mental model is "I
excluded node_modules"; the behaviour is "I excluded one directory by that name".

**Fixed** by moving the matching into the tool: after `git add`, it reads the
index, matches each path with `fnmatch` against the whole path *or* against any
single path component, and drops the matches with `update-index
--force-remove` on stdin. So `--exclude node_modules` now means "a directory by
that name, at any depth" — what the name suggests — and wildcards keep working
(`*.pyc` matches `a/b/c.pyc`, because `fnmatch`'s `*` crosses `/`). A trailing
slash is ignored. `*dist*` stays over-broad, so the help points at `*dist/*`
only where a wildcard is genuinely wanted.

The help epilog was corrected with it: it had been printing the unquoted,
root-anchored form, which is how the wrong behaviour came to be documented as
the recommended invocation.

### 2. `--dry-run` does not suppress `--code-remote`

The code-repo push ran *before* the dry-run branch was reached, so
`--dry-run --code-remote origin` really pushed. The help text for `--dry-run`
promises "build the snapshot but do not move the branch or push" — true of the
vault, false of the code remote.

**Fixed**: the code push is now skipped under `--dry-run`, and the run prints
`dry run -- would push the source repo to '<name>'` instead. The test points
`--code-remote` at a repository that does not exist, so a regression fails
loudly rather than silently pushing.

### 3. Nothing checks that the vault's remote differs from the code remote

The one with the worst blast radius, because it defeats the argument in
[section 11](#11-why-a-separate-repository-not-a-branch-or-a-subdirectory). A
separate *local* repository only keeps the ignored files away from the code
remote if it is pushed somewhere else. Point `--remote` at the same library the
code lives in and one push publishes `.env`, keys and the rest, with no warning.
So the claim §11 used to make — that a separate repository makes this
"impossible rather than merely discouraged" — overstated it; what a separate
repository actually makes it is *deliberate*. §11 has been corrected to say that.

**Fixed**: before anything is written, `--remote` is compared against every
`remote.<name>.url` and `remote.<name>.pushurl` in the source repository (read
with `--local`, so a global `insteadOf` cannot answer for it), and a match aborts
the run with an explanation naming both remotes. The comparison is deliberately
loose — a trailing slash, a trailing `.git` and the scheme's case are all ignored
— because the two error directions are not symmetric: a false match costs one
error message and an override flag, while a false miss publishes the secrets.
`--allow-shared-remote` is the escape hatch. This was the smallest change here
and the one that mattered most.

### 4. The lock had no stale recovery

`VaultLock` used `O_EXCL` and told the user to delete the file by hand. A run
killed with SIGKILL, or a crash, left the vault un-snapshotable until someone
read the message and acted.

**Fixed**: the lock now records `hostname pid`, and on contention the holder is
identified. If the host is *this* host and the pid is gone, the lock is reclaimed
with a printed notice; otherwise the run refuses as before. Reclaiming only ever
happens for this host, because a pid from another machine means nothing here and
guessing would be worse than refusing.

One trap worth recording: the obvious liveness probe, `os.kill(pid, 0)`, is
**wrong on Windows** — there `os.kill` is `TerminateProcess` for every signal
except the two console events, so probing a pid that way would *kill* it. The
Windows path goes through `OpenProcess`/`GetExitCodeProcess` instead, and treats
"cannot tell" as alive, so a lock is never stolen on a guess. The helper's own
`lock.py` solves the harder distributed version of this and is worth reading.

### 5. No integrity check on restore

`--restore` cloned, checked out, and counted files. It never ran `git fsck`, and
never re-hashed the restored files against the tree. Git verifies object hashes
on read, so a corrupt *object* fails loudly — but a silently wrong *checkout*
(the `core.autocrlf` class of bug) would not.

**Fixed**, and the fix is more interesting than it looks, because **on a naive
`git clone` of the vault both of the obvious checks pass a mangled restore**:

| Check | On a CRLF-mangled checkout |
| :--- | :--- |
| `git status --porcelain` | **empty** — it compares *through* `core.autocrlf`, so the mangling is normalised away before the comparison happens |
| `git hash-object <path>` | **matches** — it applies the clean filter, re-normalising the very difference being looked for |
| `git hash-object --no-filters <path>` | **mismatches** — it hashes the bytes as they lie on disk |

All three measured on one clone, in one run. So every restore now walks the
snapshot's tree and re-hashes each file with `--no-filters`, failing loudly on a
mismatch with a message that says the *vault* is intact and the checkout is at
fault. `--verify` additionally runs `git fsck`. The two useless checks are worth
remembering: a verification step that cannot fail is worse than no verification,
because it gets believed.

One correction, found by the test suite rather than by reading: on a checkout
made by `--restore` itself, `git status` and plain `git hash-object` **do** see
the mangling, because that clone has `core.autocrlf=false` and
`* -text -filter -ident` pinned by [section 7](#7-restore). So "both obvious
checks pass" is a property of a *naive* clone, not of our own restore — which is
precisely why the check must not lean on either of them, and why the test
asserts only the `--no-filters` invariant as load-bearing.

### 6. Secrets were warned about in prose only

The doc and the module docstring both say secrets are permanent, and the tool
printed nothing when it captured `.env`, `id_rsa`, `*.pem` or `credentials.json`.

**Fixed**: the captured paths are matched against a list of credential-shaped
names (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `.aws/credentials`, `.ssh/*`,
`credentials.json`, …) and the matches are listed at the end of every run,
`--dry-run` included. It is a warning, not a refusal — a *full* backup should
capture `.env`; the point is that the user finds out before the first push,
because Git history is permanent and the remote is the last part still under
their control.

### Smaller notes — two of these were fixed too

- **`push_snapshot` was called with `commit=None`** when the tree was unchanged,
  printing "pushing … / push complete" for a push that could not do anything.
  **Fixed**: an unchanged tree against an up-to-date remote now reports `the
  remote already has this snapshot; nothing to push`. The genuinely useful half
  is kept — a machine that snapshotted offline still pushes its chain, because
  there the remote really is behind.
- **`datetime.now()`** in the default message was naive local time.
  **Fixed**: the message carries the UTC offset now, since a shared vault need
  not live in one timezone.
- **The exec bit is lost.** Measured: `core.fileMode=false` on Git for Windows,
  and `chmod +x` recorded as `100644`. `core.fileMode=true` is the fix on POSIX;
  on Windows it is unreliable, so it stays a documented caveat there. **Not
  fixed** — on Windows there is nothing to fix.
- **`--code-remote` pushes the source repo before the vault is touched**, so a
  later failure leaves the code pushed and no snapshot. The ordering is
  deliberate — the snapshot should sit on top of a pushed state — but it is worth
  stating, because "snapshot aborted before touching the vault" is printed after
  a real side effect has already happened. **Not changed.**

### What I would not change

- **No dedup, compression, encryption or retention.** §12 says why; restic and
  Borg own those problems.
- **Do not merge diverged chains.** Refusing is correct.
- **Do not promote this to a subcommand**, and do not split it into its own
  repository yet.
- **Keep `--exclude` opt-in.** "All files" is the point.
- **Keep the secrets report a warning, not a refusal.** A backup that refuses to
  back up is a backup that gets switched off.

### The shape of these six

Four of the six were **silent**: the exclusion dropped fewer files than the flag
implied, the dry run pushed, a restore could be wrong and still report success,
and credentials were captured without a word. Only two were loud. That is the
class of bug a backup tool actually suffers from, and it is why most of the fixes
here are *"say something"* rather than *"do something differently"*.

## 17. Should the helper grow hooks, or support restic and Borg?

Short answer: **no to both** — and the second "no" is more interesting than the
first.

### Hooks: no, because Git already has the one that matters

The helper is a *transport*. Git invokes it as `git-remote-seafile` during a
push, on the user's machine, with the user's credentials. A hook mechanism inside
it would mean executing configured commands at push time: a code-execution
surface, another config file to document and guard, and a per-remote notion of
"which commands" that has no equivalent anywhere else in Git.

None of that is needed, because **`git push` already runs `.git/hooks/pre-push`**
before any remote helper is invoked. Anything that should happen *around* a push
can live there, with Git's own semantics, documentation and tooling.
Reimplementing Git's own extension point inside a remote helper would be strictly
worse: fewer people understand it, and the docs guards would have to learn a
whole new config surface.

The one genuine gap is "run something *after* a successful push" — Git has
`pre-push` but no `post-push`. That belongs in a wrapper script
(`git push && …`) or a scheduler, not in the transport. This project already has
the shape of that answer in `tools/seafile_doctor.py`; a `tools/` script that
wraps push-plus-snapshot would be the same kind of thing.

### restic and Borg: no bridge, because the helper is the wrong layer

restic and Borg want a **dumb blob store**: put bytes, get bytes, list a prefix,
delete a prefix. A Git remote helper is not that. It speaks `list` / `fetch` /
`push` and moves *packfiles and refs*. Bridging the two is not a feature, it is a
second product — a Seafile backend for restic, with restic's repository format on
top of it.

That work already has an owner, and it is not this project — but the first
version of this section got the owner's address wrong, so it is worth being
precise.

**Correction.** This document first said "restic reaches WebDAV through rclone",
as though WebDAV were the only road in. It is not. **rclone ships a native
Seafile backend** (`backend/seafile/`) that speaks the Seafile REST API directly
— the same `/api2` surface this helper uses — with no WebDAV involved. It works
against Community and Professional editions, supports Seafile 6.x through 9.x
(and newer), supports **encrypted libraries**, supports 2FA, and is actively
integration-tested against a current Seafile image. So the honest instruction is
not "enable WebDAV and point restic at it" — it is:

```
restic -r rclone:seafile:backups/restic-repo init
restic -r rclone:seafile:backups/restic-repo backup ~/code/myproject
```

with an rclone remote of `type = seafile` configured against the server. **No
WebDAV, no administrator, no `ENABLE_SEAFDAV`.**

Two caveats on that route, both real:

- **rclone's Seafile backend authenticates with username and password** (plus a
  2FA code), and it explicitly **does not support a Library API Token** — which
  is the credential shape this helper's zero-config path prefers. A user who has
  only ever set up token-based access needs a password login for rclone.
- **It is not fast.** rclone's Seafile backend drives the API's per-file
  endpoints; restic's many small pack and index objects mean many such calls.

So the restic route exists after all — it just does not run through this helper,
which is the actual point of this section: **the helper still has no restic
bridge, and should not grow one.**

### What changes if Seafile WebDAV is enabled

Little, for this project — and less than the first draft of this section
implied. WebDAV is one of *several* ways into a Seafile library, and the only one
that needs the administrator to act. Turning it on:

| Route | WebDAV required? | Who enables it |
| :--- | :--- | :--- |
| `git-remote-seafile` (this project) | **No** | — |
| The snapshot tool's vault | **No** | — |
| rclone → restic/Borg | **No** (native Seafile backend) | — |
| davfs2 mount / Windows WebDAV client → Borg | **Yes** | Server administrator (`ENABLE_SEAFDAV = true`) |
| Direct WebDAV access (any client) | **Yes** | Server administrator |

What enabling WebDAV genuinely changes:

- **A `davfs2`-style mount becomes possible**, which is the one route Borg
  actually needs (Borg wants a mounted path, not a remote). If you want Borg
  specifically, and the native rclone backend does not fit your workflow, WebDAV
  is how you get there.
- **Nothing at all for the helper.** It does not read or write WebDAV and has no
  code path that would notice the setting.
- **Nothing at all for the vault.** The vault pushes over the helper, so it
  inherits the same answer.

And the caveats that remain, unchanged:

- WebDAV is **disabled by default** and turned on by the *server administrator*
  (`ENABLE_SEAFDAV = true`). On a hosted or shared Seafile instance, that is not
  the user's decision — which is exactly why it is a bad thing to depend on.
- The Seafile manual is explicit that WebDAV is "more suitable for infrequent
  file access": every file is committed separately and bulk uploads are slow.

So: enabling WebDAV adds a route (Borg over a mounted path) and changes nothing
for this project. It is a *nice-to-have for other tools*, not a switch that
alters the helper's design — and the existence of the native rclone backend means
the restic route never depended on it in the first place. That is a stronger
version of the argument the vault exists on: **it is a backup path to Seafile
that needs nothing from the server administrator beyond the account you already
have.**

### What the helper could usefully add — and what it should not

Nothing in the push path. The two candidates are both convenience:

| Candidate | Verdict |
| :--- | :--- |
| A `webdav-url` subcommand printing the SeafDAV URL for a library | **Plausible, deliberately not built.** It is ~15 lines and would turn "configure restic" from a research task into a copy-paste — but only where the server admin has enabled WebDAV, which the helper cannot discover, and it would need a `_COMMANDS` entry plus a docs-guard row. Worth doing only if restic support is actually wanted. |
| Widening `REF_NAMESPACES` to `refs/notes/*` or a custom namespace | **No.** See [section 13](#13-what-git-already-gives-us-and-what-we-leave-unused): that allowlist is not only a push guard, it also decides what compaction mirrors for reachability — and a ref the compactor cannot see makes its objects look like garbage for `repack -a -d` to delete permanently. Widening a least-privilege boundary that three subsystems share, to enable metadata the commit message already carries, is a bad trade. |

So the conclusion this document kept circling: **the helper does not need fixing
for any of this.** Every limitation found while writing it was in the snapshot
tool, and those are fixed in [section 16](#16-own-review-issues-found).
