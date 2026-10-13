

# -- Payload manifests and the per-machine hash cache (see 75_payload.py) ----------

# -- manifests ---------------------------------------------------------------


def check_manifest_shape(manifest: Any, where: str = "the payload manifest") -> list[dict[str, Any]]:
    """The manifest's file entries, if it is a manifest this tool understands.

    Structure only (types, hash format, bounds): path *safety* is
    :func:`validate_manifest`'s job.  Anything unreadable raises -- a reader that
    treats "cannot parse" as "references nothing" turns a version skew into
    deleted data.
    """
    if not isinstance(manifest, dict):
        raise SnapshotError(f"{where} is not an object")
    fmt = manifest.get("format")
    if isinstance(fmt, bool) or not isinstance(fmt, int) or fmt < 2:
        raise SnapshotError(f"{where} has no usable format")
    if fmt > PAYLOAD_FORMAT:
        raise SnapshotError(
            f"{where} is format {fmt}; this tool understands up to {PAYLOAD_FORMAT}. "
            "Upgrade it before restoring or running retention"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise SnapshotError(f"{where} has no file list")
    for item in files:
        if not isinstance(item, dict):
            raise SnapshotError(f"{where} holds an entry that is not an object")
        rel, sha, size, mtime = item.get("path"), item.get("sha256"), item.get("size"), item.get("mtime_ns")
        if not isinstance(rel, str) or not rel:
            raise SnapshotError(f"{where} has an entry with no path")
        if not isinstance(sha, str) or not _SHA256_RE.match(sha):
            raise SnapshotError(f"{where} has a bad SHA-256 for {rel!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotError(f"{where} has a bad size for {rel!r}")
        if item.get("mode") not in ("100644", "100755"):
            raise SnapshotError(f"{where} has a bad mode for {rel!r}")
        if isinstance(mtime, bool) or not isinstance(mtime, int) or not 0 <= mtime < _MAX_MTIME_NS:
            raise SnapshotError(f"{where} has a bad mtime for {rel!r}")
    return files


def load_manifest(git_dir: Path, work_tree: Path, rev: str | None) -> dict[str, Any] | None:
    """The manifest recorded in snapshot ``rev``, or ``None`` when it has none.

    A manifest that is there but unusable raises: it came from a remote, and
    carrying on as though the vault had never used payload mode is exactly how
    large files would end up in git.
    """
    if not rev:
        return None
    proc = git(
        git_dir, work_tree, ["show", f"{rev}:{PAYLOAD_MANIFEST}"], cwd=work_tree, check=False
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (ValueError, RecursionError):
        raise SnapshotError(
            f"the payload manifest in {rev[:12]} is not valid JSON; refusing to continue"
        ) from None
    check_manifest_shape(data, f"the payload manifest in {rev[:12]}")
    return data


def _component_is_git(part: str) -> bool:
    folded = part.rstrip(". ").casefold()
    return folded == ".git" or folded.startswith("git~")


def path_security_problem(rel: str) -> str | None:
    """Why *rel* may never be written by a restore, or ``None``.

    These are refusals of the whole manifest: a path that leaves the destination,
    or reaches ``.git`` (a ``core.fsmonitor`` or a hook there runs code on the
    next ``git status``) or the reserved payload directory.
    """
    if "\0" in rel:
        return "contains a NUL"
    parts = rel.split("/")
    if os.name == "nt":
        parts = [piece for part in parts for piece in part.split("\\")]
    for part in parts:
        if part in ("", ".", ".."):
            return "is absolute or has an empty, '.' or '..' component"
        if _component_is_git(part):
            return "reaches .git"
    if parts[0].rstrip(". ").casefold() == PAYLOAD_RESERVED_DIR:
        return f"is inside the reserved {PAYLOAD_RESERVED_DIR} directory"
    if re.match(r"^[A-Za-z]:", parts[0]) and os.name == "nt":
        return "names a drive"
    return None


def windows_name_problem(rel: str) -> str | None:
    """Why Windows cannot hold *rel* under that name, or ``None``.

    Legal on Linux, so not a security refusal: a restore on Windows skips and
    reports these, and a restore anywhere else is not affected.
    """
    for part in rel.split("/"):
        if re.search(r'[<>:"|?*\\\x00-\x1f]', part):
            return "has a character Windows forbids"
        if part != part.rstrip(". "):
            return "ends with a dot or a space (Windows would drop it)"
        if _WIN_RESERVED.match(part):
            return "is a reserved device name"
    return None


def validate_manifest(manifest: Any, into: Path) -> list[dict[str, Any]]:
    """The manifest's file entries, or ``SnapshotError`` if any is unsafe.

    A restore writes wherever a manifest entry says, and a manifest arrives from
    a remote that anyone with write access to the library could have altered.
    """
    files = check_manifest_shape(manifest)
    root = into.resolve()
    for item in files:
        rel = item["path"]
        problem = path_security_problem(rel)
        if problem is not None:
            raise SnapshotError(f"the payload manifest names a path that {problem}: {rel!r}")
        try:
            (into / rel).resolve().relative_to(root)
        except ValueError:
            raise SnapshotError(
                f"the payload manifest names a path outside the restore: {rel!r}"
            ) from None
    return files


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


# -- the per-machine hash cache ------------------------------------------------
#
# Kept in the vault's own git dir and never read from a remote: a hash that came
# from a manifest anyone with write access could have written is not a hash this
# machine computed.  An entry is keyed by path and is only trusted while size,
# mtime *and inode* are unchanged -- a rename keeps size and mtime, and tiles
# extracted from one archive share both, so (size, mtime) alone would hand a
# renumbered file the hash of its predecessor.


def load_hash_cache(git_dir: Path) -> dict[str, dict[str, int | str]]:
    try:
        data = json.loads((git_dir / _CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def save_hash_cache(git_dir: Path, cache: dict[str, dict[str, int | str]]) -> None:
    tmp = git_dir / (_CACHE_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, git_dir / _CACHE_NAME)
    except OSError:
        pass  # a cache that cannot be written only costs a rehash


def build_manifest(
    candidates: dict[str, Candidate],
    source: Path,
    cache: dict[str, dict[str, int | str]],
    min_size: int,
    *,
    quiet: bool = False,
    rule: str = "size",
) -> tuple[dict[str, Any], dict[str, dict[str, int | str]]]:
    """Hash every payload file; return ``(manifest, updated cache)``."""
    entries: list[dict[str, Any]] = []
    fresh: dict[str, dict[str, int | str]] = {}
    total = len(candidates)
    for index, rel in enumerate(sorted(candidates), 1):
        size, mtime_ns, mode, ino = candidates[rel]
        old = cache.get(rel) or {}
        hashed_at = old.get("at")
        trusted = (
            old.get("size") == size
            and old.get("mtime_ns") == mtime_ns
            and old.get("ino") == ino
            and isinstance(old.get("sha256"), str)
            and isinstance(hashed_at, int)
            and mtime_ns % 1_000_000_000 != 0  # whole-second mtimes: a coarse clock
            and mtime_ns < hashed_at - _RACY_WINDOW_NS
        )
        if trusted:
            digest = str(old["sha256"])
            fresh[rel] = old
        else:
            if not quiet:
                print(f"hashing [{index}/{total}] {rel} ({human_bytes(size)})", flush=True)
            started = time.time_ns()
            digest = _sha256_file(source / rel)
            fresh[rel] = {
                "size": size, "mtime_ns": mtime_ns, "ino": ino, "sha256": digest, "at": started,
            }
        entries.append(
            {"path": rel, "size": size, "mode": mode, "sha256": digest, "mtime_ns": mtime_ns}
        )
    ident = hashlib.sha256()
    for item in entries:
        ident.update(f"{item['path']}\0{item['size']}\0{item['mode']}\0{item['sha256']}\n".encode())
    manifest = {
        "format": PAYLOAD_FORMAT,
        "snapshot": "p-" + ident.hexdigest()[:16],
        "min_size": min_size,
        "rule": rule,
        "files": entries,
    }
    return manifest, fresh
