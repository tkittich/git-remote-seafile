

# -- Payload mode --------------------------------------------------------------
#
# Large immutable files do not go through git at all.  Each one is stream-hashed
# (SHA-256) and uploaded to Seafile as an ordinary file at its real relative path,
# and the vault commit carries a *manifest* -- path, mode, size, SHA-256, mtime --
# so git keeps what git is good at (the file list, the checksums, the history of
# both) and Seafile keeps what Seafile is good at (big files, shared blocks).
#
# Layout, beside the vault's own remote directory:
#
#     <path>.payload/current/<relative path>        working mirror, updated in place
#     <path>.payload/snapshots/<id>/<relative ...>  one server-side copy of current/
#
# A snapshot is a single copy operation (metadata only: the copy shares blocks),
# so it is browsable, consistent, and costs nothing on disk beyond what changed.
# Design, measurements and open items: SNAPSHOT.md section 15.1.

#: Where the manifest lives inside the snapshot tree.
PAYLOAD_MANIFEST = ".seafile-payload/manifest.json"
PAYLOAD_FORMAT = 1
_PAYLOAD_HASH_BLOCK = 8 * 1024 * 1024
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}


def parse_size(text: str) -> int:
    """``"64M"``, ``"1g"``, ``"500k"`` or ``"1048576"`` as a byte count."""
    raw = text.strip().lower().rstrip("b").replace("i", "")
    digits = raw.rstrip("kmgt")
    unit = raw[len(digits):]
    try:
        value = float(digits)
        return int(value * _SIZE_UNITS[unit])
    except (ValueError, KeyError):
        raise SnapshotError(
            f"not a size: {text!r} (examples: 64M, 1G, 500k, 1048576)"
        ) from None


#: What this server was measured to do to names (CE 13.0.28): a name is stored
#: NFC-normalised, a double quote becomes ``%22``, and a name longer than 255
#: *bytes* of UTF-8 is refused.  Everything else tried -- ``: * ? < > |``, a
#: trailing space or dot, reserved Windows names, emoji, Thai, a path of 4,200
#: characters -- was stored as given.
_MAX_NAME_BYTES = 255


def remote_rel(rel: str) -> str:
    """The path the server will hold for source path *rel*."""
    return "/".join(
        unicodedata.normalize("NFC", part).replace('"', "%22") for part in rel.split("/")
    )


def check_payload_names(paths: list[str]) -> None:
    """Refuse, before anything is uploaded, names the server cannot hold or would merge."""
    too_long = [
        p for p in paths
        if any(len(part.encode("utf-8")) > _MAX_NAME_BYTES for part in remote_rel(p).split("/"))
    ]
    if too_long:
        raise SnapshotError(
            f"{len(too_long)} payload path(s) have a name longer than {_MAX_NAME_BYTES} bytes "
            f"(UTF-8), which the server refuses; first: {too_long[0]}"
        )
    seen: dict[str, str] = {}
    for p in paths:
        other = seen.setdefault(remote_rel(p), p)
        if other != p:
            raise SnapshotError(
                f"payload paths {other!r} and {p!r} become the same name on the server "
                "(Unicode normalisation or quote escaping); rename one or exclude it"
            )


def payload_location(remote: str) -> tuple[str, str, str | None]:
    """``(library, payload_dir, server_url)`` for a vault remote URL.

    The payload directory is a *sibling* of the vault's own directory, so the
    helper's listings of the vault never see it.
    """
    from git_remote_seafile.url import parse_seafile_url

    try:
        parsed = parse_seafile_url(remote)
    except ValueError as exc:
        raise SnapshotError(f"payload mode needs a seafile:// remote: {exc}") from None
    return parsed.library_name, parsed.repo_path.rstrip("/") + ".payload", parsed.server_url


def _open_client(server_url: str | None) -> Any:
    try:
        from git_remote_seafile.client import SeafileClient
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SnapshotError(f"payload mode needs the git_remote_seafile package: {exc}") from None
    return SeafileClient(server_url=server_url)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_PAYLOAD_HASH_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def find_payload_files(
    source: Path, excludes: list[str], min_size: int
) -> dict[str, tuple[int, int, str]]:
    """``{relative path: (size, mtime_ns, mode)}`` for every payload file.

    A payload file is a *regular* file of at least ``min_size`` bytes that no
    ``--exclude`` removes.  Symlinks stay with git (they are a few bytes and git
    stores them exactly); so does everything smaller.
    """
    found: dict[str, tuple[int, int, str]] = {}
    for size, rel in _walk_sizes(source, excludes):
        if size < min_size:
            continue
        try:
            st = os.lstat(source / rel)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        found[rel] = (st.st_size, st.st_mtime_ns, mode)
    return found


