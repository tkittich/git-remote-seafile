#!/usr/bin/env python3
"""seafile_doctor.py - read-only forensics for the Seafile *sync* client.

Answers, without touching the client or the server:

  where <path>     is this path inside a Seafile-synced library?  which one?
  libs             which libraries are synced, and to which local folders
  errors           the client's own sync-error table, decoded to human names
  conflicts        *SFConflict* residue left in the working trees
  churn            how often the client re-commits / re-uploads each library
  report           all of the above

Everything here reads only:
  <ccnet>/seafile.ini        -> where seafile-data lives
  <seafile-data>/repo.db     -> libraries, worktrees, per-file sync errors
  <seafile-data>/sync_error.db
  <ccnet>/logs/seafile.log   -> sync state machine (re-commit / upload cycles)

Databases are copied to a temp dir before being opened, so a running client
holding a lock can never be disturbed.  Nothing is ever written back.

Why this exists: `git status` cannot tell you that the sync client recorded a
conflict, and the client's error table is the only place that fact lives.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------
# Sync error ids.  Authoritative source: haiwen/seafile include/seafile-error.h
# --------------------------------------------------------------------------
SYNC_ERRORS = {
    0: ("FILE_LOCKED_BY_APP", "File is locked by another application"),
    1: ("FOLDER_LOCKED_BY_APP", "Folder is locked by another application"),
    2: ("FILE_LOCKED", "File is locked by another user"),
    3: ("INVALID_PATH", "Path is invalid"),
    4: ("INDEX_ERROR", "Error when indexing (file held open / antivirus)"),
    5: ("PATH_END_SPACE_PERIOD", "Path ends with space or period character"),
    6: ("PATH_INVALID_CHARACTER", "Path contains invalid characters like '|' or ':'"),
    7: ("FOLDER_PERM_DENIED", "Update denied by folder permission setting"),
    8: ("PERM_NOT_SYNCABLE", "Syncing denied by cloud-only permission settings"),
    9: ("UPDATE_TO_READ_ONLY_REPO", "Updated a file in a non-writable library/folder"),
    10: ("ACCESS_DENIED", "Permission denied on server"),
    11: ("NO_WRITE_PERMISSION", "No write permission to the library"),
    12: ("QUOTA_FULL", "Storage quota full"),
    13: ("NETWORK", "Network error"),
    14: ("RESOLVE_PROXY", "Cannot resolve proxy address"),
    15: ("RESOLVE_HOST", "Cannot resolve server address"),
    16: ("CONNECT", "Cannot connect to server"),
    17: ("SSL", "Failed to establish secure connection"),
    18: ("TX", "Data transfer interrupted"),
    19: ("TX_TIMEOUT", "Data transfer timed out"),
    20: ("UNHANDLED_REDIRECT", "Unhandled http redirect from server"),
    21: ("SERVER", "Server error"),
    22: ("LOCAL_DATA_CORRUPT", "Internal data corrupt on the client - resync the library"),
    23: ("WRITE_LOCAL_DATA", "Failed to write data on the client (disk/permissions)"),
    24: ("SERVER_REPO_DELETED", "Library deleted on server"),
    25: ("SERVER_REPO_CORRUPT", "Library damaged on server"),
    26: ("NOT_ENOUGH_MEMORY", "Not enough memory"),
    27: ("CONFLICT", "CONFLICT: concurrent update, local copy saved as conflict file"),
    28: ("GENERAL_ERROR", "Unknown error"),
    29: ("NO_ERROR", "-"),
    30: ("REMOVE_UNCOMMITTED_FOLDER", "Folder with unuploaded files moved to recycle-bin"),
    31: ("INVALID_PATH_ON_WINDOWS", "Path has symbols unsupported by Windows"),
    32: ("LIBRARY_TOO_LARGE", "Library cannot be synced - too many files"),
    33: ("DEL_CONFIRMATION_PENDING", "REPO-LEVEL: waiting for confirmation to delete files"),
    34: ("TOO_MANY_FILES", "Library near its file-count limit; uploads refused"),
    35: ("CHECKOUT_FILE", "Failed to download file (disk space / permissions)"),
    36: ("BLOCK_MISSING", "Failed to upload file blocks (network / firewall)"),
    37: ("CASE_CONFLICT", "Path case conflict with existing file/folder"),
    38: ("STOPPED_BY_LOGOUT", "Syncing stopped by logout"),
    39: ("CORRUPTED_ENC_KEY", "Encryption key corrupted"),
    40: ("WATCH_FAILED", "Failed to monitor local folder changes"),
}


def err_name(code: int) -> str:
    return SYNC_ERRORS.get(code, ("UNKNOWN_%d" % code, "unrecognised error code"))[0]


def err_text(code: int) -> str:
    return SYNC_ERRORS.get(code, ("", "unrecognised error code"))[1]


# --------------------------------------------------------------------------
# Locating the client
# --------------------------------------------------------------------------
def ccnet_dir() -> Path:
    """The client's config dir.  ~/ccnet on Windows, ~/.ccnet elsewhere."""
    for cand in (Path.home() / "ccnet", Path.home() / ".ccnet"):
        if cand.is_dir():
            return cand
    raise SystemExit("no ccnet directory found - is the Seafile client installed?")


