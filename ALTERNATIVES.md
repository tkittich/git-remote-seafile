# Alternatives and Comparison

This page holds the comparison and alternatives analysis that used to be §3–§4 of the [User Guide](USER_GUIDE.md). It is background for choosing an approach, not a how-to.

## 1. In-Depth Comparison: `git-remote-seafile` vs. "Move Code Out + Git Remotes"

When moving code out of synced cloud folders, developers face an architectural choice:
1. **Move Code Out + Traditional Git Remotes** (Push to GitHub, GitLab, Gitea, Forgejo)
2. **Move Code Out + `git-remote-seafile`** (Push to private Seafile server via this helper)

Both approaches eliminate desktop sync thrashing and lock contention. Below is a detailed technical comparison of their trade-offs:

### 1.1 Detailed Feature & Architectural Comparison

| Dimension | Move Code Out + Git Forge (GitHub / Gitea) | Move Code Out + `git-remote-seafile` |
| :--- | :--- | :--- |
| **New Infrastructure** | **High / Moderate.** Requires creating an external SaaS account (GitHub) or provisioning, securing, and maintaining a standalone server (Gitea/GitLab, database, SSH daemon, reverse proxy, backups). | **Zero.** Reuses your existing Seafile server, user authentication, and storage backend with no new services to manage. |
| **Data Sovereignty & Privacy** | **Conditional.** SaaS keeps code on third-party servers subject to foreign jurisdiction and AI scrapers. Self-hosted forges require ongoing CVE patching. | **100% Private & Self-Hosted.** Commits and files stay within your Seafile instance. Transfers secured via TLS/HTTPS (client-side password-encrypted libraries are not supported over REST API). |
| **Storage Limits & Economics** | **Restricted / Costly.** GitHub enforces soft 1–2 GB repo limits; Git LFS costs $5/mo per 50 GB. Self-hosted forges consume separate disk pools. | **Unrestricted.** Utilizes your existing Seafile storage quota (often multi-terabyte). Native Git LFS custom transfer agent with zero extra fees. |
| **Code Review & Collaboration** | **Full Web Forge UI.** Pull requests, inline comments, code search, issue tracking, and automated CI/CD runners (GitHub Actions, GitLab CI). | **Storage & Transport Layer Only.** No built-in web code review UI. Focuses purely on reliable Git transport, distributed locking, and ref sync. |
| **Transport Protocol** | Git Smart HTTP (`git-upload-pack`, `git-receive-pack`) or SSH protocol. Server negotiates deltas dynamically. | Client-side packfile packaging over Seafile Web API v2 (`/api2/repos/.../upload-link/`). Packfiles stored as `.pack` objects. |
| **Concurrency & Safety** | Handled natively by server Git process with atomic ref updates. | Enforced by fast-forward checks and cooperative distributed lease locking (`.git-lock.d/` ticket queue with auto-expiry). |
| **Client-Side Daemon Churn** | Zero. Working tree is outside sync folders. | Zero. Working tree is outside sync folders. |

### 1.2 Decision Guide: Which Should You Use?

#### Choose Traditional Git Forges (GitHub, GitLab, Gitea) when:
- You work with a **team that requires Pull Requests, inline code review, and discussion threads**.
- You need integrated **CI/CD pipelines** (e.g. GitHub Actions, GitLab CI) triggered automatically on every push.
- You are developing **open-source software** where public visibility, forks, and issues are essential.
- You already operate a dedicated, fully-maintained Gitea or GitLab instance with dedicated administration.

#### Choose `git-remote-seafile` when:
- You already run Seafile and want **instant, zero-maintenance Git remotes** without deploying or maintaining a separate Git forge stack.
- You work on **private personal projects, research, proprietary code, or solitary workflows** across multiple personal computers (desktop, laptop, workstation).
- You manage **large datasets, ML model weights, game assets, or video files (via Git LFS)** that would exceed GitHub's expensive storage and bandwidth quotas.
- You require an **offsite, private disaster recovery backup** for existing repositories.

### 1.3 The Best of Both Worlds: Dual-Remote Setup

