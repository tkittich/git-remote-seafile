# git-remote-seafile User's Guide

`git-remote-seafile` is a transparent Git remote helper that allows you to use any private Seafile server (Community Edition or Professional) as an authentic Git remote.

---

## 1. Why `git-remote-seafile`?

Traditionally, developers tried to share code across machines with Seafile by putting their active working folder directly inside a folder mapped to a Seafile library (e.g. `C:\Users\<username>\Seafile\...` or a synced folder).

This causes severe issues:
- **Client Thrashing**: A single `git commit` touches 15+ files inside `.git/` in milliseconds, triggering continuous 65 MB index re-commit cycles in the desktop client (sometimes 1,500+ cycles a day).
- **Lock Contention**: Ephemeral locks like `.git/index.lock` get synced to other computers, freezing local Git operations.
- **Conflict Storms**: When two machines edit code, Seafile creates hundreds of `(SFConflict ...)` files instead of merging.

### How `git-remote-seafile` Solves This
`git-remote-seafile` bypasses the desktop sync client entirely:
- Your working repository lives anywhere on your disk (outside synced libraries).
- It communicates directly with the **Seafile Web API v2.1** over HTTP/HTTPS.
- Commits are bundled into native Git packfiles (`.pack` and `.idx`) before upload. Each push transfers only **1–2 files** regardless of commit count.
- The desktop sync daemon never scans, indexes, or touches `.git/`.

---

## 2. The Golden Rule: Working Tree Placement

> [!CAUTION]
> ### CRITICAL: Never Store Your Working Repository Inside a Synced Seafile Folder!
> 
> Even when using `git-remote-seafile`, your local working repository (the directory where you edit files and where `.git/` resides) **MUST NOT** be placed inside any directory synced by the Seafile desktop client (such as `C:\Users\<username>\Seafile\...`, `~/Seafile/...`, or any folder mapped to a library in the desktop client).
> 
> Instead, keep your working repository in a standard, **un-synced** directory on your drive (e.g., `C:\code\myproject`, `C:\Projects\myproject`, or `~/code/myproject`).
> 
> *(Note: If your personal `Documents` folder is **not** synced by Seafile, it is completely safe to use. The requirement is simply that the Seafile desktop sync client must not be watching or syncing that directory).*

### 2.1 The Architectural Distinction

To understand why this rule is absolute, examine how the two architectures operate:

#### ❌ The Flawed Architecture (Working Tree Stored in a Synced Folder)
```
[Local Disk]
C:\path\to\synced-seafile-folder\myproject\   <-- Synced Library Folder
  ├── src/
  └── .git/                                  <-- Seafile Desktop Client watches every file change!
        ├── index.lock                       <-- Synced to other machines, freezing Git operations!
        └── objects/                         <-- 15+ files touched per commit = 1,500+ re-index cycles/day!
```

When your active repository sits inside a synced library, the Seafile desktop client registers operating system filesystem notifications (`ReadDirectoryChangesW` on Windows, `inotify` on Linux, `FSEvents` on macOS). Every time you run `git commit`, `git checkout`, or compile code, the client immediately scrambles to scan, checksum, and upload your internal `.git/` files. This causes:
1. **Endless Churn**: 1,500+ full library re-commit cycles per day, pegging CPU and disk I/O.
2. **Cross-Machine Freezes**: Ephemeral lock files like `.git/index.lock` sync to your other computers, blocking Git with `fatal: Unable to create '.git/index.lock': File exists`.
3. **Conflict File Explosions**: Switching branches touches hundreds of files at once. If another machine is even slightly delayed in syncing, Seafile creates destructive `(SFConflict ...)` copies across your entire project.

#### ✅ The Correct Architecture (git-remote-seafile)
```
[Local Disk (Unsynced)]
C:\code\myproject\                    <-- Seafile Client NEVER touches this!
  ├── src/
  └── .git/                          <-- Git operates at native NVMe/SSD speed with zero daemon overhead.

      │ git push origin main
      ▼ (HTTP/HTTPS REST API v2.1)
[Seafile Server]
https://seafile.example.com
  └── Library: code
        └── /myproject/               <-- Clean, bare Git packfiles and refs stored on the server!
```

With `git-remote-seafile`, your local repository operates completely free of cloud sync daemons. When you run `git push`, the helper bundles your commits into compressed Git packfiles and transfers them directly to your Seafile server over HTTP/HTTPS Web API v2.1—exactly like pushing to GitHub or GitLab.

### 2.2 Step-by-Step: Migrating an Existing Synced Project

If you currently have a project inside a Seafile-synced directory, follow these steps to migrate it safely:

1. **Pause or finish sync**: In the Seafile desktop client, ensure the library is done syncing, or right-click the library and select **Disable auto sync**.
2. **Move the project folder out**: Move the entire project folder to an unsynced location:
   - **Windows (PowerShell)**:
     ```powershell
     Move-Item "C:\path\to\synced-folder\myproject" "C:\code\myproject"
     ```
   - **Linux / macOS**:
     ```bash
     mv ~/Seafile/code/myproject ~/code/myproject
     ```
