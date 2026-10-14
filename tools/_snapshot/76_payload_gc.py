

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
#: An object, or the temporary an interrupted upload left beside it.
_COLLECTABLE_NAME = re.compile(r"^[0-9a-f]{64}(\.part-[0-9a-f]+)?$")
DEFAULT_GRACE_DAYS = 7.0
#: A grace period shorter than this is refused: it would let a retention run
#: delete what a snapshot run, started hours earlier, is about to reference.
_MIN_GRACE_DAYS = 1.0
_PENDING_NAME = "gc-pending.json"


def add_payload_gc_arguments(parser: Any) -> None:
    parser.add_argument(
        "--payload-gc",
        action="store_true",
        help="payload retention: report the stored objects that no snapshot in the remote's "
        "history (nor the local vault) references. With --payload-delete, mark them, and "
        "delete those that stayed unreferenced for --payload-grace-days. Needs --remote; "
        "--vault adds the local vault's refs",
    )
    parser.add_argument(
        "--payload-verify",
        nargs="?",
        const="20",
        default=None,
        metavar="N|all",
        help="download N randomly chosen payload objects (default 20, or 'all') of the newest "
        "snapshot of the remote and check each against its SHA-256. The server cannot report a "
        "content hash, so this is the only way to find an object that was damaged after it was "
        "stored. Needs --remote",
    )
    parser.add_argument(
        "--payload-delete",
        action="store_true",
        help="with --payload-gc: act. The first run only marks unreferenced objects; a "
        "later run deletes those still unreferenced after the grace period",
    )
    parser.add_argument(
        "--payload-keep",
        type=int,
        default=None,
        metavar="N",
        help="with --payload-gc: count only the newest N snapshots of each branch and tag "
        "as using objects. Objects only older snapshots need become unused and, with "
        "--payload-delete, are removed after the grace period -- those older snapshots can "
        "then no longer be restored. Default: keep every snapshot",
    )
    parser.add_argument(
        "--payload-no-vault",
        action="store_true",
        help="with --payload-gc --payload-delete: delete without --vault. Snapshots that "
        "exist only in a vault on some machine (not yet pushed) would then count as "
        "unreferenced, so this must be said out loud",
    )
    parser.add_argument(
        "--payload-accept-loss",
        action="store_true",
        help="with --payload-gc --payload-delete: go on although snapshots would become "
        "unrestorable -- the remote's history was rewritten since the last --payload-delete "
        "run (a branch or tag deleted, a force push), or --payload-keep expires older "
        "snapshots",
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
    if args.payload_verify is not None:
        for flag, value in (("--restore", args.restore), ("--maintain", args.maintain),
                            ("--dry-run", args.dry_run), ("--payload-gc", args.payload_gc),
                            ("--payload-min-size", args.payload_min_size),
                            ("--no-payload", args.no_payload)):
            if value:
                problems.append(f"{flag} does not apply to --payload-verify")
        if not args.remote:
            problems.append("--payload-verify requires --remote <url>")
        if args.payload_verify != "all" and not args.payload_verify.isdigit():
            problems.append("--payload-verify takes a count or 'all'")
        elif args.payload_verify != "all" and int(args.payload_verify) < 1:
            problems.append("--payload-verify takes a count of at least 1")
    if args.payload_gc:
        for flag, value in (("--restore", args.restore), ("--maintain", args.maintain),
                            ("--dry-run", args.dry_run), ("--payload-min-size", args.payload_min_size),
                            ("--no-payload", args.no_payload),
                            ("--payload-jobs", args.payload_jobs), ("--exclude", args.exclude),
                            ("--no-push", args.no_push)):
            if value:
                problems.append(f"{flag} does not apply to --payload-gc")
        if not args.remote:
            problems.append("--payload-gc requires --remote <url>")
        if args.payload_grace_days < _MIN_GRACE_DAYS:
            problems.append(f"--payload-grace-days must be at least {_MIN_GRACE_DAYS:g}")
        if args.payload_keep is not None and args.payload_keep < 1:
            problems.append("--payload-keep must be at least 1")
        if args.payload_delete and not args.vault and not args.payload_no_vault:
            problems.append(
                "--payload-delete needs --vault <the long-lived vault>: a snapshot that is "
                "only in a vault and was never pushed would look unreferenced. Pass "
                "--payload-no-vault if the remote really is the only copy"
            )
        if not args.payload_delete:
            for flag, value in (("--payload-no-vault", args.payload_no_vault),
                                ("--payload-accept-loss", args.payload_accept_loss)):
                if value:
                    problems.append(f"{flag} only applies to --payload-gc --payload-delete")
        elif args.payload_no_vault and args.vault:
            problems.append("--payload-no-vault and --vault contradict each other")
    else:
        for flag, value in (("--payload-no-vault", args.payload_no_vault),
                            ("--payload-accept-loss", args.payload_accept_loss)):
            if value:
                problems.append(f"{flag} only applies to --payload-gc")
        if args.payload_delete:
            problems.append("--payload-delete only applies to --payload-gc")
        if args.payload_keep is not None:
            problems.append("--payload-keep only applies to --payload-gc")
        if args.payload_grace_days != DEFAULT_GRACE_DAYS:
            problems.append("--payload-grace-days only applies to --payload-gc")
    return problems


class _History(NamedTuple):
    hashes: dict[str, int]       #: every referenced object and its size
    commits: int                 #: commits scanned
    manifests: int               #: distinct manifests found
    tips: dict[str, str]         #: the remote's refs now: ``refs/heads/x`` -> commit
    dropped: list[str]           #: refs seen by the last --payload-delete that lost history
    expired: int                 #: commits --payload-keep leaves out of the count


def _hashes_in_history(
    remote: str, vault: Path | None, keep: int | None = None,
    seen: dict[str, str] | None = None,
) -> _History:
    """Every payload object referenced from every ref of *remote* and, when given, of
    the local *vault* (which must be one).  *seen* is the ref tips the last
    --payload-delete run saw: any that the remote no longer contains are reported."""
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
                ["fetch", "-q", "--no-tags", str(vault),
                 "+refs/heads/*:refs/remotes/local/*", "+refs/tags/*:refs/remotes/local-tags/*"],
                cwd=Path(td),
            )
        tips: dict[str, str] = {}
        for line in git(
            repo, None, ["for-each-ref", "--format=%(objectname) %(refname)",
                         "refs/remotes/remote/", "refs/remotes/remote-tags/"], cwd=Path(td),
        ).stdout.splitlines():
            oid, _, name = line.partition(" ")
            if name.startswith("refs/remotes/remote-tags/"):
                tips["refs/tags/" + name[len("refs/remotes/remote-tags/"):]] = oid
            elif name.startswith("refs/remotes/remote/"):
                tips["refs/heads/" + name[len("refs/remotes/remote/"):]] = oid
        dropped: list[str] = []
        for ref, old in sorted((seen or {}).items()):
            now = tips.get(ref)
            if now is None:
                dropped.append(f"{ref} was deleted")
            elif now != old and git(
                repo, None, ["merge-base", "--is-ancestor", old, now], cwd=Path(td), check=False,
            ).returncode != 0:
                dropped.append(f"{ref} no longer contains {old[:12]}")
        expired = 0
        if keep is None:
            commits = [c for c in git(repo, None, ["rev-list", "--all"], cwd=Path(td)).stdout.split() if c]
        else:
            refs = git(repo, None, ["for-each-ref", "--format=%(refname)"], cwd=Path(td)).stdout.split()
            kept: set[str] = set()
            for ref in refs:
                kept.update(git(repo, None, ["rev-list", f"--max-count={keep}", ref],
                                cwd=Path(td)).stdout.split())
            commits = sorted(kept)
            everything = set(git(repo, None, ["rev-list", "--all"], cwd=Path(td)).stdout.split())
            # Only a snapshot that has a manifest can become unrestorable: one taken
            # before payload mode is whole in git and stays restorable.
            older = "".join(f"{c}:{PAYLOAD_MANIFEST}\n" for c in sorted(everything - kept))
            if older:
                listed = git(repo, None, ["cat-file", "--batch-check"], cwd=Path(td),
                             stdin=older).stdout.splitlines()
                expired = sum(1 for line in listed
                              if len(line.split()) == 3 and line.split()[1] == "blob")
        if not commits:
            return _History({}, 0, 0, tips, dropped, expired)
        specs = "".join(f"{c}:{PAYLOAD_MANIFEST}\n" for c in commits)
        checked = git(repo, None, ["cat-file", "--batch-check"], cwd=Path(td), stdin=specs).stdout
        blobs = sorted({
            line.split()[0] for line in checked.splitlines()
            if len(line.split()) == 3 and line.split()[1] == "blob"
        })
        hashes: dict[str, int] = {}
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
                hashes[item["sha256"]] = item["size"]
        return _History(hashes, len(commits), len(blobs), tips, dropped, expired)


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