You are not locked into one solution. A highly effective workflow is to push to GitHub for collaboration/CI while maintaining an automated private mirror on Seafile:

```bash
# Configure origin with multiple push URLs:
git remote set-url --add --push origin git@github.com:myorg/myproject.git
git remote set-url --add --push origin seafile://code/myproject

# Now a single push command updates both GitHub and your private Seafile server:
git push origin main
```

---

## 2. Comprehensive Analysis of Alternative Workarounds & Why They Fail

Before developing `git-remote-seafile`, extensive real-world testing and diagnostic auditing were conducted on various workarounds. Below is the technical post-mortem of why each alternative fails:

### 2.1 Workaround 1: Live Working Tree in Synced Seafile Folder
- **How it works**: Storing an active Git repository directly inside a synced Seafile directory.
- **Success Case**: Works only as an offline single-machine backup if no other device touches the library and no rapid Git operations take place.
- **Why it Fails (Real-World Evidence)**:
  - **Re-commit Storms**: A real audit of a live Seafile installation (`9.0.21`) measured **1,606 full re-commit cycles in a single day** on a code library—approximately one full library re-index every 10 seconds.
  - **Lockfile Deadlock**: Ephemeral locks (`.git/index.lock`, `HEAD.lock`) are uploaded before Git can release them, downloading onto secondary machines and freezing Git operations with `fatal: Unable to create '.git/index.lock': File exists`.
  - **The 1,749-File Conflict Storm**: On 2026-10-01, branch divergence caused Seafile to create **1,749 `(SFConflict ...)` files**. Because conflict copies preserve the original file extension (e.g. `test_suite (SFConflict ...).py`), test runners like `pytest` immediately collected the duplicate files, causing catastrophic test pollution and broken builds.

### 2.2 Workaround 2: Pausing Sync During Development (`Disable auto sync` / `seaf-cli stop`)
- **How it works**: Developer pauses sync on the library before coding, commits changes, and unpauses sync when finished.
- **Success Case**: Eliminates disk churn on the local machine *during* active editing.
- **Why it Fails**:
  - **Sync pause is strictly local**: Pausing auto-sync on Machine A does nothing to Machine B. Machine B remains active and continues to modify or sync files.
  - **Unpause shockwaves**: When Machine A unpauses after multiple commits or branch switches, hundreds of file modifications hit the Seafile server simultaneously. If Machine B has touched anything, Seafile's conflict resolution triggers massive duplicate copies because Seafile cannot perform Git 3-way merges.
  - **Human error**: Developers inevitably forget to pause before running `git checkout` or `npm install`, or forget to unpause, leaving work unbacked up.

### 2.3 Workaround 3: Seafile Ignore Rules (`seafile-ignore.txt`)
- **How it works**: Placing `.git/` or `code/` patterns in `seafile-ignore.txt`.
- **Success Case**: Effectively ignores non-synced directories like `node_modules/` or build artifacts *if configured prior to the library's initial creation*.
- **Why it Fails**:
  1. **The "Already Synced" Rule**: As explicitly documented in the official Seafile manual, `seafile-ignore.txt` only affects files that have **never been synced**. If `.git/` was ever uploaded, adding it to the ignore file is completely ignored. The only fix is to delete and re-sync the entire library.
  2. **Root Placement Limitation**: Ignore files **must** sit in the library root (e.g. `Documents/seafile-ignore.txt`). Placing ignore files in subfolders (e.g. `Documents/code/myrepo/seafile-ignore.txt`) is silently ignored by the client daemon.
  3. **Dot-Prefixed Folder Bug**: Seafile client has a long-standing parser issue with dot-prefixed directories (`haiwen/seafile#1137`), frequently failing to match patterns like `.git/` or `*/.git/*`.
  4. **VCS Amputation**: If `.git/` is ignored while source code is synced, secondary machines receive source files without any Git repository! Developers on secondary machines lose all branch history, commit capabilities, and version control.

