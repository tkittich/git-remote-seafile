

# -- Payload mode --------------------------------------------------------------
#
# Large immutable files do not go through git at all.  Each one is stream-hashed
# (SHA-256) and stored on the server *by its hash*:
#
#     <path>/payload/objects/<first two hex>/<sha256>
#
# inside the vault's own remote directory (the helper ignores what it does not
# know; measured against its push, clone, doctor, gc --tail and gc).  The vault
# commit carries a *manifest* -- path, mode, size, SHA-256, mtime -- so git keeps
# what git is good at (the file list, the checksums, the history of both) and
# Seafile keeps what Seafile is good at (big files, shared blocks).
#
# Why by hash: an object is never overwritten, so a snapshot is just a manifest
# and costs nothing to keep; a rename or a duplicate uploads nothing; concurrent
# writers cannot conflict (the same name always has the same content); and what
# the server holds depends on no file name, so the server's name rules (NFC,
# ``"`` -> ``%22``, 255 bytes) never apply.  Design, measurements and open items:
# SNAPSHOT.md section 15.1.

#: Where the manifest lives inside the snapshot tree.
PAYLOAD_MANIFEST = ".seafile-payload/manifest.json"
PAYLOAD_FORMAT = 2
_PAYLOAD_HASH_BLOCK = 8 * 1024 * 1024
#: An mtime this close to the previous manifest's creation cannot vouch for a
#: cached hash: a file edited to the same size within the filesystem's timestamp
#: granularity would look untouched (git calls this "racily clean").
_RACY_WINDOW_NS = 2_000_000_000
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


def payload_location(remote: str) -> tuple[str, str, str | None]:
    """``(library, payload_dir, server_url)`` for a vault remote URL."""
    from git_remote_seafile.url import parse_seafile_url

    try:
        parsed = parse_seafile_url(remote)
    except ValueError as exc:
        raise SnapshotError(f"payload mode needs a seafile:// remote: {exc}") from None
    return parsed.library_name, parsed.repo_path.rstrip("/") + "/payload", parsed.server_url


def object_dir(sha: str) -> str:
    return f"objects/{sha[:2]}"


def _open_client(server_url: str | None) -> Any:
    try:
        from git_remote_seafile.client import SeafileClient
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SnapshotError(f"payload mode needs the git_remote_seafile package: {exc}") from None
    return SeafileClient(server_url=server_url)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(_PAYLOAD_HASH_BLOCK)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        # A file another process holds open exclusively (a database, a GIS
        # project on Windows) cannot be read; a backup that skipped it quietly
        # would be worse than one that stops and says which.
        raise SnapshotError(f"cannot read {path}: {exc}") from None
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
    reference_ns: int = 0,
    quiet: bool = False,
) -> dict[str, Any]:
    """Hash every payload file and return the manifest, snapshot id included.

    The previous run's hash is reused when size and mtime are unchanged *and*
    the mtime is not within the racy window of ``reference_ns`` (the time the
    previous snapshot was committed, in nanoseconds).
    """
    known: dict[str, dict[str, Any]] = {}
    for entry in (previous or {}).get("files", []):
        if isinstance(entry, dict) and "path" in entry:
            known[entry["path"]] = entry
    entries: list[dict[str, Any]] = []
    total = len(candidates)
    for index, rel in enumerate(sorted(candidates), 1):
        size, mtime_ns, mode = candidates[rel]
        old = known.get(rel)
        trusted = (
            old is not None
            and old.get("size") == size
            and old.get("mtime_ns") == mtime_ns
            and old.get("sha256")
            and mtime_ns < reference_ns - _RACY_WINDOW_NS
        )
        if trusted and old is not None:
            digest = old["sha256"]
        else:
            if not quiet:
                print(f"hashing [{index}/{total}] {rel} ({human_bytes(size)})", flush=True)
            digest = _sha256_file(source / rel)
        entries.append(
            {"path": rel, "size": size, "mode": mode, "sha256": digest, "mtime_ns": mtime_ns}
        )
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


def _remote_objects(client: Any, rid: str, base: str, shas: set[str]) -> dict[str, int]:
    """``{sha: size}`` of the objects among *shas* the server already holds.

    Lists only the fan-out directories those hashes fall in (at most 256).
    """
    held: dict[str, int] = {}
    for prefix in sorted({sha[:2] for sha in shas}):
        for entry in client.list_dir(rid, f"{base}/objects/{prefix}"):
            name = entry.get("name")
            if name in shas and entry.get("type") != "dir":
                held[name] = int(entry.get("size", -1))
    return held