def payload_verify(remote: str, vault: Path | None, *, count: str) -> int:
    """Download a sample of the newest snapshot's objects and check their SHA-256."""
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    history = _hashes_in_history(remote, vault, 1)
    sizes, manifests = history.hashes, history.manifests
    if manifests == 0:
        raise SnapshotError("found no payload manifest in the newest snapshot of the remote")
    shas = sorted(sizes)
    chosen = shas if count == "all" else sorted(random.sample(shas, min(int(count), len(shas))))
    print(f"verifying {len(chosen)} of {len(shas)} object(s) of the newest snapshot ...", flush=True)
    bad: list[str] = []
    with tempfile.TemporaryDirectory(prefix="grs-payload-verify-") as td:
        for index, sha in enumerate(chosen, 1):
            why = _download_verified(
                client, rid, f"{base}/{object_dir(sha)}/{sha}", Path(td) / sha,
                {"size": sizes[sha], "sha256": sha},
            )
            (Path(td) / sha).unlink(missing_ok=True)
            if why is not None:
                bad.append(f"{sha}: {why}")
            if index % 10 == 0 or index == len(chosen):
                print(f"checked [{index}/{len(chosen)}]", flush=True)
    if bad:
        shown = "\n  ".join(bad[:20])
        raise SnapshotError(f"{len(bad)} of {len(chosen)} sampled object(s) are damaged:\n  {shown}")
    print(f"verified {len(chosen)} object(s): every sampled object matches its SHA-256")
    record_verification(client, rid, base, len(chosen), len(shas))
    return 0