### 2.4 Workaround 4: Generic Cloud Sync (Dropbox, OneDrive, Google Drive, Syncthing)
- **How it works**: Storing active repositories inside Dropbox, OneDrive, Google Drive, or Syncthing.
- **Success Case**: Basic single-file text document backups.
- **Why it Fails**:
  - **Broken Transaction Atomicity**: Git requires multi-file atomicity (updating index, writing packfiles, updating ref pointers in a strict order). Cloud sync engines sync files asynchronously in arbitrary order, exposing inconsistent repository states to other machines.
  - **Reparse Points & Files-On-Demand**: Features like OneDrive "Files On-Demand" replace idle Git objects with offline NTFS reparse points/placeholders. When Git executes `git status` or `git rev-parse`, accessing dehydrated objects fails with `ERROR_CANT_ACCESS_FILE` or severe command latency.
  - **Corrupted Loose Objects**: Conflict files generated in `.git/objects/` or `.git/refs/` cause fatal Git errors: `fatal: bad object HEAD` or `error: object file ... is empty`.

### 2.5 Workaround 5: Git Bundles in Synced Folders (`git bundle create`)
- **How it works**: Working repository is kept outside cloud storage. Developer periodically runs `git bundle create myproject.bundle --all` and saves the single `.bundle` file into a Seafile synced folder.
- **Success Case**: Fully safe from lockfile contention and `.git/` file churn because a `.bundle` is a single, self-contained binary archive.
- **Why it Fails for Daily Development**:
  - Terrible ergonomics: Requires manual bundling and unbundling commands for every single push or pull.
  - No standard Git commands: You cannot run `git push`, `git pull`, or `git fetch`.
  - No remote branch tracking, conflict warnings, or automated fast-forward validation.

### 2.6 Workaround 6: Bare Git Repositories in Synced Folders (`git init --bare`)
- **How it works**: Working tree lives outside Seafile. A bare repository (`myproject.git`) is placed inside a synced Seafile folder and accessed via local file URLs (`git push file:///C:/path/to/synced-folder/git-bare/myproject.git`).
- **Success Case**: Isolates working tree churn from the sync client.
- **Why it Fails**:
  - Bare repositories still contain hundreds of loose object files and reference files (`refs/heads/*`).
  - When two machines push to their respective local synced copies of the bare repo, Seafile syncs loose objects out of order and creates `(SFConflict ...)` copies on `refs/heads/main`, corrupting branch pointers.

### 2.7 Summary Matrix of All Evaluated Solutions

| Solution | Working Tree Churn | Lock Safety | Multi-Machine Merge | Ergonomics | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Live Sync in Seafile** | ❌ 1,500+ cycles/day | ❌ Freezes `.git/*.lock` | ❌ 1,749 SFConflict files | Normal Git CLI | ❌ Unusable |
| **Sync Pause / Resume** | ⚠️ High on resume | ❌ Collisions on unpause | ❌ Destructive conflicts | Manual toggle | ❌ Unviable |
| **Seafile Ignore Rules** | ❌ Buggy on dot-dirs | ❌ Doesn't fix synced dirs | ❌ Desyncs `.git/` from code | Config file | ❌ Unviable |
| **OneDrive / Dropbox** | ❌ File placeholder lag | ❌ Corrupts refs & index | ❌ Broken commit graphs | Normal Git CLI | ❌ Unusable |
| **Git Bundles in Sync** | ✅ Zero | ✅ Safe single file | ⚠️ Manual unpack | ❌ Manual bundle cmds | ⚠️ Archive only |
| **Bare Repo in Sync** | ⚠️ Moderate churn | ❌ Ref file conflicts | ❌ Async ref collisions | Standard Git CLI | ❌ Fragile |
| **Move Code + Git Forge** | ✅ Zero | ✅ Server-side locks | ✅ Git 3-way merge | Full Web UI + CLI | ✅ Recommended (Teams/PRs) |
| **Move Code + git-remote-seafile** | ✅ Zero | ✅ Distributed lease lock | ✅ Git 3-way merge | Native Git CLI | ✅ Recommended (Private/Self-hosted) |

---

