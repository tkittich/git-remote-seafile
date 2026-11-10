
def links_as_text_advice(windows: "bool | None" = None) -> str:
    """What to do about symbolic links a restore could only write as text files.

    On Windows a link needs Developer Mode; on a FAT, exFAT or NTFS volume under Linux or
    macOS the volume itself cannot hold one, and Developer Mode is no advice (ROADMAP Z44).
    """
    if os.name == "nt" if windows is None else windows:
        return "Turn on Developer Mode, or restore on Linux or macOS, to get real links"
    return "Restore onto a Linux or macOS file system (not FAT, exFAT or NTFS) to get real links"


def _volume_keeps_name(folder: Path, name: str) -> bool:
    """Create *name* in *folder* and say whether the folder lists it under exactly that name.

    In bytes, end to end: the question is what the *volume* keeps, not what this process can
    encode.  With an ASCII filesystem encoding (``LC_ALL=C``, ``PYTHONUTF8=0``) a ``str`` path
    holding ``é`` raised ``UnicodeEncodeError`` here, which is not an ``OSError``, and the whole
    restore crashed (found by the locale legs, ROADMAP Z39); and a listing decoded with
    surrogate escapes would never equal the name asked for.
    """
    base = os.fsencode(folder)
    wanted = name.encode("utf-8", errors="surrogateescape")
    try:
        before = set(os.listdir(base))
        os.close(os.open(os.path.join(base, wanted), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except OSError:
        return False  # refused (EINVAL on FAT and exFAT for a colon)
    try:
        created = set(os.listdir(base)) - before
    except OSError:
        created = set()
    # Only a name this probe made: a person's file that landed in the folder between the two
    # listings is not ours to delete (review 8eb416b, glm E1; the project's own rule 7).
    ours = {entry for entry in created if entry.startswith(_NAME_PROBE_PREFIX)}
    for leftover in ours:
        try:
            os.unlink(os.path.join(base, leftover))
        except OSError:
            pass
    # NFC on both sides: HFS+ and ZFS ``formD`` list ``é`` decomposed, and the volume that holds
    # the name must not be judged as one that does not (review 853a7a2, sonnet G7.2).
    return any(_nfc(entry) == _nfc(wanted) for entry in ours)


#: Every probe name starts with this, so only such names are ever deleted.
_NAME_PROBE_PREFIX = b"grs-probe-"


def _nfc(name: "bytes | str") -> str:
    """A listed name as comparable text: UTF-8 decoded without failing, then NFC."""
    text = name.decode("utf-8", errors="surrogateescape") if isinstance(name, bytes) else name
    return unicodedata.normalize("NFC", text)


def _fold(name: str) -> str:
    """The key under which two names are one on a volume that ignores case (and normalises)."""
    return _nfc(name).lower()


def destination_name_problem(into: Path) -> "Callable[[str], str | None] | None":
    """How to judge a name for the volume *into* lives on: a ``name -> reason`` function, or ``None``.

    Windows keeps its fixed rules (:func:`windows_name_problem`).  Elsewhere the folder is
    asked: a FAT or exFAT volume mounted on Linux or macOS refuses a colon, and FAT32
    refuses a trailing space, which stopped a restore at git's first error and left half a
    tree (ROADMAP Z44, measured).  Only the rules the volume enforces are applied; an ext4
    or APFS destination answers "no problem" after three probes and the checkout runs as before.
    """
    if os.name == "nt":
        return windows_name_problem
    return probed_name_problem(into)


def probed_name_problem(into: Path) -> "Callable[[str], str | None] | None":
    """The rules the volume under *into* enforces, found by creating names there."""
    stem = f"grs-probe-{os.getpid()}"
    refuses_chars = not _volume_keeps_name(into, f"{stem}:x")
    keeps_space = _volume_keeps_name(into, f"{stem} ")
    keeps_dot = _volume_keeps_name(into, f"{stem}.")
    # A FAT volume mounted with the default ``iocharset=ascii`` (Debian) refuses every
    # non-ASCII name, which stopped a restore at ``café`` (measured, ROADMAP Z44).
    keeps_non_ascii = _volume_keeps_name(into, f"{stem}é")
    if not refuses_chars and keeps_space and keeps_dot and keeps_non_ascii:
        return None

    def problem(rel: str) -> "str | None":
        for part in rel.split("/"):
            if refuses_chars and re.search(r'[<>:"|?*\\\x00-\x1f]', part):
                return "has a character this volume refuses"
            if not keeps_space and part.endswith(" "):
                return "ends with a space (this volume refuses or drops it)"
            if not keeps_dot and part.endswith("."):
                return "ends with a dot (this volume drops it)"
            if not keeps_non_ascii and any(ord(char) > 127 for char in part):
                return "has a character outside ASCII (this volume is mounted without a UTF-8 charset)"
        return None

    return problem


def snapshot_file_names(repo: Path, rev: str) -> list[str]:
    """Every file and link path of *rev* (what a restore is meant to write), as text."""
    listing = _run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", rev],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    names: list[str] = []
    if listing.returncode != 0:
        return names  # the checkout and the byte check report a revision that cannot be read
    for entry in listing.stdout.split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) >= 3 and fields[1] == b"blob":
            names.append(path.decode("utf-8", errors="surrogateescape"))
    return names


def names_on_disk(repo: Path) -> "set[str] | None":
    """Every file name below *repo* (not ``.git``), as the folder lists it, or ``None``.

    Listed, not opened: opening ``dot.`` on FAT or NTFS finds the file stored as ``dot``,
    which is how a name the volume changed passed the byte check (ROADMAP Z45).
    """
    found: set[str] = set()

    def fail(error: OSError) -> None:
        raise error  # os.walk skips a folder it cannot read; here that would read as "every name is missing"

    if not repo.is_dir():
        return None
    # In bytes off Windows, like the probe: a ``str`` walk of a folder holding ``café`` under an
    # ASCII filesystem encoding lists ``caf\udcc3\udca9`` while the snapshot's names are decoded
    # as UTF-8, so every non-ASCII file read as "not written under the name given"
    # (review 853a7a2, sonnet G7.1; the Z39 fix moved the probe and left this listing).
    root: Any = str(repo) if os.name == "nt" else os.fsencode(repo)  # bytes or str: one walk, two types
    git_dir: Any = ".git" if isinstance(root, str) else b".git"
    try:
        for folder, dirs, files in os.walk(root, onerror=fail):
            if folder == root and git_dir in dirs:
                dirs.remove(git_dir)
            base = os.path.relpath(folder, root)
            links = [d for d in dirs if os.path.islink(os.path.join(folder, d))]
            for entry in [*files, *links]:
                text = entry.decode("utf-8", errors="surrogateescape") if isinstance(entry, bytes) else entry
                prefix = (base.decode("utf-8", errors="surrogateescape") if isinstance(base, bytes) else base)
                prefix = prefix.replace(os.sep, "/")
                found.add(text if prefix == "." else f"{prefix}/{text}")
    except (OSError, ValueError):
        return None  # a listing that fails says nothing: no false alarm
    return found


def classify_missing_names(
    expected: "list[str]", on_disk: "set[str]", skip: "set[str] | None" = None
) -> "tuple[list[str], list[str]]":
    """Names the snapshot holds that the folder does not list, as ``(collided, renamed)``.

    *collided*: the same name once case is ignored as another name of the snapshot, so a
    folder that ignores case could keep only one (``README`` beside ``readme``).
    *renamed*: no such twin, so the volume stored it under another name (a trailing dot
    or space dropped).  Compared in NFC, so a volume that lists names decomposed (HFS+)
    does not raise a false alarm; names in *skip* (set aside as unholdable) are not asked about.
    """
    present = {_nfc(name) for name in on_disk}
    skipped = skip or set()
    folded: dict[str, int] = {}
    for name in expected:
        folded[_fold(name)] = folded.get(_fold(name), 0) + 1
    collided: list[str] = []
    renamed: list[str] = []
    for name in expected:
        if name in skipped or _nfc(name) in present:
            continue
        (collided if folded[_fold(name)] > 1 else renamed).append(name)
    return collided, renamed


def report_names_not_restored_as_named(
    repo: Path, rev: str, skip: "set[str]", mismatched: list[str]
) -> list[str]:
    """Say which names did not come back as named, and return *mismatched* without the collided ones.

    A collision is a limit of the destination, not a defect of the checkout: the byte
    check named the loser of a pair and told the person to report it (ROADMAP Z45).
    """
    on_disk = names_on_disk(repo)
    if on_disk is None:
        return mismatched
    collided, renamed = classify_missing_names(snapshot_file_names(repo, rev), on_disk, skip)
    for names, text in (
        (collided, "share a name with another file once upper and lower case are ignored, "
                   "and the folder restored into ignores case, so only one of each pair could be "
                   "written"),
        (renamed, "were not written under the name the snapshot gives them (a trailing dot or "
                  "space is dropped by some volumes); the bytes are intact, under the changed name"),
    ):
        if names:
            shown = "\n".join(f"  {name}" for name in names[:10])
            more = f"\n  ... and {len(names) - 10} more" if len(names) > 10 else ""
            print(f"\nWarning: {len(names)} file(s) in this snapshot {text}:\n{shown}{more}\n"
                  "Restore into a folder that keeps names exactly (Linux ext4, or a case-sensitive "
                  "folder) to get them all; everything else was restored and is verified below.")
    # The byte check names the twin that was written *last* (``D/x.txt``), this listing the one
    # that is missing (``d/x.txt``): the exact spelling never matched, so the "defect, please
    # report it" text still printed beside the right warning (review 853a7a2, sonnet G7.3).
    # A pair is judged by its folded name, the key the classifier grouped it under.
    lost = {_fold(name) for name in collided}
    return [path for path in mismatched if _fold(path) not in lost]


def _free_bytes_where(into: Path) -> "int | None":
    """Free bytes on the volume *into* will be created on, or ``None`` when no ancestor answers.

    *into* usually does not exist yet, and ``shutil.disk_usage`` of a missing path raises, so the
    floor was skipped for every normal restore and every drill (review 8eb416b, deepseek G4.1).
    The nearest ancestor that exists is on the same volume the clone will land on.
    """
    probe = into
    while True:
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            if probe.parent == probe:
                return None
            probe = probe.parent


def _ensure_local_branch(repo: Path, branch: str) -> None:
    """Make *branch* a local branch of a ``--no-checkout`` clone, from ``origin/<branch>``.

    A clone has a local branch only for the remote's HEAD; ``git checkout`` makes the others on
    the way, but the name listing that decides what to write runs before it, and ``ls-tree
    <branch>`` fails for a branch that is only ``origin/<branch>``.  A branch that is already
    there, or that no remote branch backs, is left alone: the caller reports what is wrong.
    """
    have = _run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if have.returncode == 0:
        return
    _run(
        ["git", "-C", str(repo), "branch", branch, f"origin/{branch}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
