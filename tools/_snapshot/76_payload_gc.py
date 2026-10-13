

# -- Payload retention ---------------------------------------------------------
#
# A snapshot run never deletes an object (PAYLOAD.md R3).  This is the one place
# that does, and it is built around the one way it could lose data: an object that
# *looks* unreferenced because the reader did not see every manifest that names it.
# So it reads the manifest of every commit reachable from every ref of the remote
# -- and of the local vault, whose un-pushed snapshots may name objects already
# uploaded -- and it never touches an object younger than a grace period, because
# a run that has uploaded but not yet pushed is indistinguishable from garbage.
#
# Deleting frees quota at once; disk is freed only after the library's history
# expires and the server's own GC runs (SNAPSHOT.md section 15.1).

_OBJECT_NAME = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_GRACE_DAYS = 7.0


def add_payload_gc_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--payload-gc",
        action="store_true",
        help="payload retention: list the stored objects that no snapshot in the remote's "
        "history (nor the local vault) references, and with --delete remove those older "
        "than --grace-days. Needs --remote; --vault adds the local vault's refs",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="with --payload-gc, actually delete the unreferenced objects (default: report only)",
    )
    parser.add_argument(
        "--grace-days",
        type=float,
        default=DEFAULT_GRACE_DAYS,
        metavar="DAYS",
        help=f"with --payload-gc, never touch an object younger than DAYS (default "
        f"{DEFAULT_GRACE_DAYS:g}): a run that has uploaded but not yet pushed looks unreferenced",
    )


def payload_gc_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.payload_gc:
        for flag, value in (("--restore", args.restore), ("--maintain", args.maintain),
                            ("--dry-run", args.dry_run), ("--payload-min-size", args.payload_min_size)):
            if value:
                problems.append(f"{flag} does not apply to --payload-gc")
        if not args.remote:
            problems.append("--payload-gc requires --remote <url>")
    else:
        if args.delete:
            problems.append("--delete only applies to --payload-gc")
        if args.grace_days != DEFAULT_GRACE_DAYS:
            problems.append("--grace-days only applies to --payload-gc")
    return problems


def referenced_hashes(remote: str, vault: Path | None) -> tuple[set[str], int, int]:
    """``(hashes, commits scanned, manifests found)`` over every ref of *remote*
    (and of the local *vault*, when it exists)."""
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
        if vault is not None and (vault / ".git").exists():
            git(
                repo, None,
                ["fetch", "-q", "--no-tags", str(vault), "+refs/heads/*:refs/remotes/local/*"],
                cwd=Path(td), check=False,
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
            except ValueError:
                raise SnapshotError(
                    f"a payload manifest in the history ({oid[:12]}) is unreadable; refusing "
                    "to decide what is unreferenced"
                ) from None
            for item in data.get("files", []) if isinstance(data, dict) else []:
                sha = item.get("sha256") if isinstance(item, dict) else None
                if isinstance(sha, str) and _OBJECT_NAME.match(sha):
                    hashes.add(sha)
        return hashes, len(commits), len(blobs)


def payload_gc(
    remote: str, vault: Path | None, *, delete: bool, grace_days: float
) -> int:
    """Report (or, with *delete*, remove) unreferenced payload objects."""
    library, base, server_url = payload_location(remote)
    client = _open_client(server_url)
    rid = client.get_repo_id(library)

    referenced, commits, manifests = referenced_hashes(remote, vault)
    print(f"history: {commits} commit(s), {manifests} distinct manifest(s), "
          f"{len(referenced)} referenced object(s)")

    stored: dict[str, tuple[int, float]] = {}
    strangers = 0
    for prefix in (f"{n:02x}" for n in range(256)):
        for entry in client.list_dir(rid, f"{base}/objects/{prefix}"):
            name = entry.get("name", "")
            if entry.get("type") == "dir" or not _OBJECT_NAME.match(name):
                strangers += 1
                continue
            stored[name] = (int(entry.get("size", 0)), float(entry.get("mtime", 0) or 0))

    now = time.time()
    keep = {sha for sha in stored if sha in referenced}
    orphans = {sha: v for sha, v in stored.items() if sha not in referenced}
    old = {sha: v for sha, v in orphans.items() if v[1] and now - v[1] >= grace_days * 86400}
    young = {sha: v for sha, v in orphans.items() if sha not in old}
    missing = referenced - set(stored)

    def total(items: Any) -> str:
        return human_bytes(sum(v[0] for v in items))

    print(f"stored : {len(stored)} object(s), {total(stored.values())} under {base}/objects")
    print(f"in use : {len(keep)} object(s), {total(stored[s] for s in keep)}")
    print(f"unused : {len(orphans)} object(s), {total(orphans.values())}")
    print(f"  older than {grace_days:g} day(s) (deletable): {len(old)}, {total(old.values())}")
    print(f"  within the grace period (kept): {len(young)}, {total(young.values())}")
    if strangers:
        print(f"other  : {strangers} entry(ies) that are not objects (left alone)")
    if missing:
        print(f"WARNING: {len(missing)} object(s) named by a manifest are NOT on the server; "
              f"the snapshots that need them cannot be restored")

    if not delete:
        if old:
            print("\nnothing deleted (report only); add --delete to remove the deletable objects")
        return 0
    if manifests == 0:
        raise SnapshotError(
            "found no payload manifest anywhere in the history, so every object would look "
            "unreferenced; refusing to delete (wrong remote, or a fetch that returned nothing?)"
        )
    deleted = 0
    for index, sha in enumerate(sorted(old), 1):
        if not client.delete_entry(rid, f"{base}/{object_dir(sha)}/{sha}"):
            raise SnapshotError(f"the server refused to delete object {sha}")
        deleted += 1
        if index % 100 == 0 or index == len(old):
            print(f"deleted [{index}/{len(old)}]", flush=True)
    print(f"deleted {deleted} object(s), {total(old.values())}; quota is released at once, disk "
          f"after the library's history expires and the server's GC runs")
    return 0
