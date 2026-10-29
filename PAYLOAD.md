# PAYLOAD.md — specification of the snapshot tool's payload mode

**Status:** shipped in 0.9.0 and revised through 0.9.4 (the modification-times
record, D14, is 0.9.2). This file is the specification the implementation is held to: the requirements, the decisions
with the evidence behind them, the failure model, and an adversarial pass
("Attack") that records every way the design was found wanting and what became of
it. [SNAPSHOT.md section 15.1](SNAPSHOT.md) keeps the measurements and the
reasoning for the layout; this file is the contract.

Each decision carries a status: **decided** (implemented and tested), **proposed**
(recommended, not yet built) or **open** (needs a measurement or a choice).

---

## 1. Purpose and non-goals

Payload mode lets `git seafile-snapshot` capture a tree that is mostly large,
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
| R2 | A snapshot never references an object that was not verified present before the vault was pushed. | ordering in D5; `test_every_object_named_is_rechecked_just_before_the_push` (asserts the object is present *at the moment the ref is pushed*), `test_a_failed_parallel_upload_names_the_file_and_resumes` |
| R3 | A run never replaces an object with different bytes, and never deletes one. (It does replace a *wrong-size* object, which is damage, with the correct bytes.) | `test_a_file_removed_from_the_source_keeps_its_object`, `test_a_name_another_writer_just_stored_is_not_deleted_by_a_collision`, `test_a_rename_onto_a_taken_name_makes_a_copy_and_it_is_judged` (the server's rename is create-or-duplicate: measured on CE 13.0.28; the wrong-size branch and the leave-a-right-sized-object-alone branch are both pinned there), `test_a_damaged_object_no_longer_blocks_snapshots_through_the_stub` |
| R4 | An unchanged tree uploads nothing and commits nothing. | `test_an_unchanged_tree_uploads_nothing_and_commits_nothing` |
| R5 | A rename or duplicate uploads nothing. | `test_a_rename_uploads_nothing`, `test_duplicate_content_is_one_object` |
| R6 | A file that changed between hashing and upload is never stored under another content's name. | `test_a_file_that_changes_between_hash_and_upload_is_refused` (the stat check), `test_bytes_that_differ_from_the_hash_are_never_named_after_it` (the digest of the bytes sent) |
| R7 | A restore never writes outside its destination, whatever the manifest says. | `test_manifest_paths_that_could_escape_or_run_code_are_refused` |
| R8 | Corruption of a stored object is detected at restore, never accepted. | `test_corrupt_payload_fails_the_restore_and_leaves_nothing_behind` |
| R9 | A run that cannot read a file *or a directory* stops and names it; nothing is skipped silently. (git only warns for a directory, so the tool reads that warning.) | `test_an_unreadable_file_names_itself`, `test_a_directory_that_cannot_be_read_stops_the_run_and_is_named` (POSIX permissions) |
| R10 | A payload vault never silently starts staging its large files into git. | `test_a_vault_that_used_payload_still_refuses_when_it_cannot_stay_in_it`, `test_a_fresh_vault_on_another_machine_follows_the_default` |
| R11 | Payload mode is on by default, and a tree with no qualifying file is snapshotted exactly as without it (no manifest, no server directory; since 0.9.2 the tree also holds the times record `.seafile-payload/mtimes.json`, which is not part of payload mode); where it cannot work (no remote, a remote that is not `seafile://`, `--no-push`) the default stays off instead of failing. | `test_a_tree_with_nothing_big_is_snapshotted_as_without_payload_mode`, `test_the_default_stays_off_where_payload_mode_cannot_work` |

## 4. Decisions

### D1. Which files are payload — **decided** (`auto`)

`--payload-min-size` takes `auto` (the default; also what a bare flag means) or a size. A size
selects on size alone. **`auto` selects a file when it is at least 64 MiB, or when
it is at least 4 MiB and a sample of it does not compress** (zlib level 1 over three
256 KiB chunks, ratio ≥ 0.9). Everything else stays in git, where batching,
compression and delta apply. The manifest records which rule produced it, and a
vault that used payload mode refuses a run that would have to turn it off by
accident, naming the value.

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

The bounds are constants (`_AUTO_UPPER`, `_AUTO_LOWER`). The sample ratio defaults to
`_AUTO_INCOMPRESSIBLE` (0.9) and is tunable with `--payload-auto-ratio R` (0 < R ≤ 1),
remembered like the payload value. A lower R sends moderately compressible data to payload
instead of deflating it through git, which pays when deflate (about 48 MiB/s per core on
the measuring machine) is slow next to the link, and gives up git's delta between
snapshots. Whether it pays depends on the machine and the link, so the tool measures
both once, for a **new vault** (no earlier snapshot, nothing remembered) whose git-held
4-64 MiB band is a gigabyte or more: it deflates real chunks of that band, uploads four
16 MiB probe files (one, then three at once) directly under `payload/objects/` (outside
the 256 fan-out directories every payload tool reads, so none of them sees one) and
deletes them, and
takes the break-even `single * (1/upload + 1/hash - 1/deflate)` kept between 0.5 and 0.9
(`tuned_ratio`). The result, with the two rates, is remembered in the vault
(`snapshot.payloadratio`, `snapshot.uplink`, `snapshot.uplink1`), so the choice is made
once. An established vault is never re-tuned on its own, and neither is a vault whose
remote already holds payload objects, because two machines on different cut-offs would
move files between git and payload on alternate runs; `--payload-auto-ratio measure`
measures again on request, and a probe that fails leaves 0.9. A `--dry-run` measures
nothing and writes nothing. Every `auto` run prints a `placement:` line and, when a cut-off
of 0.5 would save a minute or more at the measured (or, until measured, assumed) rates,
a `Hint:` line that changes nothing.
*Caveat:* a bare `--payload-min-size`
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
it knows. *Measured (2026-10-14, synthetic; 2026-10-26 at scale, T11):* a manifest of
100,000 files is ~19 MB (about 190 bytes an entry) and ~6.2 MB in a pack. At
scale: 500,000 files are ~78 MB raw and 1,000,000 files are ~156 MB raw, parsed
in ~6 s with a ~600 MB transient peak -- survivable on a desktop, and the point
where `--exclude`ing the bulk starts to matter. The retention mark list is
~82 bytes an entry, so the helper's 16 MiB text-reader cap bites at ~205,000
marked objects; `--payload-gc` before then keeps the list small. After a
`repack -ad`, a snapshot that changes 0.1% of the entries adds ~10 kB, 1% adds
~110 kB, and one that changes every entry (the hash and mtime of each) adds
~6 MB, because the new hashes are incompressible. git's delta absorbs the
unchanged lines, so growth follows what changed, not the size of the tree. The
cost is a full-manifest rewrite only when every file changed.

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
`.part-` temporary, which retention collects through the same two phases as
unreferenced objects), reused by the next run; the first failed
server upload stops the queue and is reported once the running uploads finish, whereas a file
that changed or vanished after it was hashed does not -- the rest still go up and the run then
fails naming every such file, so one live file does not cost the others. A tree that is always being written (a database, a mailbox) would still fail every run, so `--payload-skip-changing` makes the run drop such a file from the snapshot instead (named in a `Warning:`, and absent from that manifest, whose derived id is recomputed); the previous snapshot keeps its last version, and the next quiet run captures it again. It is opt-in: the default stays an error. The server
cannot report a content hash, so a listing proves only presence at a size;
**restore always verifies SHA-256** (R8).

