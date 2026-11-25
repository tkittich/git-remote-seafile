# seafile-blob: User's Guide

Keep large files on a Seafile library under their SHA-256, and get them back verified. A **reference** (`sha256:<64 hex>`) means the same bytes everywhere and always, so it can live in git, a database row or a notebook. The command is `seafile-blob`; the short forms are `sfblob`, `git-seafile-blob` and `seafile blob`, all the same program (the examples use `sfblob`).

It is for **large, rarely-read data that a team already keeps on Seafile**: datasets, model weights, recordings, build artefacts. It is not a general object store: a small file costs several requests (about 0.1 s each on the servers measured), and the server slows a client down above about four transfers at once.

## Contents

- [1. Set it up](#1-set-it-up)
- [2. One file](#2-one-file)
- [3. A folder, with a record file for git](#3-a-folder-with-a-record-file-for-git)
- [4. Finding a reference again](#4-finding-a-reference-again)
- [5. Keeping objects: pins and `gc`](#5-keeping-objects-pins-and-gc)
- [6. Checking what is stored](#6-checking-what-is-stored)
- [7. The local cache](#7-the-local-cache)
- [8. Scripts and AI agents](#8-scripts-and-ai-agents)
- [9. What to know before you rely on it](#9-what-to-know-before-you-rely-on-it)
- [10. Commands](#10-commands)

---

## 1. Set it up

Log in once (the same login as the other tools: `git-remote-seafile login`, or the Seafile desktop client's account). Then name a store, a **folder** on a library that nothing else uses:

```
sfblob init seafile://<library>/<folder>
```

`init` checks the login and the folder, creates the store (a marker file and a `blobs/` folder), and saves the address as the git config `seafile.blob.store` so no other command needs it (`--global` saves it for every repository, which is the default outside one). To name a store without saving it, give `--store seafile://<library>/<folder>` or set `SFBLOB_STORE`.

A store is never made inside a vault, a git remote or another store, and a folder that holds something else is never adopted: use an empty or new folder.

## 2. One file

```
sfblob put dataset.tar            # prints:  sha256:ab12...  734003200  dataset.tar
sfblob get sha256:ab12cd -o dataset.tar     # a prefix of 6 or more hex characters is enough when it names one object
sfblob cat sha256:ab12cd | tar x            # straight to a pipe, once the bytes are proved
sfblob path sha256:ab12cd                   # the cached copy's path, for  gdalinfo "$(sfblob path ...)"
```

- `put` uploads only what the store does not already hold (it lists first), proves the stored object is the right size, and tells you the reference. A second `put` of the same file uploads nothing.
- `get` and `path` fetch into a local cache, verify the SHA-256, and only then give the file to you. A file that does not match is never kept. `get -o FILE` copies it to a path that must not exist; `get --to DIR` copies into a folder under the object's reference.
- A short reference is said in full on standard error (`Note: sha256:ab12cd is sha256:ab12...`). **Keep the full reference** in scripts and records: a short one can name another object later, when a new object shares the prefix.
- Progress is drawn on standard error on a terminal; `-q` turns it off. Standard output is only the answer.
- An interrupted download continues where it stopped on the next run.

## 3. A folder, with a record file for git

```
sfblob put -r data/ --record blobs.txt        # uploads every file, writes one sorted line per file
git add blobs.txt && git commit -m "data as of today"
sfblob get --from blobs.txt --to restored/    # restores every path, verified
```

The record file has one line per file (`path`, size, reference), sorted, so two branches that add different files merge line by line, and two that give one path different content are a plain git conflict, not a silent loss.

- Re-running either command is safe: what is already there is skipped (`put` by listing, `get --from` by size **and** hash). A path that holds other bytes is **never overwritten**: it is reported and the run fails after doing everything else. Nothing is deleted.
- `--dry-run` shows what would be uploaded or restored and changes nothing (no store marker, no folder, no cache, no record file). `--exclude 'PATTERN'` (repeatable, shell-style, on the relative path) leaves paths out: `--exclude '.git/*'` for a working tree.
- The record file itself is left out of its own folder's upload. Two paths that differ only in Unicode form are an error; a record with paths that differ only in case cannot be restored on a case-insensitive volume, and the restore says which.
- `get --from` reports a name the destination refuses, never writes through a link, and restores paths past 260 characters on Windows.
- `--jobs N` runs N transfers at once (default 4).

## 4. Finding a reference again

The store keeps no names. Each `put` is noted in a **local log** on the machine that ran it (never sent anywhere), and `log` searches it:

```
sfblob log survey_2024          # a part of the path, or a pattern such as '*survey*.tif'
```

`--no-log` (or `SFBLOB_LOG=off`) writes nothing; the log holds local paths. For folders the record file is the better memory.

## 5. Keeping objects: pins and `gc`

A reference kept outside the store (git, a notebook) does not protect an object from a later cleanup. A **pin** does:

```
sfblob put dataset.tar --pin thesis        # the pin is written first, then the object is listed
sfblob pin thesis sha256:<full reference>... [--from-file refs.txt]
sfblob pin ls
sfblob unpin thesis
```

In this version `gc` is a **report only**: it says what no pin names, which interrupted uploads and conflict copies are lying around, and what it does not recognise, and it **changes nothing** (`gc --apply` is refused). Pinning now means the later removal will never touch those objects. A pin that cannot be read stops `gc` and names the file; it is never read as "no pin".

## 6. Checking what is stored

```
sfblob verify                      # every object is present
sfblob verify --deep               # also reads each object back and hashes it (reads every byte)
sfblob verify --from blobs.txt     # every entry of a record, at its recorded size
sfblob exists sha256:...           # exit 1 when one is missing
sfblob ls                          # every object and its size
sfblob usage                       # how many objects and bytes, and the local cache
```

## 7. The local cache

Fetched objects are kept in a read-only cache (`$SFBLOB_CACHE`, or the user's cache folder) and checked again if anything could have written to a file. A cache is a copy: deleting it loses nothing.

```
sfblob cache                  # what it holds, and its bound
sfblob cache --size 20G       # keep it to 20 GiB from now on (the least recently used go first)
sfblob cache --size off       # no bound
sfblob cache --clear
```

Once over its bound the cache trims down to 70% of it, so it is not trimmed one object at a time. A path printed by `get` or `path` is not evicted for a minute.

## 8. Scripts and AI agents

- Standard output is the answer and nothing else; notes, warnings and progress go to standard error.
- `--json` answers with one JSON document, including the items that failed. Nothing ever prompts.
- Exit codes: `0` success, `1` failure (a missing object, a server error), `2` a usage error (including a malformed reference). `exists` exits 1 when any reference is missing.
- The shared flags (`--store`, `--cache-dir`, `--json`, `--jobs`, `-q`) work before or after the command's name.

## 9. What to know before you rely on it

- **Objects are write-once.** An object is never changed after it is stored. Nothing in this version deletes an object on the server.
- **Deleting frees quota, not disk.** When a later version removes objects, the library's quota falls at once, but the disk space is freed only after the server's history is truncated and its garbage collector (`seafserv-gc`) has run; both are the server administrator's.
- **A bare `put` is not protected from `gc`.** Say `--pin` for objects you want kept.
- **Small files are slow.** Measured on one Seafile CE server: about 4 files a second for `put -r`, at 8 to 12 requests per file. A folder of 50,000 small files takes hours. Put them in one archive (`tar`) and store that.
- **Over plain `http://`** a `put` is proved by reading the object back, never by the hash the upload computed.
- **Large uploads** continue across the server's one-hour upload-link limit, and a request the server's parser mishandles is noticed by its stored size and sent again (0.10.7).

## 10. Commands

| Command | What it does |
| :-- | :-- |
| `init [URL]` | check the login, create the store, remember it |
| `put FILE... \| -r DIR` | upload; `--record FILE`, `--pin NAME`, `--dry-run`, `--exclude`, `--no-log` |
| `get REF... \| --from RECORD` | fetch; `-o FILE`, `--to DIR`, `--dry-run`, `--exclude` |
| `path REF`, `cat REF` | the cache path; the bytes on standard output |
| `exists REF...`, `stat REF` | presence; size |
| `ls`, `usage` | the objects; counts and sizes |
| `verify [REF...]` | presence and size; `--deep`, `--from RECORD` |
| `pin NAME REF...`, `pin ls`, `unpin NAME` | keep objects from a later `gc` |
| `gc` | a report; changes nothing |
| `log [PATTERN]` | find a reference by the file's name (local) |
| `cache` | show, bound (`--size`) or empty (`--clear`) the cache |

Not in this version: removing objects (`gc --apply`), `offload` (upload, then free the local original), range reads, names, and share links.