3. **Create or designate your remote library on Seafile**:
   - **Option A (Recommended)**: Log in to the Seafile Web UI (e.g. `https://seafile.example.com`), click **New Library**, and choose any name you like (such as `code`, `git-repos`, or `projects`). In our examples, we use `code`. **Do NOT sync this library** in your Seafile desktop client applet.
   - **Option B (Using existing synced library, e.g. `Documents`)**: If you prefer keeping the bare remote inside an existing synced library, store it under an ignored subfolder (such as `seafile-git/`, `git-vault/`, or any folder name of your choice) and add that subfolder name to `<library-root>/seafile-ignore.txt` **before** pushing. *(Note: `seafile-ignore.txt` only works at the library root and only on files that have never been synced yet).*
4. **Navigate to the new location**:
   ```bash
   cd C:/code/myproject  # or cd ~/code/myproject
   ```
5. **Set the Seafile remote URL**:
   ```bash
   git remote remove origin  # if origin pointed to old sync location

   # Option A (Dedicated unsynced library):
   git remote add origin seafile://code/myproject

   # Option B (Ignored subfolder in synced library, e.g. seafile-git/):
   # git remote add origin seafile://Documents/seafile-git/myproject
   ```
6. **Push your branches**:
   ```bash
   git push -u origin main
   ```
7. **Re-enable sync**: In your Seafile desktop client, re-enable auto sync. Your active working files and `.git/` folder are now completely isolated from sync interference!

### 2.3 Two Supported Architecture Workflows

Depending on your library organization, two safe workflows are supported:

#### Workflow A (Recommended): Dedicated Unsynced Library
Create a dedicated library on your Seafile server with any name you like (such as `code`, `git-repos`, or `projects`) that is **never synced** to your local desktop client:
- **Local working tree**: `C:\code\myproject` or `D:\Dev\myproject` (outside synced libraries)
- **Remote destination**: `seafile://code/myproject`
- **Advantages**: Complete physical and logical separation. Zero chance of desktop client sync collisions or disk reflection.

#### Workflow B: Ignored Subfolder in a Synced Library (e.g. `seafile-git/`)
If you prefer keeping remote repositories within an existing synced library (such as `Documents`), or your local working tree is inside an ignored subfolder like `Documents/code/myproject`:
1. Store remote repositories under an ignored subfolder (we suggest **`seafile-git/`**, but any name such as `git-vault/` or `git-remotes/` works):
   ```bash
   git remote add origin seafile://Documents/seafile-git/myproject
   ```
2. Add the subfolder name (e.g. `seafile-git/`) to `seafile-ignore.txt` at the root of the synced library (`<library-root>/seafile-ignore.txt`).

> [!WARNING]
> **Two Critical Traps of `seafile-ignore.txt`**:
> 1. **Root Library Only**: `seafile-ignore.txt` **must** be placed directly in the root directory of the synced library (e.g. `<library-root>/seafile-ignore.txt`). Unlike Git's `.gitignore`, placing `seafile-ignore.txt` in a subfolder (such as `Documents/seafile-git/seafile-ignore.txt`) is silently ignored by the Seafile desktop client daemon!
> 2. **Never-Synced Files Only (No Retroactive Ignore)**: As documented in the official Seafile manual, `seafile-ignore.txt` **only affects files and folders that have never been synced**. If the subfolder or files were already synced to the server or local disk prior to adding the ignore rule, Seafile will continue syncing them regardless! Adding ignore rules does *not* untrack or delete already-synced files. (If this happens, you must delete the folder locally and remotely or un-sync and re-sync the entire library).
> 
> **Why Workflow A is Recommended**: This is why **Workflow A (Dedicated Unsynced Library)** is strongly recommended over Workflow B. An unsynced library bypasses all `seafile-ignore.txt` limitations, root placement requirements, and retroactive sync traps entirely.

> [!NOTE]
> **Is `seafile-git/` mandatory?**
> **No.** The folder name `seafile-git/` is not mandatory or hardcoded in `git-remote-seafile`. You can name this subfolder whatever you prefer (e.g. `git-vault/`, `my-remotes/`, `remote-repos/`). Any subfolder works as long as it is listed in `<library-root>/seafile-ignore.txt` so the Seafile desktop client does not sync it.
> 
> We suggest `seafile-git/` in our examples as an explicit, descriptive convention rather than dot-prefixed names like `.git-remotes` (which resemble Git internal plumbing and can confuse developers and tooling).

### 2.4 Automated Pre-Flight Safety Guardrails

`git-remote-seafile` includes built-in automated pre-flight checks that inspect your local Seafile desktop client configuration (`repo.db`) and active Git working tree to protect you from common misconfigurations:

#### 🛡️ Trap 1: Working Tree & Remote Path Collision (Hard Block)
- **The Hazard**: Setting your remote URL to the exact same library path as your local working tree (e.g., local code at `C:\Users\<username>\Documents\code\myproject` and remote at `seafile://Documents/code/myproject`).
- **The Consequence**: Pushing uploads bare Git packfiles and refs (`objects/pack/`, `refs/heads/main`) to the server. The desktop sync client would see these files on the server and download them directly into your working copy, corrupting your index, spraying bare packfiles across your project, and creating sync conflicts.
- **The Guardrail**: The helper detects the collision between your local working tree and the remote destination and **aborts the push immediately**, suggesting either an unsynced library or an ignored subfolder (`seafile-git/`).

