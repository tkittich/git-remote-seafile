# SNAPSHOT.md — full-tree backup to Seafile, `.gitignore`d files included

> **Status: local tool, not a shipped feature.** The implementation is
> `tools/seafile_snapshot.py`; the tests are `tests/test_snapshot_tool.py`
> (23 cases). The full suite is 486 tests across 90 targets. It is deliberately
> absent from `USER_GUIDE.md`, `DESIGN.md` and `CHANGELOG.md` until a decision
> is made about whether it ships as a feature. Everything below was measured on
> this machine unless a claim is explicitly marked otherwise.
>
> This file is kept current **as part of the work** — see
> [Maintenance](#10-maintenance).

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
`--restore`, `--into`, `--ref`.

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
> caveat 5 in [section 7](#7-caveats).

## 6. Restore

```bash
python tools/seafile_snapshot.py --restore \
    --remote seafile://code/myproject-vault \
    --into C:/restore/myproject

# roll back: any revision works
... --ref snapshot~3
```

The snapshot holds the **whole** tree, tracked files included, so this single
command reconstitutes the working directory. Clone the *code* repository
separately only if you want its branches and history — see caveat 3 below.

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
   an `info/attributes` override carrying `* -text`.
2. `info/attributes` has the **highest precedence** in Git's attribute chain, so
   it also outranks a `.gitattributes` in the source tree — a source
   `* text=auto` cannot re-enable conversion. Confirmed with `git check-attr`,
   which reports `text: unset` for every path.

`--restore` is still required, because **`info/attributes` is not transferred by
a clone** while the source's `.gitattributes` is. A naive clone re-inherits the
system autocrlf and re-converts. So `--restore` clones `--no-checkout`, applies
the byte-exact settings to the clone, and only then checks out.

Verified byte-identical after restore: LF text, CRLF text, a 256-byte binary
blob, a file with no trailing newline, `.gitattributes`, `.gitignore`, and
ignored files under `node_modules/`. The naive clone differed — which is what
makes the contrast real rather than assumed.

## 7. Caveats

### Verified on this machine

1. **Nested Git repositories are recorded as gitlinks — their contents are
   NOT captured.** `git add` records a directory containing its own `.git` as a
   single commit reference (mode `160000`), and that commit is not copied into
   the vault. Measured: `git cat-file -t` on the recorded object fails with
   "could not get object info", and `nested/inner.txt` is absent from the
   snapshot. A restore would produce a broken directory. The tool now prints a
   prominent warning naming each such path — **back those up separately**.
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
3. **Empty directories are not captured.** Git does not track them.
4. **The source's `.git` is not captured.** The vault records the *working
   tree*, not the code repository's history — verified as 0 `.git/` entries in
   the snapshot. Code history needs `git push` to the code remote; the vault is
   not a substitute for it.
5. **Large files cost one full copy per snapshot until the remote is
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
6. **A snapshot is not a transaction.** The tree is read file by file. A file
   changing mid-scan yields a snapshot that is internally consistent but not a
   point-in-time image of the whole tree.
7. **Secrets are permanent.** The vault will happily record `.env`, keys and
   credentials, and Git history is forever. Push it only to a library only you
   can read.
8. **The vault must be outside the source, and outside any synced library.**
   Both are enforced for the source/vault relationship; the synced-library rule
   is not detectable and is on you.

### Known Git-on-Windows limitations, NOT measured in this session

9. **File modes and symlinks.** Git for Windows does not reliably record the
   executable bit (`core.fileMode`), and symlink support depends on developer
   mode and privileges. A restore performed on Linux may not reproduce
   permissions or symlinks. If that matters, verify before relying on it.
10. **Case-insensitive filesystems.** Windows and macOS can collapse `Foo.txt`
    and `foo.txt`; Git will warn, but the snapshot may not round-trip both.

### Not a caveat, but worth knowing

11. **Touching a file without changing its content produces no commit** — the
    vault is content-addressed, not timestamped. If you want a heartbeat record
    of "a backup ran", this tool will not give you one.
12. **There is no retention policy.** Every snapshot is kept forever; nothing
    expires, and pruning would mean rewriting history, which the vault does not
    do. Retention is a real feature of restic and Borg, and it is the clearest
    reason to reach for one of them instead of this — see section 9.

## 8. Prior art

The design is well-trodden. The closest relatives:

| Project | Relationship |
| :--- | :--- |
| **[`git-store-file`](https://blog.iany.me/2025/12/backup-ignored-files-with-git-remote-branch/)** ([source](https://github.com/doitian/dotfiles-public/blob/master/default/bin/git-store-file)) | Uses the *same plumbing* — a temporary index via `GIT_INDEX_FILE`, `git add -f`, `git commit-tree`, a direct push — but stores to a **branch in the same repository** (`origin/_store`) and for a **named file list**, not the whole tree. Its author's write-up lists the "separate repository + symlinks" approach as the thing it replaced. |
| **[`safe-gitignore`](https://github.com/sstraus/safe.gitignore)** | A post-commit hook that copies `# safe`-marked ignored files into a **separate private repository**, preserving directory structure, with optional `git-crypt` encryption. Closest in spirit to the vault, but allowlist-driven rather than whole-tree. |
| **Dotfiles bare-repository idiom** ([write-up](https://www.atlassian.com/git/tutorials/dotfiles)) | `git --git-dir=$HOME/.dotfiles --work-tree=$HOME` is the direct ancestor of the vault pattern: a Git directory whose work-tree is somewhere else. |
| **[`git-remote-dropbox`](https://github.com/anishathalye/git-remote-dropbox)** | The landmark demonstration of transparent Git packfile transport plus distributed locking over a cloud storage API — the model `git-remote-seafile` itself follows. |
| **[git-annex](https://git-annex.branchable.com/)**, **[BorgBackup](https://www.borgbackup.org/)**, **[restic](https://restic.net/)** | Occupy the same "back up everything, incrementally" space with purpose-built formats. They deduplicate and compress better than Git and handle large binaries properly; they do not give you Git's history, diff and merge semantics. |

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

## 9. Build or reuse?

Short answer: **keep the vault tool, and do not grow it into a backup engine.**
It fills a gap none of the prior art fills, and it should not try to fill the
gaps they do.

**Nothing else does this.** `git-store-file` stores a *named file list* to a
branch in the *same* repository — a way to version a few local config files, not
to capture a working tree. `safe-gitignore` is allowlist-driven (`# safe`
markers), so it captures what you remembered to mark rather than everything.
Neither pins byte-exactness, and neither handles the Windows `core.autocrlf`
trap or the LFS-pointer trap, because neither was written for "restore this
exactly on another machine".

**What the vault is not, and should not become:**

| Missing capability | Hand it to |
| :--- | :--- |
| Deduplication across *different* files, compression | restic / Borg |
| Encryption at rest | restic / Borg, or `git-crypt` on the vault |
| Retention and expiry policies | restic / Borg |
| Efficient large, volatile binaries | Git LFS — this project ships a transfer agent — or keep them out of the vault entirely |

The vault's value is that it is **Git**: history, diff, merge, and a
content-addressed integrity check, for free, pushed through the helper you
already run. If what you actually want is a backup *system*, restic pointed at a
Seafile-backed WebDAV or rclone target is the better answer, and rebuilding that
here would be a mistake.

### Placement: this project, or its own?

The tool has **zero code dependency** on the package — it imports nothing from
`git_remote_seafile` and shells out to plain `git`. Its only tie is a *runtime*
one: it pushes to a `seafile://` URL, so it needs the helper installed.

That means it *could* be separate. It should not be:

- The audience overlap is near-total. Anyone who wants a Seafile-backed Git
  remote is a candidate for a Seafile-backed file backup, and vice versa.
- It is ~500 lines that mostly invoke `git`. A separate repository means its own
  CI matrix, its own docs guards, its own release process and version numbers —
  real, recurring cost for very little isolation.
- `tools/seafile_doctor.py` is the precedent: adjacent tooling already lives
  here.

It should also **not** be promoted to a `git-remote-seafile` subcommand. That
CLI is about *the remote* (`check-auth`, `lock-status`, `unlock`, `gc`,
`set-head`, `test`, `desktop-url`); a local-filesystem backup command dilutes
it, and the docs guard would then require it to be documented as a first-class
subcommand of a released package.

The trigger to reconsider is concrete: **if it ever imports from
`git_remote_seafile`, or grows a user interface, it has become its own thing.**

## 10. Maintenance

This file is kept current **as part of the work**, not by a robot: when a
discussion changes the tool, the tests, or the thinking in here, this file is
updated in the same pass. That is the convention.

Two backstops exist, and they catch different things:

- **Mechanical claims** — config keys, environment variables, subcommands, the
  `seafile-ignore.txt` name — are guarded by `tests/test_docs_consistency.py`,
  which scans this file because it is listed in `_DOC_NAMES`. Drift fails the
  suite.
- **Prose and measurements** — the numbers in section 7, the test counts, the
  prior-art links — are invisible to that guard; the project's own experience is
  that prose drift survives a green suite for releases at a time. A weekly
  scheduled task re-checks them as a safety net. It is a backstop, not the
  mechanism.

If you change the tool: change the tool, change its tests, run the suite, and
update this file. The guard will tell you if a mechanical claim went stale.
