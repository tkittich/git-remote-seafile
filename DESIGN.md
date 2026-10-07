# Architecture & Design Specification: git-remote-seafile

**Author**: Community Contribution  
**Target Organization**: [haiwen](https://github.com/haiwen) (Seafile Ecosystem)  
**Status**: Ready for Upstream Integration  

---

## 1. Executive Summary

`git-remote-seafile` introduces native Git remote support for Seafile servers using Git's standard Remote Helper specification (`gitremote-helpers(7)`).

It allows Seafile users and organizations to use existing Seafile libraries for private, encrypted, access-controlled Git repository hosting without:
1. Deploying a separate Git server (e.g. GitLab, Gitea), or
2. Experiencing the severe performance thrashing and data corruption that occurs when placing live `.git/` working trees inside desktop-synced folders.

---

## 2. Background & Problem Statement

Seafile's desktop synchronization client (`seaf-daemon`) is designed for document and file synchronization. When developers store active code repositories within synced libraries (e.g. `Documents/code/`), pathological failure modes emerge:

1. **Continuous Thrashing**: Git operations frequently write to `.git/` (`index.lock`, `objects/tmp_obj_*`, `COMMIT_EDITMSG`, `refs/heads/*`). Each write triggers full-library re-index cycles (hundreds of cycles daily).
2. **Lock Synchronization**: Ephemeral `.git/*.lock` files are synced across devices, locking Git on secondary machines.
3. **Conflict Storms**: When two machines edit code concurrently, the sync daemon generates `* (SFConflict ...).*` duplicate files rather than performing a 3-way line merge.

Attempting to fix this by modifying the desktop client to understand Git is technically fragile due to platform differences, stat cache discrepancies in `.git/index`, and concurrent worktree races.

### The Solution: API-Driven Git Remote Helper
Instead of synchronizing the local `.git/` directory, the developer's working tree remains completely outside Seafile's file watcher. Git uses Seafile Server as an authentic remote destination over the standard **Seafile Web API v2.1**.

---

## 3. Remote Storage Architecture

Within the designated Seafile library (e.g. `Documents` or a dedicated `code` library), each Git repository is stored in a clean, bare-like layout:

```text
/<repo-path>/
├── HEAD                        <- Symbolic ref ("ref: refs/heads/main\n")
├── refs/
│   ├── heads/
│   │   ├── main                <- Commit SHA ("3a8f94b15c...\n")
│   │   └── feature-auth        <- Commit SHA
│   └── tags/
│       └── v1.0.0
└── objects/
    └── pack/
        ├── pack-9a8f4c....pack <- Git binary packfile containing objects
        └── pack-9a8f4c....idx  <- Corresponding packfile index
```

### Why Packfiles Instead of Loose Objects?
- A standard commit can involve dozens of loose objects. Transferring individual loose objects over HTTP would require dozens of requests per push/fetch.
- By packaging commits into Git packfiles using `git pack-objects`, every `git push` transfers **exactly two files** (`.pack` and `.idx`) regardless of commit size.
- Pushes complete in hundreds of milliseconds.
- Storage inside Seafile is content-addressed, deduplicated, and block-compressed according to standard Seafile server storage mechanisms.

---

## 4. Seafile Web API v2.1 Integration

`git-remote-seafile` uses exclusively standard, stable endpoints present in all modern Seafile servers (CE and Pro):

1. **Authentication**: Reuses existing desktop client tokens (`Accounts` table in `accounts.db`) or token headers (`Authorization: Token <token>`).
2. **Directory Listing**: `GET /api2/repos/{repo-id}/dir/?p=/{repo-path}/refs/heads/`
3. **Ref Reading**: `GET /api2/repos/{repo-id}/file/?p=/{repo-path}/refs/heads/{branch}`
4. **Packfile & Ref Upload**:
   - `GET /api2/repos/{repo-id}/upload-link/?p=/{repo-path}/objects/pack/`
   - `POST {upload-link}` with `multipart/form-data` and `replace=1`.
5. **Branch Deletion**: `DELETE /api2/repos/{repo-id}/dir/?p=/{repo-path}/refs/heads/{branch}`

---

## 5. Protocol Flow (Git Core ↔ Helper)

```mermaid
sequenceDiagram
    autonumber
    participant Git as Git Client
    participant Helper as git-remote-seafile
    participant API as Seafile Server API

    Note over Git,Helper: Capability Handshake
    Git->>Helper: capabilities
    Helper->>Git: fetch\npush\n\n

    Note over Git,Helper: Ref Discovery
    Git->>Helper: list
    Helper->>API: GET /refs/heads/ & /refs/tags/
    API-->>Helper: JSON directory listing & ref SHAs
    Helper->>Git: <sha1> refs/heads/main\n@refs/heads/main HEAD\n\n

    Note over Git,Helper: Push Transaction
    Git->>Helper: push refs/heads/main:refs/heads/main
    Helper->>Git: (internal) run git pack-objects
    Helper->>API: GET upload-link
    API-->>Helper: temporary upload URL
    Helper->>API: POST pack-{sha}.pack & pack-{sha}.idx
    Helper->>API: POST /refs/heads/main (new commit SHA)
    Helper->>Git: ok refs/heads/main\n\n
```

---

---

## 6. Fast-Forward Safety & Conflict Guarantees

- **Fast-forward check**: Before writing a ref update, `git-remote-seafile` checks if the current remote SHA is an ancestor of the local commit (`git merge-base --is-ancestor`).
- If another developer pushed to that branch in the interim, the helper outputs `error <dst> non-fast-forward`.
- Git aborts the push, prompting the user to `git pull` and merge using standard Git tools.
- **Zero file duplication**: No `(SFConflict ...)` files can ever be generated.

---

## 7. Advanced Storage & Concurrency Features

### 7.1 Distributed Lease Locking
- In multi-developer teams, concurrent pushes could race during packfile uploads.
- `git-remote-seafile` implements a cooperative lease mutex stored at `/.git-lock.json` on the remote repository.
- Locks include owner, machine ID, timestamp, and a 60-second lease expiration to guarantee that aborted or crashed pushes cannot permanently lock out other collaborators.

### 7.2 Remote Packfile Compaction (`git-remote-seafile gc`)
- Over time, numerous pushes create multiple packfiles in `/objects/pack/`.
- The `gc` subcommand downloads all packs, invokes `git repack -ad -l` to consolidate and delta-compress them into a single unified packfile, uploads the result, and removes obsolete remote packs from Seafile.

### 7.3 Git LFS Custom Transfer Agent Protocol
- Implements the official Git LFS line-based JSON custom transfer protocol.
- Handles `init`, `upload`, and `download` events over standard I/O.
- Allows large binary assets to be stored directly in Seafile `/lfs/` while maintaining small, agile Git commit histories.

---

## 8. Upstream Integration Path for Haiwen / Seafile

To incorporate this capability into the official Seafile ecosystem:
1. **Repository Home**: Hosted at `tkittich/git-remote-seafile`; can be adopted upstream as `haiwen/git-remote-seafile` or bundled within official client distributions.
2. **Packaging**: Distributed via PyPI (`pip install git-remote-seafile`) and bundled into Seafile Windows/macOS client installers.
3. **Desktop Applet Integration**: A ready-to-merge Qt patch is provided in `contrib/seafile-client-context-menu.patch`, adding a *"Copy Git Remote URL"* action to the Seafile desktop applet's right-click context menu.

---

## 9. Acknowledgments & Prior Art

`git-remote-seafile` draws technical inspiration and design patterns from:
- [git-remote-dropbox](https://github.com/anishathalye/git-remote-dropbox) by Anish Athalye: Pioneered transparent Git packfile transport and cooperative lockfiles over cloud file storage APIs.
- [git-remote-gcrypt](https://spwhitton.name/tech/code/git-remote-gcrypt/) by Joey Hess: Pioneered decentralized, encrypted, push-to-anything Git remotes.
- [git-remote-s3](https://github.com/jason-riddle/git-remote-s3): Git remote helper for Amazon S3 object stores.
- [Git Remote Helpers Protocol Specification](https://git-scm.com/docs/git-remote-helpers): The bidirectional stdio protocol defined by core Git (`git-remote-helpers(7)`).
- [Git LFS Custom Transfer Agent Protocol](https://github.com/git-lfs/git-lfs/blob/main/docs/custom-transfers.md): The official specification for custom Git LFS transport agents.
- [Seafile Web API v2.1](https://manual.seafile.com/develop/web_api_v2.1/): The REST API provided by Seafile Ltd.

---

## 10. AI Disclosure & Authorship

This design specification and the underlying implementation were authored primarily with generative AI assistance (Google DeepMind's Antigravity / Gemini) in collaboration with human architectural design, real-world forensic diagnostics, and verification on Windows by [@tkittich](https://github.com/tkittich). All code and protocol implementations are fully open source and licensed under the Apache License 2.0.

