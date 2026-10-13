# PAYLOAD.md — specification of the snapshot tool's payload mode

**Status:** the core is implemented on the `feat/payload-mode` branch. This file is
the specification the implementation is held to: the requirements, the decisions
with the evidence behind them, the failure model, and an adversarial pass
("Attack") that records every way the design was found wanting and what became of
it. [SNAPSHOT.md section 15.1](SNAPSHOT.md) keeps the measurements and the
reasoning for the layout; this file is the contract.

Each decision carries a status: **decided** (implemented and tested), **proposed**
(recommended, not yet built) or **open** (needs a measurement or a choice).

---

## 1. Purpose and non-goals

Payload mode lets `tools/seafile_snapshot.py` capture a tree that is mostly large,
immutable, already-compressed data (rasters, archives, arrays, video) without
pushing that data through git: no second local copy, no staging pass, no pack cap,
and a snapshot that costs nothing to keep.

**Non-goals.** No encryption (a Seafile encrypted library is refused; encrypt the
volume). No client-side delta or dedup below whole files (the REST API gives no
resumable block protocol). No merge of diverging histories. No replacement for the
desktop client when the user wants a *synced* copy of a directory and no
consistency with code. Not a general backup system: restic and Borg own dedup,
encryption and retention policy.

## 2. Terms

- **vault** — the snapshot tool's own git repository whose work tree is the source.
- **payload file** — a regular file selected by the rule in D1; it never enters git.
- **object** — a payload file's content stored on the server under its SHA-256.
- **manifest** — `.seafile-payload/manifest.json` in the snapshot tree: one entry
  per payload file (path, mode, size, SHA-256, mtime).
- **snapshot** — a vault commit; its manifest names every object it needs.

## 3. Requirements

| # | Requirement | How it is checked |
| :-- | :--- | :--- |
| R1 | A restored tree is byte-identical to the source at snapshot time, payload files included. | `test_restore_downloads_and_verifies_the_payload`; real-server soak |
| R2 | A snapshot never references an object that was not verified present before the vault was pushed. | ordering in D5; `test_a_failed_parallel_upload_names_the_file_and_resumes` |
| R3 | A run never replaces an object with different bytes, and never deletes one. (It does replace a *wrong-size* object, which is damage, with the correct bytes.) | design; `test_a_file_removed_from_the_source_keeps_its_object`, `test_a_name_another_writer_just_stored_is_not_deleted_by_a_collision` |
| R4 | An unchanged tree uploads nothing and commits nothing. | `test_an_unchanged_tree_uploads_nothing_and_commits_nothing` |
| R5 | A rename or duplicate uploads nothing. | `test_a_rename_uploads_nothing`, `test_duplicate_content_is_one_object` |
| R6 | A file that changed between hashing and upload is never stored under another content's name. | `test_a_file_that_changes_between_hash_and_upload_is_refused` |
| R7 | A restore never writes outside its destination, whatever the manifest says. | `test_a_hostile_manifest_cannot_write_outside_the_restore` |
| R8 | Corruption of a stored object is detected at restore, never accepted. | `test_corrupt_payload_fails_the_restore` |
| R9 | A run that cannot read a file stops and names it; nothing is skipped silently. | `test_an_unreadable_file_names_itself` |
| R10 | A payload vault never silently starts staging its large files into git. | `test_a_vault_that_used_payload_refuses_a_run_without_the_flag` |
| R11 | Payload mode is opt-in and changes nothing for a run without the flag. | the existing snapshot suite |

## 4. Decisions

### D1. Which files are payload — **decided** (`auto`)

`--payload-min-size` takes `auto` (also what a bare flag means) or a size. A size
selects on size alone. **`auto` selects a file when it is at least 64 MiB, or when
it is at least 4 MiB and a sample of it does not compress** (zlib level 1 over three
256 KiB chunks, ratio ≥ 0.9). Everything else stays in git, where batching,
compression and delta apply. The manifest records which rule produced it, and a
vault that used payload mode refuses a run without the flag, naming the value.

