
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
    try:
        for folder, dirs, files in os.walk(repo, onerror=fail):
            if Path(folder) == repo and ".git" in dirs:
                dirs.remove(".git")
            base = Path(folder).relative_to(repo).as_posix()
            for name in files + [d for d in dirs if os.path.islink(os.path.join(folder, d))]:
                found.add(name if base == "." else f"{base}/{name}")
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
    def key(name: str) -> str:
        return unicodedata.normalize("NFC", name)

    present = {key(name) for name in on_disk}
    skipped = skip or set()
    folded: dict[str, int] = {}
    for name in expected:
        folded[key(name).lower()] = folded.get(key(name).lower(), 0) + 1
    collided: list[str] = []
    renamed: list[str] = []
    for name in expected:
        if name in skipped or key(name) in present:
            continue
        (collided if folded[key(name).lower()] > 1 else renamed).append(name)
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
    lost = set(collided)
    return [path for path in mismatched if path not in lost]
