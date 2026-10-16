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
- It communicates directly with the **Seafile Web API v2** (`/api2/`) over HTTP/HTTPS.
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
      ▼ (HTTP/HTTPS REST API v2)
[Seafile Server]
https://seafile.example.com
  └── Library: code
        └── /myproject/               <-- Clean, bare Git packfiles and refs stored on the server!
```

With `git-remote-seafile`, your local repository operates completely free of cloud sync daemons. When you run `git push`, the helper bundles your commits into compressed Git packfiles and transfers them directly to your Seafile server over HTTP/HTTPS Web API v2 (`/api2/`)—exactly like pushing to GitHub or GitLab.

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
> During `git clone` the helper runs in the *parent* of the directory being created, which is not yet a Git repository, so Git cannot name a working tree for it. Git therefore exports `GIT_DIR` pointing at the destination repository it just created, and the guard checks **that directory** — so cloning into a synced library is blocked even when you run the clone from somewhere else entirely:
> ```bash
> cd C:\somewhere\else
> git clone seafile://Documents/code/myproject D:\Seafile\Documents\code\myproject   # blocked
> ```
> The guard relies on `GIT_DIR`, which Git sets for every helper it spawns; only exotic invocations that suppress it can slip past, and the advice stands regardless: never keep working repositories inside synced libraries.

#### 🛡️ Library Root Pollution Protection
- A URL that names no path means `git-repo`, never the library root, so a push cannot scatter bare objects and refs across the top level of a library. (As a backstop the helper also refuses a destination that resolves to the root, with `Cannot use the library root '/' as a Git remote destination.`; no URL you can type reaches it.)
- Note what *is* allowed: a URL with no path is not the root. `seafile://Documents` and `seafile://Documents/` both default to the `git-repo` subfolder (see §7), which is a normal destination. No URL reaches the bare root: a path that normalises away, such as `seafile://Documents/%2F`, falls back to `git-repo` rather than to `/`.

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

## 3. Comparison With "Move Code Out + Git Remotes"

How this helper compares with pushing to GitHub/GitLab/Gitea, and a decision guide for which to use, is in [ALTERNATIVES.md](ALTERNATIVES.md). Short version: use a Git forge for collaboration features; use this helper when the code must stay on infrastructure you control and the Seafile server is already where your data lives. The two combine (dual remote, §8.5).

## 4. Alternative Workarounds and Why They Fail

The six alternatives people reach for first — a live working tree in a synced folder, pausing sync, `seafile-ignore.txt`, generic cloud sync, bundles in synced folders and bare repositories in synced folders — are analysed in [ALTERNATIVES.md](ALTERNATIVES.md). None of them removes the underlying conflict between a sync daemon and Git's file churn (§2); this helper does by keeping the working tree out of the synced folder.

## 5. Installation