Why those numbers (flood tree, 6,853 files, 130.7 GiB):

| size | files | GiB | typical content |
| :--- | ---: | ---: | :--- |
| < 64 KiB | 3,603 | 0.05 | json, xml, small images |
| 64 KiB – 1 MiB | 1,804 | 0.49 | json, xml, jpg |
| 1 – 4 MiB | 542 | 1.16 | json, jp2 |
| 4 – 16 MiB | 217 | 1.51 | jp2, tif, npy |
| 16 – 64 MiB | 335 | 9.41 | npy, jp2, tif |
| ≥ 64 MiB | 352 | 118.09 | tiff, npy, zip, jp2 |

A payload object costs what a git blob in a pack does not: one request (measured
~34 ms for a tiny file), one library-history entry, and a whole new object when the
file changes instead of a delta. It saves the local copy, the staging pass and the
pack cap. Request overhead is roughly 100% of transfer time at 1 MiB and 25% at
4 MiB, hence the lower bound. The feature exists for data git cannot shrink, which
the sample detects: of the 12.1 GiB between 1 and 64 MiB, about 7.0 GiB is
incompressible and 4.6 GiB compresses (json, xml) and stays in git. A file whose
class changes between runs simply moves (A14).

The bounds are constants (`_AUTO_UPPER`, `_AUTO_LOWER`) and the sample ratio is
`_AUTO_INCOMPRESSIBLE`; none is a setting. *Caveat:* a bare `--payload-min-size`
swallows the next argument, so write the source first or `--payload-min-size=auto`.

### D2. Object identity and layout — **decided**

An object is `<vault path>/payload/objects/<sha256[:2]>/<sha256>`; its content is
exactly the file's bytes. The name is the identity: same bytes, same name, on
every machine, forever. A two-hex fan-out bounds any directory to a few thousand
entries. No other file names appear on the server, so no server name rule (NFC
normalisation, `"` → `%22`, 255 bytes) can affect a payload file.

### D3. Location — **decided**

Inside the vault's own remote directory, so one URL names both. Measured: the
helper's push, clone, `doctor`, `gc --tail` and full `gc` ignore a foreign
`payload/` directory. *Open:* an option to put objects in a *different library*
(quota, or a library nobody syncs); not needed yet.

### D4. Manifest — **decided** (format 2)

One line of ASCII JSON, keys sorted, no trailing newline (so no text-mode pipe or
filter can alter it). Fields: `format`, `snapshot` (a digest of the entries),
`min_size`, `files[]` of `{path, size, mode, sha256, mtime_ns}`. `mode` is
`100644` or `100755`. A reader must reject an unknown `format` greater than the one
it knows. *Open:* manifest growth — a single blob of ~150 bytes per file is
~15 MB at 100,000 files, stored in full for every snapshot that changes any file;
git's binary delta should absorb most of it, but this is unmeasured.

### D5. Ordering and failure model — **decided**

1. hash (D6); 2. list the fan-out directories the hashes fall in; 3. upload what is
missing or the wrong size (largest first, `--payload-jobs` at once), **each object
under a temporary name `<sha>.part-<token>`, through a reader that hashes the bytes
as they are sent, renamed to `<sha>` only if the digest is the name**; 4. list again
and require **every** object the manifest names, at its size; 5. build the tree,
commit, and — **immediately before the push** — re-list every object named by every
manifest in `remote_tip..tip`, re-uploading any that went missing from the source
(if it is unchanged) or refusing the push, naming the snapshot; 6. push.

Consequences: an object's name is always the hash of its content (R2, R6) -- even
when a file is rewritten in place without touching its mtime, which a size-and-mtime
comparison cannot see; a vault never names an object that was not verified present
at push time; an interrupted run leaves objects no snapshot names (and possibly a
`.part-` file, which retention ignores), reused by the next run; the first failed
upload stops the queue and is reported once the running uploads finish. The server
cannot report a content hash, so a listing proves only presence at a size;
**restore always verifies SHA-256** (R8).