#### 🛡️ Trap 2: Download Reflection in Synced Library (Hard Block)
- **The Hazard**: Pushing to a remote path inside an actively synced library when that subfolder is NOT ignored in `seafile-ignore.txt` (e.g., remote is `seafile://Documents/seafile-git/myproject` but `seafile-git/` is missing from `seafile-ignore.txt`).
- **The Consequence**: Every push uploads packfiles to Seafile server; seconds later, your desktop client detects them on the server and downloads them back down to your local drive. This wastes disk space, network bandwidth, and triggers CPU churn.
- **The Guardrail**: The helper parses `seafile-ignore.txt` at the library root. If the remote path is not explicitly ignored, it **aborts the push and displays exact instructions** on how to add the folder to `seafile-ignore.txt`.
  > [!IMPORTANT]
  > **Remember the Two Rules of `seafile-ignore.txt`**:
  > - **Must be placed at the library root**: It must be `<library-root>/seafile-ignore.txt`, not inside a subfolder.
  > - **Must be configured before first sync**: It only ignores never-synced files. If the subfolder was already synced by the desktop client, the ignore rule has no effect retroactively.

#### 🛡️ Guardrails Also Cover Clone and Fetch
A `git clone` writes a whole repository into the target directory, so the same collisions matter there — and they used to go unchecked, because the guardrails only ran on push. `git clone seafile://Documents/code/myproject` executed from inside the synced `Documents/` library would download packfiles straight into the synced tree and start the reflection cycle described above.

Trap 1 therefore blocks clone and fetch as well. Trap 2 stays push-only **by design**: it guards against *uploading* into a synced folder, and a fetch uploads nothing.

> [!NOTE]
> During `git clone` the helper runs in the *parent* of the directory being created, which is not yet a Git repository, so Git cannot name a working tree for it. The helper falls back to its own working directory — which is exactly the directory that needs checking.

#### 🛡️ Library Root Pollution Protection
- Pushing directly to the library root (`seafile://Documents/`) is hard-blocked to prevent cluttering the top level with bare objects and refs.

#### 🛡️ Library Typo Detection & Suggestions
- If you misspell a library name in the remote URL (e.g. `seafile://docment/myproject`), the helper queries available libraries on the server and provides fuzzy-matched suggestions (`Did you mean: Documents?`).

#### 🔍 Pre-Flight Verification via CLI
You can test any remote URL against your local environment before pushing:
```bash
git-remote-seafile check-safety seafile://Documents/seafile-git/myproject
```

#### ⚙️ Bypassing Safety Checks (CI / Headless Environments)
In automated CI runners or headless servers without a local desktop client, safety checks gracefully pass automatically. If you ever need to manually bypass checks:
- **Environment variable**: `export SEAFILE_SKIP_SAFETY_CHECKS=1` (or `$env:SEAFILE_SKIP_SAFETY_CHECKS="1"` on Windows)
- **Git configuration**: `git config seafile.skipsafetychecks true`

---

## 3. In-Depth Comparison: `git-remote-seafile` vs. "Move Code Out + Git Remotes"

When moving code out of synced cloud folders, developers face an architectural choice:
1. **Move Code Out + Traditional Git Remotes** (Push to GitHub, GitLab, Gitea, Forgejo)
2. **Move Code Out + `git-remote-seafile`** (Push to private Seafile server via this helper)

Both approaches eliminate desktop sync thrashing and lock contention. Below is a detailed technical comparison of their trade-offs:

### 3.1 Detailed Feature & Architectural Comparison

| Dimension | Move Code Out + Git Forge (GitHub / Gitea) | Move Code Out + `git-remote-seafile` |
| :--- | :--- | :--- |
| **New Infrastructure** | **High / Moderate.** Requires creating an external SaaS account (GitHub) or provisioning, securing, and maintaining a standalone server (Gitea/GitLab, database, SSH daemon, reverse proxy, backups). | **Zero.** Reuses your existing Seafile server, user authentication, and storage backend with no new services to manage. |
| **Data Sovereignty & Privacy** | **Conditional.** SaaS keeps code on third-party servers subject to foreign jurisdiction and AI scrapers. Self-hosted forges require ongoing CVE patching. | **100% Private & Self-Hosted.** Commits and files stay within your Seafile instance. Transfers secured via TLS/HTTPS (client-side password-encrypted libraries are not supported over REST API). |
| **Storage Limits & Economics** | **Restricted / Costly.** GitHub enforces soft 1–2 GB repo limits; Git LFS costs $5/mo per 50 GB. Self-hosted forges consume separate disk pools. | **Unrestricted.** Utilizes your existing Seafile storage quota (often multi-terabyte). Native Git LFS custom transfer agent with zero extra fees. |
| **Code Review & Collaboration** | **Full Web Forge UI.** Pull requests, inline comments, code search, issue tracking, and automated CI/CD runners (GitHub Actions, GitLab CI). | **Storage & Transport Layer Only.** No built-in web code review UI. Focuses purely on reliable Git transport, distributed locking, and ref sync. |
| **Transport Protocol** | Git Smart HTTP (`git-upload-pack`, `git-receive-pack`) or SSH protocol. Server negotiates deltas dynamically. | Client-side packfile packaging over Seafile Web API v2.1 (`/api2/repos/.../upload-link/`). Packfiles stored as `.pack` objects. |
| **Concurrency & Safety** | Handled natively by server Git process with atomic ref updates. | Enforced by fast-forward checks and cooperative distributed lease locking (`.git-lock.json` with auto-expiry). |
| **Client-Side Daemon Churn** | Zero. Working tree is outside sync folders. | Zero. Working tree is outside sync folders. |

### 3.2 Decision Guide: Which Should You Use?

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

### 3.3 The Best of Both Worlds: Dual-Remote Setup