def load_manifest(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, Any] | None:
    """The manifest recorded in snapshot ``rev``, or ``None`` when it has none."""
    if not rev:
        return None
    proc = git(
        git_dir, work_tree, ["show", f"{rev}:{PAYLOAD_MANIFEST}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def build_manifest(
    candidates: dict[str, tuple[int, int, str]],
    source: Path,
    previous: dict[str, Any] | None,
    min_size: int,
    *,
    quiet: bool = False,
) -> dict[str, Any]:
    """Hash every payload file (reusing the previous run's hash when size and
    mtime are unchanged) and return the manifest, snapshot id included."""
    known: dict[str, dict[str, Any]] = {}
    for entry in (previous or {}).get("files", []):
        if isinstance(entry, dict) and "path" in entry:
            known[entry["path"]] = entry
    entries: list[dict[str, Any]] = []
    hashed = reused = 0
    total = len(candidates)
    for index, rel in enumerate(sorted(candidates), 1):
        size, mtime_ns, mode = candidates[rel]
        old = known.get(rel)
        if old and old.get("size") == size and old.get("mtime_ns") == mtime_ns and old.get("sha256"):
            digest = old["sha256"]
            reused += 1
        else:
            if not quiet:
                print(f"hashing [{index}/{total}] {rel} ({human_bytes(size)})", flush=True)
            digest = _sha256_file(source / rel)
            hashed += 1
        record: dict[str, Any] = {
            "path": rel, "size": size, "mode": mode, "sha256": digest, "mtime_ns": mtime_ns,
        }
        if remote_rel(rel) != rel:
            record["remote"] = remote_rel(rel)  # what the server actually holds
        entries.append(record)
    ident = hashlib.sha256()
    for item in entries:
        ident.update(f"{item['path']}\0{item['size']}\0{item['mode']}\0{item['sha256']}\n".encode())
    return {
        "format": PAYLOAD_FORMAT,
        "snapshot": "p-" + ident.hexdigest()[:16],
        "min_size": min_size,
        "files": entries,
    }


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    # One line, ASCII, no trailing newline: nothing between here and the object
    # store (text-mode pipes, filters) has a newline to translate.
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("ascii")


def add_manifest_to_index(
    git_dir: Path, work_tree: Path, manifest: dict[str, Any]
) -> None:
    """Record the manifest in the vault index (and so in the snapshot tree)."""
    blob = git(
        git_dir, work_tree, ["hash-object", "-w", "--no-filters", "--stdin"],
        cwd=work_tree, stdin=manifest_bytes(manifest).decode("ascii"),
    ).stdout.strip()
    git(
        git_dir, work_tree,
        ["update-index", "--add", "--cacheinfo", f"100644,{blob},{PAYLOAD_MANIFEST}"],
        cwd=work_tree,
    )


def _remote_index(client: Any, rid: str, directory: str) -> dict[str, int]:
    """``{relative path: size}`` of every file under *directory* on the server."""
    found: dict[str, int] = {}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        for entry in client.list_dir(rid, directory + ("/" + rel_dir if rel_dir else "")):
            name = entry.get("name")
            if not name:
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if entry.get("type") == "dir":
                stack.append(rel)
            else:
                found[rel] = int(entry.get("size", -1))
    return found


def _upload_all(
    client: Any, rid: str, server_url: str | None, current: str, source: Path,
    todo: list[str], remote_of: dict[str, str], sizes: dict[str, int], jobs: int,
) -> None:
    """Upload *todo* into ``current/`` with *jobs* concurrent workers, largest first.

    Measured on the real server: one upload runs at ~32 MiB/s and three at once
    reach ~53 MiB/s in aggregate, so a small pool helps and a large one does
    not.  Directories are created up front in this thread; each worker thread
    then uses a client of its own (a ``requests`` session is not meant to be
    shared).  The first failure stops the queue and is raised once the running
    uploads have finished -- the next run resumes from what landed.
    """
    for parent in sorted({remote_of[rel].rpartition("/")[0] for rel in todo}):
        client.mkdir_p(rid, current + ("/" + parent if parent else ""))
    order = sorted(todo, key=lambda rel: (-sizes[rel], rel))
    local = threading.local()
    lock = threading.Lock()
    counter = [0]
    failed: list[str] = []

    def one(rel: str) -> None:
        if failed:
            return
        worker = getattr(local, "client", None)
        if worker is None:
            worker = local.client = _open_client(server_url)
        with lock:
            counter[0] += 1
            print(
                f"uploading [{counter[0]}/{len(order)}] {rel} ({human_bytes(sizes[rel])})",
                flush=True,
            )
        parent, _, name = remote_of[rel].rpartition("/")
        try:
            ok = worker.upload_file(
                rid, current + ("/" + parent if parent else ""), name, source / rel
            )
        except Exception as exc:  # reported once, below
            failed.append(f"{rel}: {exc}")
            return
        if not ok:
            failed.append(f"{rel}: the server did not accept the upload")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(one, order))
    if failed:
        raise SnapshotError(
            f"{len(failed)} payload upload(s) failed (the next run resumes); first: {failed[0]}"
        )


