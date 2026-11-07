

# -- Payload safeguards: provenance, preflight, markers (see PAYLOAD.md D13) --------
#
# Everything here is advisory or a refusal *before* something expensive or
# destructive starts; none of it changes what a snapshot contains.  The tool
# version goes in the commit message (a trailer), never in the tree, so a tree
# that did not change still commits nothing after an upgrade.

TOOL_TRAILER = "Git-Remote-Seafile"
#: The oldest tool that reads the payload manifests this tool writes correctly.
#: Raised only when a change to the manifest's meaning needs it, never per release.
PAYLOAD_MIN_TOOL = "0.9.0"
_VERIFIED_NAME = "verified.json"
_SEEN_NAME = "gc-seen.json"
_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


def tool_version() -> str:
    try:
        from git_remote_seafile import __version__
    except Exception:  # a copy of this file run on its own
        return "unknown"
    return str(__version__)


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    match = _VERSION_RE.match(text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def with_tool_trailer(message: str) -> str:
    """The snapshot message with the writing tool's version as a git trailer."""
    return f"{message.rstrip()}\n\n{TOOL_TRAILER}: {tool_version()}\n"


def tool_of_commit(git_dir: Path, work_tree: Path, rev: str | None) -> str | None:
    """The tool version recorded in a snapshot commit, or ``None`` (older tool)."""
    if not rev:
        return None
    proc = git(git_dir, work_tree, ["show", "-s", "--format=%B", rev], cwd=work_tree, check=False)
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        key, sep, value = line.partition(":")
        if sep and key.strip() == TOOL_TRAILER:
            return value.strip() or None
    return None


def warn_if_newer_tool(git_dir: Path, work_tree: Path, rev: str | None) -> None:
    """Say so when the snapshot this run builds on was written by a newer tool."""
    theirs, ours = tool_of_commit(git_dir, work_tree, rev), tool_version()
    a, b = _version_tuple(theirs), _version_tuple(ours)
    if a is not None and b is not None and a > b:
        print(
            f"Warning: the snapshot this run builds on was written by git-remote-seafile {theirs}; this is "
            f"{ours}. A newer tool may have changed what a vault holds -- upgrade before "
            "relying on this run.",
            file=sys.stderr,
            flush=True,
        )


def check_min_tool(manifest: dict[str, Any], where: str) -> None:
    """Refuse a manifest that says it needs a newer tool than this one."""
    needed = manifest.get("min_git_remote_seafile")
    need, have = _version_tuple(needed if isinstance(needed, str) else None), \
        _version_tuple(tool_version())
    if need is not None and have is not None and need > have:
        raise SnapshotError(
            f"{where} needs git-remote-seafile {needed} or newer (this is {tool_version()}). "
            "Upgrade it before restoring or running retention"
        )


# -- preflight: say it before hours of upload, not after -------------------------


def _account_quota(client: Any) -> tuple[int, int] | None:
    """``(usage, total)`` bytes of the signed-in account, or ``None`` if unknown/unlimited."""
    resp = client.session.get(f"{client.server_url}/api2/account/info/", timeout=client.timeout)
    if resp.status_code != 200:
        return None
    data = resp.json()
    usage, total = data.get("usage"), data.get("total")
    if isinstance(usage, bool) or isinstance(total, bool):
        return None
    if not isinstance(usage, int) or not isinstance(total, int) or total <= 0:
        return None
    return usage, total


def quota_note(client: Any, need_bytes: int, *, action: str = "uploads") -> str | None:
    """A warning when the upload would not fit the account's quota.

    *action* names what the run does with the bytes: a payload view copies
    server-side and moves nothing, but the quota counts each copy at its full
    size, so "uploads" would be the wrong word there.

    A warning, not a refusal: the quota that counts is the library owner's, which
    is not necessarily this account.
    """
    try:
        quota = _account_quota(client)
    except Exception:  # informational only
        return None
    if quota is None:
        return None
    usage, total = quota
    free = total - usage
    if need_bytes <= free:
        return None
    return (
        f"Warning: this run {action} {human_bytes(need_bytes)} but the account has "
        f"{human_bytes(max(free, 0))} of quota left ({human_bytes(usage)} of "
        f"{human_bytes(total)} used); it will probably fail partway. Free space, or "
        "ask for more, first -- already stored objects are kept and the next run resumes."
    )


def synced_payload_library_note(rid: str) -> str | None:
    """A warning when this machine's Seafile client syncs the library holding the payload."""
    try:
        import git_remote_seafile.safety as _safety
        libraries = _safety.discover_local_synced_libraries()
    except Exception:
        return None
    for lib in libraries:
        if lib.get("repo_id") == rid and lib.get("auto_sync", True):
            return (
                f"Warning: the library holding the payload is synced on this machine "
                f"({lib.get('worktree')}). The desktop client will download every payload "
                "object and fight over the vault directory. Sync a different library, turn "
                "auto sync off for this one, or add the vault's path to seafile-ignore.txt."
            )
    return None


def payload_preflight(client: Any, rid: str, need_bytes: int, *, action: str = "uploads") -> None:
    for note in (synced_payload_library_note(rid), quota_note(client, need_bytes, action=action)):
        if note:
            print(note, file=sys.stderr, flush=True)


def restore_space_problem(
    files: list[dict[str, Any]], into: Path, *, allow_low_disk: bool
) -> str | None:
    """A reason the payload would not fit where it is being restored, or ``None``."""
    need = 0
    for item in files:
        dest = into / item["path"]
        try:
            if dest.is_file() and dest.stat().st_size == item["size"]:
                continue
        except OSError:
            pass
        need += item["size"]
    try:
        free = shutil.disk_usage(into).free
    except OSError:
        return None
    if need <= free or allow_low_disk:
        return None
    return (
        f"the payload needs {human_bytes(need)} and {into} has {human_bytes(free)} free; "
        "free space or restore elsewhere (--allow-low-disk to try anyway)"
    )


# -- server-side markers ----------------------------------------------------------


def _read_json_marker(client: Any, rid: str, base: str, name: str) -> dict[str, Any]:
    try:
        text = client.get_file_text(rid, f"{base}/{name}")
        data = json.loads(text) if text else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_marker(client: Any, rid: str, base: str, name: str, data: dict[str, Any]) -> None:
    try:
        client.mkdir_p(rid, base)
        body = json.dumps(data, sort_keys=True).encode("ascii")
        client.upload_file(rid, base, name, body)
    except Exception as exc:  # a marker must never fail the run that has just succeeded
        print(f"Warning: could not write {name}: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return
    # Read the marker back (ROADMAP S3, write half): the upload's 2xx is not
    # evidence the bytes are there, and a baseline that never landed erases
    # the history-rewrite fence exactly like a torn one.  The markers are
    # written rarely (once per delete or verify run), so the read costs
    # nothing that matters.
    try:
        landed = client.get_file_text(rid, f"{base}/{name}")
        if landed is not None and landed.strip() != body.decode("ascii"):
            print(f"Warning: {name} did not read back as what was written; a later "
                  "run may re-decide from an empty or stale marker",
                  file=sys.stderr, flush=True)
    except Exception:
        pass  # the write itself succeeded; an unreachable read proves nothing


def record_verification(client: Any, rid: str, base: str, checked: int, total: int) -> None:
    _write_json_marker(client, rid, base, _VERIFIED_NAME,
                       {"at": int(time.time()), "checked": checked, "of": total})


def verification_note(client: Any, rid: str, base: str) -> str:
    data = _read_json_marker(client, rid, base, _VERIFIED_NAME)
    at = data.get("at")
    if isinstance(at, bool) or not isinstance(at, int):
        return "last --payload-verify: never (stored objects are not re-checked by a snapshot run)"
    days = max(0, int((time.time() - at) // 86400))
    return (f"last --payload-verify: {days} day(s) ago "
            f"({data.get('checked', '?')} of {data.get('of', '?')} object(s))")


def read_seen_tips(client: Any, rid: str, base: str) -> dict[str, str]:
    """The ref tips the last delete run saw, or ``{}`` for a first run.

    A marker that is *there* but unreadable or unparseable is an error, not an
    empty history: treating it as ``{}`` erased the history-rewrite fence, and
    a delete run then marked objects that rewritten snapshots still need
    (ROADMAP S3, read half).  The remedy names the file, like the reader for
    ``gc-pending.json`` beside it.  "There" is what the folder lists: a listed
    file the file host answers 404 for, or serves empty, raises too (review
    06cc623: it read as a first run, and even a report-only run rewrote it).
    """
    from git_remote_seafile.client import read_listed_text

    try:
        text = read_listed_text(client, rid, f"{base}/{_SEEN_NAME}")
    except Exception as exc:
        raise SnapshotError(
            f"could not read {base}/{_SEEN_NAME} ({type(exc).__name__}: {exc}); the "
            "history-rewrite fence cannot be checked. Retry, or remove the file (it "
            "will be re-created from the tips this run sees)"
        ) from None
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise SnapshotError(
            f"{base}/{_SEEN_NAME} is unreadable; the history-rewrite fence cannot be "
            "checked. Remove the file to start a fresh baseline -- after checking "
            "that no snapshot history was rewritten"
        ) from None
    if not isinstance(data, dict):
        raise SnapshotError(
            f"{base}/{_SEEN_NAME} is not an object; the history-rewrite fence cannot "
            "be checked. Remove the file to start a fresh baseline"
        )
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def write_seen_tips(client: Any, rid: str, base: str, tips: dict[str, str]) -> None:
    _write_json_marker(client, rid, base, _SEEN_NAME, tips)


def change_time_ns(path: Path, st: os.stat_result) -> int:
    """The inode's change time, which an in-place edit moves even when the
    modification time is put back.

    On POSIX that is ``st_ctime``.  On Windows ``st_ctime`` is the *creation*
    time (Python 3.12 deprecated it for exactly that reason): it never moves, so
    a same-size in-place write followed by ``utime`` left size, mtime, inode and
    "ctime" all unchanged and the cache vouched for the old hash (measured on
    NTFS, Python 3.14).  NTFS keeps a real change time, reachable through
    ``GetFileInformationByHandleEx(FileBasicInfo)``; it moved in the same
    experiment.  Where it cannot be read the creation time is returned, which is
    what the key held before -- a cache entry written then differs in this field
    and is hashed once more, deliberately.
    """
    if os.name != "nt":
        return st.st_ctime_ns
    try:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        # FILE_READ_ATTRIBUTES, all sharing, OPEN_EXISTING, BACKUP_SEMANTICS |
        # OPEN_REPARSE_POINT (the file itself, as lstat saw it).
        handle = kernel32.CreateFileW(
            str(extended_path(path)), 0x80, 7, None, 3, 0x02000000 | 0x00200000, None
        )
        if handle is None or handle == wintypes.HANDLE(-1).value:
            return st.st_ctime_ns
        try:
            info = _Basic()
            if not kernel32.GetFileInformationByHandleEx(
                handle, 0, ctypes.byref(info), ctypes.sizeof(info)
            ):
                return st.st_ctime_ns
            return int(info.ChangeTime) * 100
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return st.st_ctime_ns


def require_complete_history(repo: Path, *, walk: bool) -> None:
    """Refuse to decide what is unreferenced from a history that has holes.

    Retention deletes whatever no manifest in the history names, so a history
    that is missing some of its manifests makes live objects look dead.  A
    ``git fetch`` of a remote is connectivity-checked by git itself; the case it
    does not cover is a vault read *in place*, which a person may have made
    shallow (``--depth``), partial (``--filter``) or damaged (an object file
    deleted).  This tool never creates one of those -- ``cat-file --batch-check``
    cannot tell a commit with no manifest from one whose manifest blob is gone,
    so the holes are looked for directly.  A shallow or promisor repository is
    refused outright; with *walk* every reachable object is listed and a
    missing one refuses too (ROADMAP V7).
    """
    problems: list[str] = []
    if (repo / "shallow").exists():
        problems.append("it is a shallow repository")
    config = git(repo, None, ["config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$"],
                 cwd=repo, check=False).stdout
    if config.strip():
        problems.append("it is a partial clone")
    if walk and not problems:
        listing = git(repo, None, ["rev-list", "--all", "--objects", "--missing=print"],
                      cwd=repo, check=False)
        absent = sum(1 for line in listing.stdout.splitlines() if line.startswith("?"))
        if absent or listing.returncode != 0:
            problems.append(f"{absent or 'some'} object(s) it refers to are not in its object store")
    if problems:
        raise SnapshotError(
            f"the history to read is incomplete ({'; '.join(problems)}), and retention decides what is "
            "unreferenced from the whole history, so it refuses rather than guess. Use a full clone "
            "of the vault (no --depth, no --filter) or repair it with 'git fsck'"
        )


#: A retention run refuses to delete when the server's clock and this machine's differ
#: by more than this: one of them has stepped, and a step is exactly what turns "marked
#: seconds ago" into "marked a week ago" (PAYLOAD D9).  An hour, not a day: a step the
#: check cannot see shortens the grace period by as much, unseen, and two clocks kept
#: by NTP agree to well under a second (review 4f1cdd4, deepseek-v4.1-flash D1;
#: ROADMAP B3).  The offset is the client's median over the replies' ``Date``
#: headers, which have one-second resolution, so an hour is far above its noise.
_MAX_CLOCK_SKEW_SECONDS = 3600.0


def _require_whole_history(client: Any, rid: str, delete: bool) -> None:
    """Refuse a deletion by an account that may not see the whole history (ROADMAP B2).

    On Seafile Professional an ``invisible`` folder permission hides the folder from
    a non-owner's listing (measured, 13.0.29), so a branch or tag under it would
    read as gone and its objects as unused.  A report goes on (it deletes nothing);
    ``--payload-allow-non-owner`` says no folder permission applies to this account.
    """
    risk = client.hidden_history_risk(rid) if delete else None
    if isinstance(risk, str) and risk:
        from git_remote_seafile.client import owner_advice

        raise SnapshotError(
            f"{risk}; snapshots under a hidden folder would look unreferenced and their "
            "objects would be deleted. Nothing was deleted. For --payload-delete, "
            + owner_advice(risk, "--payload-allow-non-owner")
        )


def _require_server_clock(client: Any, delete: bool) -> None:
    """Refuse a deletion whose grace period would not be timed on a sane server clock.

    A server that sends no ``Date`` leaves only this machine's clock, the one thing
    the grace period must not depend on; a server whose clock is an hour or more away
    from this machine's has stepped (an NTP correction, a restored VM snapshot, a
    proxy rewriting ``Date``) or this machine has.  A report may go on either way
    (it deletes nothing); a deletion stops (REVIEW.be01e37 deepseek E1, sonnet D2).
    """
    if not delete:
        return
    if not getattr(client, "server_time_measured", True):
        raise SnapshotError(
            "the server sent no Date header, so there is no server clock to time the grace "
            "period on, and this machine's clock is exactly what it must not depend on; "
            "nothing was deleted. Run the report (without --payload-delete), or put a proxy "
            "that adds Date in front of the server"
        )
    try:
        skew = abs(float(client.get_server_time()) - time.time())
    except Exception as exc:
        # Passing here skipped the hour bound without a word (review 06cc623, opus G4.1).
        raise SnapshotError(
            f"the server's clock could not be read ({type(exc).__name__}: {exc}), so the grace "
            "period cannot be timed on it; nothing was deleted. Run again, or run the report "
            "(without --payload-delete)"
        ) from None
    if skew > _MAX_CLOCK_SKEW_SECONDS:
        raise SnapshotError(
            f"the server's clock and this machine's differ by {skew / 3600:.1f} hours; one of them "
            "has stepped, and a step is what turns an object marked seconds ago into one marked "
            "hours or days ago. Nothing was deleted. Correct the clock that is wrong and run again"
        )


def _rule_text(rule: Any, min_size: Any, ratio: Any) -> str:
    if rule == "auto":
        return "auto" + (f", ratio {float(ratio):g}" if isinstance(ratio, (int, float)) else "")
    return f"--payload-min-size {min_size}"


def _rule_differs(previous: dict[str, Any], rule: str, min_size: Any, ratio: Any) -> bool:
    """Whether this run's payload rule would place files differently from *previous*'s."""
    if previous.get("rule", "size") != rule:
        return True
    if rule != "auto":
        return previous.get("min_size") != min_size
    theirs = previous.get("ratio")
    return (isinstance(theirs, (int, float)) and not isinstance(theirs, bool)
            and isinstance(ratio, (int, float)) and abs(float(theirs) - float(ratio)) > 1e-9)
