# Proposals for Seafile Upstream Integration (Phase B)

This document contains ready-to-copy proposals for submitting `git-remote-seafile` to the Seafile community and maintainers across three complementary channels.

---

## Channel 1: Seafile Community Forum RFC

- **Destination**: https://forum.seafile.com/
- **Category**: **Feature Requests** or **Development**
- **Title**: `[RFC] git-remote-seafile: Transparent Git Remote Helper for Seafile (No sync churn, Git LFS, distributed locking)`

### Post Body:

Hi Seafile Community and Haiwen Team,

For years, developers using Seafile have tried to sync active software repositories across machines by storing working directories directly inside synced libraries. As many of us have experienced firsthand, this causes severe friction:

1. **Sync Client Thrashing**: A single `git commit` touches 15+ files inside `.git/` in milliseconds, triggering hundreds of continuous library re-index cycles per day (over 1,500+ cycles/day observed in active repos).
2. **Lock Contention**: Ephemeral lock files like `.git/index.lock` sync across machines, freezing Git operations on secondary computers.
3. **Conflict Storms**: Diverging branches cause Seafile to generate hundreds of `(SFConflict ...)` duplicate files rather than allowing Git to perform 3-way line merges.
4. **Ignore Limitations**: `.seafile-ignore.txt` cannot ignore folders retroactively, must sit at library root, and amputates `.git/` from secondary machines if used.

To solve this properly without requiring developers to deploy and maintain a separate Git server stack (like GitLab or Gitea), I have built **`git-remote-seafile`**—a transparent, zero-thrash Git remote helper for Seafile.

- **GitHub Repository**: https://github.com/tkittich/git-remote-seafile
- **Architecture & Design Spec**: https://github.com/tkittich/git-remote-seafile/blob/main/DESIGN.md
- **User's Guide**: https://github.com/tkittich/git-remote-seafile/blob/main/USER_GUIDE.md

### How It Works

Instead of placing local working repositories inside synced folders, developers keep their code in normal un-synced folders (e.g. `C:\code\myproject` or `~/code/myproject`).

`git-remote-seafile` implements the official `git-remote-helpers(7)` protocol and speaks directly to the **Seafile Web API v2.1** over HTTP/HTTPS:

```bash
# Push directly to any Seafile library as an authentic Git remote:
git remote add origin seafile://code/myproject
git push -u origin main

# Clone on another workstation:
git clone seafile://seafile.example.com/code/myproject
```

### Key Capabilities & Guarantees

- **Atomic Packfile Transport**: Commits are bundled into native Git packfiles (`.pack` and `.idx`) before upload. Pushing 100 commits uploads only **1–2 files**, resulting in sub-second pushes (<1s) and zero desktop daemon scanning.
- **Fast-Forward Safety & Conflict Elimination**: Non-fast-forward pushes are rejected unless force-pushed. Standard Git pull/rebase handles merging; no `(SFConflict ...)` files can ever be generated.
- **Distributed Lease Locking**: Pushes acquire an advisory lease on `.git-lock.json` on Seafile with auto-expiration (60s), preventing race conditions without risk of deadlocks from crashed clients.
- **Git LFS Integration**: Implements the official Git LFS Custom Transfer Agent protocol (`lfs-transfer`), allowing multi-gigabyte models, datasets, or video assets to stream into Seafile storage.
- **Remote Packfile Compaction & Auto-GC (`gc`)**: Re-packs accumulated remote packs, delta-compresses history, and automatically prunes unreachable commits from rolled-back branches.
- **Zero-Config Auth**: Automatically detects active logins from Windows, macOS, Linux, and `seaf-cli` local desktop client SQLite databases (`accounts.db`), or falls back to `~/.git-seafile.json` / environment variables.
- **Test Suite**: Includes 38 automated unit tests tested on Windows, with CI workflows configured for Windows, Linux, and macOS (Python 3.8–3.14).

### Proposal to Haiwen / Upstream Integration

I would love to donate or upstream this project into the official Seafile ecosystem:
1. Host under the official organization as `haiwen/git-remote-seafile` (or bundle within the client distribution).
2. Distribute via PyPI and package with Seafile desktop installers.
3. Integrate a context-menu option in the Seafile desktop client (*"Copy Git Remote URL"*, patch ready in repo).