You are not locked into one solution. A highly effective workflow is to push to GitHub for collaboration/CI while maintaining an automated private mirror on Seafile:

```bash
# Configure origin with multiple push URLs:
git remote set-url --add --push origin git@github.com:myorg/myproject.git
git remote set-url --add --push origin seafile://code/myproject

# Now a single push command updates both GitHub and your private Seafile server:
git push origin main
```

---

## 4. Comprehensive Analysis of Alternative Workarounds & Why They Fail

Before developing `git-remote-seafile`, extensive real-world testing and diagnostic auditing were conducted on various workarounds. Below is the technical post-mortem of why each alternative fails:

### 4.1 Workaround 1: Live Working Tree in Synced Seafile Folder
- **How it works**: Storing an active Git repository directly inside a synced Seafile directory.
- **Success Case**: Works only as an offline single-machine backup if no other device touches the library and no rapid Git operations take place.
- **Why it Fails (Real-World Evidence)**:
  - **Re-commit Storms**: A real audit of a live Seafile installation (`9.0.21`) measured **1,606 full re-commit cycles in a single day** on a code library—approximately one full library re-index every 10 seconds.
  - **Lockfile Deadlock**: Ephemeral locks (`.git/index.lock`, `HEAD.lock`) are uploaded before Git can release them, downloading onto secondary machines and freezing Git operations with `fatal: Unable to create '.git/index.lock': File exists`.
  - **The 1,749-File Conflict Storm**: On 2026-10-01, branch divergence caused Seafile to create **1,749 `(SFConflict ...)` files**. Because conflict copies preserve the original file extension (e.g. `test_suite (SFConflict ...).py`), test runners like `pytest` immediately collected the duplicate files, causing catastrophic test pollution and broken builds.

### 4.2 Workaround 2: Pausing Sync During Development (`Disable auto sync` / `seaf-cli stop`)
- **How it works**: Developer pauses sync on the library before coding, commits changes, and unpauses sync when finished.
- **Success Case**: Eliminates disk churn on the local machine *during* active editing.
- **Why it Fails**:
  - **Sync pause is strictly local**: Pausing auto-sync on Machine A does nothing to Machine B. Machine B remains active and continues to modify or sync files.
  - **Unpause shockwaves**: When Machine A unpauses after multiple commits or branch switches, hundreds of file modifications hit the Seafile server simultaneously. If Machine B has touched anything, Seafile's conflict resolution triggers massive duplicate copies because Seafile cannot perform Git 3-way merges.
  - **Human error**: Developers inevitably forget to pause before running `git checkout` or `npm install`, or forget to unpause, leaving work unbacked up.

### 4.3 Workaround 3: Seafile Ignore Rules (`seafile-ignore.txt`)
- **How it works**: Placing `.git/` or `code/` patterns in `seafile-ignore.txt`.
- **Success Case**: Effectively ignores non-synced directories like `node_modules/` or build artifacts *if configured prior to the library's initial creation*.
- **Why it Fails**:
  1. **The "Already Synced" Rule**: As explicitly documented in the official Seafile manual, `seafile-ignore.txt` only affects files that have **never been synced**. If `.git/` was ever uploaded, adding it to the ignore file is completely ignored. The only fix is to delete and re-sync the entire library.
  2. **Root Placement Limitation**: Ignore files **must** sit in the library root (e.g. `Documents/seafile-ignore.txt`). Placing ignore files in subfolders (e.g. `Documents/code/myrepo/seafile-ignore.txt`) is silently ignored by the client daemon.
  3. **Dot-Prefixed Folder Bug**: Seafile client has a long-standing parser issue with dot-prefixed directories (`haiwen/seafile#1137`), frequently failing to match patterns like `.git/` or `*/.git/*`.
  4. **VCS Amputation**: If `.git/` is ignored while source code is synced, secondary machines receive source files without any Git repository! Developers on secondary machines lose all branch history, commit capabilities, and version control.

### 4.4 Workaround 4: Generic Cloud Sync (Dropbox, OneDrive, Google Drive, Syncthing)
- **How it works**: Storing active repositories inside Dropbox, OneDrive, Google Drive, or Syncthing.
- **Success Case**: Basic single-file text document backups.
- **Why it Fails**:
  - **Broken Transaction Atomicity**: Git requires multi-file atomicity (updating index, writing packfiles, updating ref pointers in a strict order). Cloud sync engines sync files asynchronously in arbitrary order, exposing inconsistent repository states to other machines.
  - **Reparse Points & Files-On-Demand**: Features like OneDrive "Files On-Demand" replace idle Git objects with offline NTFS reparse points/placeholders. When Git executes `git status` or `git rev-parse`, accessing dehydrated objects fails with `ERROR_CANT_ACCESS_FILE` or severe command latency.
  - **Corrupted Loose Objects**: Conflict files generated in `.git/objects/` or `.git/refs/` cause fatal Git errors: `fatal: bad object HEAD` or `error: object file ... is empty`.

### 4.5 Workaround 5: Git Bundles in Synced Folders (`git bundle create`)
- **How it works**: Working repository is kept outside cloud storage. Developer periodically runs `git bundle create myproject.bundle --all` and saves the single `.bundle` file into a Seafile synced folder.
- **Success Case**: Fully safe from lockfile contention and `.git/` file churn because a `.bundle` is a single, self-contained binary archive.
- **Why it Fails for Daily Development**:
  - Terrible ergonomics: Requires manual bundling and unbundling commands for every single push or pull.
  - No standard Git commands: You cannot run `git push`, `git pull`, or `git fetch`.
  - No remote branch tracking, conflict warnings, or automated fast-forward validation.

