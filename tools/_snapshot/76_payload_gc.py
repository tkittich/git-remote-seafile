

# -- Payload retention ---------------------------------------------------------
#
# A snapshot run never deletes an object.  This is the one place that does, and
# it is built around the one way it could lose data: an object that *looks*
# unreferenced because the reader did not see every manifest that names it, or
# because a run that is about to name it has not pushed yet.
#
# So, in order of how much each guard matters:
#
# 1. **Two phases.**  A run with ``--payload-delete`` does not delete what it
#    finds unreferenced; it *marks* it, with this machine's clock, in a small
#    state file on the server.  A later run deletes an object only if it has been
#    marked unreferenced for the whole grace period *and is still unreferenced*.
#    An object a run reused in the meantime is referenced again by the time the
#    second run looks -- unless that run never pushed, in which case nothing names
#    it and it is garbage.  No clock comparison with the server is involved.
# 2. **Every manifest.**  It reads the manifest of every commit reachable from
#    every ref of the remote -- and of the local vault, whose un-pushed snapshots
#    may name objects already uploaded.  A manifest it cannot read, or whose
#    format it does not know, stops it; it never treats "cannot parse" as "names
#    nothing".
# 3. **No refs it cannot see.**  A ref the helper cannot list (a Seafile conflict
#    copy) would hide a snapshot, so their presence stops it.
# 4. **Only exact object names**, nothing else in the directory.
#
# Deleting frees quota at once; disk is freed only after the library's history
# expires and the server's GC runs (SNAPSHOT.md section 15.1).

_OBJECT_NAME = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_GRACE_DAYS = 7.0
#: A grace period shorter than this is refused: it would let a retention run
#: delete what a snapshot run, started hours earlier, is about to reference.
_MIN_GRACE_DAYS = 1.0
_PENDING_NAME = "gc-pending.json"


def add_payload_gc_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--payload-gc",
        action="store_true",
        help="payload retention: report the stored objects that no snapshot in the remote's "
        "history (nor the local vault) references. With --payload-delete, mark them, and "
        "delete those that stayed unreferenced for --payload-grace-days. Needs --remote; "
        "--vault adds the local vault's refs",
    )
    parser.add_argument(
        "--payload-delete",
        action="store_true",
        help="with --payload-gc: act. The first run only marks unreferenced objects; a "
        "later run deletes those still unreferenced after the grace period",
    )
    parser.add_argument(
        "--payload-grace-days",
        type=float,
        default=DEFAULT_GRACE_DAYS,
        metavar="DAYS",
        help=f"with --payload-gc: how long an object must stay unreferenced before it is "
        f"deleted (default {DEFAULT_GRACE_DAYS:g}, minimum {_MIN_GRACE_DAYS:g})",
    )


def payload_gc_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.payload_gc:
        for flag, value in (("--restore", args.restore), ("--maintain", args.maintain),
                            ("--dry-run", args.dry_run), ("--payload-min-size", args.payload_min_size),
                            ("--payload-jobs", args.payload_jobs), ("--exclude", args.exclude),
                            ("--no-push", args.no_push)):
            if value:
                problems.append(f"{flag} does not apply to --payload-gc")
        if not args.remote:
            problems.append("--payload-gc requires --remote <url>")
        if args.payload_grace_days < _MIN_GRACE_DAYS:
            problems.append(f"--payload-grace-days must be at least {_MIN_GRACE_DAYS:g}")
    else:
        if args.payload_delete:
            problems.append("--payload-delete only applies to --payload-gc")
        if args.payload_grace_days != DEFAULT_GRACE_DAYS:
            problems.append("--payload-grace-days only applies to --payload-gc")
    return problems


