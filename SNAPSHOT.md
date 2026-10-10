# SNAPSHOT.md — full-tree backup to Seafile, `.gitignore`d files included

> **Status: local tool, not a shipped feature.** The implementation is
> `tools/seafile_snapshot.py`; the tests are `tests/test_snapshot_tool.py`
> (32 cases). The full suite is 495 tests across 90 targets. It is deliberately
> absent from `USER_GUIDE.md`, `DESIGN.md` and `CHANGELOG.md` until a decision
> is made about whether it ships as a feature or stays a personal utility.
> Everything below was measured on this machine unless a claim is explicitly
> marked otherwise.
>
> This file is kept current **as part of the work** — see
> [Maintenance](#15-maintenance). Open work is collected in
> [section 16](#16-own-review-issues-found).

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

```bash
# see what would be captured, write nothing
python tools/seafile_snapshot.py --source C:/code/myproject --dry-run

# snapshot + push, and push the code repo first in the same command
python tools/seafile_snapshot.py --source C:/code/myproject \
    --vault C:/code/myproject-vault \
    --code-remote origin \
    --remote seafile://code/myproject-vault \
    --exclude '*node_modules/*' --exclude '*venv/*' --exclude '*.venv/*' \
    --exclude '*__pycache__/*' --exclude '*.pyc' --exclude '*dist/*' \
    --exclude '*build/*'
```

Useful flags: `--branch` (default `snapshot`), `--message`, `--dry-run`,
`--restore`, `--into`, `--ref`, `--reset-to-remote`.

**Recommended exclusions** are the *reproducible* directories — `node_modules`,
`venv`, `dist`, `build`, `__pycache__`. There is no default exclude list,
because "all files" is the point; add them deliberately.

> **Quote the patterns, and mind the leading `*`.** An exclusion is a Git
> pathspec, and a *literal* pathspec is anchored at the root of the tree: the
> pattern `node_modules` excludes `node_modules/` and **nothing else**, so
> `packages/api/node_modules/` is still captured. Measured on a tree holding
> `node_modules/x.js`, `pkg/node_modules/y.js` and `a/b/node_modules/z.js`, the
> pattern `node_modules` removed **one of the three**. The form
> `*node_modules/*` removes all three. `*.pyc` already works at any depth, because
> a wildcard *does* cross `/` while a literal path is treated as a directory
> prefix. Quote the pattern so your own shell does not expand it first.
>
> `*dist*` is the trap in the other direction — it matches `distance.txt` too.
> Use `*dist/*`.
>
> This is a defect in the tool's own help text, not just in this document: the
> help epilog and the module docstring both print the unquoted, root-anchored
> form. See [section 16](#16-own-review-issues-found).

> The vault is a Git repository and must **not** live inside a Seafile-synced
> folder — the same rule as for the source, and for the same reason.

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
| **Fixable in the tool, not yet done** — the mechanism exists, and where marked it has been measured | 1 (nested repos), 6 (compaction), 9 (synced library), plus the `--exclude` bug in [section 16](#16-own-review-issues-found) | Real work, but no new ideas needed. |
| **Inherent** — a property of Git, the filesystem or Windows, not of this design | 3, 4, 7, 8, 10, 11, 12, 13 | restic and Borg have most of these too. |
| **Deliberate policy** — a choice, not a limitation | 14, 15 | Two of these are worth *keeping*: "a snapshot is not a transaction" and "there is no retention policy" are the honest limits of a Git-backed backup. |

So the honest answer to "are the caveats fixable?" is: **two of the fifteen are
already fixed, three are fixable and unfixed, and the remaining ten should not be
fixed** — they should be *stated*, which is what this section is for. Add the
unnumbered `--exclude` pathspec bug and the fixable pile is four items, of which
that one is the only defect that is currently **silent**: see
[section 16](#16-own-review-issues-found).

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
   The source/vault relationship is enforced by two path guards. The
   synced-library rule is **not** enforced — see the note below, which corrects
   an earlier version of this document.

### The synced-library rule, and what the helper does and does not cover

An earlier version of this file said the synced-library rule was undetectable.
**That was wrong**, and it is worth recording how, because the detail matters.

The helper ships `discover_local_synced_libraries()`, which reads the Seafile
desktop client's database and returns every synced library on the machine.
Measured here: **4 libraries**, including `Documents -> D:\theera\Documents` —
which is the folder this very project lives in. So the condition is detectable;
the snapshot tool simply does not call it yet.

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
| Synced-library discovery | **Not used** | Would close the vault-inside-a-synced-library gap above. |

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
- Nested repositories, a source `filter=`, an older vault needing upgrade.

**Known, documented, not handled:**

- The vault sitting inside a synced library (detectable, not yet detected).
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
lives in and the snapshot is published there on purpose. The tool does not check
for that yet — it is issue 3 in
[section 16](#16-own-review-issues-found). So the accurate claim is that a
separate repository turns publishing the ignored files from an accident into a
deliberate act; the check that would make it an *error* is still to be written.

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

1. **Refuse a vault remote that is also a code remote** — issue 3 in
   [section 16](#16-own-review-issues-found). The smallest change with the
   largest effect: it turns §11's claim from an intention into a guarantee.
2. **Fix `--exclude`** — the pathspec depth problem (issue 1), in the tool *and*
   in its help epilog. Silent under-exclusion is the wrong kind of bug for a
   backup to have.
3. **Warn when the vault is inside a synced library** — a confirmed gap with a
   confirmed detection path. Best-effort import of the helper's discovery,
   silent when the helper is absent.
4. **Capture nested repositories** instead of only warning about them — the
   plumbing is measured and written up at the end of
   [section 8](#8-caveats).
5. **Surface `gc`** — either run `git-remote-seafile gc` after a snapshot when
   the helper reports enough packfiles, or document a schedule. Today the
   warning is printed and ignored.
6. **Decide the shared-vs-per-machine vault question** before documenting this
   as a feature; it changes what "restore on a new machine" means.
7. **Do not** add dedup, encryption, or retention. Point at restic.

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
| `git notes` (per-snapshot metadata) | **Blocked.** The helper writes only `refs/heads/` and `refs/tags/` (`REF_NAMESPACES` in `refs.py`), and notes live under `refs/notes/`, so they could not be pushed to a `seafile://` remote at all. |
| Custom ref namespaces (`refs/snapshots/<host>`) | **Blocked** by the same allowlist. A per-machine chain would have to be `refs/heads/snapshot-<host>`. |
| Tags (`refs/tags/*`) | **Allowed, unused.** The natural way to mark a milestone snapshot. |
| `git fsck` | **Unused.** A cheap integrity check of the vault; would make a good `--verify`. |
| Shallow / partial clone | **Unused.** `--restore` fetches the whole history; `--depth` or `--filter=blob:none` would speed up restoring a large vault. |
| `git count-objects` | **Unused.** The tool reports the tree's size but not the remote's growth. |
| `git bundle` | **Unused.** A portable single-file snapshot, if one is ever needed offline. |
| Commit signing, `git replace`, grafts | **Unused.** No need. |

So the honest answer to "are we taking advantage of what Git offers?" is: **the
storage and history model, yes; the metadata and maintenance surface, only
partly.** And two of the obvious ideas — `git notes`, custom refs — are
unavailable through this helper rather than merely unbuilt.

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

## 16. Own review: issues found

A pass over the tool looking for defects rather than features, ordered by how
much each one matters. Everything here was measured or read out of the code, not
guessed at.

### 1. `--exclude` is root-anchored, so the documented invocation under-excludes

**Silent, and it hits monorepos hardest.** `--exclude node_modules` excludes the
root `node_modules/` and nothing else. Measured on a tree holding
`node_modules/x.js`, `pkg/node_modules/y.js` and `a/b/node_modules/z.js`: **one of
the three** was removed. The same holds for `dist` and `build` — precisely the
directories a monorepo keeps under `packages/*/`. The user's mental model is "I
excluded node_modules"; the behaviour is "I excluded one directory by that name".

Fix: recommend `*node_modules/*` and friends, quoted (measured: that form removes
all three, while `*dist*` is too broad because it also matches `distance.txt`).
Better still, match in Python — read `git ls-files` and filter with `fnmatch` —
which sidesteps pathspec semantics entirely and lets `--exclude node_modules`
mean what everyone assumes. The help epilog prints the root-anchored form today,
so it needs the same correction.

### 2. `--dry-run` does not suppress `--code-remote`

The code-repo push runs *before* the dry-run branch is reached, so
`--dry-run --code-remote origin` really pushes. The help text for `--dry-run`
promises "build the snapshot but do not move the branch or push" — true of the
vault, false of the code remote. Fix: skip the code push under `--dry-run` and
print what it would have done.

### 3. Nothing checks that the vault's remote differs from the code remote

The one with the worst blast radius, because it defeats the argument in
[section 11](#11-why-a-separate-repository-not-a-branch-or-a-subdirectory). A
separate *local* repository only keeps the ignored files away from the code
remote if it is pushed somewhere else. Point `--remote` at the same library the
code lives in and one push publishes `.env`, keys and the rest, with no warning.
So the claim §11 used to make — that a separate repository makes this
"impossible rather than merely discouraged" — overstated it; what a separate
repository actually makes it is *deliberate*. §11 has been corrected to say that,
but the check itself is still missing. Fix: compare `--remote` against every
`remote.<name>.url` and `.pushurl` in the source repository and refuse on a match
unless an explicit override flag is passed. Cheap, and it is the difference
between a safety property and a safety hope.

### 4. The lock has no stale recovery

`VaultLock` uses `O_EXCL` and tells the user to delete the file by hand. A run
killed with SIGKILL, or a crash, leaves the vault un-snapshotable until someone
reads the message and acts. The pid is already written into the lock, so checking
whether that pid is still alive would turn a dead end into an automatic recovery.
The helper's own `lock.py` solves the harder distributed version of this and is
worth reading first.

### 5. No integrity check on restore

`--restore` clones, checks out, and counts files. It never runs `git fsck`, and
never re-hashes the restored files against the tree. Git verifies object hashes
on read, so a corrupt *object* does fail loudly — but a silently wrong *checkout*
(the `core.autocrlf` class of bug) would not. A `--verify` would have caught the
CRLF bug directly instead of through a hand-written probe.

### 6. Secrets are warned about in prose only

The doc and the module docstring both say secrets are permanent. The tool prints
nothing when it captures `.env`, `id_rsa`, `*.pem` or `credentials.json`. A
one-line summary of suspicious names — especially under `--dry-run` — would put
the warning where the decision is made.

### Smaller notes

- **`push_snapshot` is called with `commit=None`** when the tree is unchanged.
  Harmless (the branch already equals the base, so the push is a no-op) and
  *useful* in one case — a machine that snapshotted offline ends up pushing its
  chain. But it is accidental rather than designed, and it prints
  "pushing … / push complete" for nothing.
- **The exec bit is lost.** Measured: `core.fileMode=false` on Git for Windows,
  and `chmod +x` recorded as `100644`. `core.fileMode=true` is the fix on POSIX;
  on Windows it is unreliable, so it stays a documented caveat there.
- **`datetime.now()`** in the default message is naive local time. Harmless, but
  a shared vault across timezones would read better with an offset.
- **`--code-remote` pushes the source repo before the vault is touched**, so a
  later failure leaves the code pushed and no snapshot. The ordering is
  deliberate — the snapshot should sit on top of a pushed state — but it is worth
  stating, because "snapshot aborted before touching the vault" is printed after
  a real side effect has already happened.

### What I would not change

- **No dedup, compression, encryption or retention.** §12 says why; restic and
  Borg own those problems.
- **Do not merge diverged chains.** Refusing is correct.
- **Do not promote this to a subcommand**, and do not split it into its own
  repository yet.
- **Keep `--exclude` opt-in.** "All files" is the point.

### If only one thing gets fixed

**Number 3.** Everything else here is a papercut or a missing convenience. That
one decides whether the tool's central safety claim is true or merely
aspirational.
