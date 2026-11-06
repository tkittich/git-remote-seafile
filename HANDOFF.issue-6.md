# HANDOFF: issue 6 (Seafile Pro answers 403 or 500 on the first push)

Written 2026-11-06 for whoever picks this up next. You may not be the person who worked on it, and
you have none of the previous session's memory: everything you need is here or in the files named
here, except the test servers' details, which the maintainer keeps outside the repository. Delete this file when issue 6 is closed. [AGENTS.md](AGENTS.md) still governs (gates, traps,
"the maintainer pushes and tags").

The issue: https://github.com/tkittich/git-remote-seafile/issues/6 (reporter: ByteWolfGang, Seafile
Professional). The first `git push` to a new path failed with
`Failed to get download link for /TEST/refs/heads/main: HTTP 403 {"error_msg":"You do not have
permission to access this file."}`.

## State

- **The fix is committed** (see `git log`: "client: a file read under a missing folder ...").
  `SeafileClient._file_is_under_a_missing_folder` (`git_remote_seafile/client.py`): a 403 or 500 on a
  file read asks for the listing of the file's own folder; only a clean **404** means "not there
  yet", and `get_file_bytes` / `download_file_to` then answer "absent" as they do for a 404.
  Everything else keeps the error. Gates were green before the commit. Not pushed, not tagged.
- **Issue 6 has not been answered with the findings yet.** Draft below. Do not post without the
  maintainer's yes.
- The CHANGELOG `[Unreleased]` section holds the entry; the version is not bumped (the maintainer
  decides 0.9.23 and tags).

## What was measured (Pro 13.0.29 and CE 13.0.29, same requests)

`GET /api2/repos/<id>/file/?p=<path>`, library owner and a user it is shared with, under every
sub-folder permission tried (r, rw, preview, cloud-edit, invisible, none):

| Request | CE | Pro |
| :--- | :--- | :--- |
| file missing, its folder exists | 404 | 404 |
| one folder level missing | 404 | **403** `You do not have permission to access this file.` |
| two or more levels missing | 404 | **500** HTML "Page unavailable" |
| `GET dir/?p=<missing folder>` | 404 | 404 `Folder ... not found.` (every depth, every rule) |
| `GET dir/?p=<existing folder you may not see>` | n/a | 403 `Permission denied.` |

Why: seahub's file view checks `check_folder_permission(parent_dir)` before it checks the file
exists, and that function turns any `SearpcError` into "no permission" (`views/__init__.py`).
Pro's path-based check evidently fails on a path that does not exist. The 403 body is identical to a
real permission denial, so **the file read cannot tell the two apart; the directory listing can**.
That is the whole design of the fix. Pro's RPC code is closed, so the mechanism is inferred.

**Not explained:** the reporter got 403 for `/TEST/refs/heads/main` with (as far as we know) only
`/TEST` existing, where we get 500 for that state and 403 only if `refs` also exists. Either their
Pro version answers differently or `refs` existed. Ask them for `curl -s https://<server>/api2/server-info/`
and the listing of `/TEST` (`dir/?p=/TEST`) before the first push. The fix handles both statuses.

The reporter's second error (`/TEST/refs/heads/main exists but twice answered empty`) is a **folder**
named `main` they made by hand, following the 0.9.22 message that said "create the folder". That
advice is removed and the empty-folder error now says what to look at.

## The reporter's second comment (2026-11-06)

ByteWolfGang probed their server and found what we measured: 404 when only the file is missing
(`/TEST/HEAD`, `/TEST/refs/heads/main` with `refs/heads` present), **403** for `/TEST/nope/x` (one
folder level missing). No sub-folder permissions, library owner. That settles the earlier "not
explained" note above only partly: they did not try two missing levels, where our Pro answers 500.
They got the push working with a workaround (an empty `refs/heads` holding a `placeholder.lock`
file, deleted afterwards), and suggested four fixes:

1. on a 403, check the parent folder and answer "absent" if it is missing -- **this is 0b1de22**;
2. treat an empty `refs/heads` as an empty namespace on a first push -- **declined**: the "exists
   but answered empty twice" guard protects `gc` and the fetch advertisement from a server that
   lists short, and the helper itself removes `refs/heads` when the last branch goes
   (`_prune_empty_ref_parents`), so only a hand-made folder reaches it; the message now says so;
3. create `refs/heads` before the first read -- not needed once (1) is in;
4. better hint text -- the advice to create folders is gone (CHANGELOG `[Unreleased]`, Changed).

## Draft reply for the issue (not posted)

> Thank you, your table matches what we measured on a Seafile Professional 13.0.29 test server:
> a file read under a missing folder answers 403 (one level missing) or a 500 error page (two or
> more), for the library owner and with no sub-folder permissions; Community Edition answers 404.
>
> Your first suggestion is the fix we made: after a 403 or 500 on a file read, the helper lists the
> file's folder, and only a 404 there (folder missing) counts as "not there yet". A folder that
> exists, or a listing that fails, keeps the error, so a real permission denial still stops the push.
> It is on `main` and will be in 0.9.23. The advice to create folders by hand is removed; it was
> wrong and caused your second error.
>
> We kept the "exists but twice answered empty" error for an empty `refs/heads` (your second
> suggestion): the same check stops `gc` and fetch from trusting a server that lists a folder
> short, and the helper removes `refs/heads` itself when the last branch is deleted, so only a
> folder made by hand gets there. The message now says to delete such a folder.
>
> Your remote is fine as it is after the workaround. If you have time, could you send the output of
> `curl -s https://<server>/api2/server-info/` so we know which Pro version answers this way?

## Servers

