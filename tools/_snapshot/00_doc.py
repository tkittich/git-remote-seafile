#!/usr/bin/env python3
# GENERATED FILE -- do not edit.  The source is tools/_snapshot/*.py;
# run `python tools/build_snapshot.py` after changing it.
"""Snapshot a working tree -- including gitignored files -- into a Seafile remote.

Why this exists
===============
``git push`` transfers *commits*.  Anything matched by ``.gitignore`` never
enters a commit, so no remote helper -- this project's included -- can carry it.

The obvious workaround is "rename ``.gitignore`` away, ``git add .``, push,
rename it back".  That is both unnecessary and unsafe:

* **Unnecessary.**  Renaming tracks nothing.  Ignored files merely become
  *visible* as untracked; you still have to ``git add`` them.  ``git add -f``
  stages ignored files directly, with no rename at all.
* **Unsafe.**  It mutates user-visible state -- the working tree *and* the
  repository's own index -- for the duration of the run.  A crash, a Ctrl-C, or
  a concurrent editor/IDE leaves the repository with no ``.gitignore`` and a
  fully-staged index.  There is no way to make that atomic, because the
  intermediate state is on disk and observable.

What this does instead
======================
A **vault**: a separate Git repository whose ``--work-tree`` points at your
source directory.  It records a full snapshot -- ignored files included -- as
ordinary commits and pushes them to Seafile::

    source dir  --(--work-tree)-->  vault repo  --git push-->  seafile://...
    (untouched)                     own .git, own index

Nothing in the source directory is modified: not ``.gitignore``, not ``.git``,
not the index.  Your code repository stays small, so ``git clone`` on a second
machine stays fast -- the snapshot never enters its history.

Atomicity
=========
* The snapshot is built in the vault's **own index**.  An interrupted run leaves
  at worst a stale index, which the next run overwrites.  The source is never at
  risk, because it is only ever read.
* The branch moves via ``git update-ref`` with an **expected old value**, so a
  concurrent snapshot loses loudly instead of silently clobbering.
* A vault-local lock file keeps two runs from interleaving.  It records the
  holding host and pid, so a lock left behind by a killed run is reclaimed
  automatically once that process is gone -- but only on the *same* host, since
  a pid from another machine means nothing here.
* The push is a single ref update, guarded by the helper's distributed lease.
* A **failed push rolls the local branch back** to what the remote actually has,
  so the vault never claims a snapshot the remote never took.  Without this, a
  rejected push left the branch advanced and every later run re-pushed the same
  doomed commit.

Updates
=======
Each run is a commit on top of the previous one, so the vault is a linear chain
of snapshots.  Only changed blobs are uploaded: Git is content-addressed, so a
snapshot that changed one file in a 486 KB tree pushed 31 KB, and a snapshot
that changed nothing pushes nothing at all (no commit is created).  The vault's
index keeps a stat cache, so unchanged files are not re-hashed either.

Several machines
================
When ``--remote`` is given the remote tip is fetched **before** the snapshot is
built, and the new commit is based on that tip, so several machines can append
to one shared vault.  A machine that is behind fast-forwards first; one that is
ahead (snapshots taken offline) simply pushes.  If the two have genuinely
diverged the run stops with an explanation and no changes -- pass
``--reset-to-remote`` to adopt the remote chain deliberately.  A push that loses
a race with another machine is re-parented on the new tip and retried once.

Because it is ordinary Git history, "roll back to last Tuesday" is ``git show``,
``git diff``, or a checkout of an older commit -- not a restore from a
proprietary archive format.

Restoring
=========
::

    python tools/seafile_snapshot.py --restore \
        --remote seafile://code/myproject-vault \
        --into C:/restore/myproject

    # any revision works -- e.g. roll back three snapshots
    ... --ref snapshot~3

The snapshot holds the *whole* tree, tracked files included, so this one command
reconstitutes the working directory.  Clone the code repository separately only
if you want its branches and history.

Every restore re-hashes the checked-out files against the snapshot's blobs and
fails loudly on any difference.  The check is byte-level on purpose: on a mangled
checkout ``git status`` reports a clean tree and a plain ``git hash-object``
matches, because both normalise the difference away.  Add ``--verify`` to run
``git fsck`` over the clone as well.

Do **not** simply ``git clone`` the vault and check out.  On Windows the clone
inherits ``core.autocrlf=true`` from Git for Windows' system config, and the
vault's ``info/attributes`` -- the thing that makes it byte-exact -- is not
carried by a clone, while the source's own ``.gitattributes`` is.  The result is
a restore with LF rewritten to CRLF.  ``--restore`` applies the byte-exact
settings to the clone *before* checking out, so the files match the originals.

Usage
=====
::

    # the whole thing, from inside the project -- no arguments at all
    cd C:/code/myproject
    python tools/seafile_snapshot.py

    # what that run would do: nothing is pushed, the branch does not move, and
    # the remembered settings are left alone -- but the vault is initialised if
    # missing and the snapshot is built into its index, so the diff is real
    python tools/seafile_snapshot.py --dry-run

    # the same, said explicitly
    python tools/seafile_snapshot.py --source C:/code/myproject \
        --vault C:/code/myproject-vault \
        --remote seafile://code/myproject-vault

    # also push the code repo itself first, in one command
    python tools/seafile_snapshot.py --code-remote origin

Defaults
========
Every option has one, so a bare run is a complete run.  They are resolved most
specific first:

1. **the flag you passed** -- always wins;
2. **what the vault remembered** from its last run (its own git config, under
   ``snapshot.*``);
3. **the source repository** -- a single ``seafile://`` remote names the vault's
   destination (``seafile://code/app`` -> ``seafile://code/app-vault``);
4. **the current directory** for the source, ``'<source>-vault'`` for the vault.

A snapshot is of a whole repository, as ``git push`` is.  Run from a subfolder
of a git repository, the source becomes the repository root (with a note); an
explicit ``--source`` that is a subfolder is refused, naming the root.

So the first run is the only one that needs an argument, and every explicit flag
is written back to the vault for next time.  ``--no-push`` overrides the
remembered or derived remote *for that run* -- it pushes nothing (the vault and
the code repo alike) and leaves the vault's memory alone, so a one-off local run
does not unhook a scheduled job; ``--dry-run`` previews the run (the vault is
initialised if missing and the snapshot is built into its index) without moving
the branch, pushing, or touching the remembered settings.

The recommended exclusions are **never applied silently**.  A snapshot is a
backup, and the one failure that cannot be forgiven is a backup that quietly
omitted the file that mattered -- ``node_modules`` is reproducible, ``.env`` is
not, and this tool cannot tell them apart.  It captures everything, then points
at the reproducible bulk it noticed::

    --exclude node_modules --exclude venv --exclude .venv \
    --exclude __pycache__ --exclude '*.pyc' --exclude dist --exclude build

``--default-excludes`` adds exactly that set.  A pattern with no slash matches
that name at *any* depth, so ``--exclude node_modules`` also drops
``packages/*/node_modules``.  Quote a wildcard so your own shell does not expand
it first.

Secrets
=======
This tool will happily back up ``.env``, keys and credentials -- that is what a
*full* backup means -- and Git history is permanent.  Every run therefore lists
the captured paths that look like credentials, so the choice of remote is made
with that in view.  Push the vault to a library only you can read; never point
the vault at a public remote.

A snapshot contains exactly the files ``.gitignore`` keeps *out* of the code
repository, so ``--remote`` is refused when it names a place the source
repository already pushes to -- ``--allow-shared-remote`` overrides that
deliberately.

Guarding against mistakes
==========================
The failures this tool can cause are quiet ones, so it refuses rather than
guesses wherever a guess would be silent:

* **A vault belongs to one source.**  It records the source directory it first
  saw, and refuses a run pointing a *different* tree at it -- mixing two trees
  into one linear chain of snapshots is not something a later reader could undo.
* **Neither the source nor the vault may live in a Seafile-synced folder** --
  the sync client rewrites files underneath it and competes with git for every
  path, which is the exact problem this tool exists to avoid.  Checked at
  startup, before anything is written.  Two states make a containing library
  harmless and are let through with an explanatory note: its auto sync is
  disabled in the client, or the path is covered by that library's
  seafile-ignore.txt.  ``--allow-synced`` overrides every remaining refusal.
* **``--code-remote`` must name a real remote** of the source, so a typo fails
  before the snapshot instead of after it.
* **Flags that would be ignored are errors, not no-ops.**  ``--ref`` without
  ``--restore`` is the sharp one: a run meant to restore an *old* snapshot would
  otherwise quietly take a *new* one.
* **``--no-push`` and ``--remote`` together** are contradictory and refused.
"""
