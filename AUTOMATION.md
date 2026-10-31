# Automation, Schedulers and AI Agents

One page for anything that runs these tools without a human reading every line
first: cron, systemd timers, Task Scheduler, CI, backup scripts, and AI agents
acting for a user. Nothing here is new behaviour — every claim below is a
contract the tools already keep and their tests already enforce; this page only
assembles it in one place. For recovering after something breaks, the manual
for humans is [RUNBOOK.md](RUNBOOK.md); its §0 is three diagnostics that need
no preparation.

## The output contract (both tools)

stdout carries the command's result or report. stderr carries usage errors,
failures, and warnings. Exit codes:

| exit | meaning |
|:---:|:---|
| `0` | Done, or nothing to do. A `Warning:` may still have been printed. |
| `1` | The command ran and the answer is "no", or it failed. |
| `2` | The command line was wrong. |
| `3` | Only `git seafile-snapshot` with `--exit-warnings`: the run completed but printed a `Warning:`. Off by default, so a job that works today does not start failing tomorrow. |

Every message opens with a level word, and the level is the instruction:

| opens with | means | what automation should do |
|:---|:---|:---|
| `Error:` | fatal; the run stopped | capture the message verbatim and act on §[When something fails](#when-something-fails) below |
| `Warning:` | needs attention | surface it to the human; with `--exit-warnings` it is already exit `3` |
| `Notice:` | a default changed what this run does | log it |
| `Note:` | for information | log it |
| `Hint:` | advice that changes nothing | safe to drop |

Report commands print a greppable verdict and exit `1` when it is negative:
`doctor`'s `[FAIL]`, `check-safety`'s `[BLOCKED ...]`, `lock-status`'s `LOCKED`
(a lease whose ticket has expired reports `STALE` instead). These strings are
part of the tested contract; matching on them is stable.

## The snapshot run document: `--json`

With `--json`, `git seafile-snapshot` prints exactly one JSON line on stdout
and moves the human-readable text to stderr, so a driver can log one and show
the other. A wrong command line exits `2` before any document is written, and
an interrupted run still writes its document (status `failed`) rather than
dying with no output. The fields:

| field | |
|:---|:---|
| `schema` | `1` |
| `tool`, `version` | `git-seafile-snapshot` and the version a support question should quote |
| `mode` | `snapshot`, `status`, `restore`, `restore-drill`, `maintain`, `payload-verify`, `payload-view`, `payload-gc` |
| `status` | `ok`, `warnings`, or `failed` |
| `exit_code` | the process's exit status |
| `seconds` | wall time |
| `warnings` | every `Warning:` line the run printed |
| `error` | the last `Error:` line, or `null` |
| `result` | facts of the mode (below); keys are only ever added to, never removed |

A real `--status --json` document (from a local vault that had recorded
change records but never pushed):

```json
{"error": null, "exit_code": 0, "mode": "status",
 "result": {"status": {"changed": 0, "comparable": true, "deleted": 0,
   "last_snapshot": "2026-10-31T22:04:50+07:00", "new": 0, "pushed": null,
   "tip": "757f42b37a79dcfad7f509043f5a3003e1a98cfc", "uncompared": 0,
   "unpushed": null, "unreadable": 0}},
 "schema": 1, "seconds": 0.3, "status": "ok",
 "tool": "git-seafile-snapshot", "version": "0.9.20", "warnings": []}
```

Recipes:

```bash
git seafile-snapshot --json --exit-warnings | jq -e '.status == "ok"'
git seafile-snapshot --status --json      | jq -e '.result.status.pushed == true'
```

The `pushed` field is **three-valued**, and the distinction is the point:

| value | the report said | meaning |
|:---:|:---|:---|
| `true` | `pushed : yes` | the newest snapshot is the one this vault last pushed |
| `false` | `pushed : NO -- N snapshot(s) were taken after the last push` | a `--no-push` run, or a push that failed; `unpushed` carries the count |
| `null` | `pushed : not recorded` | this vault has not pushed since it began keeping the record, or nothing could be compared |

`null` never equals `true` in `jq`, so the recipe above alerts rather than
reading "no record" as "all clear" — the same reason every `result.status`
key is present with value `null` when the comparison is impossible, instead
of absent. For a vault whose schedule deliberately never pushes, check the
`changed`/`new` counts and the `last_snapshot` time instead.

One expectation to set for a **first** scheduled run: on a tree with
credential-shaped files (a `.env`, a key) the run prints one `Warning:` naming
them, so with `--exit-warnings` the first run exits `3`. The vault remembers
that it warned; later runs exit `0` unless the set of such files changes.
Treat a single exit `3` on a first run as "show the human the warnings once",
not as a broken job.

## An unattended run never asks a question

The first-run question about reproducible directories (`node_modules`,
virtualenvs, `dist` — with sizes) is asked only at a terminal; when stdin or
stdout is not a tty, or the run is `--json`, `--status` or `--maintain`, it is
skipped and the run captures **everything**, then says what it noticed. An
agent therefore cannot hang on a prompt, and also cannot silently inherit the
exclude list: exclusions are set by the human, on the command line or in the
vault's remembered settings.

## What an agent may run without asking

| Run freely — read-only | Run on a schedule — mutating, additive | Ask the human first |
|:---|:---|:---|
| `--status`, `--dry-run`, `--payload-verify`, `--restore-drill`, `--payload-view`, `git-remote-seafile doctor`, `check-safety`, `lock-status`, `check-auth`, `config` | a snapshot (push included); `git-remote-seafile gc`; `set-head`; `--restore --into` a **new, empty** directory | `--payload-gc --payload-delete` and any `--payload-keep` change; `unlock --force`; `--reset-to-remote`; any `--allow-*` or `--payload-accept-loss` bypass; `doctor --create-library`; restoring anywhere a human's files might already be |

The right column is not politeness, it is the shape of the commands: payload
retention deletes server-side objects, `--force` breaks a lock another machine
may hold, and the `--allow-*` flags exist precisely to switch off a guard.
Two guards make the middle column safe to automate: `--restore` refuses a
non-empty destination ("restore destination is not empty"), so an agent cannot
overwrite an existing tree by accident, and `--payload-gc` without
`--payload-delete` only reports what retention would remove.

## Credentials without a terminal

Set `SEAFILE_SERVER` and `SEAFILE_TOKEN` in the environment, or write
`~/.git-seafile.json` (see [USER_GUIDE.md §6](USER_GUIDE.md#6-authentication--configuration)).
Verify non-interactively with `git-remote-seafile check-auth` (whoami on the
server) and `git-remote-seafile doctor <url>` (can authenticate; would this
push be allowed). Never put the token in a command line, a log, or output an
agent echoes — it is a password.

## When something fails

Every `Error:` message names the flag or the action that fixes the problem —
that is a tested convention, so a message that names no way forward is a bug
worth reporting. On failure, capture: the exact message, the exit code, the
output of `git-remote-seafile version`, and, for a snapshot run, the `--json`
document. With those four in hand, [RUNBOOK.md](RUNBOOK.md) §0's three
diagnostics cover the common failures; each of its sections is a recipe keyed
by the error you saw.
