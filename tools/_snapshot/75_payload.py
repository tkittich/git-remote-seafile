

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
# Why by hash: an object is never replaced by different bytes, so a snapshot is
# just a manifest and costs nothing to keep; a rename or a duplicate uploads
# nothing; concurrent writers cannot conflict (the same name always has the same
# content); and what the server holds depends on no file name, so the server's
# name rules (NFC, ``"`` -> ``%22``, 255 bytes) never apply.
#
# What makes "the same name always has the same content" true is that an object
# is written under a temporary name, hashed *as it is sent*, and renamed to its
# hash only when the bytes that went out are the bytes the name promises.
#
# Specification, requirements and the review that shaped this: PAYLOAD.md.

#: Where the manifest lives inside the snapshot tree.  The directory is reserved:
#: nothing under it is ever staged from the source.
PAYLOAD_RESERVED_DIR = ".seafile-payload"
PAYLOAD_MANIFEST = PAYLOAD_RESERVED_DIR + "/manifest.json"
PAYLOAD_FORMAT = 2
_PAYLOAD_HASH_BLOCK = 8 * 1024 * 1024
#: A cached hash is trusted only if the file's mtime is at least this much older
#: than the moment *this machine* hashed it (git's "racily clean" rule): a
#: same-size edit inside the timestamp granularity would otherwise look untouched.
_RACY_WINDOW_NS = 2_000_000_000
_CACHE_NAME = "payload-cache.json"
#: Objects are uploaded under ``<sha>.part-<token>`` and renamed; GC ignores them.
_PART_SUFFIX = ".part-"
#: The ``auto`` selection rule (PAYLOAD.md D1): a file is payload when it is at
#: least ``_AUTO_UPPER`` bytes, or at least ``_AUTO_LOWER`` bytes *and* a sample
#: of it does not compress.
_AUTO_UPPER = 64 * 1024 ** 2
_AUTO_LOWER = 4 * 1024 ** 2
_AUTO_INCOMPRESSIBLE = 0.9
_AUTO_SAMPLE_CHUNK = 256 * 1024
_SIZE_RE = re.compile(r"^\s*(\d+)\s*([kmgt]?)(?:i?b)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
_MAX_MTIME_NS = 2 ** 62

#: ``(size, mtime_ns, mode, inode)`` for one candidate payload file.
Candidate = tuple[int, int, str, int]


def parse_size(text: str) -> int:
    """``"64M"``, ``"1g"``, ``"500k"`` or ``"1048576"`` as a byte count (0 = off).

    Whole numbers only: ``-1`` would select every file (empty ones too) and
    ``0.5`` would read as "off" without saying so.
    """
    match = _SIZE_RE.match(text)
    if not match:
        raise SnapshotError(
            f"not a size: {text!r} (examples: 64M, 1G, 500k, 1048576, or auto). A bare "
            "--payload-min-size takes the next argument as its value: put SOURCE first, "
            "or write --payload-min-size=auto"
        )
    return int(match.group(1)) * _SIZE_UNITS[match.group(2).lower()]


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


def _api(call: Any, what: str) -> Any:
    """Run a server call, turning transport and API errors into a snapshot error."""
    try:
        return call()
    except SnapshotError:
        raise
    except Exception as exc:  # SeafileAPIError, requests errors, JSON errors
        raise SnapshotError(f"{what} failed: {type(exc).__name__}: {exc}") from None


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


class _HashingFile:
    """A read-only file object that hashes the bytes handed to the uploader.

    The uploader reads the file itself, possibly in chunks and possibly twice
    (a retried chunk seeks back).  Only a *forward-contiguous* first read is
    hashed, so the digest is that of the bytes the server received, in order.
    ``complete`` says whether every byte passed through.
    """

    def __init__(self, fh: Any, size: int) -> None:
        self._fh = fh
        self._size = size
        self._hash = hashlib.sha256()
        self._hashed = 0

    def read(self, size: int = -1) -> bytes:
        at = self._fh.tell()
        data = self._fh.read(size)
        if data and at == self._hashed:
            self._hash.update(data)
            self._hashed += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return int(self._fh.seek(offset, whence))

    def tell(self) -> int:
        return int(self._fh.tell())

    @property
    def complete(self) -> bool:
        return self._hashed == self._size

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _incompressible(path: Path, size: int) -> bool:
    """Whether three sampled chunks of *path* barely shrink under a fast deflate."""
    raw = comp = 0
    try:
        with open(path, "rb") as fh:
            for offset in sorted({0, max(0, size // 2 - _AUTO_SAMPLE_CHUNK // 2),
                                  max(0, size - _AUTO_SAMPLE_CHUNK)}):
                fh.seek(offset)
                chunk = fh.read(_AUTO_SAMPLE_CHUNK)
                raw += len(chunk)
                comp += len(zlib.compress(chunk, 1))
    except OSError:
        return False  # unreadable here is unreadable later: git reports it
    return raw > 0 and comp / raw >= _AUTO_INCOMPRESSIBLE


_CLOUD_PLACEHOLDER_ATTRS = 0x1000 | 0x400000  # OFFLINE | RECALL_ON_DATA_ACCESS


def find_payload_files(
    source: Path, excludes: list[str], min_size: int, auto: bool = False
) -> dict[str, Candidate]:
    """``{relative path: (size, mtime_ns, mode, inode)}`` for every payload file.

    A payload file is a *regular* file that no ``--exclude`` removes and that the
    rule selects: size alone (``size >= min_size``), or with *auto* the rule in
    PAYLOAD.md D1.  Symlinks stay with git (they are a few bytes and git stores
    them exactly), as does everything the rule leaves out.  A Windows cloud
    placeholder (OneDrive "files on demand") is refused by name: reading it would
    download it.
    """
    found: dict[str, Candidate] = {}
    placeholders: list[str] = []
    floor = _AUTO_LOWER if auto else min_size
    for size, rel in _walk_sizes(source, excludes):
        if size < floor or rel == PAYLOAD_RESERVED_DIR or rel.startswith(PAYLOAD_RESERVED_DIR + "/"):
            continue
        try:
            st = os.lstat(source / rel)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if getattr(st, "st_file_attributes", 0) & _CLOUD_PLACEHOLDER_ATTRS:
            placeholders.append(rel)
            continue
        if auto and size < _AUTO_UPPER and not _incompressible(source / rel, size):
            continue
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        found[rel] = (st.st_size, st.st_mtime_ns, mode, st.st_ino)
    if placeholders:
        shown = ", ".join(placeholders[:5]) + (" ..." if len(placeholders) > 5 else "")
        raise SnapshotError(
            f"{len(placeholders)} payload file(s) are cloud placeholders (not stored locally), "
            f"and reading them would download them: {shown}. Make them available offline, "
            "or --exclude them"
        )
    return found


# -- the server side -----------------------------------------------------------


def _remote_objects(client: Any, rid: str, base: str, shas: set[str]) -> dict[str, int]:
    """``{sha: size}`` of the objects among *shas* the server already holds.

    Lists only the fan-out directories those hashes fall in (at most 256).
    """
    held: dict[str, int] = {}
    for prefix in sorted({sha[:2] for sha in shas}):
        entries = _api(
            lambda p=prefix: client.list_dir(rid, f"{base}/objects/{p}"),
            f"listing {base}/objects/{prefix}",
        )
        for entry in entries:
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


def _commit_object(client: Any, rid: str, directory: str, temp: str, sha: str) -> bool:
    """Give the verified temporary file its hash name, replacing a wrong-size one."""
    if client.rename_entry(rid, f"{directory}/{temp}", sha):
        return True
    # The name is taken: a stale wrong-size object (the run only gets here for
    # objects it found missing or the wrong size).  Remove it and try once more.
    client.delete_entry(rid, f"{directory}/{sha}")
    return bool(client.rename_entry(rid, f"{directory}/{temp}", sha))


def _upload_all(
    client: Any, rid: str, server_url: str | None, base: str, source: Path,
    todo: dict[str, dict[str, Any]], jobs: int,
) -> None:
    """Upload each object in *todo* (``sha -> manifest entry``), *jobs* at once.

    Largest first.  Measured on the real server: one upload runs at ~32 MiB/s and
    three at once reach ~53 MiB/s in aggregate, so a small pool helps and a large
    one does not.  Directories are created up front in this thread; each worker
    thread then uses a client of its own (a ``requests`` session is not meant to
    be shared).  The first failure stops the queue and is raised once the running
    uploads have finished -- the next run resumes from what landed.

    Each object goes up under a temporary name through a hashing reader and is
    renamed to its hash only if the bytes sent are the bytes the name promises.
    A file edited after it was hashed (or rewritten in place without touching its
    mtime, which a memory-mapped writer on Windows can do) therefore never ends up
    under another content's name -- the failure that otherwise survives every
    later run, because "present at the right size" is all a listing can prove.
    """
    for prefix in sorted({sha[:2] for sha in todo}):
        _api(lambda p=prefix: client.mkdir_p(rid, f"{base}/objects/{p}"),
             f"creating {base}/objects/{prefix}")
    order = sorted(todo, key=lambda sha: (-todo[sha]["size"], todo[sha]["path"]))
    local = threading.local()
    lock = threading.Lock()
    counter = [0]
    failed: list[str] = []

    def one(sha: str) -> None:
        if failed:
            return
        item = todo[sha]
        rel, size = item["path"], item["size"]
        if not _unchanged(source, rel, size, item["mtime_ns"]):
            failed.append(f"{rel}: changed since it was hashed; run again")
            return
        worker = getattr(local, "client", None)
        if worker is None:
            worker = local.client = _open_client(server_url)
        with lock:
            counter[0] += 1
            print(
                f"uploading [{counter[0]}/{len(order)}] {rel} ({human_bytes(size)})",
                flush=True,
            )
        directory = f"{base}/{object_dir(sha)}"
        temp = f"{sha}{_PART_SUFFIX}{os.urandom(4).hex()}"
        try:
            with open(source / rel, "rb") as fh:
                reader = _HashingFile(fh, size)
                ok = worker.upload_file(rid, directory, temp, reader)
            if not ok:
                failed.append(f"{rel}: the server did not accept the upload")
                return
            if not reader.complete or reader.hexdigest() != sha:
                worker.delete_entry(rid, f"{directory}/{temp}")
                failed.append(f"{rel}: changed while it was being uploaded; run again")
                return
            if not _commit_object(worker, rid, directory, temp, sha):
                worker.delete_entry(rid, f"{directory}/{temp}")
                failed.append(f"{rel}: the server would not name the object")
        except OSError as exc:
            failed.append(f"{rel}: {exc}")
        except Exception as exc:  # SeafileAPIError, requests errors: reported once, below
            failed.append(f"{rel}: {type(exc).__name__}: {exc}")

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
    *,
    dry_run: bool,
    jobs: int = 3,
) -> tuple[int, int]:
    """Make the server hold every object the manifest names.

    Uploads the objects (deduplicated by hash) that are missing or the wrong
    size, then lists **every** object the manifest names and requires each at its
    size.  Returns ``(objects_to_upload, bytes_to_upload)``.

    ``dry_run`` reads the server and changes nothing.
    """
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")

    by_sha: dict[str, dict[str, Any]] = {}
    for item in sorted(manifest["files"], key=lambda e: e["path"]):
        by_sha.setdefault(item["sha256"], item)
    held = _remote_objects(client, rid, base, set(by_sha))
    todo = {sha: item for sha, item in by_sha.items() if held.get(sha) != item["size"]}
    todo_bytes = sum(item["size"] for item in todo.values())
    if dry_run:
        return len(todo), todo_bytes
    if todo:
        _upload_all(client, rid, server_url, base, source, todo, jobs)
    after = _remote_objects(client, rid, base, set(by_sha))
    wrong = [sha for sha, item in by_sha.items() if after.get(sha) != item["size"]]
    if wrong:
        raise SnapshotError(
            f"payload objects are missing or the wrong size on the server: {len(wrong)}, "
            f"first: {by_sha[wrong[0]]['path']}"
        )
    return len(todo), todo_bytes


def verify_payload_before_push(
    git_dir: Path,
    source: Path,
    remote: str,
    remote_tip: str | None,
    tip: str,
    manifest: dict[str, Any] | None,
    jobs: int = 3,
) -> None:
    """Make sure every object a commit about to be pushed names is on the server.

    A run lists the server once, near its start; hours later (or, for an
    earlier un-pushed snapshot, days) the objects it relied on may be gone.  This
    re-lists, just before the push, every object named by every manifest in
    ``remote_tip..tip``.  Missing ones that the current manifest names are
    re-uploaded from the source; anything else is a snapshot that could not be
    restored, and the push is refused naming it.
    """
    span = f"{remote_tip}..{tip}" if remote_tip else tip
    commits = [c for c in git(git_dir, source, ["rev-list", span], cwd=source, check=False)
               .stdout.split() if c]
    if not commits:
        return
    specs = "".join(f"{c}:{PAYLOAD_MANIFEST}\n" for c in commits)
    checked = git(git_dir, source, ["cat-file", "--batch-check"], cwd=source, stdin=specs).stdout
    needed: dict[str, int] = {}
    owners: dict[str, str] = {}
    for line in checked.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        body = git(git_dir, source, ["cat-file", "blob", parts[0]], cwd=source).stdout
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise SnapshotError(
                f"a payload manifest ({parts[0][:12]}) in the commits about to be pushed is "
                "unreadable; refusing to push it"
            ) from None
        for item in check_manifest_shape(data):
            needed[item["sha256"]] = item["size"]
            owners.setdefault(item["sha256"], item["path"])
    if not needed:
        return
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    held = _remote_objects(client, rid, base, set(needed))
    missing = {sha for sha, size in needed.items() if held.get(sha) != size}
    if not missing:
        return
    current = {item["sha256"]: item for item in (manifest or {}).get("files", [])}
    redo = {
        sha: current[sha] for sha in missing
        if sha in current and _unchanged(source, current[sha]["path"],
                                         current[sha]["size"], current[sha]["mtime_ns"])
    }
    if redo:
        print(f"re-uploading {len(redo)} payload object(s) that went missing before the push",
              flush=True)
        _upload_all(client, rid, server_url, base, source, redo, jobs)
        missing -= set(redo)
    if missing:
        sample = ", ".join(sorted(owners[sha] for sha in missing)[:5])
        raise SnapshotError(
            f"{len(missing)} payload object(s) named by snapshots about to be pushed are "
            f"missing from the server and the source no longer has them (e.g. {sample}); a "
            "restore of those snapshots would fail. Not pushing"
        )