def payload_gc(
    remote: str, vault: Path | None, *, delete: bool, grace_days: float, keep: int | None = None,
    accept_loss: bool = False,
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
    seen = read_seen_tips(client, rid, base)
    history = _hashes_in_history(remote, vault, keep, seen)
    sizes_by_hash, commits, manifests = history.hashes, history.commits, history.manifests
    referenced = set(sizes_by_hash)
    if keep is not None:
        print(f"--payload-keep {keep}: only the newest {keep} snapshot(s) of each branch and "
              f"tag count as using objects; {history.expired} older snapshot(s) become "
              f"unrestorable once their objects are deleted")
    if history.dropped:
        print("WARNING: the remote's history changed since the last --payload-delete run:\n  "
              + "\n  ".join(history.dropped)
              + "\n  snapshots only reachable from there no longer count as using objects")
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
            if (entry.get("type") == "dir" or not isinstance(name, str)
                    or not _COLLECTABLE_NAME.match(name)):
                others += 1
                continue
            size = entry.get("size", 0)
            stored[name] = size if isinstance(size, int) and not isinstance(size, bool) else 0

    # A temporary is never referenced; the grace period keeps one that a run in
    # progress is still writing.
    unused = {name for name in stored if name[:64] not in referenced or len(name) > 64}
    missing = referenced - {name for name in stored if len(name) == 64}
    pending_text = _api(lambda: client.get_file_text(rid, f"{base}/{_PENDING_NAME}"), "reading gc state")
    try:
        pending_raw = json.loads(pending_text) if pending_text else {}
    except (ValueError, RecursionError):
        raise SnapshotError(f"{base}/{_PENDING_NAME} is unreadable; remove it and run again") from None
    pending = {
        sha: float(ts) for sha, ts in (pending_raw.items() if isinstance(pending_raw, dict) else [])
        if isinstance(sha, str) and _COLLECTABLE_NAME.match(sha) and isinstance(ts, (int, float))
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
    print(verification_note(client, rid, base))

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
    if (history.dropped or (keep is not None and history.expired)) and not accept_loss:
        raise SnapshotError(
            "refusing to mark or delete: "
            + ("the remote's history was rewritten since the last --payload-delete run "
               f"({len(history.dropped)} ref(s)); " if history.dropped else "")
            + (f"--payload-keep {keep} would make {history.expired} older snapshot(s) "
               "unrestorable; " if keep is not None and history.expired else "")
            + "if that is intended, run again with --payload-accept-loss. Objects that "
            "only those snapshots need would otherwise be deleted after the grace period"
        )
    deleted = 0
    for index, sha in enumerate(sorted(due), 1):
        if not client.delete_entry(rid, f"{base}/{object_dir(sha)}/{sha}"):
            raise SnapshotError(f"the server refused to delete {sha}")
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
    write_seen_tips(client, rid, base, history.tips)
    print(f"marked {marked} newly unreferenced object(s); deleted {deleted} object(s), "
          f"{total(due)}; quota is released at once, disk after the library's history "
          f"expires and the server's GC runs")
    return 0