The maintainer keeps a Seafile CE and a Seafile Pro 13.0.29 test server for manual runs. Their
addresses, accounts, libraries and tokens are **not** in this repository and must not be added to
it (nor to commit messages): ask the maintainer. Everything that has to be repeatable belongs in the
throwaway CI containers (`.github/seafile-ci.yml`), which is where the to-do below moves it.

Useful API facts for setting up a test, measured on Pro 13.0.29 (all of them are now exercised by
`SharedUserTests` in `tests/test_realserver.py`, which is the place to look first):

- a **folder permission** is `POST /api2/repos/<id>/user-folder-perm/` (form `user_email`,
  `folder_path`, `permission` one of `r`, `rw`, `preview`, `cloud-edit`, `invisible`); remove with
  `DELETE` and `user_email`, `folder_path`. It is a Pro feature. (An earlier note here said these
  routes do not exist and that a sub-folder *share* is the rule: wrong. A share of a sub-folder with
  a lesser permission does not restrict a user who has the library read-write.)
- share a library with a user: `PUT /api2/repos/<id>/dir/shared_items/?p=/` (form
  `share_type=user`, `username` = the id `/api2/account/info/` gives, `permission=rw`); with a
  group: `share_type=group`, `group_id`.
- group-owned libraries need a **department** (`POST /api/v2.1/admin/address-book/groups/`,
  `group_name`, `group_owner=system admin`, `parent_group=-1`); the library is
  `POST /api/v2.1/groups/<dept>/group-owned-libraries/` with the field **`name`** (`repo_name`
  answers 400 "name invalid."); an ordinary group answers 403 "No group quota.".
- `mkdir` of a name that exists makes a numbered copy (`grs-x (1)`), even when nothing else is
  running: create each folder once.
- **Never use `git-remote-seafile login`** against a manual test server: it refuses plain HTTP, and
  it writes `~/.git-seafile.json`, which can send real remotes that name no server to the test one.

### The mistake to not repeat

Pass `SEAFILE_SERVER=<test server> SEAFILE_TOKEN=<token>` **on the command that needs them**, and
check that the first line of `git-remote-seafile doctor` names the test server *before* pushing. An
empty `SEAFILE_TOKEN` is silently ignored and the helper falls back to the desktop client's account,
which may be a real server. A previous session did exactly that once. Scratch work goes only in a
`grs-*` top-level folder of a scratch library, removed afterwards (folders that carry permission
rules are the exception: use `grs-*` sub-folders of them, never touch the folders themselves).

## Still to do, in this order

1. **Answer the issue** (draft above) when the release is ready, not before; ask for the version.
2. **More Pro cases: done (2026-11-06).**
   - *Code audit:* every file read (helper, lock tickets, refs, gc, LFS, repair, snapshot, payload)
     goes through `get_file_bytes` / `download_file_to`, the two the fix covers. Listings, `mkdir_p`
     and uploads use `dir/` only, which Pro answers 404 for a missing folder at every depth.
     `rename_entry` / `delete_entry` / `copy_entries` act only on paths just listed or uploaded, in
     folders that exist (Pro's `DELETE file/` under a missing folder is 403/500 too, never reached).
   - *Stub sweep:* the whole suite passes with `GRS_STUB_PRO=1`.
   - *Manual Pro runs*, all as expected: owner first push two levels deep, clone, second push, gc,
     lock-status; the snapshot tool's first run with payload, `--payload-verify all`, restore
     `--verify`; a shared user under each folder permission (rw and no rule work; r, preview,
     cloud-edit and invisible are refused with the server's 403, and the refusal names the folder);
     a group-shared and a group-owned library; two accounts' first push to one path at once (one
     wins, the other is an ordinary `rejected (fetch first)`, fsck clean).
   - *Found on the way and fixed:* the duplicate-folder check missed Seafile's real naming
     (`.git-lock (1).d`), see CHANGELOG; the real-server suite could send its git-driven tests to a
     different server than the one named in `SEAFILE_SERVER` (fixed, with a guard); Pro answers a
     file delete under a missing folder like a read (test made edition-aware).
3. **The CI tier: written, not yet dispatched.** `realserver.yml` is a `ce`/`pro` matrix (public
   Pro image, three-user trial, no secret); the driver creates a second account; `SharedUserTests`
   and the missing-folder test passed against a Pro 13.0.29 server by hand. What is left is a
   dispatch of the workflow (the maintainer's; first runs fail environmentally, see the workflow's
   own notes): the second-account creation (`POST /api/v2.1/admin/users/`) and Pro booting without
   Elasticsearch are the two untried steps.
4. **An outside review round first, then the release** (the maintainer's decision, 2026-11-06).
   0.9.23 is prepared in the tree (version, CHANGELOG `[0.9.23]`, release notes, UPGRADING,
   ROADMAP) but **not pushed or tagged**. `review-prompt.md` is written for this pre-release round.
   After the round: triage, fold the fixes into 0.9.23, **correct the release date** in CHANGELOG
   and the ROADMAP line if the day has changed, gates on Windows and the Debian full suite, push,
   CI green, dispatch the real-server workflow (its first CE/Pro run; enable the weekly schedule
   once both legs are green), `python tools/release_check.py`, tag, then post the issue reply.

## Where the evidence is

- The stub fault `pro_missing_parent` (`tests/e2e_harness.py`), `TestServerThatAnswersForbiddenForAMissingFolder`
  (`tests/test_e2e.py`), and the client unit tests in `tests/test_client.py` (`TestClientAPI`,
  `_file_and_dir`). Deleting the listing check turns nine of them red (checked by hand).
- `USER_GUIDE.md` troubleshooting row for the 403/500; `CHANGELOG.md` `[0.9.23]`; `DESIGN.md` §4.3 items 5 and 7.