### D6. Change detection — **decided** (clock-domain gap documented)

Hashes are cached **per machine**, in the vault's git directory
(`payload-cache.json`), never read from a manifest: a manifest comes from a remote
anyone with write access could have written. An entry is keyed by path and trusted
only while size, mtime **and inode** are unchanged (a rename keeps size and mtime,
and tiles extracted from one archive share both), the mtime is not a whole second
(a coarse clock), and the mtime is older than the moment this machine hashed the
file by 2 s (git's "racily clean" rule). A file is compared with its recorded size
and mtime before upload, and its bytes are hashed as they are sent (D5), so a
modification the cache missed cannot put wrong bytes under a name.

*Known limits:* the racy rule compares the file's mtime with this machine's clock,
which is the right domain for a local disk and not for a network share whose clock
differs; a file with a future mtime is rehashed on every run (a cost, not a risk).
There is no force-rehash switch yet.

### D7. Concurrency — **decided**

Within one machine the vault lock serialises runs. Between machines, objects are a
union: two machines storing the same object write identical bytes under one name,
which cannot conflict in a meaningful way (measured: same-*path* concurrent writers
produce `(SFConflict ...)` copies; a copy of an object is an extra file whose name
is not a hash and is ignored and collectable). The vault chain is arbitrated as
before (SNAPSHOT.md section 6).

### D8. Restore — **decided**

Clone and check out the snapshot, read its manifest, **validate it**, then for each
entry download the object to `<name>.part`, verify size and SHA-256, and rename into
place. Two classes of problem, handled differently:

- **Security** refuses the whole manifest (R7): a path that is empty, absolute,
  or contains `.`, `..` or an empty component (on Windows, split on `\` too); that
  reaches `.git` in any spelling (case, trailing dots and spaces, `git~1`) -- a
  `core.fsmonitor` or a hook there runs code on the next `git status` -- or the
  reserved `.seafile-payload` directory; that resolves outside the destination
  (a symlink the tree holds); a hash that is not 64 lowercase hex digits; a bad
  size, mode or mtime; a `format` this tool does not know.
- **Representability** skips and reports the entry, restores the rest, and then
  fails: a name Windows cannot hold (forbidden characters, trailing dot or space,
  reserved device names), a path something is already at (git's own file, or a
  name the filesystem treats as another entry's -- nothing is ever overwritten), a
  download or verification that fails after three tries. A rerun into the same
  directory keeps files that verify.

### D9. Retention — **decided** (`--payload-gc`)

Nothing in a snapshot run deletes. `--payload-gc --remote URL [--vault DIR]`
reports; with `--payload-delete` it **marks, and later deletes, in two phases**:

- the first run records, in `payload/gc-pending.json` on the server and by this
  machine's clock, every object no snapshot references; a later run deletes an
  object only if it has been recorded unreferenced for `--payload-grace-days`
  (default 7, minimum 1) **and is still unreferenced**. An object a run reused in
  the meantime is referenced again when the second run looks, so it survives;
  nothing depends on the server's timestamps;
- it reads the manifest of **every commit reachable from every branch and tag of the remote**
  (fetched into a scratch repository), plus the local vault's refs when `--vault`
  is given -- which must be a vault, and whose fetch must succeed -- because an
  un-pushed local snapshot may name objects already uploaded;
- a manifest it cannot read, or of a format it does not know, stops it: "cannot
  parse" is never "references nothing";
- Seafile conflict copies under `refs/` stop it (a snapshot reachable only from one
  is invisible to git);
- it refuses to act when it found no manifest at all;
- it deletes only names that are exactly 64 hex digits (temporary `.part-` files and
  strangers are reported and left alone);
- it reports any object a manifest names that is **missing** from the server.

Deleting frees quota at once; disk is freed only after the library's history
expires and the server's GC runs (measured). Recommended library setting: a short
history (days), as a safety net for a mistaken delete.

### D10. Views — **proposed**

`--payload-view <snapshot-ref>` builds, on demand, a directory of real file names
by server-side copy and rename of objects (about two API calls per file, no data
moved). Names go through the server's rules, so a view reports files it cannot
name (over 255 bytes) rather than failing. A view is a convenience: deleting it
loses nothing.

### D11. Flags — **decided**, remembered setting **open**

`--payload-min-size [SIZE|auto]` (`0` = off; a bare flag means `auto`),
`--payload-jobs N` (default 3; measured: one upload 32 MiB/s, three 53 MiB/s, five
no better), `--payload-gc`, `--payload-delete`, `--payload-grace-days N`.
Combinations that would be silently ignored are refused. Incompatible with
`--no-push`. A vault whose last snapshot has a manifest refuses a run that omits
the flag (R10), naming the value. The `.seafile-payload` directory of a source is
never staged. *Open:* remembering the setting in the vault the way the remote and
excludes are, so a scheduled bare command keeps working.

### D12. Privacy and security — **decided**

Objects are readable by anyone with read access to the library; names reveal
nothing, but equal files are visibly equal. Payload paths are checked against the
same credential-name patterns as staged paths and reported. The manifest is data
from the network and is validated before use (D8). The restore never executes or
interprets file content.

## 5. Interactions with Seafile

- **Quota** counts live objects at logical size; deleting an object frees quota
  immediately. **History**: one library commit per upload or delete (measured).
- **Disk**: a deleted object's blocks remain while history references them and
  until GC runs. With history set to *no history*, online GC on the one library
  freed 33.8 GiB within seconds (measured).
- **Desktop client**: a library that holds objects downloads each once; there is
  no per-snapshot duplication. Names are hex, so no Windows-client name problem.
- **DESIGN.md section 2** (why `.git` cannot live in a synced library): all of it
  concerns the desktop client watching a working tree. Payload mode has no such
  client in the path, and refuses a source or vault in a synced folder.

## 6. Attack

An adversarial pass over the design: each line is a way it could lose data, leak
data, or surprise. Disposition is one of **fixed**, **handled**, **specified**
(a requirement above now covers it), or **open**.

| # | Attack or failure | Finding | Disposition |
| :-- | :--- | :--- | :--- |
| A1 | A manifest with `../../x` or `/etc/x` (a library writer alters it) | the first implementation wrote wherever the manifest said | **fixed**: D8 validation, R7 |
| A2 | A manifest hash like `../../other` becomes part of a server path | same | **fixed**: hash must be 64 hex |
| A3 | File edited after hashing, before upload | bytes under another content's name | **fixed** (the first handling was wrong, see 6.1): D5 hashes the bytes as they are sent |
| A4 | Same-size edit within the timestamp granularity | cached hash would be trusted | **handled**: D6 racy rule against this machine's hash time, plus inode |
| A5 | mtime restored by a copy tool (`cp -p`, `robocopy`) | cache trusted, content differs | **open**: force-rehash switch |
| A6 | Another machine's un-pushed snapshot references an object; retention deletes it | data loss | **handled within the grace period**: D9 two-phase, every branch and tag of the remote plus the local vault's (which must be one). Another machine's objects that are uploaded but not pushed are invisible to this machine's retention; they survive `--payload-grace-days`, after which that machine's next run re-uploads them (source unchanged) or refuses to push. Run retention from one machine |
| A7 | Run interrupted after upload, before push | orphan objects | **handled**: harmless, reused (D5) |
| A8 | An object is truncated or altered on the server, same size | upload skipped as "present"; backup silently bad until a restore | **open**: only a restore drill detects it; the tool no longer creates this state itself (D5); consider `--payload-verify` sampling |
| A9 | `(SFConflict ...)` copies appear next to an object | listing sees a non-hash name | **handled**: ignored; collectable |
| A10 | Two paths differ by case, Unicode form, or a trailing dot or space; restore onto Windows or macOS | one overwrites the other, both verify | **fixed**: D8 never overwrites; collisions are skipped and reported |
| A11 | Payload path looks like a credential | the secrets report covered only staged paths | **fixed**: payload paths scanned |
| A12 | File held open exclusively (Windows) | traceback or silent skip | **handled**: R9 |
| A13 | OneDrive "files on demand" placeholder | hashing triggers a download of the whole file | **handled**: a placeholder is refused by name, and reparse points are not walked |
| A14 | The threshold changes between runs | files move between git and payload | **handled**: staging skips payload paths; old snapshots unaffected; flag is required (R10) |
| A15 | A payload file becomes a symlink or shrinks below the threshold | falls to git | **handled** |
| A16 | `--reset-to-remote` drops local-only snapshots | their objects become orphans; the previous-manifest cache disappears | **handled**: harmless; a rehash |
| A17 | Thousands of objects in one fan-out directory | listing cost | **handled**: 256 directories; list only those touched |
| A18 | Manifest of 100,000 files committed each run | repository growth | **open**: measure delta compression (D4) |
| A19 | Disk full or Ctrl-C during restore | partial tree | **specified**: restore needs an empty destination; rerun elsewhere |
| A20 | Sparse files, alternate data streams, hard links | sparse read as zeros; ADS dropped; hard links become copies | **specified**: byte content only; documented limit |
| A21 | Read-only attribute, ACLs, ownership | not preserved | **specified**: mode bits and mtime only |
| A22 | Clock jumps or future mtimes | a future mtime is never "older than the commit" | **handled**: always rehashed (safe) |
| A23 | An encrypted library | the client refuses it | **handled** |
| A24 | A server that rewrites object names | would break "listing proves presence" | **handled**: names are lowercase hex; verified on the real server |
| A25 | `--payload-jobs` very large on a flaky link | many partial uploads | **handled**: pool is bounded; failure stops the queue |

### 6.1 The independent review rounds (2026-10-13 and 14)

Three reviewers (one given only this specification, two the code) attacked the
branch independently. Every finding below was reproduced against the stub before
it was accepted; two claims were refuted by execution and dropped. The test that
now holds each fix was checked by deleting the fix and watching it fail (12
one-line mutations, none survive).

| Finding (reviewer ids) | Consequence | Fixed by |
| :--- | :--- | :--- |
| A file rewritten in place with the same size and mtime puts other bytes under a hash name; later copies of the genuine bytes share the bad object (F3, F1) | LOSS | D5: hash while sending, temporary name, rename |
| GC deletes an old object a run has just reused (F1, F2, F3) | LOSS | D9 two-phase; D5 pre-push re-verification |
| A snapshot whose push failed, then GC elsewhere, then two snapshots pushed together (F2) | LOSS | D5 re-verifies every manifest in `remote_tip..tip` |
| Renumbered same-size, same-mtime files restore with their predecessors' contents, "verified" (F4) | WRONG | D6 per-machine cache keyed by inode |
| A remote-written manifest hash is trusted as a cache and spliced into a server path (F2) | WRONG / injection | D6 cache never read from a manifest |
| A manifest entry `.git/config` is written into the restored clone and runs on `git status` (F5, F1) | code execution | D8 refuses `.git` in any spelling and the reserved directory |
| An unknown manifest format makes GC and restore treat it as "names nothing" (F8, F3, F4) | LOSS | D4 / D9: format and shape are checked everywhere |
| Windows drops a trailing dot or space and merges two entries; case-insensitive merges (F6, F5) | WRONG | D8: never overwrite; Windows names are skipped and reported |
| One unrepresentable name aborts the whole restore on every platform (F7, F8) | SURPRISE | D8 skip-and-report, restore the rest |
| A bad or mistyped `--vault` is silently ignored by GC (F11, F4, F13) | LOSS | D9: must be a vault, fetch must succeed |
| Seafile conflict copies of refs hide a snapshot from GC (suspicion) | LOSS | D9 stops when any exist |
| A restored payload tree reused as a source breaks a plain vault (F6, F7) | SURPRISE | the reserved directory is never staged |
| Windows junctions are walked, so a junction to a drive root hashes the drive (F7) | WRONG | the walk skips reparse points |
| Server and network errors, bad manifests and odd inputs escape as tracebacks (F9, F8) | SURPRISE | wrapped; restore catches per entry |
| `parse_size("-1")` selects every file; a bare flag swallows a directory name (F10, F14) | SURPRISE | strict sizes; a digits-only value that is a directory is questioned |
| Partial files left at their real name after a failed download (F14) | SURPRISE | `.part` and rename; reruns keep verified files |
| OneDrive placeholders are hydrated by reading (A13) | SURPRISE | refused by name |
| Ctrl-C does not stop queued uploads (F13) | refuted by execution | none needed; `Executor.map` cancels |

**Round 2** (three more reviewers, against `ce2f573`; no CRITICAL, one reviewer
found nothing above NIT). Each fix below was again verified by deleting it and
watching a named test fail (9 further mutations, none survive), and the
rename-based upload was re-run against the real server.

| Finding | Consequence | Fixed by |
| :--- | :--- | :--- |
| A retried chunk is not re-hashed, so an edit between attempts is hidden behind the first attempt's digest | LOSS (narrow) | D5: a re-read marks the object; it must then pass a full rehash equal to its name with an unchanged stat |
| Retention read the local vault's branches but not its tags | LOSS | D9 fetches tags of the vault too |
| A server listing with a non-integer size raised a traceback | SURPRISE | unknown size is "re-upload" / "not deletable" |
| A failure constructing a worker's client escaped the pool | SURPRISE | inside the failure path |
| `.part-` temporaries of interrupted uploads were never collected | disk | D9 collects them through the same two phases |
| `--code-remote` was pushed before the flagless-vault refusal | SURPRISE | the local branch tip is checked first |
| `.seafile-payload.` (trailing dot) was not caught as the reserved directory | WRONG | same spelling rules as `.git` |
| A rename collision deleted a good object another writer had just stored | NIT | an object of the right size is kept, the temporary dropped |
| The pre-push re-upload was not re-listed | NIT | it is |
| Restored mode and mtime, the post-upload listing check, unparseable JSON in history | test gaps | tests |

*Not changed, recorded:* a retention run's grace clock is the clock of the machine that
marked the object, so two machines that both run retention shift the effective window by
their skew (bounded by the one-day floor); a non-UTF-8 name from a POSIX source cannot be
restored on Windows; `SeafileClient.copy_entries` was removed until views need it.

*Not changed, recorded (round 1):* the racy rule uses this machine's clock (D6, F9/F15); a
snapshot is never expired by itself -- there is no `--keep-snapshots` yet, so a
file that changes daily grows the store until history is rewritten; a non-UTF-8
file name on Linux is not handled by the staging path (a pre-existing limit).

## 7. Test matrix

`tests/test_snapshot_payload.py` drives the real tool and helper against the
in-process stub. Each fix in 6.1 was verified by mutation: a one-line
change that removes it makes a named test fail (12 of 12). The real-server soaks
cover a 1.1 GiB chunked upload, unchanged run, same-size edit, restore and
older-snapshot restore, `auto` selection and retention, and the 131 GiB flood
tree end to end. Not yet covered: views (D10), a force-rehash switch (D6), a
remembered setting (D11), manifests at 100,000 files (D4), and a macOS or Linux
non-UTF-8 run.

## 8. Order of work

1. ~~Decide D1 (selection rule).~~ Done: `auto`.
2. ~~Retention helper (D9).~~ Done.
3. The 131 GiB flood run, as the acceptance test.
4. Remembered setting and force-rehash (D11, D6).
5. Views (D10).
