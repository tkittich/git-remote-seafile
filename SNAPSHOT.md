# SNAPSHOT.md — full-tree backup to Seafile, `.gitignore`d files included

> **Status: local tool, not a shipped feature.** The implementation is
> `tools/seafile_snapshot.py`; the tests are `tests/test_snapshot_tool.py`
> (29 cases). The full suite is 492 tests across 90 targets. It is deliberately
> absent from `USER_GUIDE.md`, `DESIGN.md` and `CHANGELOG.md` until a decision
> is made about whether it ships as a feature or stays a personal utility.
> Everything below was measured on this machine unless a claim is explicitly
> marked otherwise.
>
> This file is kept current **as part of the work** — see
> [Maintenance](#13-maintenance).

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
    --exclude node_modules --exclude venv --exclude .venv \
    --exclude __pycache__ --exclude '*.pyc' --exclude dist --exclude build
```

Useful flags: `--branch` (default `snapshot`), `--message`, `--dry-run`,
`--restore`, `--into`, `--ref`, `--reset-to-remote`.

**Recommended exclusions** are the *reproducible* directories — `node_modules`,
`venv`, `dist`, `build`, `__pycache__`. There is no default exclude list,
because "all files" is the point; add them deliberately.

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

### Verified on this machine

1. **Nested Git repositories are recorded as gitlinks — their contents are
   NOT captured.** `git add` records a directory containing its own `.git` as a
   single commit reference (mode `160000`), and that commit is not copied into
   the vault. Measured: `git cat-file -t` on the recorded object fails with
   "could not get object info", and `nested/inner.txt` is absent from the
   snapshot. A restore would produce a broken directory. The tool prints a
   prominent warning naming each such path — **back those up separately**.
   Submodules are the same mechanism and are warned about identically.
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
    [section 11](#11-why-a-new-tool-and-what-hand-it-to-restic-or-borg-means).

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

## 11. Why a new tool, and what "hand it to restic or Borg" means

An earlier version of this section said "hand it to restic / Borg" in a table
and left it there, which read as a contradiction — they are backup tools, and
so is this. The distinction is narrower than it looked, and worth stating
plainly.

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

1. **Warn when the vault is inside a synced library** — the one confirmed gap
   with a confirmed detection path. Best-effort import of the helper's
   discovery, silent when the helper is absent.
2. **Surface `gc`** — either run `git-remote-seafile gc` after a snapshot when
   the helper reports enough packfiles, or document a schedule. Today the
   warning is printed and ignored.
3. **Decide the shared-vs-per-machine vault question** before documenting this
   as a feature; it changes what "restore on a new machine" means.
4. **Do not** add dedup, encryption, or retention. Point at restic.

## 12. Prior art

The design is well-trodden. The closest relatives:

| Project | Relationship |
| :--- | :--- |
| **[`git-store-file`](https://blog.iany.me/2025/12/backup-ignored-files-with-git-remote-branch/)** ([source](https://github.com/doitian/dotfiles-public/blob/master/default/bin/git-store-file)) | Uses the *same plumbing* — a temporary index via `GIT_INDEX_FILE`, `git add -f`, `git commit-tree`, a direct push — but stores to a **branch in the same repository** (`origin/_store`) and for a **named file list**, not the whole tree. Its author's write-up lists the "separate repository + symlinks" approach as the thing it replaced. |
| **[`safe-gitignore`](https://github.com/sstraus/safe.gitignore)** | A post-commit hook that copies `# safe`-marked ignored files into a **separate private repository**, preserving directory structure, with optional `git-crypt` encryption. Closest in spirit to the vault, but allowlist-driven rather than whole-tree. |
| **Dotfiles bare-repository idiom** ([write-up](https://www.atlassian.com/git/tutorials/dotfiles)) | `git --git-dir=$HOME/.dotfiles --work-tree=$HOME` is the direct ancestor of the vault pattern: a Git directory whose work-tree is somewhere else. |
| **[`git-remote-dropbox`](https://github.com/anishathalye/git-remote-dropbox)** | The landmark demonstration of transparent Git packfile transport plus distributed locking over a cloud storage API — the model `git-remote-seafile` itself follows. |
| **[git-annex](https://git-annex.branchable.com/)**, **[BorgBackup](https://www.borgbackup.org/)**, **[restic](https://restic.net/)** | Occupy the same "back up everything, incrementally" space with purpose-built formats. They deduplicate and compress better than Git and handle large binaries properly; they do not give you Git's history, diff and merge semantics. See [section 11](#11-why-a-new-tool-and-what-hand-it-to-restic-or-borg-means). |

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

## 13. Maintenance

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