def seafile_data(ccnet: Path) -> Path:
    """seafile-data location is recorded in <ccnet>/seafile.ini (one line)."""
    ini = ccnet / "seafile.ini"
    if ini.is_file():
        raw = ini.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            p = Path(raw)
            if p.is_dir():
                return p
    raise SystemExit("could not resolve seafile-data (looked in %s)" % ini)


_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def open_ro(src: Path, tmpdir: Path):
    """Copy a sqlite db to tmpdir, then open the copy read-only.

    The client keeps these files open; copying first means we can never block
    it or see a torn read. Sidecar files (-wal, -shm, -journal) are copied as well
    so that WAL mode transactions are visible.
    """
    if not src.is_file():
        return None
    dst = tmpdir / src.name
    try:
        shutil.copy2(src, dst)
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = src.with_name(src.name + suffix)
            if sidecar.is_file():
                try:
                    shutil.copy2(sidecar, tmpdir / sidecar.name)
                except OSError:
                    pass
    except OSError:
        return None
    return sqlite3.connect("file:%s?mode=ro" % dst.as_posix(), uri=True)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def load_libs(ccnet: Path, tmpdir: Path):
    data = seafile_data(ccnet)
    con = open_ro(data / "repo.db", tmpdir)
    if con is None:
        return data, []
    props: dict[str, dict[str, str]] = {}
    for repo_id, key, value in con.execute("select repo_id, key, value from RepoProperty"):
        props.setdefault(repo_id, {})[key] = value
    libs = []
    for repo_id, kv in props.items():
        wt = kv.get("worktree")
        if not wt:
            continue
        libs.append(
            {
                "repo_id": repo_id,
                "worktree": Path(wt),
                # NB: the RepoProperty key "sync-worktree-name" is a boolean
                # flag ("true"), not a name - the library name is not stored
                # client-side.  Use the worktree folder name.
                "name": Path(wt).name,
                "server": kv.get("server-url", ""),
                "user": kv.get("username", ""),
            }
        )
    con.close()
    return data, libs


def cmd_libs(args, ccnet, tmpdir):
    data, libs = load_libs(ccnet, tmpdir)
    print("seafile-data : %s" % data)
    print("logs         : %s" % (ccnet / "logs"))
    print()
    if not libs:
        print("no synced libraries")
        return 0
    print("%d synced librar%s:" % (len(libs), "y" if len(libs) == 1 else "ies"))
    for lib in sorted(libs, key=lambda x: str(x["worktree"])):
        wt = lib["worktree"]
        exists = "ok " if wt.is_dir() else "MISSING"
        print("  [%s] %-12s %s" % (exists, lib["name"], wt))
        print("          repo %s  user %s" % (lib["repo_id"], lib["user"]))
    return 0


def cmd_where(args, ccnet, tmpdir):
    target = Path(args.path).resolve()
    _, libs = load_libs(ccnet, tmpdir)
    for lib in libs:
        try:
            rel = target.relative_to(lib["worktree"].resolve())
        except ValueError:
            continue
        print("INSIDE SYNCED LIBRARY")
        print("  path     : %s" % target)
        print("  library  : %s" % lib["name"])
        print("  worktree : %s" % lib["worktree"])
        print("  in-lib   : %s" % (rel.as_posix() or "."))
        print()
        print("Anything written here races the sync client. See USER_GUIDE.md,")
        print('section "The Golden Rule: Working Tree Placement".')
        return 0
    print("not inside any synced library: %s" % target)
    return 0