*Transparency & Authorship Note*: In the spirit of open-source transparency, this project (codebase, test suite, and documentation) was authored primarily using generative AI assistance (Google DeepMind's Antigravity / Gemini) in collaboration with human architectural design, real-world forensic diagnostics, and testing on Windows.

Feedback, suggestions, and thoughts from the maintainers (@daniel.pan, @Jonathan) and community are warmly welcome!

---

## Channel 2: Pull Request to haiwen/seafile-client

- **Destination**: https://github.com/haiwen/seafile-client/pulls
- **Branch**: Fork `haiwen/seafile-client`, branch: `feature/copy-git-remote-url`
- **Title**: `ui: Add "Copy Git Remote URL" to library and folder context menus`

### PR Description:

### Summary
This PR adds a convenient **"Copy Git Remote URL"** action to the right-click context menu of libraries and folders in the Seafile desktop client (`RepoTreeView`).

When clicked, it generates and copies the standard `seafile://` remote URL (`seafile://<server-host>/<library-name>/<path>`) to the user's system clipboard.

### Context & Motivation
Developers frequently use Seafile to back up or share software projects. However, placing active working Git repositories directly inside synced library directories causes severe sync thrashing (1,500+ re-commit cycles/day) and lock contention on `.git/*.lock`.

To address this, the companion [git-remote-seafile](https://github.com/tkittich/git-remote-seafile) remote helper allows users to keep their working folders outside synced directories and use Seafile directly as a native Git remote over Web API v2.1 (`git clone seafile://<server>/<library>/<path>`).

Adding this context-menu action makes discovering and copying the correct Git remote URL a one-click operation directly within the desktop UI.

### Changes Made
- **`src/ui/repo-tree-view.cpp`**: Added `copyGitUrlAction` to `RepoTreeView::showContextMenu()` and implemented `RepoTreeView::onCopyGitUrl()`.
- **`src/ui/repo-tree-view.h`**: Declared `onCopyGitUrl()` slot.
- Generates URL in standard format: `seafile://<host>/<library_name>` (and subpaths for folders).
- Uses `QApplication::clipboard()->setText()`.

### Patch Reference
The ready-to-apply patch is documented in `contrib/seafile-client-context-menu.patch` in the `git-remote-seafile` repository:
https://github.com/tkittich/git-remote-seafile/blob/main/contrib/seafile-client-context-menu.patch

### Checklist
- [x] Patch prepared against seafile-client RepoTreeView context menu.
- [x] No changes to network protocol or server daemons.
- [x] Non-intrusive UI addition.

---

## Channel 3: GitHub Issue / Discussion on haiwen/seafile

- **Destination**: https://github.com/haiwen/seafile/issues
- **Title**: `Feature Proposal: Transparent Git Remote Helper for Seafile (git-remote-seafile)`

### Issue Body:

### Feature Proposal
Add official support or adoption for a native Git remote helper: **`git-remote-seafile`**.

### Motivation
A well-documented pain point for developers using Seafile is managing code repositories:
- Syncing active `.git/` trees with the desktop client causes high CPU/disk churn (1,500+ re-index cycles/day on active projects) and lock file deadlocks (`.git/index.lock`).
- Ignoring `.git/` via `seafile-ignore.txt` breaks version control on secondary machines.
- Setting up separate Git servers (GitLab/Gitea) requires extra infrastructure and maintenance.

### Solution Overview
We have developed and released **`git-remote-seafile`** (https://github.com/tkittich/git-remote-seafile), an open-source remote helper that implements the standard `git-remote-helpers(7)` protocol on top of Seafile Web API v2.1.

- **Zero Client Overhead**: Working trees live in unsynced local folders; desktop sync daemons never touch `.git/`.
- **Packfile Transport**: Commits are bundled into `.pack`/`.idx` pairs before upload; pushing 100 commits transfers only 1–2 files.
- **Git LFS Support**: Implements the official Git LFS Custom Transfer Agent protocol (`lfs-transfer`) for streaming large binaries directly to Seafile.
- **Concurrency & Safety**: Distributed lease locking (`.git-lock.json` with 60s auto-expiry) and fast-forward verification prevent race conditions.
- **Remote Compaction**: Built-in `git-remote-seafile gc` consolidates remote packfiles and prunes unreachable commits.
- **Zero-Config Auth**: Automatically discovers tokens from local Seafile client `accounts.db` (Windows, macOS, Linux, seaf-cli).
- **Quality & Testing**: 38 automated unit tests tested on Windows, with GitHub Actions CI configured for cross-platform validation (Python 3.8–3.14).

### Upstream Roadmap & Resources
- **Repository**: https://github.com/tkittich/git-remote-seafile
- **Design Spec & Upstream RFC**: https://github.com/tkittich/git-remote-seafile/blob/main/DESIGN.md
- **User's Guide**: https://github.com/tkittich/git-remote-seafile/blob/main/USER_GUIDE.md
- **Desktop UI Patch**: Ready-to-merge Qt patch adding "Copy Git Remote URL" in `contrib/seafile-client-context-menu.patch`.

We would be glad to transfer/donate this project to the `haiwen` organization (`haiwen/git-remote-seafile`) or collaborate on packaging it with official client releases.

*(Authorship note: In open-source transparency, this project was developed primarily using generative AI assistance from Google DeepMind's Antigravity / Gemini under human architectural guidance, debugging, and testing on Windows).*
