# Operations Runbook

Recovery procedures for a Seafile-hosted Git repository that is not behaving. Each section states
the symptom, what it means, and what to do — in that order, because the temptation with a stuck
repository is to reach for a fix before knowing which failure it is.

Every command below is read-only unless it says otherwise. Nothing here requires deleting a library.

**Companion documents**: [DESIGN.md](DESIGN.md) (why the protocol behaves this way),
[SECURITY.md](SECURITY.md) (credentials), [USER_GUIDE.md](USER_GUIDE.md) (normal usage).

---

## 0. Start Here: Three Diagnostics

Run these before changing anything. They separate a credential problem from a safety-guard problem
from a repository problem, which is most of the work.

```bash
# Can the helper authenticate, and to which server and user?
git-remote-seafile check-auth

# Would a push to this URL be allowed, and why not?
git-remote-seafile check-safety seafile://code/myproject

# Round-trip test of the remote: refs, and a real fetch
git-remote-seafile test seafile://code/myproject
```

`check-auth` resolving a *different server than you expected* is the most common cause of a
confusing failure — see [SECURITY.md](SECURITY.md) on credential precedence. Note that
`SEAFILE_TOKEN` is ignored unless `SEAFILE_SERVER` is also set; a CI job that exports only the token
will fail here with a message saying so.

---

## 1. A Push Will Not Start: The Lock Is Held

**Symptom.** `git push` waits, then fails with a message naming a lock timeout, or one of the
ownership states.

**Diagnose.**

```bash
git-remote-seafile lock-status seafile://code/myproject
```

This is read-only and safe to run at any time, including while someone else is pushing. It lists the
active tickets, which one is granted, and when each lease expires.

**What the ticket tells you.**

| What you see | What it means |
|---|---|
| A ticket whose lease has expired | It should already be reclaimable; retry the push |
| A ticket held by a crashed process **on your own machine** | Reclaimed automatically — the helper checks process liveness by PID |
| A ticket from a **different machine** whose lease has not expired | That client is pushing, or crashed without its lease expiring |
| A granted ticket with no `Granted` marker | The holder predates v0.8.4, or the queue was force-cleared. The grant-marker protection is not in force for that lock |
| No tickets at all, but the push still fails | Not a lock problem — go to §2 |

**Recover.**

If the holder is gone and the lease is in the future, release it explicitly:

```bash
# Release a lock held by your own machine/account
git-remote-seafile unlock seafile://code/myproject

# Break any lock, whoever holds it — emergency recovery only
git-remote-seafile unlock seafile://code/myproject --force
```

**`--force` is not free.** It removes the fence that stops a stale writer from clobbering a live
push. Use it when you are confident the other client is dead — a machine that was shut down
mid-push, a CI runner that was killed — and not merely slow. If another client is genuinely
mid-push and you force-unlock, both pushes proceed and the fast-forward and read-back checks are
what stop the result from being silently wrong.

**Why this happens at all.** The lock is *advisory*. Seafile offers no atomic primitive to build a
real one on, so the design deliberately trades a small risk of two writers for a guarantee that a
lost race is *loud*. See [DESIGN.md](DESIGN.md) §4.4.

---

## 2. The Push Fails Without a Lock Error

> **HTTP 404 on `/api2/...` for a server you can open in a browser** is almost always a Seafile
> served under a path (`https://host/seafile/`). Set `SEAFILE_SERVER` to the prefixed URL, or
> let the desktop client's account supply it; `git-remote-seafile doctor` prints the server it
> resolved. See [USER_GUIDE.md](USER_GUIDE.md) section 7.1.

**Symptom.** `error <dst> fetch first`, `error <dst> non-fast-forward`, or
`error <dst> safety check failed: …`.

These are three different things:

- **`fetch first` / `non-fast-forward`** — normal Git semantics. Someone pushed to that branch.
  `git pull` and merge, then push again. The helper re-read the destination ref under the lock and
  again immediately before writing, so this is reported rather than silently overwriting.
- **`safety check failed`** — the pre-flight guardrails refused. The message on `stderr` names which
  check and what to do. The two common cases:
  - **Trap 1**: the working tree is inside a synced library *and* shares the path with the remote.
    Move the working tree out of the synced folder, or push to a different path.
  - **Trap 2**: the destination is inside a synced library but the subfolder is not listed in
    `seafile-ignore.txt`. Add the entry at the **library root** — not in a subfolder, and with no
    leading dot on the filename, or the desktop client will never read it.
- **`invalid ref name`** — the push names a destination that is not a valid ref. Check the refspec.

To confirm which guard tripped, without pushing:

```bash
git-remote-seafile check-safety seafile://code/myproject
```

---

## 3. Stray or Conflicting Refs

### 3.1 A ref is a file in one place and a directory in another

**Symptom.** A branch named `feature` exists, and so does `feature/auth`. On Seafile these are a file
and a directory with the same name, and one of them cannot exist.

