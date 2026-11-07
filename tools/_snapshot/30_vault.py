

def resolve_paths(source: Path, vault: Path) -> tuple[Path, Path]:
    source = source.expanduser().resolve()
    vault = vault.expanduser().resolve()

    if not source.is_dir():
        raise SnapshotError(f"source directory does not exist: {source}")
    if vault == source:
        raise SnapshotError("vault and source must be different directories")
    if source in vault.parents:
        raise SnapshotError(
            f"vault must live outside the source directory, or the snapshot "
            f"would swallow itself:\n  vault:  {vault}\n  source: {source}"
        )
    if vault in source.parents:
        raise SnapshotError(
            f"source must not live inside the vault:\n"
            f"  source: {source}\n  vault:  {vault}"
        )
    return source, vault


#: How long to wait before the one retry of a `git init` that failed silently.
_INIT_RETRY_SECONDS = 1.0


def ensure_vault(vault: Path, branch: str) -> Path:
    """Create the vault repository if it does not exist yet."""
    git_dir = vault / ".git"
    if git_dir.is_dir():
        return git_dir

    vault.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        proc = _run(
            ["git", "init", "-q", str(vault)],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        detail = (proc.stderr or proc.stdout).strip()
        # `git init` is safe to repeat.  Once, under the load of a full test run
        # on Windows, it exited 1 and printed nothing (ROADMAP W7); with nothing
        # said there is nothing to act on, so it is tried again after a pause.
        if proc.returncode == 0 or detail or attempt == 2:
            break
        time.sleep(_INIT_RETRY_SECONDS)
    if proc.returncode != 0:
        raise SnapshotError(
            f"git init failed in {vault} (rc={proc.returncode})\n"
            + (detail or "git printed nothing, even on a second try; another process (a virus "
               "scanner, a sync client) may be holding the directory. Run again.")
        )
    # Name the branch with `symbolic-ref`, not `git init -b`.  `-b` is Git 2.28
    # and this tool is documented to run on Git 2.20 (USER_GUIDE section 5); the
    # initial branch is only an unborn-HEAD symref, so pointing it is the same
    # operation on every version, and it keeps the documented floor honest
    # instead of raising it to match an implementation detail.  For the same
    # reason `init.defaultBranch` (also 2.28) cannot be the mechanism here.
    proc = _run(
        ["git", "-C", str(vault), "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"could not name the vault's initial branch {branch!r} "
            f"(rc={proc.returncode})\n{(proc.stderr or proc.stdout).strip()}"
        )
    return git_dir


def _config_key(key: str) -> str:
    """*key* as ``git config --list`` prints it: section and name lower case, a
    subsection as written."""
    section, dot, rest = key.partition(".")
    middle, dot2, name = rest.rpartition(".")
    if not dot:
        return key.lower()
    return f"{section.lower()}.{middle}.{name.lower()}" if dot2 else f"{section.lower()}.{rest.lower()}"


def _config(path: Path, key: str) -> str:
    """Read a git config value, with the usual global/system fallbacks.

    The last value of *key* across every scope, as ``git config --get`` answers --
    from one ``--null --list`` per directory, kept until a git call may write config
    (``_CONFIG_READS``).  A listing that fails falls back to asking for the key.
    """
    signature = config_file_signature(path)
    cache_key = ("all", str(path))
    kept = _CONFIG_READS.get(cache_key)
    values = kept[1] if kept is not None and signature is not None and kept[0] == signature else None
    if values is None:
        listing = _run(
            ["git", "-C", str(path), "config", "--null", "--list"],
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if listing.returncode == 0:
            values = {}
            for entry in listing.stdout.split("\0"):
                if entry:
                    name, _sep, value = entry.partition("\n")
                    values[name] = value
            if signature is not None:
                _CONFIG_READS[cache_key] = (signature, values)
    if values is not None:
        return values.get(_config_key(key), "").strip()
    proc = _run(
        ["git", "-C", str(path), "config", "--get", key],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return proc.stdout.strip()


def ensure_identity(
    vault: Path,
    source: Path,
    vault_config: dict[str, list[str]] | None = None,
    write_identity: bool = True,
) -> None:
    """Give the vault a committer identity, inheriting the source repo's.

    A vault created by ``git init`` has none of its own, so it falls back to the
    global identity.  When that is missing too, ``commit-tree`` dies with a bare
    "Author identity unknown" -- so resolve it up front and prefer the source
    repository's own identity, which is whose code this is.

    Only the repositories' *own* config is consulted when deciding whether to
    inherit.  ``_config`` cannot be used for that decision: it reads through to
    the global file, so it reports an identity for a vault that has none, and
    the source repository is then never consulted.  That was a real bug -- the
    inheritance worked only in the test, which cleared the global config.

    Both configs are read in one call each (see :func:`_read_local_config_map`),
    and a value that is already correct is not rewritten -- the common case
    after the first run is that there is nothing to do.  Pass ``vault_config``
    (a map already read for this vault) to avoid a second read of it.
    """
    if vault_config is None:
        vault_config = _read_local_config_map(vault)
    source_config = _read_local_config_map(source)
    for key in ("user.name", "user.email"):
        have = vault_config.get(key) or []
        if have and have[0]:
            continue
        inherited = (source_config.get(key) or [""])[0]
        if not inherited:
            # No local identity anywhere.  A global one is fine -- git resolves
            # it at commit time -- but if there is none either, fail here with
            # an actionable message rather than inside commit-tree.
            if _config(vault, key):
                continue
            raise SnapshotError(
                f"no git identity is configured for the vault.\n"
                f"Set one and retry:\n"
                f"  git -C \"{vault}\" config user.name  \"Your Name\"\n"
                f"  git -C \"{vault}\" config user.email \"you@example.com\""
            )
        if not write_identity:
            # A --dry-run validates the identity is resolvable without
            # writing any: a preview must not mutate the vault, but
            # surfacing a missing identity early is part of the preview.
            continue
        proc = _run(
            ["git", "-C", str(vault), "config", key, inherited],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise SnapshotError(
                f"could not set {key} on the vault (rc={proc.returncode})\n"
                f"{(proc.stderr or proc.stdout).strip()}"
            )


BYTE_EXACT_MARKER = "* -text -filter -ident -working-tree-encoding"

BYTE_EXACT_ATTRIBUTES = (
    "# Written by seafile_snapshot.py.  The vault exists to hold byte-exact\n"
    "# copies of the source tree, so every path is marked non-text and\n"
    "# filter-free.  info/attributes has the highest precedence in Git's\n"
    "# attribute chain, so this outranks any .gitattributes in the source tree.\n"
    "#\n"
    "#   -text    end-of-line conversion, on add and on checkout.\n"
    "#   -filter  clean/smudge filters -- Git LFS above all.  Without this, a\n"
    "#            source 'filter=lfs' attribute makes `git add` store a ~130\n"
    "#            byte pointer instead of the file, and the real bytes are\n"
    "#            uploaded nowhere, because the vault has no LFS remote.\n"
    "#            Silent data loss in a tool whose job is a full backup.\n"
    "#   -ident   $Id$ keyword expansion.\n"
    "#   -working-tree-encoding\n"
    "#            a source '*.txt working-tree-encoding=UTF-16LE' makes `git add`\n"
    "#            store the file re-encoded as UTF-8, and a restore then differs\n"
    "#            from the original.\n"
    "* -text -filter -ident -working-tree-encoding\n"
)


def ensure_byte_exact(
    vault: Path,
    git_dir: Path,
    vault_config: dict[str, list[str]] | None = None,
) -> None:
    """Stop git from rewriting line endings inside the vault.

    Git for Windows ships ``core.autocrlf=true`` in its *system* config, so a
    freshly initialised vault converts LF to CRLF on checkout even though
    ``git config --global core.autocrlf`` reports nothing.  The stored blob is
    correct; the *restored* file is not -- and a backup whose restore differs
    from the original is not a backup.

    Both levers are pulled: the config, and an ``info/attributes`` override that
    outranks any ``.gitattributes`` in the source tree.

    The config values are only written when they are not already correct,
    so a vault that has been snapshotted before costs no *writes* here (one
    config read -- see :func:`_read_local_config_map`).  Reading the *local*
    config is what makes that safe: a global ``core.autocrlf`` is irrelevant,
    because the vault has to pin its own.
    """
    if vault_config is None:
        vault_config = _read_local_config_map(vault)
    # core.longpaths is not a line-ending lever: Git for Windows refuses a path
    # over 260 characters unless it is set, and a deep source tree (or a restore
    # into a deep destination) would otherwise fail for a reason unrelated to
    # the data.  Harmless where the limit does not exist.
    # core.quotepath=false ensures international / UTF-8 filenames (e.g. Thai,
    # CJK, accented Latin) are displayed cleanly in git status and diff output
    # rather than C-quoted with octal escapes (\340\270...).
    # core.fsmonitor/core.untrackedCache: the vault inherits the *global*
    # config, and a global fsmonitor=true left a detached fsmonitor--daemon
    # watching the source tree after the tool exited (measured on Windows) --
    # the source is meant to be only ever read.  Both watchers are pinned off
    # for the vault's own commands; a user who wants them for their own git
    # still has them everywhere else.
    for key, wanted in (("core.autocrlf", "false"), ("core.safecrlf", "false"),
                        ("core.longpaths", "true"), ("core.quotepath", "false"),
                        ("core.fsmonitor", "false"), ("core.untrackedCache", "false")):
        # The map's keys are as `config --list` prints them, lower case: looking up
        # `core.untrackedCache` as written never matched, and every run rewrote it.
        have = (vault_config.get(_config_key(key)) or [""])[0]
        if have == wanted:
            continue
        git(git_dir, vault, ["config", key, wanted], cwd=vault)

    attributes = git_dir / "info" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    existing = attributes.read_text(encoding="utf-8") if attributes.exists() else ""
    # Checked on the marker line, not on "-text": a vault created by an earlier
    # version carries only "* -text", and appending is safe because a later line
    # overrides an earlier one within the same attributes file.
    if BYTE_EXACT_MARKER not in existing:
        attributes.write_text(existing + BYTE_EXACT_ATTRIBUTES, encoding="utf-8")