### D6. Change detection — **decided**

Hashes are cached **per machine**, in the vault's git directory
(`payload-cache.json`, saved even when the upload that follows fails: hashing is the long local half), never read from a manifest: a manifest comes from a remote
anyone with write access could have written. The file is stamped with the
writing machine's identity, and a reader on another machine -- the vault can
be shared, its clock and share-clock probe belong to the writer -- hashes
everything again and says so; a cache an older tool wrote without the stamp is
adopted as before. An entry is keyed by path and trusted
only while size, mtime **and inode** are unchanged (a rename keeps size and mtime,
and tiles extracted from one archive share both), the device and the inode change
time (`ctime`) are the ones recorded -- so an in-place edit that puts the old mtime
back, which still moves `ctime`, is a new file (on Windows `st_ctime` is the *creation*
time and never moves; the key there is NTFS's own change time, read with
`GetFileInformationByHandleEx`, which the 0.9.2 review round showed is the only field
that does) -- and the mtime is older than the
moment this machine hashed the file by 2 s (git's "racily clean" rule; 4 s for a
whole-second mtime, a coarse clock, rather than never trusting such a file, which
re-read a FAT or HFS+ tree on every run). A file on a different device than the
source root, or anywhere on a share whose clock could not be probed, needs a margin
of an hour instead. A file is compared with its recorded size
and mtime before upload, and its bytes are hashed as they are sent (D5), so a
modification the cache missed cannot put wrong bytes under a *new* name. It cannot
catch the case where the object is already stored: nothing is sent, so a change the
cache missed there records the old hash, and a restore returns the old bytes.
The key above is what stands between that and a wrong restore; `--payload-rehash`
is the audit.

