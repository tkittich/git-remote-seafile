

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
    check_min_tool(manifest, where)
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


# What someone finds when they clone a vault with plain git: the large files are
# not in it, and nothing else says where they are.  Recorded beside the manifest.
PAYLOAD_README_TEXT = (
    "This snapshot keeps its large files outside git.\n"
    "\n"
    "manifest.json lists each of them (path, size, SHA-256); the bytes are stored on\n"
    "the Seafile server by hash, in the payload/objects directory next to this vault's\n"
    "remote. A plain `git clone` or `git checkout` therefore does not contain them.\n"
    "\n"
    "To get the complete tree back, verified file by file:\n"
    "\n"
    "    git seafile-snapshot --restore --remote <the vault's seafile:// URL> --into <dir>\n"
    "\n"
    "Do not edit or delete this directory; a restore and the payload tools read it.\n"
)


def add_manifest_to_index(
    git_dir: Path, work_tree: Path, manifest: dict[str, Any]
) -> None:
    """Record the manifest, and a note on reading it, in the vault index."""
    # The note has newlines, which a text-mode pipe would translate on Windows and
    # so give the same tree a different blob on each platform; hand git a file.
    note = git_dir / "payload-readme.tmp"
    note.write_bytes(PAYLOAD_README_TEXT.encode("ascii"))
    try:
        readme = git(
            git_dir, work_tree, ["hash-object", "-w", "--no-filters", str(note)],
            cwd=work_tree,
        ).stdout.strip()
    finally:
        note.unlink(missing_ok=True)
    blob = git(
        git_dir, work_tree, ["hash-object", "-w", "--no-filters", "--stdin"],
        cwd=work_tree, stdin=manifest_bytes(manifest).decode("ascii"),
    ).stdout.strip()
    for path, sha in ((PAYLOAD_MANIFEST, blob), (PAYLOAD_README, readme)):
        git(
            git_dir, work_tree,
            ["update-index", "--add", "--cacheinfo", f"100644,{sha},{path}"],
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


#: Filesystem types whose clock is another machine's.
_NETWORK_FS_TYPES = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "afpfs", "webdav", "9p", "davfs", "fuse.sshfs",
     "fuse.rclone"}
)
#: A smaller offset is noise (a probe is one round trip), not a skewed clock.
_CLOCK_SKEW_NOISE_NS = 5_000_000_000


_DARWIN_MOUNT_LINE = re.compile(r"^(?P<src>.+?) on (?P<mp>/.*) \((?P<opts>[^)]*)\)\s*$")