def cmd_errors(args, ccnet, tmpdir):
    data = seafile_data(ccnet)
    con = open_ro(data / "repo.db", tmpdir)
    if con is None:
        print("repo.db not readable")
        return 1
    cutoff = None
    if args.days:
        cutoff = dt.datetime.now().timestamp() - args.days * 86400
    rows = list(
        con.execute(
            "select repo_name, path, err_id, timestamp from FileSyncError order by timestamp"
        )
    )
    if cutoff:
        rows = [r for r in rows if (r[3] or 0) >= cutoff]
    total = list(con.execute("select count(*) from FileSyncError"))[0][0]
    print("%d sync errors in the client's table (%d shown)" % (total, len(rows)))
    if not rows:
        con.close()
        return 0
    by_code: dict[int, list] = {}
    for r in rows:
        by_code.setdefault(r[2], []).append(r)
    print()
    for code, group in sorted(by_code.items(), key=lambda kv: -len(kv[1])):
        print("%s  (%d files)  err_id=%d" % (err_name(code), len(group), code))
        print("    %s" % err_text(code))
        days: dict[str, int] = {}
        for _, _, _, ts in group:
            key = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
            days[key] = days.get(key, 0) + 1
        print("    when: %s" % ", ".join("%s x%d" % (d, n) for d, n in sorted(days.items())))
        for _, path, _, ts in group[: args.limit]:
            print("      %s  %s" % (path or "<library level>", _ts(ts)))
        if len(group) > args.limit:
            print("      ... %d more" % (len(group) - args.limit))
        print()
    con.close()
    return 0


def _ts(ts) -> str:
    if not ts:
        return "?"
    try:
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(ts)


CONFLICT_RE = re.compile(r"^(?P<base>.*) \(SFConflict (?P<who>.*?) (?P<stamp>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\)(?P<ext>\.[^.]*)?$")


def cmd_conflicts(args, ccnet, tmpdir):
    _, libs = load_libs(ccnet, tmpdir)
    total = 0
    for lib in libs:
        wt = lib["worktree"]
        if not wt.is_dir():
            continue
        hits = []
        for root, dirs, files in os.walk(wt):
            # never descend into .git: conflict copies there are noise
            if ".git" in dirs:
                dirs.remove(".git")
            for f in files:
                if "SFConflict" in f:
                    hits.append(Path(root) / f)
        if not hits:
            continue
        total += len(hits)
        print("%s  (%d conflict copies)" % (lib["name"], len(hits)))
        by_month: dict[str, int] = {}
        for h in hits:
            m = CONFLICT_RE.match(h.name)
            stamp = m.group("stamp")[:7] if m else "unparsed"
            by_month[stamp] = by_month.get(stamp, 0) + 1
        for month, n in sorted(by_month.items()):
            print("    %s  x%d" % (month, n))
        for h in hits[: args.limit]:
            print("      %s" % h.relative_to(wt).as_posix())
        if len(hits) > args.limit:
            print("      ... %d more" % (len(hits) - args.limit))
        print()
    print("total conflict copies: %d" % total)
    if total:
        print()
        print("A conflict copy keeps the original extension, so build/test tools")
        print("will pick it up.  Inspect, then delete - never blanket-rm.")
    return 0


CYCLE_RE = re.compile(r"^\[(\d\d/\d\d/\d\d) (\d\d:\d\d:\d\d)\] (.*)$")