### 4.6 Workaround 6: Bare Git Repositories in Synced Folders (`git init --bare`)
- **How it works**: Working tree lives outside Seafile. A bare repository (`myproject.git`) is placed inside a synced Seafile folder and accessed via local file URLs (`git push file:///C:/path/to/synced-folder/git-bare/myproject.git`).
- **Success Case**: Isolates working tree churn from the sync client.
- **Why it Fails**:
  - Bare repositories still contain hundreds of loose object files and reference files (`refs/heads/*`).
  - When two machines push to their respective local synced copies of the bare repo, Seafile syncs loose objects out of order and creates `(SFConflict ...)` copies on `refs/heads/main`, corrupting branch pointers.

### 4.7 Summary Matrix of All Evaluated Solutions

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

## 5. Installation

### Requirements
- Python 3.9+
- Git 2.20+
- `requests` library

### Windows Installation
1. Clone or download `git-remote-seafile`:
   ```powershell
   git clone https://github.com/tkittich/git-remote-seafile.git
   cd git-remote-seafile
   pip install -e .
   ```
2. Ensure the generated executable `git-remote-seafile.exe` is in your `PATH` (e.g. in your Python `Scripts\` folder or your personal `bin\` folder).

### Linux & macOS Installation
```bash
git clone https://github.com/tkittich/git-remote-seafile.git
cd git-remote-seafile
pip install -e .
```
Ensure `~/.local/bin` (or your Python bin path) is in your `$PATH`.

---

## 6. Authentication & Configuration

`git-remote-seafile` resolves authentication automatically through three fallback tiers:

### Method A: Zero-Config (Desktop & Terminal Client Auto-Discovery)
If you run Seafile on your machine, `git-remote-seafile` automatically detects your active login token:
- **Windows**: `%USERPROFILE%/Seafile/seafile-data/accounts.db`, `%USERPROFILE%/ccnet/accounts.db`, or `<data-drive>/Seafile/seafile-data/accounts.db`
- **Linux (`seaf-cli` & GUI client)**: `~/.ccnet/accounts.db`, `~/.seafile-data/accounts.db`, or `~/.config/seafile/accounts.db`
- **macOS**: `~/.seafile-data/accounts.db` or `~/Library/Application Support/Seafile/accounts.db`

No manual configuration or token entry is required!

### Method B: Configuration File (`~/.git-seafile.json`)
Ideal for headless servers, secondary machines, or CI runners:
Create `~/.git-seafile.json` in your user home directory:
```json
{
  "server": "https://seafile.example.com",
  "token": "d82f8a19234857bf9e349204958671bc9842"
}
```

> **How to get an API Token**:
> In Seafile Web UI, go to **Settings** → **API Token**, or run:
> ```bash
> curl -d "username=user@example.com&password=yourpassword" https://seafile.example.com/api2/auth-token/
> ```

### Method C: Environment Variables
Useful for automated scripts and CI pipelines:
```bash
export SEAFILE_SERVER="https://seafile.example.com"
export SEAFILE_TOKEN="d82f8a19234857bf9e349204958671bc9842"
```

### Verifying Authentication
Run the built-in diagnostic check:
```bash
git-remote-seafile check-auth
```
Expected output:
```text
Authenticated successfully with https://seafile.example.com as user@example.com
```

### Behavioural Configuration

| Git config key | Environment variable | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `seafile.skipsafetychecks` | `SEAFILE_SKIP_SAFETY_CHECKS` | `false` | Bypass the pre-flight guardrails (Trap 1, Trap 2, root pollution). |
| `seafile.autogc` | — | `false` | Compact the remote automatically once the packfile threshold is reached. |
| `seafile.gcthreshold` | — | `20` | Packfile count at which the `gc` tip appears (or auto-GC triggers). |
| `seafile.forcefilehost` | `SEAFILE_FORCE_FILE_HOST` | `false` | Force download/upload links onto your server's **host** (a wrong scheme on the same host is always corrected). |
| `seafile.locktimeout` | — | `15` | Maximum seconds to wait when acquiring the remote lock during push. |
| `seafile.locklease` | — | `60` | Duration in seconds before an inactive lock lease is considered expired. |

```bash
# Examples
git config seafile.autogc true
git config seafile.gcthreshold 25
git config seafile.forcefilehost true
git config seafile.locktimeout 30
git config seafile.locklease 120
```

**`seafile.forcefilehost`** deserves a note. Seafile returns a short-lived URL for every file transfer, and there are three cases to tell apart:

- A **relative** link (a bare path rather than a full URL) is always completed against your server, regardless of this setting. That is not a rewrite — it is the only way the link can be used at all.
- A link naming **the same host and port as your server but over plain `http`**, while your server is `https`, is corrected to `https` automatically. This is a common reverse-proxy artefact and it cannot be left alone: an upload `POST` sent to the `http` URL is answered with a redirect, and a redirected `POST` is re-issued as a `GET`, which the upload endpoint rejects with `HTTP 400`. Downloads survive the same redirect, uploads do not — which is why only pushes break. The correction is one-way: `https` is never downgraded to `http`.
- A link naming a **different host** is left exactly as the server sent it. A **clustered or object-store-backed** deployment genuinely serves files from a different host, and forcing it onto the API host would make it 404. If instead you have a **reverse proxy** returning an internal host (`http://internal-docker-host:8082/...`) that your machine cannot reach, set this to `true` to rewrite the link's host onto your configured server.

The host comparison is exact, port included: `http://your-server:8082/...` is a different endpoint rather than a mistyped scheme, and is left alone.

---

## 7. URL Syntax

Remotes use the `seafile://` URL scheme:

| Format | Example | Notes |
| :--- | :--- | :--- |
| **Full URL** | `seafile://seafile.example.com/code/myproject` | Explicit server, library, and path |
| **HTTPS scheme** | `seafile://https://seafile.example.com/code/myproject` | Fully qualified URL |
| **Short URL** | `seafile://code/myproject` | Uses server from default configured account |

- **Library**: Name of the Seafile library (e.g. `code` or `Documents`) or the library UUID. **The library must exist on your Seafile server before pushing.** (Create it via the Seafile Web UI if you haven't already).
- **Path**: Path inside the library where bare repository objects will reside.

---

## 8. Daily Workflows

### 8.0 Prerequisite: Create Your Remote Library on Seafile
Before pushing your first repository to Seafile:
1. Log into your Seafile Web UI (`https://seafile.example.com`).
2. Click **New Library** and choose any name you like (such as `code`, `git-repos`, or `projects`). We will use `code` in the examples below.
3. **Important**: Leave this library **unsynced** in your Seafile desktop client applet. Do not sync it to your local drive.
*(Alternatively, if storing within an existing synced library like `Documents`, store under an ignored subfolder such as `seafile-git/` and add it to `<library-root>/seafile-ignore.txt` before pushing, noting that `seafile-ignore.txt` only applies at the library root and cannot retroactively ignore already-synced folders).*

### 8.1 Push an Existing Project to Seafile
```bash
cd C:\code\myproject

# Add the Seafile remote
git remote add origin seafile://code/myproject

# Push and set upstream tracking
git push -u origin main
```

### 8.2 Clone on Another Machine
On your laptop or secondary computer (with `git-remote-seafile` installed):
```bash
git clone seafile://seafile.example.com/code/myproject
```

### 8.3 Normal Fetch, Pull, and Push
All standard Git commands work transparently:
```bash
git pull origin main
git push origin feature-branch
git push origin :old-branch   # Deletes remote branch
```

### 8.4 Set or Change Remote Default Branch (`set-head`)
If you want to designate or change the default branch checked out on clone (e.g. from `master` to `main`, or to `dev`):
```bash
git-remote-seafile set-head seafile://code/myproject main
```

### 8.5 Dual-Remote Setup (GitHub + Seafile)
You can maintain your primary open-source or team remote on GitHub while maintaining a private mirror on Seafile:

```bash
# Add both remotes
git remote add github git@github.com:username/myproject.git
git remote add seafile seafile://code/myproject

# Push to either
git push github main
git push seafile main
```

Or configure `origin` to push to both targets with one command:
```bash
git remote set-url --add --push origin git@github.com:username/myproject.git
git remote set-url --add --push origin seafile://code/myproject

# Now this updates both destinations:
git push origin main
```

---

## 9. Concurrency & Conflict Prevention

`git-remote-seafile` enforces standard **fast-forward safety**:

1. If User B pushes changes to `main` while User A is working:
2. User A attempts `git push origin main`.
3. `git-remote-seafile` verifies the remote ref. Seeing that the remote branch has advanced, it rejects the push:
   ```text
   To seafile://code/myproject
    ! [rejected]        main -> main (non-fast-forward)
   error: failed to push some refs
   ```
4. User A simply pulls and merges/rebases:
   ```bash
   git pull --rebase origin main
   git push origin main
   ```
5. **No conflict files** (`SFConflict`) are ever created. Git handles all line-level merging natively.

---

## 10. Remote Packfile Compaction & Auto-GC (`git-remote-seafile gc`)

Each `git push` uploads a small `.pack` file containing the newest commits. Over time (e.g. after 50+ pushes), cloning may download dozens of small packfiles.

### 10.1 Manual Compaction
You can compact all packfiles on Seafile into a single optimized, delta-compressed packfile at any time:

```bash
git-remote-seafile gc seafile://code/myproject
```

- **Options**: `--min-packs N` (default: 2) — only repack if at least $N$ packfiles exist.
- Automatically locks the remote repository during compaction to prevent push conflicts.
- Deletes obsolete superseded packfiles from Seafile once the consolidated packfile is confirmed uploaded.

### 10.2 Automatic Detection & Notification (Default)
By default, whenever you push, `git-remote-seafile` checks the number of remote packfiles:
- If packfile count reaches **20** (or custom threshold), it prints a non-intrusive tip to your terminal:
  ```text
  Notice: Remote repository has 22 packfiles (threshold: 20).
  Tip: Run 'git-remote-seafile gc seafile://...' to optimize remote storage,
       or run 'git config seafile.autogc true' to enable automatic compaction.
  ```
- Your normal pushes remain instant (<1s) without unexpected network delays.

### 10.3 Opt-In Automatic Compaction (`seafile.autogc`)
If you want `git-remote-seafile` to automatically run compaction during `git push` whenever the threshold is reached:

```bash
# Enable automatic compaction on push
git config seafile.autogc true

# (Optional) Customize threshold (default: 20)
git config seafile.gcthreshold 25
```

### 10.4 Multiple Commits, Rollbacks, Branching & Performance Impact

Understanding how `git-remote-seafile` handles complex Git workflows and remote compaction:

#### 1. Batch Commits (Pushing 1 vs. 100 Commits)
- **$O(1)$ Network Overhead**: Whether you push 1 commit or 100 commits at once, Git discovers all reachable new objects and bundles them into **a single `.pack` and `.idx` pair**.
- **Bandwidth Efficiency**: Git applies delta compression across commits locally before upload, minimizing upload size.
- **Latency**: Pushes complete in <1s regardless of commit batch size.

#### 2. Rollbacks & Force Pushes (`git reset --hard`, `git push --force`)
- **Fast & Safe**: A force-push (`+refs/heads/main`) updates the remote branch ref to the older commit SHA in ~0.2s without uploading any unnecessary packfiles.
- **Storage Lifecycle & Pruning**: The rolled-back commits remain in existing packfiles as *unreachable (dangling) objects*. When compaction runs (`git-remote-seafile gc`), Git's reachability analysis **automatically prunes unreachable commits**, replacing remote packfiles with an optimized packfile. Note that while obsolete packfiles are removed from the repository immediately, raw block storage reclamation on the Seafile server requires the server administrator to run `seaf-gc` after the library's history retention window.

#### 3. Branching & Multi-Branch Management
- **Branch Independence**: Every branch has its own ref file (`refs/heads/<branch>`). Pushing a new branch only uploads objects unique to that branch (base commits on `main` are not re-uploaded).
- **Global Cross-Delta Compression**: When compaction runs, `gc.py` discovers **all** remote branches and tags. Objects across all branches are packed together, optimizing deduplication and significantly reducing storage for long-lived feature branches.

#### 4. Auto-GC Performance Trade-Offs (Notice Mode vs. Auto-Compaction)

| Mode | Trigger | Push Latency | Concurrency Behavior | Best Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **Notification Mode (Default)** | Remote reaches 20 packfiles | **<1s (zero delay)** | Terminal tip displayed; push never pauses | Daily development, interactive CLI usage |
| **Opt-in Auto-GC (`autogc true`)** | Remote reaches 20 packfiles | **3–7s on 20th push** | Repositories locked via `.git-lock.json` during repack | Fully automated maintenance, solo developers |

> [!TIP]
> **Recommendation**: Leave `seafile.autogc` disabled (the default) so your everyday workflow stays blazing fast (<1s). Run compaction manually (`git-remote-seafile gc seafile://...`) or as part of a scheduled CI job when convenient.

---

## 11. Git LFS (Large File Storage) Integration

`git-remote-seafile` implements the official **Git LFS Custom Transfer Agent** protocol. You can store multi-gigabyte models, datasets, or video assets in Seafile while keeping Git repository history fast and light.

### 11.1 Configure Git LFS in Your Repository
In your local repository:
```bash
# 1. Enable custom transfer agent
git config lfs.customtransfer.seafile.path git-remote-seafile
git config lfs.customtransfer.seafile.args "lfs-transfer seafile://code/myproject"
git config lfs.customtransfer.seafile.direction both
git config lfs.standalonetransferagent seafile

# 2. Track large files
git lfs track "*.bin" "*.weights" "*.mp4"
git add .gitattributes
git commit -m "chore: configure Git LFS for Seafile"
```

### 11.2 Push and Pull Normally
```bash
git add large_model.bin
git commit -m "feat: add model weights"
git push origin main
```
- Git pushes small commit pointers through `git-remote-seafile`.
- Git LFS invokes `git-remote-seafile lfs-transfer` to stream large binaries directly into the Seafile repository's `/lfs/` storage.

### 11.3 Memory Behaviour on Large Transfers

Transfers are **streamed**, not buffered in RAM:

- **Upload** streams the file in chunks with calculated `Content-Length` via `StreamingMultipartFile` (pure-Python streaming generator), avoiding monolithic multipart RAM buffering. Packfiles generated during `push` are staged directly onto disk inside `.git` scratch storage rather than buffered in RAM, and are cleaned up immediately following transfer.
- **Download** streams responses directly to temporary disk files in chunks (both for LFS objects and Git packfiles during fetch/clone).
- **Smart Pack Fetch Filtering**: During `fetch`, the helper inspects requested commit objects against local availability. If all requested commits already exist locally, remote pack downloads are bypassed entirely.

A multi-gigabyte repository transfer or model therefore does not need multi-gigabytes of RAM.

> [!NOTE]
> Transfers emit standard Git LFS `progress` events during sustained transfers, allowing Git LFS to render incremental progress counters during large uploads and downloads.

---

## 12. Multi-User Distributed Locking

For teams where multiple developers or automated CI/CD runners push simultaneously, `git-remote-seafile` includes an automatic **distributed lease lock**:

- **Ticket-Based Protocol**: During every `git push`, the helper writes a unique lock ticket (`.git-lock.d/<nonce>.json`) at the remote repository root on Seafile. Tickets are ordered deterministically using server timestamps and HTTP `Date:` response headers, preventing write-write collision races and client clock drift.
- **Legacy Compatibility**: A mirrored `.git-lock.json` is maintained for backward compatibility with earlier client versions.
- **Dead Local PID Fast-Reclaim**: If an active lock belongs to the current machine (verified via hostname and machine hardware/container network identifier) and the holding process has crashed or exited, the helper detects the dead PID and immediately reclaims the lock without waiting for the timeout.
- **In-Transfer Lease Renewal**: During active multi-gigabyte packfile uploads and remote compaction, `git-remote-seafile` automatically refreshes the lock lease every 20 seconds of sustained progress without spawning background daemon threads.
- If another developer is pushing, subsequent pushes wait and retry for up to 15 seconds (configurable via `seafile.locktimeout`).
- **Lease Safety**: If a client crashes or loses power mid-push, the lock automatically expires after 60 seconds (configurable via `seafile.locklease`), preventing permanent repository deadlocks.

### Inspecting Lock Status (`lock-status`)
Check whether a remote repository is currently locked:
```bash
git-remote-seafile lock-status seafile://code/myproject
```
Example outputs:
```text
LOCKED
  Owner: user_a1b2c3d4
  Machine: workstation-1
  PID: 12345
  Nonce: e6b1f24d78a945b0
  Protocol: ticket
  Expires in: 42s
```
or when idle:
```text
UNLOCKED (Repository is available)
```

### Breaking or Clearing a Lock (`unlock`)
Release a stale lock held by your machine, or forcibly break an abandoned lock:
```bash
# Release cooperative lock held by your machine/account
git-remote-seafile unlock seafile://code/myproject
# Output: Unlocked repository seafile://code/myproject

# Forcibly break any active lock (emergency recovery)
git-remote-seafile unlock seafile://code/myproject --force
# Output: Forcibly unlocked repository seafile://code/myproject
```

---

## 13. Desktop Integration & Path Helper

If you have a local folder synced via the Seafile desktop client, you can convert its filesystem path to the exact `seafile://` remote URL:

```bash
git-remote-seafile desktop-url "C:\path\to\synced-library\myproject"
# Output: seafile://seafile.example.com/code/myproject
```

An official Qt desktop client patch is also provided in `contrib/seafile-client-context-menu.patch` to add *"Copy Git Remote URL"* directly into the Seafile desktop applet's right-click menu.

---

## 14. Troubleshooting & Diagnostics

### Test Remote Connection
Inspect repository references without cloning:
```bash
git-remote-seafile test seafile://code/myproject
```

### Common Issues

| Error | Cause | Resolution |
| :--- | :--- | :--- |
| `fatal: remote helper 'seafile' not found` | Helper binary not in `PATH` | Ensure `git-remote-seafile` or `git-remote-seafile.bat` is in a folder listed in your system `PATH`. |
| `DANGEROUS PATH COLLISION DETECTED (Trap 1)` | Local working tree is inside synced library at identical remote path | Change remote to an unsynced library (`seafile://code/repo`) or use an ignored subfolder (e.g. `seafile://Documents/seafile-git/repo`). |
| `UNIGNORED REMOTE PATH IN SYNCED LIBRARY (Trap 2)` | Remote destination in synced library is not in `seafile-ignore.txt` | Add the target subfolder (e.g. `seafile-git/`) to `seafile-ignore.txt` at the synced library root. |
| `Cannot use the library root '/'` | Remote URL points to library root without project subfolder | Specify a subfolder name, e.g. `seafile://library/myproject` or `seafile://library/seafile-git/myproject`. |
| `Seafile library not found: 'XYZ'` | Library not yet created, name typo, or permissions | Create the library via the Seafile Web UI, or verify spelling. The helper will suggest close matches. |
| `HTTP 401 Unauthorized` | Invalid or expired token | Run `git-remote-seafile check-auth` and verify credentials in `~/.git-seafile.json`. |
| `HTTP 403 Forbidden` on push | Read-only library permissions | Ensure your Seafile account has Read-Write permission on the target library. |
| `fatal: remote locked by user@host` | Concurrent push in progress or stale lock | Wait for the other push to complete, or increase `seafile.locktimeout`. If a previous client crashed, the lock expires after its lease (`seafile.locklease`, default 60s). |

---

## 15. Acknowledgments & Inspirations

`git-remote-seafile` builds upon pioneering concepts and implementations in the Git remote helper and cloud storage ecosystem:
- [git-remote-dropbox](https://github.com/anishathalye/git-remote-dropbox) by Anish Athalye: The landmark implementation that demonstrated transparent Git packfile transport and distributed locking over cloud storage APIs.
- [git-remote-gcrypt](https://spwhitton.name/tech/code/git-remote-gcrypt/) by Joey Hess: Pioneered decentralized, encrypted, push-to-anything Git remotes.
- [git-remote-s3](https://github.com/jason-riddle/git-remote-s3): Git remote helper for Amazon S3 object stores.
- [Git Remote Helpers Protocol Specification](https://git-scm.com/docs/git-remote-helpers): Official Git documentation on building custom remote helpers (`git-remote-helpers(7)`).
- [Git LFS Custom Transfer Agent Protocol](https://github.com/git-lfs/git-lfs/blob/main/docs/custom-transfers.md): Official Git LFS specification for line-based stdio custom transfer agents.
- [Seafile Web API v2.1](https://manual.seafile.com/develop/web_api_v2.1/): The underlying REST API provided by Seafile Ltd.

---

## 16. AI Disclosure & Authorship

This project—including its source code, test suites, architecture specifications, and documentation—was authored primarily with generative AI assistance from multiple AI systems, in collaboration with human architectural design, real-world forensic diagnostics, and verification by [@tkittich](https://github.com/tkittich). All code and protocol implementations are fully open source, tested on Windows (with CI workflows configured for cross-platform validation), and licensed under the Apache License 2.0.