def _darwin_mount_is_network(resolved: str, mount_output: str | None = None) -> bool:
    """Whether the longest mount point containing *resolved* is a network filesystem.

    macOS has no ``/proc``; ``mount`` prints ``//user@host/share on /Volumes/x
    (smbfs, nodev, ...)``, the filesystem type being the first option.
    """
    if mount_output is None:
        try:
            mount_output = subprocess.run(
                [shutil.which("mount") or "/sbin/mount"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
    best, kind = "", ""
    for line in mount_output.splitlines():
        m = _DARWIN_MOUNT_LINE.match(line)
        if not m:
            continue
        mount = m.group("mp")
        inside = resolved == mount or resolved.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) >= len(best):
            best, kind = mount, m.group("opts").split(",")[0].strip()
    return kind in _NETWORK_FS_TYPES


def _is_network_fs(path: Path) -> bool:
    """Whether ``path`` lives on a filesystem another machine's clock stamps."""
    text = str(path)
    if os.name == "nt":
        if text.startswith(chr(92) * 2) and not text.startswith(chr(92) * 2 + "?" + chr(92)):
            return True
        try:
            import ctypes

            anchor = Path(text).anchor
            dll = "windll"  # a name, not an attribute: ctypes has none off Windows
            kernel32 = getattr(ctypes, dll).kernel32
            return bool(anchor) and kernel32.GetDriveTypeW(anchor) == 4
        except (OSError, AttributeError, ValueError):
            return False
    if sys.platform == "darwin":
        return _darwin_mount_is_network(os.path.realpath(text))
    try:
        best, kind = "", ""
        resolved = os.path.realpath(text)
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            head, _sep, tail = line.partition(" - ")
            fields = head.split()
            if len(fields) < 5 or not tail:
                continue
            mount = fields[4].replace("\\040", " ")
            inside = resolved == mount or resolved.startswith(mount.rstrip("/") + "/")
            if inside and len(mount) >= len(best):
                best, kind = mount, tail.split()[0]
        return kind in _NETWORK_FS_TYPES
    except (OSError, ValueError):
        return False


def _probe_fs_clock(directory: Path) -> int | None:
    """The filesystem's own "now", from a file created and removed at once; ``None`` if it cannot be."""
    name = directory / f".grs-clock-probe-{os.getpid()}-{time.time_ns()}"
    try:
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return None
    try:
        os.close(fd)
        return os.stat(name).st_mtime_ns
    except OSError:
        return None
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def fs_clock_offset_ns(
    source: Path, *, is_network: Any = _is_network_fs, probe: Any = _probe_fs_clock
) -> int:
    """The source filesystem's clock minus this machine's, in ns; 0 when it is this machine's.

    The racy rule (D6) needs "now" in the clock that stamps the files.  On a
    local disk that is this machine's; on a share it is the server's, and a
    server running an hour behind makes every freshly edited file look old
    enough to trust -- a same-size edit within one timestamp granule then keeps
    its stale hash.  A probe file measures the difference; it is made only on a
    network filesystem, so a local source tree is never written to.
    """
    if not is_network(source):
        return 0
    before = time.time_ns()
    stamp = probe(source)
    if stamp is None:
        print("note: the source is on a network share and its clock could not be measured "
              "(no write access); a file edited right after it was hashed is detected only "
              "if its size or timestamp differs -- --payload-rehash re-checks everything",
              flush=True)
        return 0
    offset = int(stamp) - before
    return 0 if abs(offset) < _CLOCK_SKEW_NOISE_NS else offset


def build_manifest(
    candidates: dict[str, Candidate],
    source: Path,
    cache: dict[str, dict[str, int | str]],
    min_size: int,
    *,
    quiet: bool = False,
    rule: str = "size",
    clock_offset_ns: int = 0,
) -> tuple[dict[str, Any], dict[str, dict[str, int | str]]]:
    """Hash every payload file; return ``(manifest, updated cache)``.

    ``clock_offset_ns`` is the source filesystem's clock minus this machine's
    (:func:`fs_clock_offset_ns`).  The racy rule compares a file's mtime with the
    moment it was hashed, and an mtime is written by the *filesystem's* clock, so
    the moment is recorded in that clock's domain.
    """
    entries: list[dict[str, Any]] = []
    fresh: dict[str, dict[str, int | str]] = {}
    total = len(candidates)
    stale = 0
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
            # A timestamp taken in this machine's clock says nothing about a
            # skewed share's, so it cannot vouch for a file there.
            and (not clock_offset_ns or old.get("fs") is True)
        )
        if trusted:
            digest = str(old["sha256"])
            fresh[rel] = old
        else:
            stale += isinstance(old.get("sha256"), str)
            if not quiet:
                print(f"hashing [{index}/{total}] {rel} ({human_bytes(size)})", flush=True)
            started = time.time_ns() + clock_offset_ns
            digest = _sha256_file(source / rel)
            fresh[rel] = {
                "size": size, "mtime_ns": mtime_ns, "ino": ino, "sha256": digest, "at": started,
            }
            if clock_offset_ns:
                fresh[rel]["fs"] = True
        entries.append(
            {"path": rel, "size": size, "mode": mode, "sha256": digest, "mtime_ns": mtime_ns}
        )
    if stale and not quiet:
        print(
            f"note: {stale} file(s) were hashed again because their size, timestamp or inode "
            "changed since the last run (or are too recent to trust); a copy tool that resets "
            "timestamps does this to every file",
            flush=True,
        )
    ident = hashlib.sha256()
    for item in entries:
        ident.update(f"{item['path']}\0{item['size']}\0{item['mode']}\0{item['sha256']}\n".encode())
    manifest = {
        "format": PAYLOAD_FORMAT,
        "snapshot": "p-" + ident.hexdigest()[:16],
        "min_size": min_size,
        "min_git_remote_seafile": PAYLOAD_MIN_TOOL,
        "rule": rule,
        "files": entries,
    }
    return manifest, fresh