def cmd_churn(args, ccnet, tmpdir):
    """Count full re-commit/upload cycles per day, per library.

    A cycle is 'Removing blocks for repo X' -> 'Adding remaining files'.
    Normal clients do this a handful of times a day.  Hundreds means the
    library is thrashing.
    """
    log = ccnet / "logs" / "seafile.log"
    if not log.is_file():
        print("no seafile.log at %s" % log)
        return 1
    blocks: dict[str, dict[str, int]] = {}
    uploads: dict[str, dict[str, int]] = {}
    size = log.stat().st_size
    with io.open(log, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = CYCLE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            day, _time, rest = m.group(1), m.group(2), m.group(3)
            m2 = re.match(r"Removing blocks for repo (.*?)\(([0-9a-f-]+)\)", rest)
            if m2:
                key = "%s (%s)" % (m2.group(1), m2.group(2)[:8])
                blocks.setdefault(key, {})
                blocks[key][day] = blocks[key].get(day, 0) + 1
            if rest.startswith("Transfer repo") and "'finished'" in rest:
                key = rest.split("'")[1]
                uploads.setdefault(key, {})
                uploads[key][day] = uploads[key].get(day, 0) + 1
    print("seafile.log: %.1f MB" % (size / 1e6))
    print()
    for label, table in (("re-commit cycles ('Removing blocks')", blocks),
                         ("completed upload cycles ('finished')", uploads)):
        print(label)
        for key, days in table.items():
            recent = sorted(days.items())[-args.days:]
            if not recent:
                continue
            print("  %s" % key)
            for day, n in recent:
                flag = ""
                if n >= 200:
                    flag = "   <-- THRASHING"
                elif n >= 50:
                    flag = "   <-- busy"
                print("      %s  %5d%s" % (day, n, flag))
        print()
    return 0


def cmd_identity(args, ccnet, tmpdir):
    """Machine identity + client version history.

    `seafile-data/id` is not a cache - it is the identity this machine
    registers with the server.  If it is regenerated (reinstall, or deleting
    seafile-data to "fix" something) the server sees a NEW DEVICE and
    re-syncs every library.  That is the most expensive thing a user can
    accidentally do, so it is worth watching.
    """
    data = seafile_data(ccnet)
    idfile = data / "id"
    log = ccnet / "logs" / "seafile.log"

    current = idfile.read_text(encoding="utf-8", errors="replace").strip() if idfile.is_file() else ""
    if idfile.is_file():
        mtime = dt.datetime.fromtimestamp(idfile.stat().st_mtime)
        print("identity file : %s" % idfile)
        print("current id    : %s" % current)
        print("id file mtime : %s" % mtime.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        print("identity file : %s (MISSING)" % idfile)

    if not log.is_file():
        print()
        print("no seafile.log - cannot build a history")
        return 0

    versions: list[tuple[str, str, str]] = []   # (version, first, last)
    ids: dict[str, list[str]] = {}              # id -> [first, last, n]

    def _key(d: str, t: str) -> str:
        # log dates are MM/DD/YY; render as YYYY-MM-DD
        return "20%s-%s-%s %s" % (d[6:8], d[0:2], d[3:5], t)

    with io.open(log, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = CYCLE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            when = _key(m.group(1), m.group(2))
            rest = m.group(3)
            m2 = re.match(r"starting seafile client (\S+)", rest)
            if m2:
                v = m2.group(1)
                if versions and versions[-1][0] == v:
                    versions[-1] = (v, versions[-1][1], when)
                else:
                    versions.append((v, when, when))
                continue
            m3 = re.match(r"client id = ([0-9a-f]+)", rest)
            if m3:
                cid = m3.group(1)
                rec = ids.setdefault(cid, [when, when, 0])
                rec[1] = when
                rec[2] += 1

    print()
    print("client versions seen (upgrades are cheap; identity changes are not)")
    for v, first, last in versions:
        print("  %-10s %s -> %s" % (v, first, last))

    print()
    print("machine identities seen (%d)" % len(ids))
    ordered = sorted(ids.items(), key=lambda kv: kv[1][0])
    for cid, (first, last, n) in ordered:
        mark = "  <-- current" if cid == current else ""
        print("  %s  %3d sessions  %s -> %s%s" % (cid, n, first, last, mark))

    if len(ids) > 1:
        print()
        print("WARNING: this machine has registered under %d identities. Each new" % len(ids))
        print("identity is a NEW DEVICE to the server, which re-syncs every library.")
        print("Expect conflict copies in the working trees afterwards - check with")
        print("`seafile_doctor.py errors` and `conflicts`.")
    return 0


def cmd_report(args, ccnet, tmpdir):
    rc = 0
    for name, fn in (("LIBRARIES", cmd_libs), ("MACHINE IDENTITY", cmd_identity),
                     ("SYNC ERRORS", cmd_errors),
                     ("CONFLICT COPIES", cmd_conflicts), ("CHURN", cmd_churn)):
        print("=" * 70)
        print(name)
        print("=" * 70)
        rc |= fn(args, ccnet, tmpdir) or 0
        print()
    return rc


# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("libs", help="list synced libraries")

    sub.add_parser("identity", help="machine identity + client version history")

    w = sub.add_parser("where", help="is a path inside a synced library?")
    w.add_argument("path")

    e = sub.add_parser("errors", help="client sync-error table, decoded")
    e.add_argument("--days", type=int, default=0, help="only rows newer than N days")
    e.add_argument("--limit", type=int, default=10, help="paths shown per error code")

    c = sub.add_parser("conflicts", help="find *SFConflict* residue")
    c.add_argument("--limit", type=int, default=10)

    h = sub.add_parser("churn", help="re-commit / upload cycle counts per day")
    h.add_argument("--days", type=int, default=10)

    r = sub.add_parser("report", help="everything")
    r.add_argument("--days", type=int, default=10)
    r.add_argument("--limit", type=int, default=5)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 2

    ccnet = ccnet_dir()
    with tempfile.TemporaryDirectory(prefix="seafile-doctor-") as td:
        tmpdir = Path(td)
        fn = {
            "libs": cmd_libs,
            "identity": cmd_identity,
            "where": cmd_where,
            "errors": cmd_errors,
            "conflicts": cmd_conflicts,
            "churn": cmd_churn,
            "report": cmd_report,
        }[args.cmd]
        for attr, default in (("limit", 10), ("days", 10)):
            if not hasattr(args, attr):
                setattr(args, attr, default)
        return fn(args, ccnet, tmpdir)


if __name__ == "__main__":
    sys.exit(main())