*Clock domain.* An mtime is written by the clock of the machine that owns the
filesystem, so "the moment it was hashed" is recorded in that clock, not this
machine's. On a network share (a UNC path or a mapped remote drive on Windows; an
NFS, CIFS/SMB, 9p, sshfs, davfs or rclone mount on Linux) the tool creates and
removes one probe file in the source root to read the share's clock, stores the
offset's effect in each cache entry (`fs`), and trusts only entries made that way.
A share *ahead* by under 5 s is treated as in step (that only costs a rehash), and one *behind* by more than 0.25 s is applied (ignoring it is the unsafe side; the racy window is 2 s, so a wider floor let a 4 s skew through); a share that cannot be written to is
reported once per vault and given the one-hour margin above instead of the 2 s one.
A local source tree is never written to; a share's source root gets the probe file
(removed at once, swept by the next probe if a run was killed, and never
captured). *Known
limit:* a mount type this list does not know is treated as local, so the
rule there is the machine-clock one; `--payload-rehash` is the audit. (macOS is
read from `mount`: smbfs, nfs, afpfs and webdav.) A file with a future mtime is
rehashed on every run (a cost, not a risk). A filesystem with no change time of
its own — the FAT family keeps one timestamp in the directory entry, and a
volume or API that answers the change-time query with the modification time
does the same — cannot move the key on an in-place edit either, so such a file
(measured on vfat: `ctime == mtime` to the nanosecond) is trusted only after an
hour, the same margin an unprobed share gets, and the run says how many. On
Windows, bytes changed through a memory-mapped view leave size, mtime *and*
NTFS's change time untouched (measured, 90 s after unmap) — no stat-based key
can see that; `--payload-rehash` (payload files) and `--rebuild-index`
(git-stored files) are the audits, and a writer like that should be excluded or
stopped.

### D7. Concurrency — **decided**

Within one machine the vault lock serialises runs. Between machines, objects are a
union: two machines storing the same object write identical bytes under one name,
which cannot conflict in a meaningful way (measured: same-*path* concurrent writers
produce `(SFConflict ...)` copies; a copy of an object is an extra file whose name
is not a hash, so retention ignores it and does not collect it: delete it by hand, it costs quota at full size). The vault chain is arbitrated as
before (SNAPSHOT.md section 6).

### D8. Restore — **decided**

Clone and check out the snapshot, read its manifest, **validate it**, then for each
entry download the object to a temporary file under `.git/grs-restore/` (never
beside the destination: a real file named `<name>.part` that git had just restored
was overwritten and renamed away), verify size and SHA-256, and rename into place.
The stream is cut off the moment it passes the size the manifest declares, so a
hostile manifest cannot fill the disk before the sizes are compared. Two classes of
problem, handled differently:

