# git-remote-seafile

[![CI](https://github.com/haiwen/git-remote-seafile/actions/workflows/ci.yml/badge.svg)](https://github.com/haiwen/git-remote-seafile/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

A transparent, zero-thrash **Git remote helper** for Seafile servers.

`git-remote-seafile` allows you to use your private Seafile server (Community or Professional Edition) as an authentic, encrypted, access-controlled Git remote backend using native Git commands (`git clone`, `git push`, `git pull`).

---

## Why This Exists

Storing active Git repositories inside Seafile desktop-synced folders (e.g. `Documents/code/`) causes severe thrashing:
- A single `git commit` writes ~15 files into `.git/` in milliseconds, triggering hundreds of continuous library re-index cycles per day.
- Ephemeral `.git/*.lock` files sync across machines, freezing Git commands on secondary machines.
- Diverging edits generate destructive `(SFConflict ...)` copies instead of Git merges.

### The Solution
Instead of syncing the local `.git/` folder, `git-remote-seafile` communicates directly with the **Seafile Web API v2.1**:
- **Zero Daemon Churn**: The desktop sync client never scans or indexes your working repository.
- **Fast & Efficient**: Commits are packed using Git packfiles (`.pack` and `.idx`). Pushing 100 commits uploads only **two files**.
- **Rollback & Branch Safe**: Clean ref management handles branches, force-pushes, and automatic unreachable commit pruning.
- **No Extra Servers**: Uses your existing Seafile server without needing GitLab, Gitea, or third-party Git hosts.
- **Fast-forward Protection**: Rejects non-fast-forward pushes unless force-pushed, preventing accidental clobbering.

---

## Critical Rule: Working Repository Placement

> [!CAUTION]
> ### DO NOT keep your working repository inside any folder synced by Seafile!
> 
> Your active Git repository (the directory containing your working tree and `.git/`) **MUST be located outside of any folder actively synced by the Seafile desktop client**.
> 
> - **Safe locations**: Any folder that Seafile does not watch or sync, such as `C:\code\myproject`, `C:\Projects\myproject`, or `~/code/myproject`. (If your `Documents` folder is **not** synced by Seafile, it is also safe to use).
> - **Unsafe locations**: Any local directory currently mapped to a Seafile library in your desktop client (such as `C:\Users\<username>\Seafile\...` or a synced folder).
> 
> **Why?**
> If your local repository directory is actively watched by the Seafile desktop client, the sync daemon will continuously monitor, lock, and re-index the internal `.git/` files on every commit or branch switch. `git-remote-seafile` communicates directly with your Seafile server over HTTP/HTTPS Web API v2.1—just like GitHub or GitLab. Keeping your working folder unsynced allows Git to operate at native filesystem speed with zero desktop client interference.

---

## Architecture Overview

```mermaid
graph TD
    A[Developer Worktree<br/>C:\code\myproject<br/><i>Unsynced Folder</i>] -->|git push / fetch| B[git-remote-seafile<br/>Git Remote Helper]
    B -->|REST API v2.1| C[Seafile Server<br/>https://seafile.example.com]
    C -->|Stores Packfiles & Refs| D[Seafile Library: code<br/>/myproject/]
    
    style A fill:#4a5568,stroke:#2d3748,stroke-width:2px,color:#fff
    style B fill:#2b6cb0,stroke:#2d3748,stroke-width:2px,color:#fff
    style C fill:#2f855a,stroke:#2d3748,stroke-width:2px,color:#fff
```

---

## Comparison: `git-remote-seafile` vs. "Move Code Out + Git Remotes"

When moving active code out of synced cloud storage, there are two primary paths:
1. **Move Code Out + Traditional Git Remotes** (GitHub, GitLab, Gitea, Forgejo)
2. **Move Code Out + `git-remote-seafile`** (This Project)

Here is how they compare:

| Dimension | Move Code Out + Git Forge (GitHub / Gitea) | Move Code Out + `git-remote-seafile` |
| :--- | :--- | :--- |
| **New Infrastructure** | Requires third-party SaaS (GitHub) or deploying & maintaining a separate server (Gitea/GitLab, database, SSH daemon, domain, backups). | **Zero new infrastructure.** Reuses your existing Seafile server, user accounts, and storage pool. |
| **Privacy & Data Sovereignty** | SaaS: Code stored on 3rd-party servers; Self-hosted: Requires securing a new server stack. | **100% self-hosted & private.** Retains your existing Seafile permissions, SSL, and data retention policies. |
| **Storage & Quotas** | GitHub: 1–2 GB repo soft limits; paid Git LFS bandwidth/storage tiers. | **Uses existing Seafile quota.** Store multi-GB datasets and models with built-in Git LFS custom transfer agent at no extra cost. |
| **Web Code Review & PRs** | **Full web forge:** Pull requests, inline code comments, issue tracking, CI/CD runners (GitHub Actions / GitLab CI). | **VCS storage backend only.** No web code review UI or built-in CI runner (though CI can clone/push via API tokens). |
| **Client Overhead** | Zero background churn. Normal Git Smart HTTP/SSH transport. | Zero background churn. Client-side packfile generation via REST API v2.1 (<1s pushes). |
| **Multi-Machine Sync** | Standard `git push` / `git pull`. | Standard `git push` / `git pull` with cooperative distributed lease locking. |
| **Best For** | Open-source projects, large teams needing code review & CI pipelines. | Personal projects, private research, proprietary code, multi-GB LFS assets, zero-maintenance self-hosted remotes. |

> [!TIP]
> **Complementary, Not Mutually Exclusive**: You can use both simultaneously! Keep your open-source remote on GitHub and push an offsite, self-hosted mirror to Seafile using a dual-remote config (`git remote set-url --add --push origin seafile://...`).

---

## Alternative Solutions & Workarounds: Success vs. Failure Modes

Before building `git-remote-seafile`, numerous alternative workarounds were investigated and tested. Here is an overview of how each approach performs:

| Approach / Workaround | Success Cases | Failure Modes & Gotchas | Overall Verdict |
| :--- | :--- | :--- | :--- |
| **1. Live Working Tree in Synced Seafile Folder** (`Documents/code/`) | Works for single-user offline backups if only one machine touches files. | • 1,500+ library re-commit cycles/day.<br>• `.git/*.lock` files sync, deadlocking secondary machines.<br>• Branch switches trigger massive conflict storms (e.g. 1,749 `(SFConflict ...)` files). | ❌ **Fatal failure.** Do not use. |
| **2. Pause Syncing During Development** (`Disable auto sync` / `seaf-cli stop`) | Temporarily reduces local disk/CPU churn while coding. | • Sync pause is strictly *local*—secondary machines keep syncing.<br>• Unpausing causes massive sync latency and conflict explosions.<br>• Human error: forgetting to pause or resume. | ❌ **High failure rate.** Flawed concurrency model. |
| **3. Seafile Ignore Rules** (`seafile-ignore.txt`) | Effectively excludes non-synced build folders (`node_modules`, `dist`) if configured *before* first sync. | • Cannot ignore retroactively (already-synced files keep syncing).<br>• Must be at library root; subfolder ignore files are ignored.<br>• Client parser bug with dot-prefixed folders (`haiwen/seafile#1137`).<br>• Ignoring `.git/` strips Git tracking from other machines. | ❌ **Unviable for Git.** Breaks VCS across machines. |
| **4. Generic Cloud Sync on `.git/`** (Dropbox, OneDrive, Syncthing) | Single-file document sync. | • Cloud sync is not atomic across multi-file transactions.<br>• Conflicts in `.git/` corrupt repo state (`fatal: bad object HEAD`).<br>• Files-on-demand placeholders cause Git IO crashes. | ❌ **Fatal failure.** Repositories inevitably corrupt. |
| **5. Git Bundles in Synced Folder** (`git bundle create`) | Creates single, atomic `.bundle` files that Seafile can sync without `.git/` churn. | • Terrible developer ergonomics (manual bundling/unbundling).<br>• No standard `git push`/`git pull`.<br>• No branch tracking or automated conflict detection. | ⚠️ **Clunky.** Safe for cold archives only. |
| **6. Bare Git Repo in Synced Folder** (`git init --bare`) | Working tree is outside Seafile; push to local bare repo via `file://`. | • Bare repos contain hundreds of loose objects that Seafile still syncs asynchronously.<br>• Multi-machine concurrent pushes corrupt `refs/heads/main`. | ❌ **Unreliable.** Suffers from asynchronous sync lag. |
| **7. Move Code Out + Git Forge** (GitHub / Gitea) | Full developer platform, PRs, CI/CD, industry standard. | • Requires separate SaaS account or hosting/maintaining another server stack. | ✅ **Recommended** for teams & code review. |
| **8. Move Code Out + `git-remote-seafile`** | Zero new servers, uses existing Seafile storage & accounts, native Git CLI, Git LFS support, distributed locking. | • Does not provide a web-based pull request / code review UI. | ✅ **Recommended** for private self-hosted Git storage. |

---

## Quick Start

### 1. Installation
```bash
pip install git-remote-seafile
```
*(Or clone and install in editable mode: `pip install -e .`)*

### 2. Verify Authentication
`git-remote-seafile` automatically discovers active logins from your local Seafile desktop client:
```bash
git-remote-seafile check-auth
# Output: Authenticated successfully with https://seafile.example.com as user@example.com
```

### 3. Push Any Repository
```bash
cd C:\code\myproject

# Add Seafile as a remote: seafile://<library-name>/<path>
git remote add origin seafile://code/myproject

# Push your branch
git push -u origin main
```

### 4. Clone on Another Machine
```bash
git clone seafile://seafile.example.com/code/myproject
```

---

## Documentation

- [User's Guide (USER_GUIDE.md)](USER_GUIDE.md): Full walkthrough of setup, configuration files, multi-account usage, and dual-remote (GitHub + Seafile) setups.
- [Architecture & Design Spec (DESIGN.md)](DESIGN.md): Technical protocol specifications and upstream RFC for Seafile maintainers.
- [Contributing Guidelines (CONTRIBUTING.md)](CONTRIBUTING.md): How to contribute, run tests, and adhere to development standards.

---

## Acknowledgments & Inspirations

`git-remote-seafile` builds upon pioneering work in the Git remote helper and cloud storage ecosystem:
- [git-remote-dropbox](https://github.com/anishathalye/git-remote-dropbox) by Anish Athalye: The landmark implementation that demonstrated transparent Git packfile transport and distributed locking over cloud storage APIs.
- [git-remote-gcrypt](https://spwhitton.name/tech/code/git-remote-gcrypt/) by Joey Hess: Pioneered decentralized, encrypted Git remotes.
- [git-remote-s3](https://github.com/jason-riddle/git-remote-s3): Git remote helper for Amazon S3 object stores.
- [Git Remote Helpers Protocol Specification](https://git-scm.com/docs/git-remote-helpers): Official Git documentation on building custom remote helpers.
- [Git LFS Custom Transfer Agent Protocol](https://github.com/git-lfs/git-lfs/blob/main/docs/custom-transfers.md): Specification for custom large-object transport agents.
- [Seafile Web API v2.1](https://manual.seafile.com/develop/web_api_v2.1/): The underlying REST API provided by Seafile Ltd.

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