def publish_payload(
    remote: str,
    source: Path,
    manifest: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    dry_run: bool,
    jobs: int = 3,
) -> tuple[int, int]:
    """Make the server hold exactly this payload, then snapshot it.

    Uploads what is missing or differs in size into ``current/``, removes what
    the manifest no longer lists, verifies the listing, and makes one
    server-side copy of ``current/`` as ``snapshots/<id>/`` unless that
    snapshot already exists.  Returns ``(files_to_upload, bytes_to_upload)``.

    ``dry_run`` reads the server and changes nothing.
    """
    library, base, server_url = payload_location(remote)
    client = _open_client(server_url)
    rid = client.get_repo_id(library)
    current = f"{base}/current"
    snap_id = manifest["snapshot"]
    wanted = {item["path"]: item for item in manifest["files"]}
    check_payload_names(list(wanted))
    remote_of = {rel: item.get("remote", rel) for rel, item in wanted.items()}
    held = set(remote_of.values())

    # The listing can only prove a file is *there at the right size*.  A file
    # edited in place to the same length would pass that test while holding the
    # old bytes, so a path whose hash differs from the last committed manifest
    # is uploaded regardless.
    committed = {
        entry.get("path"): entry.get("sha256")
        for entry in (previous or {}).get("files", [])
        if isinstance(entry, dict)
    }
    remote_files = _remote_index(client, rid, current)
    todo = [
        rel for rel, item in wanted.items()
        if remote_files.get(remote_of[rel]) != item["size"] or committed.get(rel) != item["sha256"]
    ]
    stale = [rel for rel in remote_files if rel not in held]
    todo_bytes = sum(wanted[rel]["size"] for rel in todo)
    if dry_run:
        return len(todo), todo_bytes

    if todo:
        _upload_all(
            client, rid, server_url, current, source, todo, remote_of,
            {rel: wanted[rel]["size"] for rel in todo}, jobs,
        )
    for rel in stale:
        client.delete_entry(rid, f"{current}/{rel}")
    if todo or stale:
        after = _remote_index(client, rid, current)
        wrong = [
            rel for rel, item in wanted.items() if after.get(remote_of[rel]) != item["size"]
        ]
        if wrong:
            raise SnapshotError(
                f"payload upload did not land: {len(wrong)} file(s) missing or the wrong "
                f"size on the server, first: {wrong[0]}"
            )

    snapshots = f"{base}/snapshots"
    existing = {e.get("name") for e in client.list_dir(rid, snapshots)}
    if snap_id not in existing and wanted:
        client.mkdir_p(rid, f"{snapshots}/{snap_id}")
        names = [e["name"] for e in client.list_dir(rid, current) if e.get("name")]
        if not client.copy_entries(rid, current, names, f"{snapshots}/{snap_id}"):
            raise SnapshotError(
                f"the server refused to copy {current} to {snapshots}/{snap_id}"
            )
        listed = _remote_index(client, rid, f"{snapshots}/{snap_id}")
        if len(listed) != len(wanted):
            raise SnapshotError(
                f"snapshot {snap_id} holds {len(listed)} file(s) but the manifest lists "
                f"{len(wanted)}"
            )
    return len(todo), todo_bytes