- **Security** refuses the whole manifest (R7): a path that is empty, absolute,
  or contains `.`, `..` or an empty component (on Windows, split on `\` too); that
  reaches `.git` in any spelling (case, trailing dots and spaces, `git~1`) -- a
  `core.fsmonitor` or a hook there runs code on the next `git status` -- or the
  reserved `.seafile-payload` directory (including through a link the tree holds:
  `tool -> .git` with `tool/hooks/pre-commit`; and names HFS+ ignores, such as
  `.git<U+200C>`); that resolves outside the destination (a symlink the tree
  holds); a hash that is not 64 lowercase hex digits; a bad
  size, mode or mtime; a `format` this tool does not know.
- **Representability** skips and reports the entry, restores the rest, and then
  fails: a name Windows cannot hold (forbidden characters, trailing dot or space,
  reserved device names), a path something is already at (git's own file, or a
  name the filesystem treats as another entry's -- nothing is ever overwritten), a
  download or verification that fails after three tries. The payload step keeps
  files that already verify, but the command refuses a non-empty destination
  (A19), so a rerun goes to a new directory.

### D9. Retention — **decided** (`--payload-gc`)

Nothing in a snapshot run deletes. `--payload-gc --remote URL [--vault DIR]`
reports; with `--payload-delete` it **marks, and later deletes, in two phases**:

- the first run records, in `payload/gc-pending.json` on the server and by the
  server's clock (the `Date` estimate, which does not jump with either
  machine's wall clock), every object no snapshot references; a later run deletes an
  object only if it has been recorded unreferenced for `--payload-grace-days`
  (default 7, minimum 1) **and is still unreferenced**. An object a run reused in
  the meantime is referenced again when the second run looks, so it survives;
  entries written by older versions carry that machine's local-clock stamp,
  and the one-off skew between it and the server is the same bounded quantity
  §6.1 records below. A mark list that is not a JSON object stops the run, and a
  mark whose time cannot be one this tool wrote (zero, negative, more than a day
  ahead, not a number) is marked afresh with a `Warning:` -- "now minus a stamp
  of -1" would otherwise call the grace period over at once;
- it reads the manifest of **every commit reachable from every branch and tag of the remote**
  (fetched into a scratch repository, with `SEAFILE_STRICT_REFS=1` so the helper's
  listing fails rather than skip a ref it cannot read -- a skipped branch would make
  its objects look unused: an entry of an unknown type, a name git refuses, a ref
  nested deeper than the walk goes and a ref file that holds no object id all stop
  it; a Seafile conflict copy is refused separately, below), plus the local vault's refs when `--vault`
  is given -- which must be a vault, and whose fetch must succeed -- because an
  un-pushed local snapshot may name objects already uploaded. The vault is read
  where it lies (its object store is an alternate of the scratch repository, so
  the fetch copies nothing); the remote's packs are downloaded, so the run is
  refused first when the temporary directory's volume cannot hold them;
- **immediately before the first deletion it lists the remote's refs again** and stops
  with nothing deleted if they differ from the ones it decided from: a snapshot pushed
  in the interval may name an object that was unused when the history was read;
- a manifest it cannot read, or of a format it does not know, stops it: "cannot
  parse" is never "references nothing";
- Seafile conflict copies under `refs/` stop it (a snapshot reachable only from one
  is invisible to git);
- it refuses to act when it found no manifest at all;
- it deletes only 64-hex names and `<64 hex>.part-<token>` temporaries: the temporaries of
  interrupted uploads go through the same two phases as unreferenced objects, while strangers
  (names that are neither) are reported and left alone;
- it reports any object a manifest names that is **missing** from the server.

`--payload-keep N` is the expiry policy: only the newest N snapshots of each branch
and tag count as using objects, so older snapshots' exclusive objects become
unused and go through the same two phases (those snapshots then fail to restore,
naming the missing object). Without it every snapshot is kept for ever.

Deleting frees quota at once; disk is freed only after the library's history
expires and the server's GC runs (measured). Recommended library setting: a short
history (days), as a safety net for a mistaken delete.

### D10. Views — **decided** (`--payload-view`)

`--payload-view[=REF] --remote URL [--payload-view-name NAME] [--payload-view-replace]`
builds `<vault path>-view/<NAME>` (default NAME: the snapshot id) in the same
library: for each manifest entry the object is copied server-side into the right
directory, keeping its hash name, and renamed to the file's name (two calls per
file, no data moved, and measured to add no blocks -- but the library's quota and size count each copy at its full size, so a view of a large snapshot needs that much quota; the run warns when the account's quota is short and says so in its closing line). It refuses when any object is
missing, refuses an existing view unless `--payload-view-replace`, and reports
files the server cannot name (a component over 255 bytes, or two names that
collapse to one) instead of failing. A view is a convenience: no mode, no mtime,
the server's name rules apply, and deleting it loses nothing. `--restore` is the
faithful path. It lives beside the vault, never inside the helper's directory.

### D11. Flags — **decided**

`--payload-min-size [SIZE|auto]` (`0` = off; a bare flag means `auto`), `--no-payload`
(off, remembered), `--payload-auto-ratio R|measure` (the `auto` sample cut-off, default 0.9,
remembered; measured once for a new vault),
`--payload-jobs N` (default 3; measured: one upload 32 MiB/s, three 53 MiB/s, five
no better), `--payload-gc`, `--payload-delete`, `--payload-grace-days N`.
Combinations that would be silently ignored are refused. An explicit
`--payload-min-size` is incompatible with `--no-push`.

**On by default.** With no flag, the value is what the vault remembered, else `auto`
-- but only when the run pushes to a `seafile://` remote; otherwise the default stays
off rather than turning a working run into an error. A complete real run stores the
value in the vault's config (`snapshot.payload`, `0` included, a preview never), so a
scheduled bare command keeps its behaviour even if the default later changes. A fresh
vault on another machine therefore adopts `auto`, not the threshold the remote's last
snapshot happened to use. A run that ends up with payload mode off in a vault whose
last snapshot has a manifest (R10) is refused, naming the value, so large files are
never staged into git by accident; `--no-payload` / `--payload-min-size 0` is the
deliberate way out. A tree with nothing at or above the threshold writes no manifest
and makes no payload call, so the default costs a small tree nothing. The first run
that takes the default says so, an existing vault's conversion is announced with a
`NOTICE` before anything is hashed or uploaded, and a run that uploaded objects prints the size of
the store (nothing deletes an object until `--payload-gc`). The vault's tree carries
`.seafile-payload/README.txt` beside the manifest, so a plain `git clone` explains
itself. `--payload-rehash` ignores the hash cache for one run. The `.seafile-payload` directory of a source is never staged.

### D13. Safeguards — **decided** (v0.9.0)

Nothing here changes what a snapshot contains; each item is a refusal or a note
*before* something expensive or destructive starts.

| Hazard | Safeguard | Test |
| :--- | :--- | :--- |
| A newer tool wrote the snapshot this run builds on | every snapshot commit carries a `Git-Remote-Seafile: <version>` trailer (never in the tree, so an unchanged tree still commits nothing after an upgrade); a run warns when the base was written by a newer tool. A manifest carries `min_git_remote_seafile`, and a tool older than that refuses it | `test_every_snapshot_carries_the_tool_version_as_a_trailer`, `test_a_snapshot_by_a_newer_tool_is_warned_about`, `test_a_manifest_that_needs_a_newer_tool_is_refused` |
| Two machines on different thresholds move files between git and payload on alternate runs | a vault with no remembered setting that finds the remote's manifest on another rule refuses, naming the flag; the default never silently picks | `test_a_fresh_vault_is_refused_when_the_remote_used_another_rule` |
| `--payload-delete` from a machine that lacks snapshots which were never pushed | needs `--vault`, or `--payload-no-vault` said out loud | `test_delete_needs_a_vault_or_an_explicit_no_vault` |
| The remote's history was rewritten (force push, deleted branch or tag) and a delete would then free objects those snapshots need | each `--payload-delete` run stores the ref tips it saw (`payload/gc-seen.json`); a later run that finds one no longer contained refuses to mark or delete until `--payload-accept-loss`. A report only prints it | `test_a_rewritten_history_stops_a_delete_until_it_is_accepted` |
| `--payload-keep N` makes older snapshots unrestorable | the report says how many; a delete needs `--payload-accept-loss` | `test_keep_that_expires_snapshots_needs_the_acknowledgement` |
| The payload library is synced by the desktop client | warning before the upload | `test_a_synced_payload_library_is_warned_about` |
| An upload larger than the account's remaining quota | warning before the upload (a warning, not a refusal: the quota that counts is the library owner's) | `test_an_upload_that_cannot_fit_the_quota_is_warned_about_first` |
| A retention run whose history fetch would fill the temporary volume | refused before the fetch, naming `TMPDIR`/`TEMP` and `--allow-low-disk` | `test_a_retention_run_that_cannot_hold_the_history_is_refused_before_the_fetch` |
| A restore with too little free disk | refused before the first download (`--allow-low-disk` overrides) | `test_a_restore_that_cannot_fit_is_refused_before_downloading` |
| Stored objects that rot unnoticed | `--payload-verify` leaves `payload/verified.json`; the gc report and the store line say when it last ran | `test_verify_leaves_a_marker_the_store_line_and_gc_report_show` |
| A long hashing pass nobody explains (a copy tool reset every timestamp) | the run says how many files were hashed again and why | `test_files_hashed_again_are_explained` |