**Cause.** `refs/heads/feature/auth` is stored as a directory `feature` containing a file `auth`.
Creating a plain branch `feature` would need `feature` to be a file.

**What the helper does.** Deleting a nested ref prunes the newly-empty parent directories up to
`refs/heads` and evicts them from the client's directory cache, so the conflict does not accumulate
on its own.

**Manual fix**, if a conflict is already present: in the Seafile Web UI, navigate to the
repository's `refs/heads/` and delete the *directory* (e.g. `feature`), then re-push the branch you
actually want. Deleting a directory removes every ref beneath it, so check what is inside first.

### 3.2 A ref points at an object that is not there

**Symptom.** `fatal: bad object HEAD`, or a clone that succeeds and then cannot check out.

**Cause.** The ref was written but the packfile carrying its objects is missing or incomplete. On
this helper the ref is written last, after the objects, so this normally means an upload was
interrupted between the two, or something removed a packfile out of band.

**Diagnose** from a machine that has a working clone:

```bash
git -C /path/to/good/clone fsck --full
```

**Recover.** A plain `git push` is **not** enough here: the remote's intact refs still cover the
missing pack's commits, so the push's exclusion set contains them, git reports "Everything
up-to-date" and nothing is re-uploaded (measured; the branch looks repaired and is not). Delete
the broken ref and push it again, which forces a pack of everything the branch names:

```bash
# from a machine that holds the objects (verify with: git fsck --full)
git push origin --delete <branch>
git push origin <branch>
```

Repeat per broken branch. If no machine has the objects, the ref must be reset back to a commit whose
objects are present.

---

## 4. Half-Uploaded or Orphaned Packfiles

**Symptom.** A fetch or clone fails while verifying an index, or compaction refuses to run.

**Cause.** A packfile upload was interrupted, leaving a `.pack` with no `.idx`, a truncated `.pack`,
or an `.idx` that does not match its pack. The remote directory is a plain folder; nothing
garbage-collects it.

**What the helper already does.** Downloaded `.idx` files are validated with `git verify-pack`; a
corrupt or truncated index is discarded and rebuilt locally with `git index-pack`. A pack whose size
disagrees with the directory listing aborts compaction rather than being used.

**Recover.** An orphaned packfile — one with no `.idx`, or whose `.idx` fails verification — is
unreferenced and safe to delete from the Seafile Web UI. Identify it by comparing the
`objects/pack/` listing against what a healthy clone expects. **Delete only a pack you can account
for**: a `.pack` that is the sole carrier of a branch's objects is your only copy, and the correct
move there is to restore it from a clone, not to delete it.

After removing orphaned files, compact:

```bash
git-remote-seafile gc seafile://code/myproject
```

Compaction refuses to delete anything until it has proven the consolidated packfiles are present at
the expected size, so a failed run leaves the repository exactly as it was.

> **Scratch space.** Compaction downloads every pack, repacks them, and re-uploads from the system
> temporary directory (`TMPDIR`, or `TEMP`/`TMP` on Windows). Budget roughly 2–3× the repository's
> packed size there. On a small `tmpfs` this fails late — and after the lock is taken, so the lock
> must be released (§1) before retrying.

---

## 5. Conflict Artifacts Inside the Repository

**Symptom.** Files named like `main (SFConflict someone 2026-10-12).json` appear inside
`refs/heads/`, `objects/pack/`, or `.git-lock.d/`, and refs stop resolving.

**Cause.** The Seafile desktop client — or the server's own concurrent-write handling — produced a
conflict copy instead of overwriting. This is the failure mode the whole project exists to avoid.
It appears when a *synced* library is used as a Git remote and something writes to it from two
places at once, and — measured against a real Seafile CE 13.0.28 — also when two *API* writers hit
one path at the same moment: every upload reports success and the server keeps the losers as
`<name> (SFConflict <user> <date>)` files beside the path (for a name with a dot the text after the first dot is
repeated after the marker: `v1.0.0` becomes `v1.0.0 (SFConflict <user> <date>).0.0` and
`release-2.1` becomes `release-2.1 (SFConflict <user> <date>).1`, measured). Through the lock this does not happen
(nine concurrent pushers produced none); a client that bypasses the lock, or an older helper that
predates it, can produce it. The helper now warns after a ref write when a conflict copy of that ref
appears, and names the copy when it lists refs.

**Recover.**

1. List `refs/heads/` in the Seafile Web UI.
2. Determine which file holds the intended commit SHA and which is the conflict copy. The real ref
   is the one named exactly `main`; a conflict copy has a decorated name.
3. Read both. If the conflict copy holds a *newer* commit you want, note its SHA.
4. Delete the conflict copy, leaving the ref holding the SHA you intend.
5. From a clone, `git fetch` and confirm the tip is what you expect.

**Prevent.** Never point a remote at a synced path. Use an unsynced library, or an ignored subfolder
of a synced one — that is what `seafile-ignore.txt` and the Trap 2 guard are for.

