

# -- Payload manifests and the per-machine hash cache (see 75_payload.py) ----------

# -- manifests ---------------------------------------------------------------


#: The snapshot id this tool writes into a manifest: derived, never user text.
_SNAPSHOT_ID_RE = re.compile(r"p-[0-9a-f]{16}")


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
    ratio = manifest.get("ratio")
    if ratio is not None and (
        isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio <= 1
    ):
        # The field feeds the cross-machine cut-off guard, so a hostile or
        # hand-written value is refused rather than compared.
        raise SnapshotError(f"{where} has a bad ratio {ratio!r}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise SnapshotError(f"{where} has no file list")
    ident = manifest.get("snapshot")
    if isinstance(ident, str) and not _SNAPSHOT_ID_RE.fullmatch(ident):
        # The field is a name this tool derives, never user text; a value that
        # is not the derived shape is a manifest someone wrote by hand, and it
        # reaches a server-side directory name in --payload-view.  Refused as
        # unparseable rather than trusted (REVIEW.iq3_s E2).
        raise SnapshotError(f"{where} has a snapshot id that is not one this tool writes: {ident!r}")
    for item in files:
        if not isinstance(item, dict):
            raise SnapshotError(f"{where} holds an entry that is not an object")
        rel, sha, size, mtime = item.get("path"), item.get("sha256"), item.get("size"), item.get("mtime_ns")
        if not isinstance(rel, str) or not rel:
            raise SnapshotError(f"{where} has an entry with no path")
        if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
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


#: Code points HFS+ ignores in a name (the set git's own ``.git`` check strips):
#: ``.git<U+200C>`` is ``.git`` there.
_HFS_IGNORABLE = dict.fromkeys(
    [*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
)


def _component_is_git(part: str) -> bool:
    # An NTFS stream suffix is stripped first: Windows resolves
    # `.git::$INDEX_ALLOCATION/x` and `.git:$I30:x` to the real `.git`, and
    # those spellings passed this test as strings (REVIEW.sonnet-5.5 D10).
    # Everything before the first colon is what the filesystem reads.
    part = part.split(":", 1)[0]
    folded = part.translate(_HFS_IGNORABLE).rstrip(". ").casefold()
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
    if parts[0].translate(_HFS_IGNORABLE).rstrip(". ").casefold() == PAYLOAD_RESERVED_DIR:
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
        resolved = (into / rel).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise SnapshotError(
                f"the payload manifest names a path outside the restore: {rel!r}"
            ) from None
        # A symlink the checkout created (``tool -> .git``) makes a path with no
        # ".git" in it land inside the repository: ``tool/hooks/post-checkout``
        # would be an executable hook.  Inside the tree is not enough; the
        # protected directories are refused after links are followed.
        folded = os.path.normcase(str(resolved))
        for guarded in (root / ".git", root / PAYLOAD_RESERVED_DIR):
            prefix = os.path.normcase(str(guarded))
            if folded == prefix or folded.startswith(prefix + os.sep):
                raise SnapshotError(
                    f"the payload manifest names a path that reaches {guarded.name} "
                    f"through a link: {rel!r}"
                )
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


def _machine_id() -> str:
    """A stable per-installation identifier for the hash cache's provenance.

    The cache is documented as per-machine (PAYLOAD D6), but the file lives in
    the vault, which two machines can share (a source on a NAS with the vault
    beside it): an entry's validity depends on the writing machine's clock and
    its probe of the share clock, so a reader on another machine cannot vouch
    for it (REVIEW.iq3_s D5).
    """
    import uuid

    return f"{socket.gethostname()}/{hex(uuid.getnode())}"


def load_machine_hash_cache(git_dir: Path) -> tuple[dict[str, dict[str, int | str]], str | None]:
    """The hash cache entries and the machine that wrote them.

    ``(entries, None)`` for a cache without a stamp (an older tool wrote it;
    its entries are adopted, as before), ``(entries, this machine)`` when the
    stamp matches, and ``({}, other machine)`` when it does not -- a reader
    cannot vouch for another machine's clock, so every file is hashed again.
    """
    try:
        data = json.loads((git_dir / _CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    if "entries" in data and isinstance(data["entries"], dict):
        machine = data.get("machine")
        if not isinstance(machine, str):
            return data["entries"], None
        return (data["entries"] if machine == _machine_id() else {}), machine
    return data, None


def load_hash_cache(git_dir: Path) -> dict[str, dict[str, int | str]]:
    """The hash cache entries, whatever machine wrote them (legacy reader)."""
    entries, _machine = load_machine_hash_cache(git_dir)
    return entries


def save_hash_cache(git_dir: Path, cache: dict[str, dict[str, int | str]]) -> None:
    tmp = git_dir / (_CACHE_NAME + ".tmp")
    document = json.dumps({"machine": _machine_id(), "entries": cache},
                          separators=(",", ":"))
    try:
        tmp.write_text(document, encoding="utf-8")
        os.replace(tmp, git_dir / _CACHE_NAME)
    except OSError:
        pass  # a cache that cannot be written only costs a rehash


#: Filesystem types whose clock is another machine's.
_NETWORK_FS_TYPES = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "afpfs", "webdav", "9p", "davfs", "fuse.sshfs",
     "fuse.rclone"}
)
#: A share whose clock is *ahead* by less than this is treated as in step: its
#: files then look newer than they are, which only costs a rehash.
_CLOCK_SKEW_NOISE_NS = 5_000_000_000
#: A share *behind* by more than this is applied.  Ignoring it is the unsafe
#: side (an edit looks older than it is), and the racy window is 2 s, so a
#: floor wider than a small fraction of it let a 4 s skew through.
_CLOCK_BEHIND_NOISE_NS = 250_000_000


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


#: The probe file's name prefix.  A run killed between creating and removing it
#: leaves one behind, and the next snapshot would capture it as content.
_PROBE_PREFIX = ".grs-clock-probe-"
#: Debris older than this is removed by the next probe (generous: a clock that
#: is hours off must not make a live probe look stale).
_PROBE_DEBRIS_AGE_S = 6 * 3600


def is_clock_probe_debris(rel: str) -> bool:
    """True for a probe file at the source root, which never belongs in a snapshot."""
    return "/" not in rel and rel.startswith(_PROBE_PREFIX)


def _probe_fs_clock(directory: Path) -> int | None:
    """The filesystem's own "now", from a file created and removed at once; ``None`` if it cannot be."""
    try:
        for stale in directory.glob(_PROBE_PREFIX + "*"):
            try:
                if abs(time.time() - stale.stat().st_mtime) > _PROBE_DEBRIS_AGE_S:
                    stale.unlink()
            except OSError:
                pass
    except OSError:
        pass
    name = directory / f"{_PROBE_PREFIX}{os.getpid()}-{time.time_ns()}"
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


#: The racy window used where the clock cannot be trusted to be this machine's:
#: a share that cannot be probed, or a different device under the source.  A
#: file edited within this long of being hashed is hashed again (a cost), so a
#: clock that is off by less than this cannot hide an edit (a risk).
_UNPROBED_WINDOW_NS = 3600 * 1_000_000_000


def fs_clock_offset_ns(
    source: Path,
    *,
    is_network: Any = _is_network_fs,
    probe: Any = _probe_fs_clock,
    note: Any = print,
) -> int:
    """Just the offset of :func:`fs_clock_probe` (for callers that need nothing else)."""
    return int(fs_clock_probe(source, is_network=is_network, probe=probe, note=note)[0])


def fs_clock_probe(
    source: Path,
    *,
    is_network: Any = _is_network_fs,
    probe: Any = _probe_fs_clock,
    note: Any = print,
) -> tuple[int, bool]:
    """``(offset_ns, unprobed)``: the source filesystem's clock minus this machine's.

    ``unprobed`` is True on a network share whose clock could not be measured
    (no write access): the machine-clock rule is then the one thing known to be
    wrong there, so the caller widens the racy window instead of using it.

    The racy rule (D6) needs "now" in the clock that stamps the files.  On a
    local disk that is this machine's; on a share it is the server's, and a
    server running an hour behind makes every freshly edited file look old
    enough to trust -- a same-size edit within one timestamp granule then keeps
    its stale hash.  A probe file measures the difference; it is made only on a
    network filesystem, so a local source tree is never written to.
    """
    if not is_network(source):
        return 0, False
    before = time.time_ns()
    stamp = probe(source)
    if stamp is None:
        note("Note: the source is on a network share and its clock could not be measured "
             "(no write access); a file is trusted from the hash cache only when it is more "
             "than an hour older than its hash, and --payload-rehash re-checks everything")
        return 0, True
    offset = int(stamp) - before
    return (0 if -_CLOCK_BEHIND_NOISE_NS < offset < _CLOCK_SKEW_NOISE_NS else offset), False


def build_manifest(
    candidates: dict[str, Candidate],
    source: Path,
    cache: dict[str, dict[str, int | str]],
    min_size: int,
    *,
    quiet: bool = False,
    rule: str = "size",
    ratio: float | None = None,
    clock_offset_ns: int = 0,
    racy_window_ns: int | None = None,
    root_dev: int | None = None,
    checkpoint: Callable[[dict[str, dict[str, int | str]]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, int | str]]]:
    """Hash every payload file; return ``(manifest, updated cache)``.

    ``checkpoint`` receives the cache so far about once a minute, so a pass that
    is killed (not merely interrupted) keeps what it hashed: a 131 GiB tree takes
    minutes to hash and the result is only useful as a whole.

    ``clock_offset_ns`` is the source filesystem's clock minus this machine's
    (:func:`fs_clock_offset_ns`).  The racy rule compares a file's mtime with the
    moment it was hashed, and an mtime is written by the *filesystem's* clock, so
    the moment is recorded in that clock's domain.  ``racy_window_ns`` is how much
    older than its hash a file must be to be trusted; a file on a device other
    than ``root_dev`` (a mount under the source) gets the wide window, because
    its clock was not probed.  The cache key also holds the device and the
    inode change time, so an in-place edit that puts the old mtime back (which
    still moves ``ctime``) is a new file, not a trusted one.
    """
    entries: list[dict[str, Any]] = []
    fresh: dict[str, dict[str, int | str]] = {}
    total = len(candidates)
    stale = 0
    # Files whose filesystem reports no change time distinct from the mtime
    # (the FAT family; any volume that answers the query with the mtime).
    coarse = [0]

    def vouched(rel: str) -> tuple[dict[str, int | str], bool]:
        """The cache entry for *rel* and whether it may stand in for a fresh hash."""
        size, mtime_ns, _mode, ino, *rest = candidates[rel]
        dev = rest[0] if rest else None
        ctime_ns = rest[1] if len(rest) > 1 else None
        old = cache.get(rel) or {}
        hashed_at = old.get("at")
        window = _RACY_WINDOW_NS if racy_window_ns is None else racy_window_ns
        if root_dev is not None and dev is not None and dev != root_dev:
            window = max(window, _UNPROBED_WINDOW_NS)
        if mtime_ns % 1_000_000_000 == 0:
            # A whole second is a coarse clock (FAT's two seconds, HFS+'s one):
            # an edit can share the stamp of the one that was hashed, so the
            # margin doubles rather than the entry being distrusted forever,
            # which re-read the whole tree on every run of such a volume.
            window *= 2
        if ctime_ns is not None and ctime_ns == mtime_ns:
            # The filesystem has no change time of its own: the FAT family
            # keeps one timestamp in the directory entry, and a volume or API
            # that reports the modification time back as the change time does
            # the same.  A same-size in-place rewrite with the mtime put back
            # then leaves *every* field of the key unchanged and the cache
            # vouches for the old hash (measured on vfat, REVIEW.iq3_s D2).
            # Treat it like an unprobed share: the one-hour margin, so a
            # scheduled run in steady state re-hashes nothing but an edit
            # inside the hour cannot hide.
            window = max(window, _UNPROBED_WINDOW_NS)
            coarse[0] += 1
        trusted = (
            old.get("size") == size
            and old.get("mtime_ns") == mtime_ns
            and old.get("ino") == ino
            and isinstance(old.get("sha256"), str)
            and isinstance(hashed_at, int)
            and mtime_ns < hashed_at - window
            # A timestamp taken in this machine's clock says nothing about a
            # skewed share's, so it cannot vouch for a file there.
            and (not clock_offset_ns or old.get("fs") is True)
            # ctime and device are new to the key: an entry written before them
            # adopts the current values (below); one that has them must match.
            and (ctime_ns is None or old.get("ctime") in (None, ctime_ns))
            and (dev is None or old.get("dev") in (None, dev))
        )
        return old, trusted

    # What will really be read, so the progress line's rate and estimate describe
    # the hashing and not the files the cache vouches for.
    to_hash = 0 if quiet else sum(
        candidates[rel][0] for rel in candidates if not vouched(rel)[1]
    )
    hashed = 0
    started_at = last_pulse = last_checkpoint = time.monotonic()
    for index, rel in enumerate(sorted(candidates), 1):
        size, mtime_ns, mode, ino, *rest = candidates[rel]
        dev = rest[0] if rest else None
        ctime_ns = rest[1] if len(rest) > 1 else None
        old, trusted = vouched(rel)
        if trusted:
            digest = str(old["sha256"])
            fresh[rel] = dict(old)
            if ctime_ns is not None:
                fresh[rel]["ctime"] = ctime_ns
            if dev is not None:
                fresh[rel]["dev"] = dev
        else:
            stale += isinstance(old.get("sha256"), str)
            if not quiet:
                print(f"hashing [{index}/{total}] {rel} ({human_bytes(size)})", flush=True)
            started = time.time_ns() + clock_offset_ns
            digest = _sha256_file(source / rel)
            hashed += size
            now = time.monotonic()
            if not quiet and now - last_pulse >= _PULSE_SECONDS:
                last_pulse = now
                print(_progress_line(hashed, to_hash, now - started_at, "hashed"), flush=True)
            if checkpoint is not None and now - last_checkpoint >= _CHECKPOINT_SECONDS:
                last_checkpoint = now
                checkpoint({**cache, **fresh, rel: {
                    "size": size, "mtime_ns": mtime_ns, "ino": ino, "sha256": digest,
                    "at": started, **({"ctime": ctime_ns} if ctime_ns is not None else {}),
                    **({"dev": dev} if dev is not None else {}),
                    **({"fs": True} if clock_offset_ns else {}),
                }})
            fresh[rel] = {
                "size": size, "mtime_ns": mtime_ns, "ino": ino, "sha256": digest, "at": started,
            }
            if ctime_ns is not None:
                fresh[rel]["ctime"] = ctime_ns
            if dev is not None:
                fresh[rel]["dev"] = dev
            if clock_offset_ns:
                fresh[rel]["fs"] = True
        entries.append(
            {"path": rel, "size": size, "mode": mode, "sha256": digest, "mtime_ns": mtime_ns}
        )
    if coarse[0] and not quiet:
        print(
            f"Note: {coarse[0]} file(s) are on a filesystem with no change time of its own "
            "(FAT/exFAT, or a volume that answers with the modification time); a cached hash "
            "for them is trusted only after an hour, and --payload-rehash re-checks everything",
            flush=True,
        )
    if stale and not quiet:
        print(
            f"Note: {stale} file(s) were hashed again because their size, timestamp or inode "
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
    if rule == "auto" and ratio is not None:
        # Which cut-off produced this snapshot, recorded beside the rule it
        # belongs to: two machines whose cut-offs differ move files in the
        # 4-64 MiB band between git and the payload on alternate runs, and
        # the fresh-vault guard can only see the difference if it is here
        # (ROADMAP T6).  check_manifest_shape validates it.
        manifest["ratio"] = ratio
    return manifest, fresh