**A tag is a pin.** `--payload-gc` reads every branch and tag of the remote and of
`--vault`, so `git tag keep-2026-q3 <snapshot>` keeps that snapshot's objects alive
whatever happens to the branch.

### D14. Modification times of git-stored files — **decided** (0.9.2)

Git stores content and a mode, not a time, so a restore gave every git-stored file the
checkout time (payload files already carry theirs in the manifest). A snapshot that
pushes to a `seafile://` remote, or is given `--record-mtimes`, writes
`.seafile-payload/mtimes.json`: one line of ASCII JSON, sorted keys,
`{"format": 1, "files": {path: mtime_ns}, "sizes": {path: bytes}}`, for every file in the snapshot's tree
except the reserved directory (payload files are in the manifest).

- **The times are the real ones** (`lstat`, so a symlink's own). When nothing in the
  tree changed since the parent snapshot, the parent's record blob is reused
  unchanged, so a touch alone neither changes the record nor makes a commit (R4). A
  per-file "first seen" time was tried and removed: a source reverted to older content
  kept its old time, which could sort before an object built from the version it
  replaced, so `make` kept the stale object after a restore.
- **A restore applies it after the checkout**, with the checks a manifest path gets
  (D8: no `..`, no `.git`, nothing outside the destination, a time in range); a path
  that is absent or not a file is skipped. A symlink's own time is set where the
  platform allows it.
- **Choice:** the flag as typed, else what the vault remembered (`snapshot.mtimes`,
  only an explicit choice is written), else on if the parent snapshot has a record,
  else on for a run that pushes to a `seafile://` remote. A default is not a request:
  a local-only run on a vault with no record stays as it was.
- **`sizes`** is an additive key of the same format-1 document, one entry per regular
  file (a symlink's own size says nothing about content). A tool that does not know it
  reads `files` exactly as before, and a record written before it simply has none: the
  parent's record is reused unchanged for an unchanged tree, so such a record stays
  size-less until the tree changes. `--status` uses it: with a size, a rewrite that
  restores the mtime no longer reads as unchanged.
- **Cost:** about 70 bytes a file; at 100,000 files about 5 MB raw and 1.5 MB
  compressed, rewritten whenever any file's content changes, and uploaded whole
  (the helper does not delta against the remote's copy). Sharding it is a format
  change and is not done.

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
| A4 | Same-size edit within the timestamp granularity | cached hash would be trusted | **handled**: D6 racy rule against the filesystem's hash time (a share's own clock, measured by a probe file), plus inode, device and the change time (on NTFS the real one, not `st_ctime`, which is the creation time); `test_a_file_edited_within_the_racy_window_is_rehashed`, `test_a_share_whose_clock_is_behind_does_not_vouch_for_a_fresh_edit`, `test_the_change_time_in_the_key_moves_on_a_real_in_place_edit` |
| A5 | mtime restored by a copy tool (`cp -p`, `robocopy`) | cache trusted, content differs | **handled**: inode in the cache key catches most; `--payload-rehash` for the rest |
| A6 | Another machine's un-pushed snapshot references an object; retention deletes it | data loss | **handled within the grace period**: D9 two-phase, every branch and tag of the remote plus the local vault's (which must be one). Another machine's objects that are uploaded but not pushed are invisible to this machine's retention; they survive `--payload-grace-days`, after which that machine's next run re-uploads them (source unchanged) or refuses to push. Run retention from one machine |
| A7 | Run interrupted after upload, before push | orphan objects | **handled**: harmless, reused (D5) |
| A8 | An object is truncated or altered on the server, same size | upload skipped as "present"; backup silently bad until a restore | **handled**: the tool no longer creates this state itself (D5); `--payload-verify [N\|all]` downloads and hashes a sample of the newest snapshot's objects; schedule it as a drill |
| A9 | `(SFConflict ...)` copies appear next to an object | listing sees a non-hash name | **handled**: ignored, not collected (delete by hand; it costs quota at full size) |
| A10 | Two paths differ by case, Unicode form, or a trailing dot or space; restore onto Windows or macOS | one overwrites the other, both verify | **fixed**: D8 never overwrites; collisions are skipped and reported |
| A11 | Payload path looks like a credential | the secrets report covered only staged paths | **fixed**: payload paths scanned |
| A12 | File held open exclusively (Windows) | traceback or silent skip | **handled**: R9 |
| A13 | OneDrive "files on demand" placeholder | hashing triggers a download of the whole file | **handled**: a placeholder of any size is refused by name (the attribute check precedes the size floor; `test_a_placeholder_is_refused_below_the_payload_floor_too`), and reparse points are not walked |
| A14 | The threshold changes between runs | files move between git and payload | **handled**: staging skips payload paths; old snapshots unaffected; flag is required (R10) |
| A15 | A payload file becomes a symlink or shrinks below the threshold | falls to git | **handled** |
| A16 | `--reset-to-remote` drops local-only snapshots | their objects become orphans; the previous-manifest cache disappears | **handled**: harmless; a rehash |
| A17 | Thousands of objects in one fan-out directory | listing cost | **handled**: 256 directories; list only those touched |
| A18 | Manifest of 100,000 files committed each run | repository growth | **measured**: delta keeps growth proportional to what changed (D4) |
| A19 | Disk full or Ctrl-C during restore | partial tree | **specified**: restore needs an empty destination (the tool refuses a non-empty one); rerun into a new directory. The temporary download directory under `.git` is removed on every exit |
| A20 | Sparse files, alternate data streams, hard links | sparse read as zeros; ADS dropped; hard links become copies | **specified**: byte content only; documented limit |
| A21 | Read-only attribute, ACLs, ownership | not preserved | **specified**: mode bits and mtime only |
| A22 | Clock jumps or future mtimes | a future mtime is never "older than the commit" | **handled**: always rehashed (safe) |
| A23 | An encrypted library | the client refuses it | **handled** |
| A24 | A server that rewrites object names | would break "listing proves presence" | **handled**: names are lowercase hex; verified on the real server |
| A25 | `--payload-jobs` very large on a flaky link | many partial uploads | **handled**: pool is bounded; failure stops the queue |
| A26 | A report-only `--payload-gc` after every run | cost of listing 256 directories; a failing report failing the run; a history download | **handled**: at most once per `--payload-report-days` (7), last in the run (after the settings are remembered), never fatal, never deletes, and reads the history from the local vault so it downloads nothing; `test_a_failing_report_never_fails_the_run_that_pushed`, `test_the_routine_report_reads_the_local_vault_not_the_remote` |
| A27 | A restore path over 260 characters on Windows | file skipped or `git checkout` fails | **handled**: payload files use extended-length paths and git runs with `core.longpaths=true`; `test_the_vault_and_the_restored_clone_allow_long_paths` |
| A28 | The run is killed (not interrupted) while a large tree is hashed | the hours of hashing are lost; the next run reads everything again | **handled**: the hash cache is saved about once a minute while hashing (`_CHECKPOINT_SECONDS`) and, as before, whenever the upload then fails; a stale entry still fails the size/mtime/inode/ctime/device key; `test_a_killed_hashing_pass_keeps_what_it_hashed` |
| A29 | A backup is never restored, so a damaged one is found on the day it is needed | silent loss | **handled on request**: `--restore-drill [N\|all]` restores the newest snapshot into a temporary directory, checks every file byte-for-byte and `git fsck`, verifies N sampled payload files by SHA-256 and deletes it; a failure keeps the directory; `test_a_damaged_object_fails_the_drill_and_keeps_the_directory` |
| A28 | A directory the tool cannot open (a permission, an ACL) | `git add` prints a warning and exits 0, so everything under it is silently missing from the snapshot | **fixed** (0.9.2): the run stops and names it, `--exclude` leaves it out on purpose; `test_a_directory_that_cannot_be_read_stops_the_run_and_is_named` |
| A29 | A real file named `<payload file>.part` beside a payload file | the restore's download overwrote it and renamed it away, then certified the tree | **fixed** (0.9.2): downloads stage under `.git/grs-restore/`; `test_a_git_file_named_like_the_download_temp_file_survives_the_restore` |
| A30 | A symlink `tool -> .git` and a manifest path `tool/hooks/pre-commit` | an executable hook written into the restored repository | **fixed** (0.9.2): protected directories are refused after links are followed; `test_a_link_to_dot_git_cannot_carry_a_payload_write_into_the_repository` |
| A31 | A manifest that declares a small size for a huge object | the disk fills before the sizes are compared | **fixed** (0.9.2): the download is cut off at the declared size; `test_download_file_to_stops_at_the_size_the_caller_expects` |
| A32 | A pid recycled by another process after a crash | the lock reads as held; every later snapshot is refused | **fixed** (0.9.2): the lock records the process start time; `test_a_live_pid_with_a_different_start_time_is_a_dead_holder` |
| A33 | `--payload-delete` decides from a listing that skipped a branch, or from a decision minutes old | objects of the skipped branch, or of a snapshot pushed meanwhile, look unused | **fixed** (0.9.2): the listing is strict (`SEAFILE_STRICT_REFS`) and the refs are read again before the first deletion; `test_a_push_that_lands_while_gc_is_deciding_stops_the_deletion`. **Narrowed further** (0.9.3): they are read again every 25 deletions or 10 s, and a change stops the run (`test_a_push_that_lands_during_the_deletion_loop_stops_it`). *Residual, narrowed again (0.9.3):* a push that verified an object, then lands after that object was deleted, still names it; the retention run takes no remote lock, and a lock would not close this either, because the push checks the objects before it takes the lock. The pushing run now lists the objects its manifest names once more after the push, re-uploads any that went missing while the source file is as it was, and otherwise fails naming them (`test_an_object_deleted_between_the_check_and_the_push_is_put_back`) *Strict walk completed (unreleased, REVIEW.af9e9e1 T3.1):* an entry of an unknown type, a name git refuses, a ref nested past the walk's depth and a ref file holding no object id used to be skipped with a `Warning:` even under `SEAFILE_STRICT_REFS`, so the branch behind one lost its objects after the grace period (reproduced against the stub); each now stops the listing; `test_an_entry_of_an_unknown_type_stops_a_strict_walk_and_only_warns_a_lax_one`, `test_a_strict_listing_stops_at_a_ref_that_holds_no_object_id`. |
| A34 | A file on a mount under the source, or on a share whose clock cannot be probed | the machine-clock racy rule is wrong there | **fixed** (0.9.2): a one-hour margin, and the device and the inode change time are in the cache key; `test_an_unprobed_share_is_trusted_only_for_files_an_hour_older_than_their_hash` |
| A35 | A vault read in place that is shallow, a partial clone, or missing an object (ROADMAP V7) | a manifest silently drops out of the history (`cat-file --batch-check` words an absent manifest and a lost manifest blob the same way), so live payload objects look unreferenced and `--payload-delete` removes them | **fixed** (0.9.20): retention refuses a shallow or promisor repository, and for a vault read in place lists every reachable object (`rev-list --all --objects --missing=print`) and refuses on a missing one; a remote's history is connectivity-checked by git's own fetch; `test_a_vault_that_lost_a_manifest_blob_is_refused`, `test_a_shallow_vault_is_refused`, `test_a_partial_clone_is_refused` |

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
| Windows junctions are walked, so a junction to a drive root hashes the drive (F7) | WRONG | the walk skips reparse points -- but git for Windows follows them, so before 0.9.2 a large file behind a junction went into git and the three walks disagreed; staging now leaves paths behind a junction out and reports them (`test_paths_behind_a_junction_are_left_out_and_reported`) |
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

*Changed in 0.9.13, was recorded as accepted:* a retention run's grace clock was the clock of
the machine that marked the object, so the wall clock of whichever machine ran the delete
decided both ends — a jump of +8 days deleted an object marked seconds before (measured,
REVIEW.sonnet-5.5 D3). The mark and the comparison now run on the server's `Date` estimate.
*Still not changed, recorded:* a non-UTF-8 name from a POSIX source cannot be
restored on Windows; `SeafileClient.copy_entries` was removed until views need it.

*Round 3 (0.9.3), recorded:* the probe file is `.grs-clock-probe-<pid>-<ns>`, created
with `O_EXCL`, so it cannot overwrite a user's file; a watcher on the share (a build
daemon, an indexer, a sync client) can react to its creation and removal, which
nothing here can prevent; view paths are validated with the same class of checks as
restore paths (D8) and `--payload-view-name` is validated; the default conversion of
an existing vault is announced and not refused, because a remembered or default
setting must never turn a run that worked into an error. Since 0.9.3 the notice is
repeated as the last line of the run, on stdout and stderr, so a scheduler's log ends
with it; a scheduled job that must not convert passes `--no-payload`, which is
remembered.

*Not changed, recorded (round 1):* the racy rule uses this machine's clock (D6, F9/F15); a
snapshot is never expired by itself -- there is no `--keep-snapshots` yet, so a
file that changes daily grows the store until history is rewritten; a non-UTF-8
file name on Linux is not handled by the staging path (a pre-existing limit).
*Superseded in 0.9.3:* git output is decoded with `surrogateescape`, so a name that is
not UTF-8 stages, excludes, verifies and restores on POSIX; it still cannot be
restored on Windows, and the manifest and mtime record (ASCII JSON) refuse to carry
it.

## 7. Test matrix

`tests/test_snapshot_payload.py` (five classes, so the suite parallelises) and
`tests/test_snapshot_tool.py` drive the real tool and helper against the
in-process stub. Each fix in 6.1 was verified by mutation: a one-line
change that removes it makes a named test fail (12 of 12), and every 0.9.1 and 0.9.2
fix was checked the same way against the previous source. The real-server soaks
cover a 1.1 GiB chunked upload, unchanged run, same-size edit, restore and
older-snapshot restore, `auto` selection and retention, a view, a verify, a
100,000-entry manifest pushed and cloned, a 2,000-object report, and the 131 GiB flood
tree end to end. Not covered: a macOS run (NFD names, HFS+, `utime` on symlinks),
a Linux or macOS non-UTF-8 name, and an SMB or NFS share with a skewed clock (the
probe is exercised through its seams).

## 8. Order of work

1. ~~Decide D1 (selection rule).~~ Done: `auto`.
2. ~~Retention helper (D9).~~ Done.
3. ~~The 131 GiB flood run, as the acceptance test.~~ Done: 633 files, 124.6 GB, restored and verified.
4. ~~Remembered setting and force-rehash (D11, D6).~~ Done.
5. Snapshot expiry (`--payload-keep N`), sampled verification (`--payload-verify`), views (D10).