---

## 6. Compaction Reported a Failure After a Successful Push

**Symptom.** The push printed `ok`, then auto-GC printed an error.

**This is not data loss.** The push succeeded; compaction is housekeeping that runs afterwards. The
consolidated packfiles are verified before any old pack is removed, so a failed compaction leaves the
old packs in place and the repository readable.

**Recover.** Run it manually and read the message:

```bash
git-remote-seafile gc seafile://code/myproject
```

Common causes: insufficient scratch space (§4), the lease being lost mid-run (the run stops
cleanly and reports the deletions it did not perform), or a remote listing that could not be read.

**If it stops part-way through cleanup**, the command says so and exits non-zero, naming the reason —
normally that ownership of the lock was lost to another client mid-run. That is clutter, not damage:
the new packfiles are verified by that point, so nothing was lost, and a later compaction removes the
leftovers. The non-zero exit is deliberate. A scheduled job that reads only the exit code has to be
able to tell a finished compaction from one that left orphans behind, so this reports
`status: "incomplete"` rather than `"ok"`. Treat it as "run `gc` again later", not as "the repository
is broken".

---

## 7. A git Command Timed Out (`seafile.gittimeout`)

**Symptom.** The push, fetch or `gc` fails with a message naming the command and a number of seconds:

```text
git: pack-objects ... did not finish within 300s and was killed.
git: repack -a -d ... did not finish within 300s; aborting compaction.
```

A cut-short `cat-file --batch-check` is not fatal and shows up as a warning instead:

```text
Warning: git cat-file --batch-check did not finish within 300s; assuming no objects are present.
```

**Cause.** Every git subprocess the helper starts is bounded by a wall-clock ceiling, 300 seconds by
default. The bound exists because a git child can hang in two ways that never resolve on their own: a
wedged `index-pack` or `repack` — which during `gc` holds the remote lock open until its lease
expires and another client lawfully takes over — and a credential-helper prompt on a machine with
nobody at the keyboard.

Five minutes is generous for an ordinary repository and too short for a large one. A first push of a
few GB, an `index-pack` of one large downloaded pack, and a full `repack -a -d` during manual `gc` all
routinely exceed it. The automatic tail merge is bounded by design — only packs below
`seafile.gcmaxpackmib`, within the download budget (2 × `seafile.gcmaxpackmib`) — so it rarely reaches the
ceiling; when any of these does, the failure is deterministic (the same repository stops at the same point every
time), and with `seafile.autogc` enabled the failing run defers and is retried on the next push.

**Fix: raise the ceiling.** It is deliberately *not* a per-repository setting, because a repository
that could raise the ceiling could remove the protection it provides. Set it from a scope the
repository cannot reach — the environment, for a single command:

```bash
SEAFILE_GIT_TIMEOUT=1800 git push seafile://code/myproject
```

or the system or global git config, to make it stick for the machine:

```bash
git config --global seafile.gittimeout 1800
```

A repository-local `git config seafile.gittimeout …` is ignored on purpose, and a value that is not a
positive number is refused with a warning, so the default is kept rather than silently becoming a
zero-second ceiling. To see what is in effect:

```bash
echo "$SEAFILE_GIT_TIMEOUT"                  # empty when unset
git config --global --get seafile.gittimeout # exits 1 when unset
```

**If it still times out.** Raise it again before suspecting corruption: the operation is not failing,
it is being cut short. But a run that outlasts even a raised ceiling twice in a row is more likely to
be a wedged child than a slow one — stop retrying, capture the evidence listed under *Escalating*,
and open an issue rather than raising the number a third time.

---

## 8. A Repository Health Command (Proposed, Not Implemented)

The manual procedures above — listing refs, spotting an orphaned pack, finding a stale ticket — are
exactly what a health command should automate. A `fsck`-style subcommand that reports stray refs,
orphaned `.git-lock.d` tickets, packfiles without indexes, and refs whose objects are missing is
**proposed but not implemented**; it is recorded as a non-goal in [DESIGN.md](DESIGN.md) §7.

Until it exists, §0's three diagnostics plus a Web UI listing cover the same ground by hand.

---

## 9. Escalating

If a repository is unreadable and the procedures above did not identify the cause, capture the
following before opening an issue — it is the difference between a diagnosis and a guess:

- the output of `git-remote-seafile version`,
- the output of `git-remote-seafile doctor <url>` (login, how the URL was interpreted, safety checks, a write probe) for the affected URL — or, if you prefer the individual checks, `check-auth`, `check-safety` and `test`,
- the full `stderr` from the failing command, including any `Warning:` lines,
- a listing of the repository's `refs/heads/`, `objects/pack/` and `.git-lock.d/` from the Seafile
  Web UI,
- the operating system and the Python version (`python -V`).

**Do not paste a token.** If a command echoed one, redact it before posting.