def restore_payload(remote: str, into: Path) -> None:
    """Download and verify the payload a restored snapshot's manifest names."""
    manifest_path = into / PAYLOAD_MANIFEST
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except ValueError as exc:
        raise SnapshotError(f"the snapshot's payload manifest is unreadable: {exc}") from None
    files = manifest.get("files", [])
    library, base, server_url = payload_location(remote)
    client = _open_client(server_url)
    rid = client.get_repo_id(library)
    snap_dir = f"{base}/snapshots/{manifest['snapshot']}"
    print(f"restoring {len(files)} payload file(s) from {snap_dir} ...", flush=True)
    problems: list[str] = []
    for index, item in enumerate(files, 1):
        rel = item["path"]
        dest = into / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading [{index}/{len(files)}] {rel} ({human_bytes(item['size'])})", flush=True)
        if not client.download_file_to(rid, f"{snap_dir}/{item.get('remote', rel)}", dest):
            problems.append(f"{rel}: not found in {snap_dir}")
            continue
        if dest.stat().st_size != item["size"]:
            problems.append(f"{rel}: size {dest.stat().st_size}, manifest says {item['size']}")
            continue
        if _sha256_file(dest) != item["sha256"]:
            problems.append(f"{rel}: SHA-256 does not match the manifest")
            continue
        if item.get("mode") == "100755" and os.name != "nt":
            dest.chmod(dest.stat().st_mode | 0o111)
        try:
            os.utime(dest, ns=(item["mtime_ns"], item["mtime_ns"]))
        except OSError:
            pass
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise SnapshotError(
            f"{len(problems)} payload file(s) failed verification:\n  {shown}{more}"
        )
    print(f"payload verified: {len(files)} file(s), SHA-256 matches the manifest")


def add_payload_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--payload-min-size",
        metavar="SIZE",
        default=None,
        help="payload mode: files of at least SIZE (e.g. 64M, 1G) bypass git; they are "
        "hashed (SHA-256) and uploaded to Seafile as plain files beside the vault, and the "
        "snapshot records a manifest instead. Needs a remote; a restore downloads and "
        "verifies them. 0 turns it off. A vault that has used it needs the flag on every "
        "run (SNAPSHOT.md section 15.1)",
    )


def add_payload_jobs_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--payload-jobs",
        type=int,
        default=3,
        metavar="N",
        help="payload mode: upload N files at once (default 3; measured on one server: "
        "1 upload ~32 MiB/s, 3 at once ~53 MiB/s, so more rarely helps)",
    )


def payload_conflicts(args: argparse.Namespace) -> list[str]:
    if args.payload_min_size is not None and args.no_push:
        return [
            "--payload-min-size cannot be combined with --no-push: payload files exist "
            "only on the server, so a snapshot that is not pushed would reference files "
            "that were never uploaded"
        ]
    return []


def plan_payload(
    value: str | None, remote: str | None, source: Path, excludes: list[str]
) -> tuple[int | None, dict[str, tuple[int, int, str]]]:
    """``(min_size, candidates)`` for this run; ``(None, {})`` when payload mode is off."""
    min_size = parse_size(value) if value is not None else None
    if not min_size:
        return min_size, {}
    if not remote:
        raise SnapshotError(
            "payload mode needs a remote to upload to: pass --remote "
            "seafile://<library>/<path> (or run from a source with a seafile:// remote)"
        )
    return min_size, find_payload_files(source, excludes, min_size)


def run_payload_step(
    git_dir: Path,
    source: Path,
    base: str | None,
    remote: str | None,
    min_size: int | None,
    candidates: dict[str, tuple[int, int, str]],
    *,
    dry_run: bool,
    jobs: int = 3,
) -> tuple[dict[str, Any] | None, tuple[int, int]]:
    """Hash, upload and snapshot the payload; ``(manifest, (files, bytes) to upload)``."""
    previous = load_manifest(git_dir, source, base)
    if previous is not None and min_size is None:
        raise SnapshotError(
            "this vault's last snapshot used payload mode, so its large files are not "
            "in git; running without --payload-min-size would stage them into git "
            f"instead. Repeat the flag (--payload-min-size {previous.get('min_size', 'SIZE')}), "
            "or pass --payload-min-size 0 to leave payload mode deliberately"
        )
    if not min_size:
        return None, (0, 0)
    manifest = build_manifest(candidates, source, previous, min_size)
    todo = publish_payload(
        remote or "", source, manifest, previous, dry_run=dry_run, jobs=jobs
    )
    return manifest, todo


def payload_summary(manifest: dict[str, Any], todo: tuple[int, int], dry_run: bool) -> str:
    total = sum(item["size"] for item in manifest["files"])
    verb = "to upload" if dry_run else "uploaded"
    return (
        f"payload: {len(manifest['files'])} file(s) ({human_bytes(total)}) bypass git; "
        f"snapshot {manifest['snapshot']}; {verb} {todo[0]} file(s) ({human_bytes(todo[1])})"
    )