### Requirements
- Python 3.10+
- Git 2.20+
- [`requests`](https://pypi.org/project/requests/) **2.32.4 or newer** — a
  security floor rather than a feature one. `pip install` resolves it for you;
  the number matters if you are pinning dependencies by hand or packaging this
  tool for a distribution. See [SECURITY.md](SECURITY.md#the-dependency-floor)
  for the three credential-handling CVEs behind it.

> [!NOTE]
> **Two features raise the floor above 2.20.** The number above is the ordinary
> SHA-1 case. A **SHA-256 repository needs Git 2.29+**: `object-format`
> negotiation is what lets the helper name the format of the refs it lists, and
> `gc` on such a remote initialises its scratch repository with
> `--object-format=sha256` for the same reason. And the **snapshot tool's
> `--exclude` / `--default-excludes` need Git 2.25+** (see §15), because it
> stages through `--pathspec-from-file`; without exclusions it stages with a
> plain `git add`, where 2.20 is enough.

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

### Upgrading & Version Compatibility

Upgrade by reinstalling from the repository. There is no migration step and no state stored outside your libraries:

```bash
cd /path/to/your/checkout
git pull
pip install -e .
```

**Upgrading to v0.9.6 from v0.9.5** changes nothing for a setup that works: a push of a repository with very long (non-ASCII) paths on Windows no longer fails with "expected object ID, got garbage". Nothing about what is stored changes.

**Upgrading to v0.9.5 from v0.9.4** changes nothing for a setup that works: a server under a path (`https://host/seafile`) can now be named in `SEAFILE_SERVER`, the credentials file or the desktop client's account (section 7.1), and that account's URL is used when the remote names the same host and port. Nothing about what is stored changes.

**Upgrading to v0.9.4 from v0.9.3** changes two timings and nothing else: `gc` and the tail merge read the new pack back and compare its SHA-256 before deleting (one more download of that pack under the lock), and a snapshot lists its payload objects once more after the push. Neither changes what is stored.

**Upgrading to v0.9.3 from v0.9.2** changes behaviour in three places: on Windows every large payload file is hashed once more on the first run (the cache key now uses NTFS's change time, which catches a same-size in-place edit that put the modification time back); plain `unlock` refuses a ticket held by a live process on this machine (`--force` still breaks it); and `--payload-gc --payload-delete` re-reads the remote's refs while it deletes and stops if another machine pushed. Nothing else changes for a working setup.

**Upgrading to v0.9.2 from v0.9.1** changes behaviour in four places: a directory the snapshot tool cannot read stops the run and is named (it used to be silently left out; `--exclude` it on purpose); a snapshot that pushes to a `seafile://` remote also records the modification time of every git-stored file, and `--restore` puts the times back (`--no-record-mtimes` turns it off); `git push --force-with-lease` works; and `--payload-gc` stops, deleting nothing, if a branch's ref cannot be read or a push landed while it was deciding. No file format changed.

**Upgrading to v0.9.1 from v0.9.0** changes behaviour in four places: `gc` stops (deleting nothing) when a ref or refs directory cannot be read, so re-run it after a transient failure; a push lists the pack directory and re-reads the refs under the lock (two more requests); a nested git repository in a snapshot source is captured as its files (`--no-embedded-repos` keeps the old record); pack names must be lowercase. No file format changed.

**Upgrading to v0.8.8 from v0.8.7 or earlier changes four defaults you may notice:**

- `seafile.autogc` is now **on**: after a qualifying push the helper merges the small packs. `git config seafile.autogc false` restores the tip-only behaviour (§10.2).
- Subcommand **usage errors exit 2 on stderr** (they used to exit 1 on stdout); results and reports stay on stdout. Re-check any script that parsed the old stream (§14, *Output and Exit Codes*).
- `seafile.locksettle` and `seafile.gcmaxmib` are no longer settings; a value still in your git config is reported once and ignored. `seafile.gcmaxpackmib` now means the largest pack the helper writes, and the automatic budget is 2 x it.
- The snapshot tool now **refuses to stage a tree the vault's volume cannot hold** (`--allow-low-disk` overrides), and `--restore` without `--branch` uses the remote's only branch.

Two compatibility facts are worth knowing before upgrading a fleet:

- **v0.7.0 removed the legacy lock protocol outright.** The `.git-lock.json` single-file lock was replaced by the `.git-lock.d/` ticket queue, and the helper no longer looks for, or deletes, an old mirror — a leftover `.git-lock.json` is harmless clutter you can delete by hand. Do not run a v0.1–v0.3 helper against the same repository as a v0.7.0+ one: those clients predate the ticket protocol and cannot see its locks.
- **v0.8.4's lock grant marker is a fleet property.** The marker stops a later arrival from preempting a live holder, but only once **every** client pushing to that repository is on v0.8.4 or later. A newer client will not preempt an older holder; an older client ignores the marker and can still preempt a newer one. Upgrading one machine is not the same as being protected.

There is no `protocol: 2` marker file in a repository to make old and new clients refuse each other, so a mixed fleet is a matter of discipline rather than enforcement.

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

> **Scoping note**: `SEAFILE_TOKEN` is only used when `SEAFILE_SERVER` is also set, and only
> when it names the same server as the remote URL. A token set on its own is **ignored**, with a
> note on `stderr` explaining why — a URL can name any server, and a token sent to one it was never
> meant for is a credential leak. Always set both variables together. The same rule applies to
> `~/.git-seafile.json`: a `token` with no `server` beside it in the same file is not used.

### Verifying Authentication
Run the built-in diagnostic check:
```bash
git-remote-seafile check-auth
```
Expected output:
```text
Authenticated successfully with https://seafile.example.com as user@example.com
```

### First-Use Check (`doctor`)
Checks everything a first push depends on, in order, and stops at the first failure:
```bash
git-remote-seafile doctor seafile://code/myproject
```
It reports the login, **how the URL was interpreted** (server, library, path), the safety guards, and a write-and-delete probe inside the repository path. With no URL it checks only the login.

### Output and Exit Codes
Every subcommand follows one convention, so scripts can rely on it:

| | |
|---|---|
| stdout | The command's result or report, including the verdict of a report command (`doctor`'s `[FAIL]`, `check-safety`'s `[BLOCKED]`, `lock-status`'s `LOCKED`). |
| stderr | Usage errors, failures that prevented a result, and warnings. |
| exit `0` | Done, or nothing to do. |
| exit `1` | The command ran and the answer is "no", or it failed. |
| exit `2` | The command line was wrong (missing operand, unknown command). |

### Behavioural Configuration

| Git config key | Environment variable | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `seafile.skipsafetychecks` | `SEAFILE_SKIP_SAFETY_CHECKS` | `false` | Bypass the pre-flight guardrails (Trap 1, Trap 2, root pollution). |
| `seafile.gittimeout` | `SEAFILE_GIT_TIMEOUT` | `300` | Seconds one git subprocess may run before it is killed. Raise it when a multi-GB push, fetch or `gc` times out (system/global scope or environment only). |
| `seafile.autogc` | — | `true` | Automatically tail-merge the remote's small packfiles after a push, once their threshold is reached. Set `false` to be told instead. |
| `seafile.gcthreshold` | — | `20` | Count of *small* packfiles at which the `gc` tip appears (or auto-GC triggers). Large packs never count toward it. |
| `seafile.gcmaxpackmib` | — | `1024` | The largest pack the helper writes, in MiB (`pack-objects --max-pack-size`, for pushes, tail merges and full `gc`): a bigger push arrives as several packs. Packs at least half this size count as full and are left where they stand by tail merge. The automatic run's download budget is 2 × this (2 GiB at the default); above it the run defers to a manual `gc`. A single object larger than this still becomes one larger pack (uploaded in chunks); the cap bounds everything else, which keeps each upload and each merge short. One tested server accepted 1.75 GiB in a single request and reset 2 GiB. |
| `seafile.forcefilehost` | `SEAFILE_FORCE_FILE_HOST` | `false` | Force download/upload links onto your server's **host** (a wrong scheme on the same host is always corrected). |
| — | `SEAFILE_STRICT_REFS` | unset | Set to `1` by a caller that decides what to *delete* from what it fetches (the snapshot tool's `--payload-gc`): the listing then fails, instead of warning and leaving out a ref whose file cannot be read. Not a setting to put in your shell profile. |
| `seafile.locktimeout` | — | `15` | Seconds to wait for the remote lock *since the queue last moved*: a waiter whose turn is getting closer keeps waiting, so a push behind several others can wait longer in total (capped at the larger of 10 x this and 120 s). |
| `seafile.locklease` | — | `60` | Duration in seconds before an inactive lock lease is considered expired. |

```bash
# Examples
git config seafile.autogc false
git config seafile.gcthreshold 25
git config seafile.forcefilehost true
git config seafile.locktimeout 30
git config seafile.locklease 120

# The two trusted-scope keys are set globally, not per repository:
git config --global seafile.skipsafetychecks true
git config --global seafile.gittimeout 1800
```

> Values that would defeat the mechanism outright are refused with a stderr warning and the documented default is used instead: a negative `seafile.locktimeout`, a zero or negative `seafile.locklease` (which would expire every lock immediately), a `seafile.gcthreshold` below 1 (which would compact on every push), a `seafile.gcmaxpackmib` below 1 (which would make even the large consolidated packs movable), and a `seafile.gittimeout` that is not a positive number of seconds (which would kill every git command outright).
>
> **Retired keys.** `seafile.locksettle` (the settlement window is fixed at one second — it is a race guard, and the only thing switching it off bought was a second per push) and `seafile.gcmaxmib` (the download budget is now 2 × `seafile.gcmaxpackmib`) are no longer settings. A value still present in your git config is ignored without a message (releases 0.8.8 to 0.9.2 reported it once per push).

**`seafile.skipsafetychecks` and `seafile.gittimeout` are read from the trusted scope only** — the environment, or the system and global git config. A repository shipping either key in its own `.git/config` is ignored, because a repository that could disarm a guardrail or raise a safety ceiling is a repository that has taken away the protection those settings exist to provide. See [RUNBOOK.md](RUNBOOK.md) §7 for what the timeout bounds and when to raise it.

**`seafile.forcefilehost`** deserves a note. Seafile returns a short-lived URL for every file transfer, and there are three cases to tell apart:

- A **relative** link (a bare path rather than a full URL) is always completed against your server, regardless of this setting. That is not a rewrite — it is the only way the link can be used at all.
- A link naming **the same host and port as your server but over plain `http`**, while your server is `https`, is corrected to `https` automatically. This is a common reverse-proxy artefact and it cannot be left alone: an upload `POST` sent to the `http` URL is answered with a redirect, and a redirected `POST` is re-issued as a `GET`, which the upload endpoint rejects with `HTTP 400`. Downloads survive the same redirect, uploads do not — which is why only pushes break. The correction is one-way: `https` is never downgraded to `http`.
- A link naming a **different host** is left exactly as the server sent it. A **clustered or object-store-backed** deployment genuinely serves files from a different host, and forcing it onto the API host would make it 404. If instead you have a **reverse proxy** returning an internal host (`http://internal-docker-host:8082/...`) that your machine cannot reach, set this to `true` to rewrite the link's host onto your configured server.

The host comparison is exact, port included: `http://your-server:8082/...` is a different endpoint rather than a mistyped scheme, and is left alone.

### TLS, Private CAs and Proxies

The helper makes no TLS decisions of its own: there is deliberately no `verify` option and no flag to disable certificate checking. Trust and proxying come from the environment, through the standard variables the underlying HTTP library honours.

| Variable | Effect |
| :--- | :--- |
| `REQUESTS_CA_BUNDLE` | Path to a CA bundle — the usual answer for a **self-hosted instance with a private CA** |
| `SSL_CERT_FILE` | Path to a CA bundle, honoured as an alternative |
| `HTTPS_PROXY` / `HTTP_PROXY` | Proxy URL for outbound requests |
| `NO_PROXY` | Comma-separated hosts that bypass the proxy; **include your Seafile host** if it is internal |

```bash
# A self-hosted server with an internal CA, reached through a proxy
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/company-ca.pem
export HTTPS_PROXY=http://proxy.internal:3128
export NO_PROXY=seafile.internal.example.com
```

> [!IMPORTANT]
> **Git's `http.sslCAInfo` is not read.** The helper is a separate process from `git-http-fetch`, so a setup that works for a GitHub remote can still fail here with a certificate error. Point `REQUESTS_CA_BUNDLE` at your CA — there is no way to disable verification, by design.

[SECURITY.md](SECURITY.md) covers the full credential and transport policy.

---

## 7. URL Syntax

Remotes use the `seafile://` URL scheme:

| Format | Example | Notes |
| :--- | :--- | :--- |
| **Full URL** | `seafile://seafile.example.com/code/myproject` | Explicit server, library, and path |
| **HTTPS scheme** | `seafile://https://seafile.example.com/code/myproject` | Fully qualified URL |
| **Port** | `seafile://seafile.example.com:8080/code/myproject` | Server on a non-default port |
| **Short URL** | `seafile://code/myproject` | Uses server from default configured account |
| **IPv6** | `seafile://[2001:db8::1]:8443/code/myproject` | IPv6 literal in brackets; the port is optional (`:80` → http, other ports → https) |
| **No path** | `seafile://code` | Library with no path defaults to the `git-repo` subfolder — the same as `seafile://code/git-repo` |
| **Ambiguous name** | `seafile://https://seafile.example.com/my.lib/team/repo` | A library named like a hostname (`my.lib`, `files.local`): the explicit server is what makes it a library |

- **Library**: Name of the Seafile library (e.g. `code` or `Documents`) or the library UUID. **The library must exist on your Seafile server before pushing.** (Create it via the Seafile Web UI if you haven't already).
- **A library name that contains a dot**: in the bare form the first segment is read as a *server* only when it has the shape of a hostname — dot-separated labels ending in an alphabetic one. So `seafile://notes.v2/team/repo` and `seafile://proj.2026/team/repo` are libraries (a numeric final label is not something a hostname can have), while `seafile://my.lib/team/repo` is read as the server `my.lib`. When a name could read either way, say which with the explicit form: `seafile://https://my.lib/team/repo` reads `my.lib` as the **server**, and `seafile://https://seafile.example.com/my.lib/team/repo` reads it as the **library**.
- **Path**: Path inside the library where bare repository objects will reside. Omitting it is allowed and means `git-repo`, so `seafile://code` and `seafile://code/` are the same remote as `seafile://code/git-repo`. No `seafile://` URL reduces to the bare library root: a path that normalises away — `seafile://code/%2F`, `seafile://code//` — falls back to `git-repo`. The root refusal is a backstop for a destination that arrives empty by some other route, not something a URL can reach. A percent-encoded separator is decoded **before** the path is split, so `%2F` acts as a separator and a `..` hiding behind one is refused like any other.
- **Scheme and port**: When the host carries a port, the scheme follows it — `:80` means `http`, `:443` means `https`, and **any other port keeps its number and uses `https`**. So `seafile://seafile.example.com:8080/code/repo` talks to `https://seafile.example.com:8080`, and a plain-HTTP server on a non-standard port needs the explicit form: `seafile://http://seafile.example.com:8080/code/repo`. A port that is not a number is refused with `Invalid Seafile URL format`, naming the host it could not parse — in the bare *and* the explicit form (`seafile://https://seafile.example.com:abc/code/repo`). An IPv6 literal must be bracketed — `seafile://[::1]/code/repo` works (scheme inferred: https, or http on `:80`) — while an unbracketed colon-bearing host is refused, with the bracketed spelling named.

### 7.1 Seafile Behind a Reverse Proxy Under a Path

Some deployments serve Seafile at `https://corp.example.com/seafile/` rather than at the root. A
`seafile://` URL cannot say so: the segment after the host is the **library**, so
`seafile://corp.example.com/seafile/code/app` asks for a library named `seafile`. The path belongs
to the *server*, and the server is set in one of three places, in this order of precedence:

```bash
# 1. the environment (the usual choice)
export SEAFILE_SERVER="https://corp.example.com/seafile"
export SEAFILE_TOKEN="<your token>"
git remote add origin seafile://code/myproject
```

```json
// 2. ~/.git-seafile.json
{ "server": "https://corp.example.com/seafile", "token": "<your token>" }
```

3. **The Seafile desktop client's account.** If the client works against that server, its stored
   account URL already includes the path, and nothing needs typing: `git remote add origin
   seafile://code/myproject` finds it. A URL that names only the host
   (`seafile://https://corp.example.com/code/myproject`) also picks the path up from the account
   or from `SEAFILE_SERVER`, because both name the same host and port.

What the path changes, and what it does not:

- Every API call goes under it (`https://corp.example.com/seafile/api2/...`), and a *relative*
  transfer link from the server is completed under it. An absolute link is used as the server
  gave it, so the server's `FILE_SERVER_ROOT` / `SERVICE_URL` must be set to the prefixed form
  (`https://corp.example.com/seafile/seafhttp`).
- **The token is still scoped by host and port, not by path.** A token for
  `https://corp.example.com/seafile` is sent to that authority and nowhere else.
- `git-remote-seafile desktop-url` cannot spell the path either: for such a server it prints the
  short form (`seafile://<library id>/<path>`) and names the `SEAFILE_SERVER` value on `stderr`.
- `doctor` shows the server it resolved, path included, which is the first thing to check when a
  request returns 404.

**The no-code alternative: give the server a host name of its own.** If you control the proxy, or
can ask whoever does, terminate the path there, so that the helper never sees one:

```nginx
server {
    listen 443 ssl;
    server_name seafile-api.corp.example.com;
    location / {
        proxy_pass https://seafile-backend/seafile/;
    }
}
```

```bash
git remote add origin seafile://seafile-api.corp.example.com/code/myproject
```

This needs no setting and no special-casing anywhere, and it is the better choice when the
server is shared by many users, because nobody has to remember an environment variable. The
server's own `SERVICE_URL` and `FILE_SERVER_ROOT` must then name the new host.

---

## 8. Daily Workflows

### 8.0 Prerequisite: Create Your Remote Library on Seafile
Before pushing your first repository to Seafile:
1. Log into your Seafile Web UI (`https://seafile.example.com`).
2. Click **New Library** and choose any name you like (such as `code`, `git-repos`, or `projects`). We will use `code` in the examples below.
3. **Important**: Leave this library **unsynced** in your Seafile desktop client applet. Do not sync it to your local drive.
4. **Set the library's history to a few days** (see [8.0.1](#801-library-history-setting)); full history keeps every pack the tool later removes.
*(Alternatively, if storing within an existing synced library like `Documents`, store under an ignored subfolder such as `seafile-git/` and add it to `<library-root>/seafile-ignore.txt` before pushing, noting that `seafile-ignore.txt` only applies at the library root and cannot retroactively ignore already-synced folders).*

### 8.0.1 Library history setting

A Seafile library keeps a history of every change to its files, and has a setting for how long (Library settings -> **History Setting**: *Keep full history*, *Don't keep history*, or *Only keep a period of history* with a number of days). It decides how much disk a Git remote costs and what you can undo on the server. **Recommended for a library that holds remotes, snapshot vaults or payload objects: keep a period of history, 7 to 30 days.**

| Setting | What it means here | When to use it |
| :--- | :--- | :--- |
| **A period of history (7 to 30 days)** | A pack, ref or payload object that is deleted or overwritten can be restored from the web UI for that long, then its blocks are reclaimed. | The default choice, and the right one for a vault you cannot recreate. |
| **Don't keep history** | Nothing can be recovered on the server. Deleted blocks are reclaimed at the next server GC. | Scratch and test libraries, or a remote that is a pure mirror of a repository you still have elsewhere. |
| **Keep full history** | Every deleted pack, merged-away pack and removed payload object stays on the server's disk forever. | Avoid for remotes: the history view is also noisy, because each push is about 13 entries (ticket create/renew/delete, pack, index, ref). |

Why not full history: Git already records the history of what you store, so the library's own history only duplicates it. What it *does* keep is everything the tool later removes. A tail merge or `gc` deletes the packs it merged; `--payload-gc --payload-delete` deletes unreferenced objects; a snapshot vault that rewrites history leaves old packs behind. With full history none of that space comes back, however many times you compact.

Why not no history, for data you care about: the server's history is the only undo for an operation that went wrong *outside* Git: a `gc` interrupted at the wrong moment, a `--payload-delete` run against the wrong remote, a client that ignored the lock and overwrote a ref, or an `unlock`/web-UI delete by mistake. Seven days costs little and covers a weekend and a slow reaction.

Things to know:

- **The setting is per library, and changing it is not retroactive on disk.** Space is reclaimed when the history it protected has expired *and* the server's garbage collection (`seaf-gc`) has run, which is an administrator task. Deleting data frees the library's *quota* at once; the disk follows later.
- **It does not replace backups.** It is a safety net for days, not a retention policy. Payload retention (`--payload-gc`, `--payload-grace-days`, `--payload-keep`) is separate and works at the application level.
- **A server administrator may restrict the choices**, or set a default for new libraries. Check an existing library before first use; a library created long ago may be on full history.
- **Encrypted libraries** are refused by the tool whatever their history setting.

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

> [!NOTE]
> **A `--depth` or `--filter` clone is silently a full clone.** The helper does not implement Git's `depth` or `filter` options. Git sends them anyway, the helper answers `unsupported`, and Git then falls back to a full clone *without a word* — so `git clone --depth 1 seafile://…` and `git clone --filter=blob:none seafile://…` cost exactly what a plain clone costs. Nothing is broken; nothing is smaller either. What the helper does handle is a repository that is shallow or partial for some *other* reason (cloned from a different remote, restored from a bundle): the fetch-skip optimisation reasons from tip-commit presence, which implies a complete object store only in a full clone, so the helper withholds the skip there and fetches the pack instead of leaving `git checkout` to report missing objects.

> [!NOTE]
> **Symlinks survive the repository, but not every Windows checkout.** A symlink is an ordinary tree entry — mode `120000`, with the link target stored as the blob's content — so packfiles carry it verbatim and a clone on Linux or macOS reproduces real links. Windows is the exception: unless `core.symlinks` is enabled (Developer Mode, or an elevated process), Git checks a link out as a **regular text file whose contents are the target path**. The repository is unaffected and `git status` stays clean, because Git knows the entry is a link; only that working tree differs, and a clone on a symlink-capable machine gets the links back. Nothing in the helper needs to special-case this — there is no symlink code in it, because a packfile never had a link to preserve.

### 8.3 Normal Fetch, Pull, and Push
All standard Git commands work transparently:
```bash
git pull origin main
git push origin feature-branch
git push origin :old-branch   # Deletes remote branch
```

`--dry-run` works too, and it is genuinely read-only: the helper reports which
refs would move without acquiring the push lock, building a packfile, or writing
a single ref.

```bash
git push --dry-run origin main
# To seafile://code/myproject
#    a1b2c3d..d4e5f6a  main -> main
```

### 8.4 Set or Change Remote Default Branch (`set-head`)
If you want to designate or change the default branch checked out on clone (e.g. from `master` to `main`, or to `dev`):
```bash
git-remote-seafile set-head seafile://code/myproject main
# Updated remote HEAD on seafile://code/myproject to refs/heads/main (was refs/heads/master)
```
The command verifies the branch exists on the remote first, and the output names the HEAD value it replaced — an aid when recovering a remote that points at the wrong default branch.

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

> [!WARNING]
> **If you also use Git LFS, a dual remote splits your objects.** `lfs.standalonetransferagent` is not scoped to a remote, so large files keep going to Seafile while the commits go wherever you push — leaving LFS pointers on GitHub with no objects behind them. See §11.1 for the scoped form that fixes it.

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
5. **No conflict copies** (`SFConflict`). Every conflict file in the failures
   described in §4 is produced by the Seafile *sync daemon*, and the guardrails
   in §2.4 keep this tool's own writes out of a synced folder. With the library
   that holds the remote left unsynced — the arrangement §2 asks for — nothing
   touches those files but Git.

   > [!NOTE]
   > That is a claim about the paths this tool writes, not about the Seafile
   > *server*. The server itself does **not** protect you: measured against a
   > real Seafile CE 13.0.28, twelve simultaneous `replace=1` uploads of one path
   > all returned success and left the path plus six
   > `<name> (SFConflict <user> <date>)` copies beside it. That is why the design
   > avoids the situation (the lock, then re-read, compare and read-back — and a
   > check of the directory for conflict copies after every ref write,
   > `DESIGN.md` §3.3), and why a bypassed lock shows up as a warning naming the
   > copy rather than as a silent loss. Six clients pushing at once through the
   > lock produced no copies.

---

## 10. Remote Packfile Compaction & Auto-GC (`git-remote-seafile gc`)

Each `git push` uploads a small `.pack` file containing the newest commits. Over time (e.g. after 50+ pushes), cloning may download dozens of small packfiles.

### 10.1 Manual Compaction
You can compact all packfiles on Seafile into a single optimized, delta-compressed packfile at any time:

```bash
git-remote-seafile gc seafile://code/myproject
```

- **Options**: `--min-packs N` (default: 2) — only repack if at least $N$ packfiles exist.
- **`--tail`**: run exactly what `seafile.autogc` runs (§10.3) — merge only the small packs, leave large ones where they are, prune nothing — so you can try, time or schedule the automatic tier by hand. It honours the same threshold and download budget; "skipped" is a normal answer.
- Automatically locks the remote repository during compaction to prevent push conflicts; the lock lease is refreshed during pack downloads, and ownership is re-validated before the deletions begin *and again before each obsolete pack is removed*.
- **Exit codes**: `0` on success and on a benign skip (fewer than `--min-packs` packfiles), `1` on a failed compaction (download error, size mismatch, invalid pack name) — safe to gate a scheduled CI job on.
- **Off-namespace refs are refused.** The compaction mirror carries only `refs/heads` and `refs/tags`; if the library holds refs anywhere else under `refs/` (hand-created `refs/notes/...`, another tool's refs) — or a ref name git could not honour — gc refuses *before downloading anything*, naming the offending refs: their objects would otherwise be deleted from the consolidated pack while the ref file survived, broken. Move such refs into `refs/heads` or `refs/tags` first.
- **Obsolete packs are deleted only after the consolidated packfile is verified.** The remote pack directory is listed again and every new `.pack`/`.idx` must be present at the size it has locally, and each is then downloaded once and compared by SHA-256 with what was uploaded (about 0.015 s per MiB, under the lock already held). An upload that returned success is not evidence that the bytes are there — a truncated write, a quota error a proxy answered `2xx`, and an `.idx` that never landed all look identical from the client — and what gc deletes are the packfiles the repository is *currently* served from. When the check cannot be satisfied (a file missing, a size that disagrees, or a listing that fails) gc refuses, names what it could not verify, and leaves every old packfile in place: until the new ones are proven present, they are still the only complete copy. Deletions that fail individually are named on stderr and in the result summary and simply remain for a later `gc` run. If ownership is lost part-way through the deletions, the run stops there, reports it, and leaves the rest — the consolidated packfiles are already verified by that point, so nothing is lost. That outcome is reported as `status: "incomplete"` and the command exits non-zero rather than printing a plain success, so a scheduled job can tell it from a compaction that finished; the leftovers are unreferenced and a later `gc` removes them.
- **Scratch space**: compaction downloads the remote's packfiles, repacks them, and re-uploads the result, all under the system temporary directory. Point `TMPDIR` (Linux/macOS) or `TEMP`/`TMP` (Windows) at a volume with room for roughly 2–3× the repository's packed size before compacting a large library; otherwise the run fails late, after it has already taken the lock.

### 10.2 Automatic Detection & Notification (when `seafile.autogc` is off)
With `seafile.autogc false`, whenever you push, `git-remote-seafile` checks the number of *small* remote packfiles — packs below half of `seafile.gcmaxpackmib` (default 1 GiB, so below 512 MiB). Larger packs are frozen and never counted:
- If the small-pack count reaches **20** (or custom threshold), it prints a non-intrusive tip to your terminal:
  ```text
  Notice: Remote repository has 22 small packfiles (threshold: 20), totaling 0.1 GiB.
  Tip: Run 'git-remote-seafile gc seafile://...' to optimize remote storage,
       or run 'git config seafile.autogc true' to enable automatic tail merge.   (shown at most once a day per remote)
  ```
- Your normal pushes are unchanged by this check: it reads a directory listing and prints a tip, and adds no network round-trip to the transfer. (The one fixed cost on every push is the lock's settlement window — see §12.)

### 10.3 Automatic Tail Merge (`seafile.autogc`, on by default)
`git-remote-seafile` automatically merges small packfiles after a `git push` whenever the threshold is reached. To switch that off and only be told:

```bash
# Disable the automatic tail merge (you will get a once-a-day tip instead)
git config seafile.autogc false

# (Optional) Customize threshold (default: 20)
git config seafile.gcthreshold 25
```

Unlike a full compaction, an automatic run never touches large packs: every pack at or above half of `seafile.gcmaxpackmib` (512 MiB at the default) is frozen where it stands, and only the smaller ones are downloaded, merged into self-contained packs of at most `seafile.gcmaxpackmib` each, re-uploaded and removed. Cost per cycle therefore tracks the small tail — a few megabytes in a typical repository — not the size of your history. If even the movable packs total more than 2 × `seafile.gcmaxpackmib` (2 GiB at the default), the run is deferred with a notice so a push never holds the lock for minutes round-tripping that much (measured: about 0.05 s per MiB, so 2 GiB is roughly a minute and a half); manual `gc` (§10.1) remains available for those cases and is the only tier that re-deltas across the entire history.

### 10.4 Multiple Commits, Rollbacks, Branching & Performance Impact

Understanding how `git-remote-seafile` handles complex Git workflows and remote compaction:

#### 1. Batch Commits (Pushing 1 vs. 100 Commits)
- **$O(1)$ Network Overhead**: Whether you push 1 commit or 100 commits at once, Git discovers all reachable new objects and bundles them into **a single `.pack` and `.idx` pair**.
- **Bandwidth Efficiency**: Git applies delta compression across commits locally before upload, minimizing upload size.
- **Latency**: the upload is a fixed 1–2 files regardless of commit batch size, so push time does not grow with the number of commits. A push does carry one fixed cost: the distributed lock's settlement window (§12, one second), which is paid before the transfer starts.

#### 2. Rollbacks & Force Pushes (`git reset --hard`, `git push --force`)
- **Fast & Safe**: A force-push (`+refs/heads/main`) updates the remote branch ref to the older commit SHA without uploading any unnecessary packfiles.
- **Storage Lifecycle & Pruning**: The rolled-back commits remain in existing packfiles as *unreachable (dangling) objects*. When compaction runs (`git-remote-seafile gc`), Git's reachability analysis **automatically prunes unreachable commits**, replacing remote packfiles with an optimized packfile. Note that while obsolete packfiles are removed from the repository immediately, raw block storage reclamation on the Seafile server requires the server administrator to run `seaf-gc` after the library's history retention window.

#### 3. Branching & Multi-Branch Management
- **Branch Independence**: Every branch has its own ref file (`refs/heads/<branch>`). Pushing a new branch only uploads objects unique to that branch (base commits on `main` are not re-uploaded).
- **Global Cross-Delta Compression**: When compaction runs, `gc.py` discovers **all** remote branches and tags. Objects across all branches are packed together, optimizing deduplication and significantly reducing storage for long-lived feature branches.

#### 4. Auto-GC Performance Trade-Offs (Auto-Compaction vs. Notice Mode)

| Mode | Trigger | Push Latency | Concurrency Behavior | Best Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **Notification Mode (`autogc false`)** | Remote reaches 20 packfiles | **No added delay** | Terminal tip displayed; push never pauses | Daily development, interactive CLI usage |
| **Auto-GC (`autogc true`, default)** | 20 *small* packfiles accumulate | **A bounded tail merge, inline** — only packs below half of `seafile.gcmaxpackmib` move, so duration tracks the small tail rather than the repository; deferred with a notice if that tail exceeds 2 × `seafile.gcmaxpackmib` | Repositories locked via `.git-lock.d/` ticket queue during repack | Fully automated maintenance, solo developers |

**Measured cost** (Seafile CE 13.0.28, one server, small commits; your network will differ):

| What | Measured |
| :--- | :--- |
| An ordinary push | about 2.3 s, nearly all of it the lock's request round trips and settlement window |
| Library history entries per push | about 13 (ticket create/renew/delete, pack, index, ref) — each push is a burst of small commits in the library's history |
| The 20th push with `autogc true` | 6.5 s, and about 65 further history entries (the merge deleted 20 packs and their indexes) |
| A 4 GiB tail merge (9 packs → 4) | 3 min 43 s with the lock held throughout: ~64 s download, ~30 s repack, ~2 min upload; peak scratch 8 GiB (2× the data), peak memory ~4 GiB while git maps the packs |
| Largest single request accepted | 1.75 GiB; a 2 GiB request was reset — hence the pack size cap, and chunked upload (`Content-Range`) for anything over 1 GiB: 2.5 GiB and 5 GiB files were assembled, downloaded and compared byte-identical |

Another client's push arriving during a merge waits for the lock and, after `seafile.locktimeout`, is told the repository is locked; it succeeds when retried. That is the practical cost of `autogc true` on a busy shared remote, and why the automatic budget is 2 GiB.

> [!TIP]
> **Recommendation**: Leave `seafile.autogc` on (the default): the typical cost is a few seconds on every 20th push, and a remote that is never merged grows without bound. Turn it off (`git config seafile.autogc false`) on a busy shared remote where a ~minute-long merge holding the lock would be disruptive, and run `git-remote-seafile gc --tail` (or a full `gc`) from a schedule instead.

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

> [!WARNING]
> **`lfs.standalonetransferagent` applies to *every* remote, and the destination library is a second setting.** That one line tells Git LFS to use `seafile://code/myproject` for all LFS transfers in this repository, regardless of which Git remote you are pushing to. In a dual-remote setup (§3.3, §8.5), `git push github main` sends the commits to GitHub and the **large files to Seafile** — so the GitHub side holds LFS pointers whose objects it does not have, and a clone from GitHub cannot resolve them.
>
> If this repository has more than one remote, scope the agent to the Seafile server rather than applying it unconditionally:
> ```bash
> # Use the Seafile agent only when the LFS API URL matches; other remotes keep their own LFS.
> git config lfs.https://seafile.example.com/.standalonetransferagent seafile
> ```
> The URL is matched with `git config --get-urlmatch`, so it is the **LFS API server URL**, not the remote's name. Leave the `lfs.customtransfer.seafile.*` settings in place — they define the agent this scoped key selects.
>
> Two things the snippet above does not say. First, `lfs.customtransfer.seafile.args` names the destination library a **second** time, so changing the push remote does not update it; a stale library name there surfaces only when a large file is actually transferred. Second, `lfs.customtransfer.seafile.concurrent` defaults to `true`, so Git LFS runs several agent processes at once — safe here, because each process gets its own scratch directory and transfers a disjoint set of objects, and the LFS path takes no repository lock.

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

> [!IMPORTANT]
> **The lock is cooperative and advisory, not enforced.** Seafile exposes no atomic server-side mutex, so nothing prevents a client that ignores the protocol — or two clients racing on an eventually-consistent directory listing — from both concluding they hold the lock. What actually keeps a push safe is what happens *around* the lock: while it is held the remote refs are re-read, the fast-forward check runs against those fresh tips, and each ref is re-read once more immediately before it is written. Treat the lock as coordination that makes the ordinary case orderly, not as a guarantee the server enforces.

- **Ticket-Based Protocol**: During every `git push`, the helper writes a unique lock ticket (`.git-lock.d/<nonce>.json`) at the remote repository root on Seafile. A ticket that has been *granted* the lock records that fact (`"holding": true`), and the queue orders granted tickets ahead of every ticket that has merely asked for it.
- **Time decides who waits, never who holds**: among tickets at the same rank the queue falls back to acquisition time — each client's *estimate* of server time, taken from its own clock plus the offset implied by the HTTP `Date:` response header. That estimate is good enough to order a queue and far too coarse to decide ownership: `Date:` has one-second resolution, so two clients routinely disagree by a fraction of a second, and an estimate half a second behind an active holder's is indistinguishable from a genuinely earlier arrival. Before v0.8.4 it was the ordering key, and a newcomer could therefore acquire a lock an active holder was still pushing under. It is no longer the key; it is only the tie-break.
- **Mixed-version fleets**: the grant marker is honoured by v0.8.4 and later, so it takes effect only once every client pushing to the repository is on v0.8.4+. A newer client will not preempt an older holder, but an older client ignores the marker and can still preempt a newer holder. Finish the upgrade across the fleet to get the guarantee.
- **Settlement Window**: A win is confirmed by reading the queue a *second* time, one second later. A single read is not enough to claim the lock: a peer that started before you but is still uploading is invisible to it, so you would both read the queue, both find yourselves first, and both push. The wait gives any in-flight peer time to land, after which both clients compare the same set of tickets and the same one wins. This is the one cost the lock adds to a push, and it is fixed: skipping the re-scan is a trade of a correctness property for a second, so it is not offered as a setting.
- **Legacy Note**: The v0.1–v0.3 single-file `.git-lock.json` mirror was removed in v0.7.0. The ticket queue in `.git-lock.d/` is the only lock protocol; a leftover mirror on the remote is ignored (delete it by hand if you like). Do not mix this version with a v0.1–v0.3 helper on the same repository.
- **Dead Local PID Fast-Reclaim**: If an active lock belongs to the current machine (verified via hostname and machine hardware/container network identifier) and the holding process has crashed or exited, the helper detects the dead PID and immediately reclaims the lock without waiting for the timeout. (The machine identifier derives from the hardware MAC: in VMs or containers that randomize it per boot, a rebooted holder is no longer recognized as local, so reclaim falls back to normal lease expiry — safe, just slower.)
- **In-Transfer Lease Renewal**: During active multi-gigabyte packfile uploads, packfile downloads, and remote compaction, `git-remote-seafile` automatically refreshes the lock lease every 20 seconds of sustained transfer progress without spawning background daemon threads. Purely local compute phases (pack generation and `git repack`) renew before and after rather than during; before any destructive step (ref writes, ref deletion, obsolete pack deletion) the client re-validates its hold two ways — that its lock ticket is still its own, and that no other active ticket has been granted the lock ahead of it. A lease that lapsed and was taken over is refused as `another client took over the lock`; a ticket that was overtaken while still alive is refused as `another client was granted the lock while this ticket was still held`.
- If another developer is pushing, subsequent pushes wait in a queue. The wait is `seafile.locktimeout` (15 s) counted from the last time the queue moved, so a push that is moving up is not told "locked" while the queue drains; a queue stuck behind one holder still times out after that, and nothing waits past the larger of 10 x the timeout and 120 s.
- **Lease Safety**: If a client crashes or loses power mid-push, the lock automatically expires after 60 seconds (configurable via `seafile.locklease`), preventing permanent repository deadlocks.

### Inspecting Lock Status (`lock-status`)
Check whether a remote repository is currently locked:
```bash
git-remote-seafile lock-status seafile://code/myproject
```
Example outputs:
```text
Repository at seafile://code/myproject is LOCKED:
  Owner    : 1a2b3c4d
  Machine  : workstation-1
  PID      : 12345
  Nonce    : e6b1f24d78a945b0
  Protocol : ticket
  Expires  : in 42s
```
(The owner is the first 8 hex digits of a SHA-256 hash of the account token — a stable identifier that never reveals the token itself.)
A winning ticket from a client older than v0.8.4 — or one left behind by an abandoned queue — carries no grant marker, and the output says so with an extra line:
```text
  Granted  : not marked -- a pre-v0.8.4 ticket, or an abandoned queue that --force would clear
```
That is not an error in itself, but it does mean the marker-based protection is not in force for this lock, so a concurrent push is worth ruling out before trusting it.
If every ticket in the queue has expired (its holder crashed and the lease ran
out), the holder is still shown but flagged as stale — a plain push would reap
it and take the lock without any intervention:
```text
Repository at seafile://code/myproject is LOCKED:
  State    : STALE -- the ticket's lease has expired, so a plain push would reclaim it
  Owner    : 1a2b3c4d
  Machine  : workstation-1
  PID      : 12345
  Nonce    : e6b1f24d78a945b0
  Protocol : ticket
  Expires  : in 0s
```
or when idle:
```text
Repository at seafile://code/myproject is UNLOCKED.
```

### Breaking or Clearing a Lock (`unlock`)
Release a stale lock held by your machine, or forcibly break an abandoned lock:
```bash
# Release cooperative lock held by your machine/account
git-remote-seafile unlock seafile://code/myproject
# Output: Unlocked repository at seafile://code/myproject.

# Forcibly break any active lock (emergency recovery)
git-remote-seafile unlock seafile://code/myproject --force
# Output: Forcibly unlocked repository at seafile://code/myproject.

# When there is no lock at all, it says so rather than claiming success:
# Output: Repository at seafile://code/myproject is not locked; nothing to release.
```

Both forms accept the flag before or after the URL, and `--help` prints the syntax.

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

### Deeper Recovery

The table below covers the common failures. For a repository that is unreadable, a lock held by a machine that no longer exists, half-uploaded packfiles, or conflict artifacts inside `refs/`, see [RUNBOOK.md](RUNBOOK.md) — it gives the diagnosis and the recovery step for each, and lists what to capture before opening an issue.

### Common Issues

| Error | Cause | Resolution |
| :--- | :--- | :--- |
| `fatal: remote helper 'seafile' not found` | Helper binary not in `PATH` | Ensure `git-remote-seafile` or `git-remote-seafile.bat` is in a folder listed in your system `PATH`. |
| `DANGEROUS PATH COLLISION DETECTED (Trap 1)` | Local working tree is inside synced library at identical remote path | Change remote to an unsynced library (`seafile://code/repo`) or use an ignored subfolder (e.g. `seafile://Documents/seafile-git/repo`). |
| `UNIGNORED REMOTE PATH IN SYNCED LIBRARY (Trap 2)` | Remote destination in synced library is not in `seafile-ignore.txt` | Add the target subfolder (e.g. `seafile-git/`) to `seafile-ignore.txt` at the synced library root. |
| `Seafile library not found: 'XYZ'` | Library not yet created, name typo, or permissions | Create the library via the Seafile Web UI, or verify spelling. The helper will suggest close matches. |
| `git clone` of a repository with a 4 GiB+ file never finishes (Windows) | Git for Windows with `core.autocrlf=true` (its default) cannot check out a blob of 4 GiB or more: measured, a 5 GiB blob stalled a clone for 240 s+, and `git add` of such a file silently *truncates* it (5 GiB became 1 GiB) | `git clone -c core.autocrlf=false <url>`; for files this large set `core.autocrlf=false` *before* `git add`, or use the snapshot tool, which always stages byte-exact. The helper prints a warning when it sees such a pack. |
| `HTTP 401 Unauthorized` | Invalid or expired token | Run `git-remote-seafile check-auth` and verify credentials in `~/.git-seafile.json`. |
| `Could not find Seafile credentials for '…'` | `SEAFILE_TOKEN` was set without `SEAFILE_SERVER`, so it was ignored | Set `SEAFILE_SERVER` alongside the token. A token is only sent to the server it names — see [SECURITY.md](SECURITY.md). |
| `HTTP 403 Forbidden` on push | Read-only library permissions | Ensure your Seafile account has Read-Write permission on the target library. |
| `Push failed: Repository is locked by '<owner>' on '<machine>'` | Another client is mid-push and holds the lock | Wait for that push to finish, or raise `seafile.locktimeout`. If the other client crashed, the lease expires on its own (`seafile.locklease`, default 60s). `git-remote-seafile lock-status <url>` names the holder; `unlock <url> --force` breaks a stale lock. |
| `the uploaded packfile could not be verified on the remote (... bytes on the remote but ... locally); no ref was updated` | The push uploaded a pack and the directory listing does not show it whole (a truncated write, a proxy that answered 2xx for a failed upload) | Nothing was published; re-run the push. If it repeats, check the server and any proxy limits (`seafile.gcmaxpackmib`, §14 *Size and storage*). |
| `could not confirm lock ownership before updating this ref` | A long multi-ref push outlived its lease, or another client took the lock | The refs written so far are intact; re-run the push. Raise `seafile.locklease` for very slow links. |
| `error <ref> stale info` | `git push --force-with-lease` and the remote moved since you last fetched | `git fetch`, look at what landed, and push again. |
| `Warning: the ref <name> is listed but its content could not be read (missing or empty); it is not advertised` | A ref file the server lists could not be downloaded (a transient failure, or a damaged file) | Re-run. A branch that is missing from `git ls-remote` for this reason is *not* gone; do not run `gc` until it reads. `gc` refuses in this state. |
| `refusing to compact ...: git reads back N of M mirrored refs` / `... was listed by its parent directory but is now missing (404)` | `gc` could not prove it knows every ref, so it stopped before deleting anything | Nothing was deleted. Re-run once the listing is stable; it is the guard working. |
| `the server returned a transfer link with an unreadable host` | A download or upload link had a backslash, userinfo or control character in its host | The helper will not send anything to it. Check the server's `SERVICE_URL` / `FILE_SERVER_ROOT` settings. |
| `N director(ies) could not be read, so what is under them is not in the snapshot` | The snapshot tool met a directory it cannot open (a permission, an ACL) | Make it readable, or `--exclude` it on purpose. |

### Known Limitations and What They Mean for You

These are the places where the tool, Git, or Seafile stops short. None is hidden: each is either reported at run time or listed here with what to do. "Measured" means observed on a real server (Seafile CE 13.0.28) rather than inferred; see [DESIGN.md](DESIGN.md) §4.3.1 for the evidence.

#### Concurrency and consistency

| Limitation | What it means for you | What to do |
| :--- | :--- | :--- |
| **The lock is advisory.** Seafile has no atomic compare-and-swap, so the ticket lock reduces contention but cannot *force* exclusion on a client that ignores it. | Two helpers cooperate and never collide. A non-cooperating writer (an old version, a script, the web UI) can overwrite a ref. Measured: simultaneous same-path uploads all report success and leave `(SFConflict …)` copies beside the file. | The helper re-reads every ref after writing it and warns when a conflict copy appears; a peer that writes *between* the helper's last read and its write is not noticed by either side, and the loser's commits are then pruned by the next full `gc`. Recovery is in [RUNBOOK.md](RUNBOOK.md) §5. Keep all writers on the helper. |
| **Pushes are not atomic across refs.** `--atomic` is not supported; refs are written one at a time. | A push of several branches can land some and fail others; each ref is reported separately, exactly as git expects. | Re-run the push; completed refs are no-ops. |
| **A lost race is reported, not retried.** If another client moved the branch between the check and the write, you get `fetch first`. | Your push did not land; nothing is corrupted. | `git fetch`, integrate, push again. |
| **A merge holds the lock.** The automatic tail merge costs about 0.05 s per MiB and runs under the same lock, taken again right after the push has released it (a 4 GiB merge measured 3 min 43 s; if the lock cannot be had the merge is skipped and the next push retries). | Another person pushing during that window is told the repository is locked after `seafile.locktimeout`. A retry succeeds. | Keep `seafile.autogc` on for solo or small-team use; on a busy shared remote, set it to `false` and run `gc --tail` from a schedule. |
| **Each push is a burst of library commits.** Measured: about 13 history entries per push (ticket create/renew/delete, pack, index, ref). | The library's history view is noisy, and server history grows with every push. | Use a library dedicated to remotes, not your document library. |

#### Size and storage

| Limitation | What it means for you | What to do |
| :--- | :--- | :--- |
| **A single request has a ceiling on some servers.** Measured on the test server: 1.75 GiB accepted, 2 GiB reset. | An uncapped 4 GiB merged pack failed at the upload. | Handled: every pack is capped at `seafile.gcmaxpackmib` (1 GiB), and files of 1 GiB or more upload in `Content-Range` chunks (5 GiB verified byte-identical). If your proxy is stricter, lower `seafile.gcmaxpackmib`. |
| **LFS objects are never reclaimed.** No command compacts or expires `/lfs/`, and an object whose name and size match is not uploaded again. | A repository that churns large LFS files grows `/lfs/` for good; a damaged object of the right size is accepted until a checkout fails its hash. | Delete unreferenced objects by hand in the web UI; `git lfs fsck` after a clone. |
| **One object cannot be split.** A single blob larger than the cap becomes one larger pack. | It uploads in chunks and works, but it is not subject to the cap and a merge never moves it. | Nothing; it is frozen where it stands. |
| **Git for Windows cannot handle a 4 GiB+ blob with `core.autocrlf=true`** (its default). Measured: `git add` silently truncated a 5 GiB file to 1 GiB, and `git clone` of a real 5 GiB blob did not finish in 240 s. With `core.autocrlf=false` the same file added in 110 s and cloned in 11 s, byte-identical. | A very large raw-data file can be **silently corrupted at `git add`** before the helper ever sees it, and cannot be recovered from git afterwards. | Set `core.autocrlf=false` *before* adding such files (or mark them `-text` in `.gitattributes`), or use the snapshot tool, which always stages byte-exact. The helper warns when it sees a 4 GiB+ pack on Windows. |
| **Deleting history does not free server space immediately.** Removing a pack removes it from the library; the blocks are reclaimed only when the server's garbage collection runs after its retention window. | Compaction and branch deletion shrink the *repository*, not the server's disk, until `seaf-gc` runs. | A server-administrator task. |
| **Tail merge never prunes.** It is a pure union of the small packs. | Objects from deleted branches survive until a full `gc`. | Run `git-remote-seafile gc <url>` occasionally for reachability pruning and re-deltification. |
| **Merging and `gc` need scratch space.** Measured: scratch about 2x the data, memory about the data size while git maps the packs. | A large manual `gc` can fail late if the temp volume is small. | Point `TMPDIR` / `TEMP` at a volume with 2–3x the repository's packed size. |

#### Servers and libraries

| Limitation | What it means for you | What to do |
| :--- | :--- | :--- |
| **Encrypted libraries are refused.** | The helper stops with a clear message; the server serves their files only after a password the helper cannot supply. | Use an unencrypted library. For at-rest encryption, encrypt the volume or use a tool built for it. |
| **Validated against one server.** Seafile CE 13.0.28 was exercised (round trip, SHA-1 and SHA-256, concurrency, large files). Pro-only features, clustered or object-store backends, and releases other than CE 13.0.x are *expected* to work on the documented `/api2` surface but are not tested. | A problem on another deployment is not unexpected, just unreported. | Run `git-remote-seafile doctor <url>` first; report what it prints. |
| **Seafile's server-side file locking is not used.** It is Pro-only and its owner is the Seafile user, not the machine. | No change to behaviour. | None. |
| **The first push to an empty remote decides the object format.** With no refs there is nothing to infer SHA-256 from. | A SHA-256 repository works, but the format is only visible once a ref exists. | None; it is correct after the first push. |
| **A deleted or renamed library is noticed late.** The library id is cached for the process, so the failure appears at the point of use with the server's error. | A confusing 404 rather than "library not found". | Re-run; the cache is per process. |
| **Server time has one-second resolution.** The lock orders clients by the server's `Date`, never the local clock. | Two pushes inside the same second are ordered by tie-breaks, not by who was "first". | None; it is a fairness detail, not a correctness one. |

#### Git features

| Feature | Status |
| :--- | :--- |
| `--atomic`, push options (`-o`), signed pushes, `followtags` | Not supported; git refuses them before the push. |
| `--force-with-lease` | Supported (from the release after 0.9.1; 0.9.1 and earlier rejected it as non-fast-forward). The helper accepts git's `cas` option, so a push may rewrite history over exactly the value you last saw and is refused as `stale info` if the remote moved; the lock and the re-check under it still apply. |
| Shallow (`--depth`) or partial (`--filter`) clones | The helper installs every remote pack whatever git asked for, so these options do not reduce what is downloaded. |
| `git lfs` | Supported through the custom transfer agent; `lfs.standalonetransferagent` is not scoped per remote, so a dual-remote setup splits objects (see §8.5 and §11.1). |
| Hostile repository config | `seafile.skipsafetychecks` and `seafile.gittimeout` are read only from system/global git config and the environment; a repository's own config cannot weaken them. |

#### Whole-tree snapshots (`git seafile-snapshot`)

| Limitation | What it means for you |
| :--- | :--- |
| No retention or pruning | Every snapshot is kept forever; pruning would rewrite history. Choose the remote knowing it only grows. |
| Secrets are permanent | `.env` and keys are captured by design and Git history is forever. Every run lists the credential-shaped paths; push only to a library only you can read. |
| File modes and symlinks on Windows | The executable bit is not reliably recorded; symlinks restore as text files unless `core.symlinks` is enabled. The restore verification names a mismatch rather than reporting success. |
| Embedded git repositories | The files are captured (ignored ones too); the nested `.git` (history, branches, stashes) is not, so back that up separately. `--no-embedded-repos` records only the commit id, as 0.9.0 did. |
| Timestamps and empty directories | Empty directories are not captured. File times are recorded since 0.9.2 (a run that pushes to a `seafile://` remote, or `--record-mtimes`) in `.seafile-payload/mtimes.json` and applied by a restore; touching a file without changing it makes no new snapshot. Older snapshots and `--no-record-mtimes` restore the checkout time. Directory times are not recorded. |
| Touching a file produces no commit | A heartbeat record of "a backup ran" is not available. |
| Case-insensitive filesystems and long Windows paths | `Foo.txt`/`foo.txt` may not both round-trip; a deep restore destination can exceed 260 characters. |
| Large payloads | The vault turns compression off and raises the big-file threshold after sampling, so a 20 GB tree stages at disk speed. A very large file that *changes* adds its own size to the remote per change until a merge; `seafile.autogc` bounds that. |

The full snapshot caveat list, with the reasoning, is in [SNAPSHOT.md](SNAPSHOT.md) §8.

---

## 15. Whole-Tree Snapshots (`git seafile-snapshot`)

Everything above moves *commits*. That leaves one gap the helper cannot close by construction: **a file your `.gitignore` excludes never enters a commit, so no remote helper can ever carry it.** `.env`, a local database, an editor's state directory, build output you would rather not rebuild — none of it reaches Seafile through a push, however many remotes you add.

`git seafile-snapshot` closes that gap. It captures the working tree **as it lies on disk** — ignored files and all — into versioned, byte-exact snapshots stored in a Seafile library, over the same transport the helper already uses. It is its own command (installed with the package; `git-remote-seafile snapshot` is an alias), and it needs no credentials of its own: it reaches Seafile the way `git push` does.

> [!NOTE]
> **Installed with the package.** `pip install git-remote-seafile` provides the helper (`git-remote-seafile`) *and* the snapshot tool as `git-seafile-snapshot` — so `git seafile-snapshot` works anywhere git finds it on `PATH` — and as `git-remote-seafile snapshot`. From a source checkout, `git seafile-snapshot` runs the same program (the file is a thin wrapper around `git_remote_seafile/snapshot_tool.py`, so existing commands and scheduled jobs keep working). (`git seafile-snapshot --help` is intercepted by git, which looks for a man page: ask with `-h`, or run `git-seafile-snapshot --help`.) The diagnostics script `tools/seafile_doctor.py` (README, *Diagnostics for the Seafile desktop client*) still ships with the source and the sdist only.

> [!NOTE]
> **Git 2.25+ if you use exclusions.** The tool stages the surviving paths through
> `--pathspec-from-file`, which arrived in Git 2.25. Without `--exclude` or
> `--default-excludes` it stages with a plain `git add`, where the package's
> documented Git 2.20 floor (§5) is enough; with them you need 2.25. It does
> **not** fall back to a plain `add` on older Git, and that is deliberate:
> staging first and dropping the excluded paths from the index afterwards would
> read exactly the files the exclude list exists to keep out of the vault.

> [!IMPORTANT]
> **Keep the Golden Rule (§2).** A snapshot's *source* may be anywhere — including inside a synced library — because the tool does not write to it (the one exception is a probe file at the root of a source on a network share, created and removed within a moment to read the share's clock). The snapshot's *destination* is a Seafile library, and like every other remote in this guide it should be an unsynced one.

### 15.1 The vault

Pointed at a source directory, the tool maintains a **separate repository — the vault — whose `--work-tree` is that source**:

```bash
git --git-dir=<vault>/.git --work-tree=<source> …
```

The source stays read-only and untouched: nothing is committed into it, no branch is created in it, and its own index and refs are never consulted. The vault does the commiting, and the vault is what you push to Seafile.

Why a separate repository rather than a branch or a subdirectory in the source? Because either alternative would *publish* the ignored files into the source's own history — and, in the subdirectory case, would place snapshot data inside the tree it is trying to snapshot. The reasoning is worked through in `SNAPSHOT.md` §11.

### 15.2 Capturing a snapshot

Every option has a default, so the first run is the only one that needs an argument — and even that can be just the directory:

```bash
# The whole thing: snapshot C:/code/myproject into C:/code/myproject-vault
# and push it to Seafile, inferring the destination from the source repo
cd C:/code/myproject
git seafile-snapshot

# From elsewhere, naming the tree. The vault defaults to '<source>-vault'.
git seafile-snapshot C:/code/myproject

# What that would do, without writing anything
git seafile-snapshot C:/code/myproject --dry-run

# The same, said explicitly
git seafile-snapshot --source C:/code/myproject \
    --vault C:/code/myproject-vault \
    --remote seafile://code/myproject-vault
```

**The first run teaches the vault what you chose, and later runs remember it.** The destination, branch and exclusions are written into the vault's own config, so a scheduled job can be nothing more than the bare command — and an explicit flag always wins and is written back for next time.

Defaults are resolved most-specific-first:

| Priority | Source | Example |
| :--- | :--- | :--- |
| 1 | The flag you passed | `--remote seafile://backups/app` |
| 2 | What the vault remembered | the remote from the last run |
| 3 | The source repository | `seafile://code/app` → `seafile://code/app-vault` |
| 4 | The current directory | source = `.`, vault = `<source>-vault` |

`--no-push` overrides the remembered or derived remote *for that run* — it pushes nothing (neither the vault nor the code repo) and leaves the vault's memory alone, so a one-off local run does not unhook a scheduled job. `--dry-run` previews the run — the vault is initialised if missing and the snapshot is built into its index, so the diff is real — without moving the branch, pushing, or touching the remembered settings.

| Flag | Effect |
| :--- | :--- |
| `SOURCE` (positional) or `--source <dir>` | The tree to snapshot. Default: the current directory. |
| `--vault <dir>` | Where the vault repository lives. Default: `<source>-vault` beside the source. |
| `--remote <url>` | The Seafile destination. Default: remembered, else derived from the source's own `seafile://` remote. |
| `--branch <branch>` | The vault branch holding the snapshots. Default: `snapshot` — one shared chain across machines (§15.6). `--branch snapshot-<host>` gives per-machine chains instead. |
| `--exclude <glob>` | Repeatable. Skip matching paths, e.g. `--exclude node_modules`. |
| `--default-excludes` / `--no-default-excludes` | Add the recommended junk exclusions (§15.2.1), or explicitly not. |
| `--no-embedded-repos` | Record a nested git repository only as its commit id (the 0.9.0 behaviour). By default its files are captured, its `.git` is not. |
| `--record-mtimes` / `--no-record-mtimes` | Record (or not) the modification time of git-stored files in `.seafile-payload/mtimes.json`. On by default for a run that pushes to a `seafile://` remote; an explicit choice is remembered by the vault. |
| `--payload-report-days N` | Payload mode: print the report-only `--payload-gc` summary when none was printed in N days (default 7, `0` off). |
| `--message <text>` | Override the snapshot commit message. |
| `--code-remote <name>` | Also `git push <name>` in the source repo first; the name is validated up front. |
| `--no-push` | This run pushes nothing and changes no remembered setting. |
| `--dry-run` | Report what would be committed and pushed, and do neither. |
| `--reset-to-remote` | Adopt the remote chain when the vault has diverged and the two have no common tip, dropping the local-only snapshots (§15.6). Never taken implicitly — this is the one way a snapshot can be lost. |
| `--allow-shared-remote` | Override the shared-remote guard (§15.5) deliberately. |
| `--allow-synced` | Proceed even when the source, the vault or a restore destination is inside a Seafile-synced folder (§15.5). The sync client rewrites files under Git, so this is for a library you have made inert (auto-sync off, or `seafile-ignore.txt` covering it). |
| `--payload-min-size [<size>\|auto]` | **Payload mode.** Selected files are not put through git: each is SHA-256-hashed and uploaded **by hash** to `<path>/payload/objects/` inside the vault's remote directory, and the commit records a manifest. With no value, or `auto`: files of 64 MiB or more, plus files of 4 MiB or more that do not compress. With a size (`64M`, `1G`): files of at least that size. A restore downloads and verifies them. Objects are never overwritten or deleted by a run, so a snapshot costs nothing to keep and a rename uploads nothing. Needs a remote; cannot be combined with `--no-push`; `0` turns it off. A vault that has used it needs the flag on every run. Put SOURCE first, or write `--payload-min-size=auto`. See PAYLOAD.md. |
| `--payload-rehash` | With payload mode, ignore the hash cache for this run and hash every payload file again (a tree whose timestamps were restored by a copy tool, or a periodic audit). A real run **remembers** `--payload-min-size` in the vault, so a later bare run keeps using it (`0` is remembered too). |
| `--payload-gc` | **Payload retention.** Report the stored objects no snapshot references (in any ref of `--remote`, or the local `--vault`, which must be a vault). With `--payload-delete` it marks them, and a later run deletes those that stayed unreferenced for `--payload-grace-days` (default 7, minimum 1). Refuses to act when it finds no manifest, an unreadable one, or a Seafile conflict copy of a ref. |
| `--payload-verify [<n>\|all]` | **Payload audit.** Download *n* randomly chosen objects (default 20, or `all`) of the newest snapshot of `--remote` and check each against its SHA-256; exit non-zero and name the damaged ones. The server cannot report a content hash, so this is the only way to find an object damaged after it was stored. About 85 MiB/s: all of a 111 GB payload takes roughly 25 minutes. |
| `--payload-view [<ref>]` | **Payload view.** Build a browsable directory of real file names for a snapshot of `--remote` (a branch, or `snapshot~2`; default `snapshot`) at `<path>-view/<name>` in the same library, by server-side copy and rename of the stored objects: no data moves and no extra disk is used (quota counts the logical size). A convenience, not a restore: no mode or mtime, and the server's name rules apply (names it cannot hold are reported). `--payload-view-name <name>` picks the directory (default: the snapshot id); `--payload-view-replace` rebuilds an existing one, so `current` can be refreshed. Write the ref as `--payload-view=<ref>`. |
| `--payload-keep <n>` | With `--payload-gc`, count only the newest *n* snapshots of each branch and tag as using objects, so older snapshots expire: their exclusive objects are removed by `--payload-delete` after the grace period and those snapshots can no longer be restored. Default: keep all. |
| `--payload-delete` | With `--payload-gc`, act (mark, then delete after the grace period); default: report only. |
| `--payload-grace-days <n>` | With `--payload-gc`, how long an object must stay unreferenced before it is deleted. |
| `--restore` | Restore a snapshot instead of taking one; requires `--remote` and `--into` (§15.4). |
| `--into <dir>` | Destination directory for `--restore`. |
| `--ref <rev>` | Which snapshot to restore. Default: the vault branch tip. Only meaningful with `--restore` — passing it without one is an error, not a no-op. |
| `--verify` | With `--restore`, also run `git fsck` over the clone. |

**An unchanged tree commits nothing.** The tool compares the new tree against the previous snapshot and stops if they match, so a scheduled job — hourly, say — costs a directory walk and a push that transfers nothing. This is what makes it safe to run far more often than you would run a backup.

#### 15.2.1 What is *not* excluded by default

Nothing. The tool captures the whole tree, including `node_modules/`, `dist/` and `__pycache__/`, and then **tells you** what it noticed:

```
WARNING: the snapshot includes reproducible build output that is usually not worth
backing up.  Leave it out next time with --default-excludes, or explicitly:
  --exclude 'build' --exclude 'dist' --exclude 'node_modules' --exclude '*.pyc'
```

This is deliberate. A snapshot is a backup, and the one failure that cannot be forgiven is a backup that quietly omitted the file that mattered. `node_modules` is reproducible; `.env` is not; the tool cannot tell them apart, so it does not try. `--default-excludes` adds exactly the set above, on top of anything you passed to `--exclude`.

### 15.3 Byte-exactness, and why it needs enforcing

A snapshot tool that quietly changes your files is worse than none. Git will rewrite line endings on checkout if `core.autocrlf` says so, and on Windows the trap is sharp: **Git for Windows sets `core.autocrlf=true` in its *system* config**, so `git config --global core.autocrlf` reports nothing while the effective value is `true`, and a fresh `git init` inherits it.

The vault therefore pins, for itself:

```
core.autocrlf=false
core.safecrlf=false
core.longpaths=true
core.quotepath=false
```

and writes this into its own `info/attributes`:

```
* -text -filter -ident
```

`info/attributes` **outranks** any `.gitattributes` copied from the source, which is what makes the pin hold. Three details worth knowing, because all were learned the hard way:

- **An attributes override must name *every* attribute class it means to neutralise.** `* -text` alone does **not** disable `filter`: a source `.gitattributes` carrying `filter=lfs` still applies, and the vault silently stored a **130-byte LFS pointer** while the real bytes went nowhere.
- **Paths over 260 characters and non-ASCII names are pinned.** `core.longpaths=true` allows deep directory structures on Windows, while `core.quotepath=false` ensures multi-byte international filenames (such as Thai, CJK, or accented Latin) remain legible in snapshot diffs and status output instead of being C-quoted as octal escapes (e.g. `\340\270...`). (If your source repository's own `git status` shows octal escapes, set `git config core.quotepath false` or `git config --global core.quotepath false`).
- **A plain `git clone` of the vault is lossy.** `info/attributes` is not carried by a clone; the source's `.gitattributes` is. Restoring by cloning the vault by hand can therefore hand you the wrong bytes. That is why `--restore` exists.

### 15.4 Restoring and verifying

```bash
# Rebuild a tree into a target directory, verifying byte-exactness
git seafile-snapshot --restore \
    --remote seafile://backups/myproject \
    --into ~/restored

# Any revision works -- e.g. roll back three snapshots
git seafile-snapshot --restore \
    --remote seafile://backups/myproject --into ~/restored --ref snapshot~3

# Additionally run `git fsck` over the vault
git seafile-snapshot --restore \
    --remote seafile://backups/myproject --into ~/restored --verify
```

`--restore` takes `--remote` (the vault to restore from) and `--into` (a new,
empty directory); `--source` and `--vault` do not apply — the tree comes from
the remote, and the guard refuses flags that would be ignored. `--restore`
clones `--no-checkout`, re-applies the settings above, then checks out — and
verifies the result on **every** run with

```bash
git hash-object --no-filters -- <path>
```

which hashes the bytes as they lie on disk. That is the only one of Git's three hash checks that cannot be fooled:

| Check | What it hashes | Fooled by a mangled checkout? |
| :--- | :--- | :--- |
| `git status --porcelain` | the file *through* the filters | Yes — compares filtered bytes to filtered bytes |
| `git hash-object` | the file after the clean filter | Yes — the filter can normalise the difference away |
| `git hash-object --no-filters` | the raw bytes on disk | **No** — and it holds for *any* checkout, including one you made by hand |

> [!NOTE]
> **A symlink restored onto Windows without `core.symlinks` fails that check, and the restore aborts.** Git checks a `120000` entry out as a plain text file whose contents are the target path, so the verification finds no link to read and names the path as a mismatch. Because the check runs on **every** restore — not only under `--verify` — the run stops there rather than quietly handing back a tree of files where the source had links. The blobs are intact: enabling `core.symlinks` (Developer Mode, or an elevated process) and restoring again reproduces the links. Symlinks are verified by reading the link itself, never by `hash-object`, which on POSIX would follow the link and hash its target.

### 15.5 What the tool refuses to do

- **It will not stage a tree the vault's volume cannot hold.** Before the first byte is staged the tool estimates the new object storage (tree size scaled by the sampled compressibility, minus what the vault already holds) and refuses when that exceeds the free space less 1 GiB; `--allow-low-disk` overrides. A 130 GiB tree on a volume with 60 GiB free is refused with the numbers rather than filling the disk; narrow the tree with `--exclude` or point `--vault` at a bigger volume.
- **It will not push the vault to one of the source's own code remotes.** A whole-tree backup legitimately contains `.env`, keys and build artefacts; pouring that into a shared code library is almost never what anyone wants. If the vault's destination collides with a URL in the source's `remote.*.url` / `remote.*.pushurl`, the push is refused and the colliding remote named. `--allow-shared-remote` overrides it.
- **It reports credential-shaped paths at the end of every run.** This is a *warning, not a refusal* — a full backup *should* capture `.env`; the point is to find out before the first push, not after.
- **It will not run on a stale lock.** A `seafile-snapshot.lock` left behind by a killed process is detected by checking whether the owning PID is still alive -- and, since 0.9.2, whether it is still the *same process* (the lock records the process's start time, because Windows recycles pids and a crashed nightly run's number soon belongs to something long-lived) -- and reclaimed rather than blocking every future run.
- **It will not leave a hole for a directory it cannot read.** `git add` only prints a warning for a directory it cannot open and carries on, so everything under it would be missing from the snapshot with exit 0. The run stops and names it; make it readable, or `--exclude` it on purpose.
- **A vault belongs to one source tree.** The vault records the source directory it first saw and refuses a run that points a *different* tree at it — mixing two trees into one linear chain of snapshots is not something a later reader could undo. Give each tree its own vault.
- **Neither the source nor the vault may live in a Seafile-synced folder.** Checked at startup, before anything is written. The sync client rewrites files underneath whatever lives in a synced folder and competes with Git for every path — the exact problem this tool exists to avoid.
- **`--code-remote <name>` must name a real remote** of the source. Without this check a typo would fail *after* the snapshot was built, leaving a commit for a run the user reads as failed.
- **Flags that would otherwise be ignored are errors.** `--ref` without `--restore` is the sharp one: a run meant to restore an *old* snapshot would quietly take a *new* one instead. `--no-push` together with `--remote` is contradictory and also refused.

### 15.6 Several machines, one vault

A vault is a Git repository, so it can be pushed to from more than one machine — but each machine needs to branch from the **remote tip**, not from its own local branch. Getting this wrong is subtle: the second machine's push is rejected `(fetch first)` **and its local branch still moves**, so the first run looks like it worked while every later run fails. The tool fetches the remote tip and bases on it, retries once by re-parenting if it loses a race, and rolls its local branch back to what the remote holds if a push fails.

By default all machines append to **one** shared chain on `refs/heads/snapshot`; the hostname in each snapshot's commit message is how `git log` says which machine saw which tree (`SNAPSHOT.md` §6 documents the reading). If you would rather keep per-machine chains, pass an explicit `--branch snapshot-<hostname>` — supported, but never the default, and the divergence handling above is what makes the shared chain safe.

### 15.7 What it costs

Measured on real pushes, not estimated (`SNAPSHOT.md` §8):

| Situation | Transferred |
| :--- | :--- |
| A 486 KB tree with one 4 KB and one 10 KB file changed | 31 KB |
| Nothing changed | **0 KB — and no commit** |
| A 1-byte edit to an 8 MB file | +8202 KB, until `git-remote-seafile gc` |
| A 32 MB file snapshotted three times | remote grows 33 → 65 → 97 MB |

The last row is the one to watch: a large file that changes daily adds its own size to the Seafile library every day. `seafile.autogc` (on by default) tail-merges those small packs between full gcs and bounds the growth; if you turned it off, run `git-remote-seafile gc` (§10) on the *vault's* remote periodically — or keep large build artefacts out with `--default-excludes` (§15.2.1).

### 15.8 Keeping the vault small

Every snapshot commit writes new objects into the vault's local object store, and like any repository they accumulate in tiers: loose objects (one file per object — what a fresh clone or first commits produce), incremental packs, and full packs after consolidation. The tool maintains the first two tiers itself; the remote tier is the helper's job (§10).

- **Automatic incremental repack.** Before each push that has a destination, the tool measures its own loose tier, and once it passes 256 MiB it runs `git repack -d` + prune — consolidating *loose objects only*, never touching existing packs. (An object-*count* trigger already exists in git itself: the `git fetch` this tool runs launches `gc --auto` once about 6,700 loose objects exist.) It is best-effort: a failure is reported and the push proceeds anyway. `--no-repack` skips it for one run.
- **Manual full reset.** `--full-repack` runs `git repack -ad` + prune over the whole store, collapsing everything into one pack. Use it after large deletions or when the local pack count has grown high; nothing runs it automatically.
- **Report.** `--maintain` changes nothing: loose/object/pack counts, object-store size against free disk space, and the storage profile (below).

**Large raw payloads get a storage profile, decided once from a sample.** Two git costs scale with raw bytes and buy nothing on data that neither compresses nor deltifies: staging deflates every blob (42 MB/s on incompressible data, 131 MB/s with `core.compression=0`), and `pack-objects` — the helper's push and the vault's own background `gc --auto` — runs a delta search over every blob below `core.bigFileThreshold` (default 512 MiB), which measured 8–10 MB/s regardless of the compression level. On a 20 GB+ tree that is the difference between minutes and a timeout.

Before the first staging the tool samples the tree (a few chunks from each of up to 32 of the largest files — about 24 MiB of reads however large the tree is). When the tree is at least 2 GiB, big files (≥ 1 MiB) hold at least half of it, and the sample keeps ≥ 50 % of its size under a fast deflate (compressing on one core costs more time than the bytes it saves cost on the wire, so only data that shrinks to under half is left on the slow path), the vault is switched to the **raw profile**: `core.compression=0` and `core.bigFileThreshold=50m` in the vault's own config, with one line saying so and how to undo it. Nothing about the *content* changes — only storage encoding and packing strategy, which are safe to flip at any time.

- The decision is remembered (`snapshot.profile`) and not repeated. A vault that began small and grew past 2 GiB is sampled again once per doubling of the tree.
- A vault where you have set `core.compression` yourself is never second-guessed.
- `--no-object-compression` applies the raw profile unconditionally.
- `--dry-run` takes the sample and reports what a real run would do, without writing it.

**Long steps show a heartbeat and are killed only when nothing is happening.** Staging, repacking and cloning print "still staging the tree ... 4m12s (9 GiB written to objects)" once a minute. A step is killed only if its object directory stops growing for 30 minutes (`SEAFILE_SNAPSHOT_STALL_LIMIT` raises that, in seconds) — a *sliding* limit, so a slow-but-real run survives and a dead disk does not. The push step is never killed for silence (its upload is silent by construction, and a dead link fails on the helper's own HTTP timeout), and children inherit `SEAFILE_GIT_TIMEOUT` = the same horizon unless you set one.

**The vault's remote is a remote.** Every push adds one small packfile there, so the §10 threshold warning and tail merge apply to it exactly as to any other — and SNAPSHOT.md §8's large-files caveat is the reason to watch it.

The full design — the alternatives that do not work, the nested-repository analysis (the measured capture shipped in 0.9.1), the own-review findings, and a comparison with restic and Borg — is in [SNAPSHOT.md](SNAPSHOT.md).

### 15.9 Large payloads: payload mode

> [!WARNING]
> **Upgrading to 0.9.0: payload mode is on by default.** A vault that holds files of 64 MiB or more (or 4 MiB or more that do not compress) in git moves them out of git on its **next** snapshot: one full hash-and-upload pass, the large files stored twice on the server (the old blobs stay in the vault's history, the new objects sit under `payload/`), and they show as deleted in `git diff`. Snapshots taken before the switch restore as before; later ones need the payload, **and 0.8.8 cannot restore them completely** -- it has no manifest support and restores the tree without the large files. A plain `git clone` of a payload vault does not contain the large files either. A vault with no such file is unaffected. To keep a vault exactly as it was, run once with `--no-payload`; it is remembered.

A vault carries every file *through git*: each is hashed, written as a loose object,
packed, uploaded. For a tree that is mostly large, immutable, already-compressed
data -- rasters, archives, arrays, video -- that is pure overhead: a second local
copy, a long staging pass, and a pack cap. **Payload mode** keeps such files out of
git. Each is hashed (SHA-256) and stored on the server under that hash, inside the
vault's own remote directory (`<path>/payload/objects/`); the snapshot records a
manifest (path, mode, size, hash, mtime) instead. Measured on a real server: a 131 GiB
tree (633 payload files, 124.6 GB) snapshotted in 38 minutes and restored
byte-exact.

```bash
# Take a snapshot. Payload mode is on by default ('auto'): files of 64 MiB or more,
# plus files of 4 MiB or more that do not compress, go to the server by hash. A size
# works too (--payload-min-size=100M); put the source first, or use the = form.
git seafile-snapshot C:/code/flood --default-excludes

# The vault remembers the setting; a bare run keeps using it.
git seafile-snapshot C:/code/flood

# Turn it off (remembered): every file goes through git, as in 0.8.8.
git seafile-snapshot C:/code/flood --no-payload

# Restore is the same command as always; it downloads and verifies the payload.
git seafile-snapshot --restore --remote seafile://code/flood-vault --into D:/restored --verify
```

What to know:

- **It is on by default, and remembered.** With a `seafile://` remote and a push, a
  run uses `auto` unless the vault remembered something else. A tree with no file
  that big is snapshotted exactly as before: no manifest, no server directory. A run
  that cannot use it (no remote, a remote that is not `seafile://`, `--no-push`)
  simply does not. A complete real run stores the setting in the vault
  (`--no-payload`, or `--payload-min-size 0`, turns it off and is remembered too); a
  preview never writes it. A vault whose last snapshot used payload mode refuses a
  run that would put its large files back into git by accident. When the default is
  about to move an *existing* vault's large files out of git, the run prints a
  `NOTICE` saying how many files and bytes, **before** it hashes or uploads anything
  (a preview says "would move"); interrupt the run and use `--no-payload` to keep
  the vault as it was.
- **A plain `git clone` of a payload vault does not contain the large files.** It
  holds the manifest and `.seafile-payload/README.txt`; get the tree back with
  `--restore`.
- **The store only grows** until you run `--payload-gc` (and `--payload-keep`); a run
  that uploaded objects prints how many and how large the store is.
- **How it meets git.** The vault is still an ordinary repository; payload files are simply never staged.
  - *Inside git* a payload snapshot is the small files plus two ordinary blobs, `.seafile-payload/manifest.json` (one line of JSON, sorted keys) and `README.txt`. `git log`, `git show`, `git fsck`, `git gc`, `git repack` and `git clone` work as on any vault; a manifest change shows in `git diff` as one long line, so read it with `git show snapshot:.seafile-payload/manifest.json | python -m json.tool`.
  - *Outside git* the large files are plain files in `<vault path>/payload/objects/` on the server. Git never sees them, so `git gc` and the helper's `gc`, `gc --tail` and `doctor` neither read nor delete them (the helper ignores a foreign `payload/` directory); only `--payload-gc` removes an object, and only one no snapshot references.
  - *A checkout is not a restore.* `git clone`, `git checkout` or `git switch` gives you the manifest, not the files; `--restore` (or `--payload-view` for a browsable copy made on the server) is what produces them, and verifies each by SHA-256.
  - *History rewrites need care.* The manifests are what keep objects alive: `git push --force`, `git filter-repo` or deleting a snapshot branch or tag make older manifests unreachable, and a later `--payload-gc --payload-delete` will then treat their objects as unused.
- **A snapshot costs nothing to keep.** Objects are not overwritten (the one exception
  is an object found with the wrong size, which is replaced), so a snapshot is a
  manifest; a renamed or duplicated file uploads nothing.
- **A payload vault cannot take a snapshot without pushing.** The objects must be on the
  server before a commit names them, so `--no-push` is refused on a vault that uses
  payload mode (and `--no-push --no-payload` stages the large files into git, which is
  the cost payload mode exists to avoid). `--dry-run` previews without pushing.
- **`--payload-verify` audits the newest snapshot only.** Objects that only older or
  tagged snapshots reference are found damaged at restore time, not before.
- **Tags are pins, but only if they are on the remote.** `git tag` in the vault is local;
  push it (`git -C <vault> push <remote> <tag>`) or a retention run on another machine
  does not see it. A pinned snapshot's own objects are kept whatever `--payload-keep`
  says.
- **The manifest is one line of JSON** (so no text filter can alter it): `git diff` and
  `git show` of it are not readable. Use `--payload-view` for names, or `jq` on the blob.
- **A machine on 0.8.8 or earlier restores a payload vault's git tree only**, without the
  large files and without a warning (it does not know manifests). Restore with 0.9.0 or
  later.
- **A weekly report, never a deletion.** After a run that pushes, the tool prints the
  report-only `--payload-gc` summary (stored, in use, reclaimable) when none was printed in
  the last 7 days; `--payload-report-days N` changes the period and `0` turns it off. A
  report that fails is a note, not a failed run, and nothing is deleted without
  `--payload-gc --payload-delete`.
- **Uploads are parallel and verified.** `--payload-jobs N` (default 3). Each object
  goes up under a temporary name, is hashed as it is sent, and is renamed to its hash
  only if the bytes match; the whole set is re-checked just before the vault is pushed.
- **Look at it, or audit it.** `--payload-view` builds a browsable directory of real
  names by server-side copy (no data moves); `--payload-verify [N|all]` downloads and
  hashes a sample, which is the only way to find an object damaged after it was stored.
- **Retention is explicit.** `--payload-gc` reports what no snapshot references;
  `--payload-delete` marks it and a later run deletes it after `--payload-grace-days`;
  `--payload-keep N` lets older snapshots expire (they then cannot be restored). Run it
  from one machine, and set the library's history to keep a few days as a safety net.
  Deleting needs `--vault <your long-lived vault>` (or `--payload-no-vault`, if the
  remote really is the only copy), because a snapshot that was never pushed would
  otherwise look unused. If the remote's history was rewritten since the last delete
  run -- a force push, a deleted branch or tag -- or `--payload-keep` would expire
  snapshots, the run refuses until you pass `--payload-accept-loss`. **A tag is a
  pin:** `git tag keep-2026-q3 <snapshot>` keeps that snapshot's objects whatever
  happens to the branch.
- **What it checks before it starts.** A restore that would not fit the free disk is
  refused before the first download (`--allow-low-disk` overrides); an upload larger
  than your remaining quota, or into a library this machine syncs, is warned about
  first; a fresh vault is refused when the remote's snapshots use another threshold
  than the default (repeat that flag, or pass `--payload-min-size auto` to switch).
  Every snapshot commit records the tool's version in a `Git-Remote-Seafile:`
  trailer, and a run warns when it builds on a snapshot written by a newer tool.
  The store line and the gc report say when `--payload-verify` last ran.
- **What it does not do:** no encryption, no delta between versions of one file (a
  changed file is a whole new object; the server shares unchanged blocks), and a
  file's name must be restorable on the platform you restore to (Windows names it
  cannot hold are skipped and reported).

The specification, the requirements and the review that shaped it are
[PAYLOAD.md](PAYLOAD.md); the measurements are in
[SNAPSHOT.md section 15.1](SNAPSHOT.md).

---

## 16. Acknowledgments & Inspirations

`git-remote-seafile` builds upon pioneering concepts and implementations in the Git remote helper and cloud storage ecosystem:
- [git-remote-dropbox](https://github.com/anishathalye/git-remote-dropbox) by Anish Athalye: The landmark implementation that demonstrated transparent Git packfile transport and distributed locking over cloud storage APIs.
- [git-remote-gcrypt](https://spwhitton.name/tech/code/git-remote-gcrypt/) by Joey Hess: Pioneered decentralized, encrypted, push-to-anything Git remotes.
- [git-remote-s3](https://github.com/jason-riddle/git-remote-s3): Git remote helper for Amazon S3 object stores.
- [Git Remote Helpers Protocol Specification](https://git-scm.com/docs/git-remote-helpers): Official Git documentation on building custom remote helpers (`git-remote-helpers(7)`).
- [Git LFS Custom Transfer Agent Protocol](https://github.com/git-lfs/git-lfs/blob/main/docs/custom-transfers.md): Official Git LFS specification for line-based stdio custom transfer agents.
- [Seafile Web API v2](https://manual.seafile.com/develop/web_api_v2/): The underlying REST API provided by Seafile Ltd.

---

## 17. AI Disclosure & Authorship

See [AI Disclosure & Authorship](README.md#ai-disclosure--authorship) in the README.