def _hashes_in_history(remote: str, vault: Path | None) -> tuple[set[str], int, int]:
    """``(hashes, commits scanned, manifests found)`` over every ref of *remote*
    and, when given, of the local *vault* (which must be one)."""
    if vault is not None and not (vault / ".git").exists():
        raise SnapshotError(
            f"--vault {vault} is not a vault (it has no .git); refusing to run without the "
            "un-pushed snapshots it was meant to protect"
        )
    with tempfile.TemporaryDirectory(prefix="grs-payload-gc-") as td:
        repo = Path(td) / "history.git"
        made = _run(
            ["git", "init", "-q", "--bare", str(repo)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if made.returncode != 0:
            raise SnapshotError(f"could not create a scratch repository: {made.stderr.strip()}")
        git(
            repo, None,
            ["fetch", "-q", "--no-tags", remote,
             "+refs/heads/*:refs/remotes/remote/*", "+refs/tags/*:refs/remotes/remote-tags/*"],
            cwd=Path(td), status=f"fetching the history of {remote}", stall_limit=None,
        )
        if vault is not None:
            git(
                repo, None,
                ["fetch", "-q", "--no-tags", str(vault), "+refs/heads/*:refs/remotes/local/*"],
                cwd=Path(td),
            )
        commits = [c for c in git(repo, None, ["rev-list", "--all"], cwd=Path(td)).stdout.split() if c]
        if not commits:
            return set(), 0, 0
        specs = "".join(f"{c}:{PAYLOAD_MANIFEST}\n" for c in commits)
        checked = git(repo, None, ["cat-file", "--batch-check"], cwd=Path(td), stdin=specs).stdout
        blobs = sorted({
            line.split()[0] for line in checked.splitlines()
            if len(line.split()) == 3 and line.split()[1] == "blob"
        })
        hashes: set[str] = set()
        for oid in blobs:
            body = git(repo, None, ["cat-file", "blob", oid], cwd=Path(td)).stdout
            try:
                data = json.loads(body)
            except (ValueError, RecursionError):
                raise SnapshotError(
                    f"a payload manifest in the history ({oid[:12]}) is unreadable; refusing "
                    "to decide what is unreferenced"
                ) from None
            for item in check_manifest_shape(data, f"the payload manifest {oid[:12]}"):
                hashes.add(item["sha256"])
        return hashes, len(commits), len(blobs)


def _refs_the_helper_cannot_see(client: Any, rid: str, repo_path: str) -> list[str]:
    """Names under ``refs/`` that are Seafile conflict copies (invisible to git)."""
    found: list[str] = []
    stack = [f"{repo_path}/refs"]
    while stack:
        directory = stack.pop()
        for entry in client.list_dir(rid, directory):
            name = entry.get("name", "")
            full = f"{directory}/{name}"
            if entry.get("type") == "dir":
                stack.append(full)
            elif "(SFConflict" in name:
                found.append(full)
    return found


def payload_gc(
    remote: str, vault: Path | None, *, delete: bool, grace_days: float
) -> int:
    """Report, and with *delete* mark and later remove, unreferenced payload objects."""
    if grace_days < _MIN_GRACE_DAYS:
        raise SnapshotError(f"the grace period must be at least {_MIN_GRACE_DAYS:g} day(s)")
    library, base, server_url = payload_location(remote)
    repo_path = base[: -len("/payload")]
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")

    conflicts = _api(lambda: _refs_the_helper_cannot_see(client, rid, repo_path), "listing refs")
    if conflicts:
        raise SnapshotError(
            f"{len(conflicts)} ref(s) are Seafile conflict copies the history reader cannot "
            f"see (first: {conflicts[0]}); a snapshot reachable only from one would look "
            "unreferenced. Resolve them (keep one ref, delete the copies) and run again"
        )
    referenced, commits, manifests = _hashes_in_history(remote, vault)
    print(f"history: {commits} commit(s), {manifests} distinct manifest(s), "
          f"{len(referenced)} referenced object(s)")

    stored: dict[str, int] = {}
    others = 0
    for prefix in (f"{n:02x}" for n in range(256)):
        entries = _api(
            lambda p=prefix: client.list_dir(rid, f"{base}/objects/{p}"),
            f"listing {base}/objects/{prefix}",
        )
        for entry in entries:
            name = entry.get("name", "")
            if entry.get("type") == "dir" or not _OBJECT_NAME.match(name):
                others += 1
                continue
            stored[name] = int(entry.get("size", 0))

    unused = {sha for sha in stored if sha not in referenced}
    missing = referenced - set(stored)
    pending_text = _api(lambda: client.get_file_text(rid, f"{base}/{_PENDING_NAME}"), "reading gc state")
    try:
        pending_raw = json.loads(pending_text) if pending_text else {}
    except (ValueError, RecursionError):
        raise SnapshotError(f"{base}/{_PENDING_NAME} is unreadable; remove it and run again") from None
    pending = {
        sha: float(ts) for sha, ts in (pending_raw.items() if isinstance(pending_raw, dict) else [])
        if isinstance(sha, str) and _OBJECT_NAME.match(sha) and isinstance(ts, (int, float))
    }
    now = time.time()
    due = {sha for sha in unused if sha in pending and now - pending[sha] >= grace_days * 86400}
    waiting = unused - due

    def total(shas: Any) -> str:
        return human_bytes(sum(stored[s] for s in shas))

    print(f"stored : {len(stored)} object(s), {total(stored)} under {base}/objects")
    print(f"in use : {len(stored) - len(unused)} object(s), {total(set(stored) - unused)}")
    print(f"unused : {len(unused)} object(s), {total(unused)}")
    print(f"  unreferenced for at least {grace_days:g} day(s) (deletable): {len(due)}, {total(due)}")
    print(f"  not yet unreferenced for that long (kept): {len(waiting)}, {total(waiting)}")
    if others:
        print(f"other  : {others} entry(ies) that are not objects (left alone)")
    if missing:
        print(f"WARNING: {len(missing)} object(s) named by a manifest are NOT on the server; "
              f"the snapshots that need them cannot be restored")

    if not delete:
        if unused:
            print("\nnothing changed (report only); --payload-delete marks unused objects now "
                  "and deletes them once they have stayed unused for the grace period")
        return 0
    if manifests == 0:
        raise SnapshotError(
            "found no payload manifest anywhere in the history, so every object would look "
            "unreferenced; refusing to mark or delete (wrong remote, or a fetch that "
            "returned nothing?)"
        )
    deleted = 0
    for index, sha in enumerate(sorted(due), 1):
        if not client.delete_entry(rid, f"{base}/{object_dir(sha)}/{sha}"):
            raise SnapshotError(f"the server refused to delete object {sha}")
        deleted += 1
        if index % 100 == 0 or index == len(due):
            print(f"deleted [{index}/{len(due)}]", flush=True)
    # The new state: everything still unused keeps its first-seen time (or gets
    # now); anything that became referenced again, or was deleted, drops out.
    fresh = {sha: pending.get(sha, now) for sha in waiting}
    marked = len(set(fresh) - set(pending))
    if fresh != pending:
        _api(lambda: client.mkdir_p(rid, base), f"creating {base}")
        _api(lambda: client.upload_file(
            rid, base, _PENDING_NAME, json.dumps(fresh, sort_keys=True).encode("ascii")
        ), "writing gc state")
    print(f"marked {marked} newly unreferenced object(s); deleted {deleted} object(s), "
          f"{total(due)}; quota is released at once, disk after the library's history "
          f"expires and the server's GC runs")
    return 0