def _unchanged(source: Path, rel: str, size: int, mtime_ns: int) -> bool:
    try:
        st = os.lstat(source / rel)
    except OSError:
        return False
    return st.st_size == size and st.st_mtime_ns == mtime_ns


def _upload_all(
    client: Any, rid: str, server_url: str | None, base: str, source: Path,
    todo: dict[str, dict[str, Any]], jobs: int,
) -> None:
    """Upload each object in *todo* (``sha -> manifest entry``), *jobs* at once.

    Largest first.  Measured on the real server: one upload runs at ~32 MiB/s
    and three at once reach ~53 MiB/s in aggregate, so a small pool helps and a
    large one does not.  Directories are created up front in this thread; each
    worker thread then uses a client of its own (a ``requests`` session is not
    meant to be shared).  The first failure stops the queue and is raised once
    the running uploads have finished -- the next run resumes from what landed.

    An object is named by the hash taken earlier, so the file is checked against
    its recorded size and mtime both before and after it is sent: bytes that
    changed in between would otherwise sit under a name that is not theirs.
    """
    for prefix in sorted({sha[:2] for sha in todo}):
        client.mkdir_p(rid, f"{base}/objects/{prefix}")
    order = sorted(todo, key=lambda sha: (-todo[sha]["size"], todo[sha]["path"]))
    local = threading.local()
    lock = threading.Lock()
    counter = [0]
    failed: list[str] = []

    def one(sha: str) -> None:
        if failed:
            return
        item = todo[sha]
        rel = item["path"]
        if not _unchanged(source, rel, item["size"], item["mtime_ns"]):
            failed.append(f"{rel}: changed since it was hashed; run again")
            return
        worker = getattr(local, "client", None)
        if worker is None:
            worker = local.client = _open_client(server_url)
        with lock:
            counter[0] += 1
            print(
                f"uploading [{counter[0]}/{len(order)}] {rel} ({human_bytes(item['size'])})",
                flush=True,
            )
        try:
            ok = worker.upload_file(rid, f"{base}/{object_dir(sha)}", sha, source / rel)
        except Exception as exc:  # reported once, below
            failed.append(f"{rel}: {exc}")
            return
        if not ok:
            failed.append(f"{rel}: the server did not accept the upload")
        elif not _unchanged(source, rel, item["size"], item["mtime_ns"]):
            failed.append(f"{rel}: changed while it was being uploaded; run again")

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
    """Make the server hold every object the manifest names.

    Uploads the objects (deduplicated by hash) that are missing or the wrong
    size, then verifies them with a listing.  Nothing is ever overwritten or
    deleted here.  Returns ``(objects_to_upload, bytes_to_upload)``.

    ``dry_run`` reads the server and changes nothing.
    """
    library, base, server_url = payload_location(remote)
    client = _open_client(server_url)
    rid = client.get_repo_id(library)

    by_sha: dict[str, dict[str, Any]] = {}
    for item in sorted(manifest["files"], key=lambda e: e["path"]):
        by_sha.setdefault(item["sha256"], item)
    held = _remote_objects(client, rid, base, set(by_sha))
    todo = {sha: item for sha, item in by_sha.items() if held.get(sha) != item["size"]}
    todo_bytes = sum(item["size"] for item in todo.values())
    if dry_run or not todo:
        return len(todo), todo_bytes

    _upload_all(client, rid, server_url, base, source, todo, jobs)
    after = _remote_objects(client, rid, base, set(todo))
    wrong = [sha for sha, item in todo.items() if after.get(sha) != item["size"]]
    if wrong:
        raise SnapshotError(
            f"payload upload did not land: {len(wrong)} object(s) missing or the wrong "
            f"size on the server, first: {todo[wrong[0]]['path']}"
        )
    return len(todo), todo_bytes


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_manifest(manifest: Any, into: Path) -> list[dict[str, Any]]:
    """The manifest's file entries, or ``SnapshotError`` if any is unsafe.

    A restore writes wherever a manifest entry says, and a manifest arrives from
    a remote that anyone with write access to the library could have altered.
    So a path must stay inside the destination (no absolute path, drive, ``..``
    or empty component, whatever the platform), a hash must be 64 hex digits (it
    becomes part of a server path), and size and mode must be what the tool
    writes.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise SnapshotError("the snapshot's payload manifest has no file list")
    root = into.resolve()
    clean: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise SnapshotError("the payload manifest holds an entry that is not an object")
        rel, sha, size = item.get("path"), item.get("sha256"), item.get("size")
        if not isinstance(rel, str):
            raise SnapshotError(f"the payload manifest names an unsafe path: {rel!r}")
        unsafe = (
            not rel
            or "\\" in rel
            or ":" in rel
            or any(part in ("", ".", "..") for part in rel.split("/"))
        )
        if unsafe:
            raise SnapshotError(f"the payload manifest names an unsafe path: {rel!r}")
        try:
            (into / rel).resolve().relative_to(root)
        except ValueError:
            raise SnapshotError(f"the payload manifest names a path outside the restore: {rel!r}") from None
        if not isinstance(sha, str) or not _SHA256_RE.match(sha):
            raise SnapshotError(f"the payload manifest has a bad SHA-256 for {rel!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotError(f"the payload manifest has a bad size for {rel!r}")
        if item.get("mode") not in ("100644", "100755"):
            raise SnapshotError(f"the payload manifest has a bad mode for {rel!r}")
        mtime = item.get("mtime_ns")
        if isinstance(mtime, bool) or not isinstance(mtime, int) or mtime < 0:
            raise SnapshotError(f"the payload manifest has a bad mtime for {rel!r}")
        # Two paths a case-insensitive or normalising filesystem would merge
        # (Linux -> Windows or macOS) would overwrite each other, each file
        # verifying fine in turn; refuse before anything is written.
        folded = unicodedata.normalize("NFC", rel).casefold()
        if seen.setdefault(folded, rel) != rel:
            raise SnapshotError(
                f"payload paths {seen[folded]!r} and {rel!r} differ only by case or Unicode "
                "form, so one would overwrite the other on a case-insensitive filesystem"
            )
        clean.append(item)
    return clean


def restore_payload(remote: str, into: Path) -> None:
    """Download and verify the payload a restored snapshot's manifest names."""
    manifest_path = into / PAYLOAD_MANIFEST
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except ValueError as exc:
        raise SnapshotError(f"the snapshot's payload manifest is unreadable: {exc}") from None
    files = validate_manifest(manifest, into)
    library, base, server_url = payload_location(remote)
    client = _open_client(server_url)
    rid = client.get_repo_id(library)
    print(f"restoring {len(files)} payload file(s) from {base}/objects ...", flush=True)
    problems: list[str] = []
    for index, item in enumerate(files, 1):
        rel, sha = item["path"], item["sha256"]
        dest = into / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading [{index}/{len(files)}] {rel} ({human_bytes(item['size'])})", flush=True)
        if not client.download_file_to(rid, f"{base}/{object_dir(sha)}/{sha}", dest):
            problems.append(f"{rel}: object {sha[:12]}... not found in {base}/objects")
            continue
        if dest.stat().st_size != item["size"]:
            problems.append(f"{rel}: size {dest.stat().st_size}, manifest says {item['size']}")
            continue
        if _sha256_file(dest) != sha:
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
        "hashed (SHA-256) and uploaded to Seafile by hash under <path>/payload/objects, "
        "and the snapshot records a manifest instead. Needs a remote; a restore downloads "
        "and verifies them. 0 turns it off. A vault that has used it needs the flag on "
        "every run (SNAPSHOT.md section 15.1)",
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
    """Hash and upload the payload; ``(manifest, (objects, bytes) to upload)``."""
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
    reference_ns = 0
    if base and previous is not None:
        stamp = git(git_dir, source, ["show", "-s", "--format=%ct", base], cwd=source, check=False)
        if stamp.returncode == 0 and stamp.stdout.strip().isdigit():
            reference_ns = int(stamp.stdout.strip()) * 1_000_000_000
    manifest = build_manifest(
        candidates, source, previous, min_size, reference_ns=reference_ns
    )
    todo = publish_payload(
        remote or "", source, manifest, previous, dry_run=dry_run, jobs=jobs
    )
    return manifest, todo


def payload_summary(manifest: dict[str, Any], todo: tuple[int, int], dry_run: bool) -> str:
    total = sum(item["size"] for item in manifest["files"])
    verb = "to upload" if dry_run else "uploaded"
    return (
        f"payload: {len(manifest['files'])} file(s) ({human_bytes(total)}) bypass git; "
        f"snapshot {manifest['snapshot']}; {verb} {todo[0]} object(s) ({human_bytes(todo[1])})"
    )
