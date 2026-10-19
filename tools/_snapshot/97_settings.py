

def _read_config_key(git_dir: Path, key: str) -> str:
    """A single value out of the *vault's own* config, or ''.

    Kept for one-off reads and for tests; the snapshot path itself reads every
    key it needs in a single call via :func:`_read_local_config_map`.
    """
    proc = _run(
        ["git", "--git-dir", str(git_dir), "config", "--local", "--get", key],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return proc.stdout.strip()


def _read_local_config_map(repo: Path) -> dict[str, list[str]]:
    """Every key in a repository's *own* config, in a single git call.

    A run consults five keys in :func:`remembered_settings` and two more in
    :func:`ensure_identity`, and every ``git config`` is a separate process --
    about 125 ms on Windows, nearly all of it process start-up.  One
    ``--local --null --list`` answers them all at once.

    ``--null`` is what makes the output unambiguous: a value may itself contain
    a newline, so each pair is NUL-*terminated* rather than newline-separated.
    ``--local`` keeps the global, system and command-scope values out, which is
    the question every caller here is actually asking -- what does *this*
    repository remember?  ``-C`` rather than ``--git-dir`` because a source
    repository may use a ``.git`` *file* (a submodule or a linked worktree),
    which ``--git-dir`` cannot open.
    """
    proc = _run(
        ["git", "-C", str(repo), "config", "--local", "--null", "--list"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return {}
    found: dict[str, list[str]] = {}
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        key, sep, value = entry.partition("\n")
        if sep:
            found.setdefault(key, []).append(value)
    return found


#: Sentinel for :func:`remember_settings`: "this setting was suppressed for
#: *this* run -- leave whatever the vault remembers alone".  Distinct from
#: ``None``, which means "the setting is empty; clear it".
_UNSET = object()


def _same_config_value(stored: str, desired: str) -> bool:
    """Whether two spellings of the path-valued ``snapshot.source`` agree.

    git stores config values verbatim -- backslashes and all -- so a value
    written by this tool round-trips exactly.  The normalisation covers the
    other writers: a value typed into the config file by hand (or by a shell
    whose path mangling rewrites separators) may spell the same directory with
    ``/`` where this tool wrote ``\\``.  Only ``snapshot.source`` holds a
    filesystem path; for every other key a backslash is data, so they are
    compared literally (see :func:`remember_settings`).
    """
    return stored.replace("\\", "/") == desired.replace("\\", "/")


class _Remembered(TypedDict, total=False):
    """What a vault remembers, keyed exactly as :func:`remember_settings` writes it.

    ``total=False`` because a vault remembers only what it has been told: a
    fresh vault has written nothing, and a key stays absent until a run sets it.
    A ``TypedDict`` rather than ``dict[str, object]`` so that the resolution
    chain reads ``str | None`` / ``list[str] | None`` out of ``.get`` instead of
    ``object``, which is what the defaults are actually made of.
    """

    remote: str
    code_remote: str
    branch: str
    exclude: list[str]


def remembered_settings(vault: Path, source: Path) -> _Remembered:
    """What the vault remembers from its previous run, if anything.

    The vault is the bottom of the resolution chain, and it is also where every
    resolved choice is *written* at the end of a run -- so ``seafile_snapshot.py
    --source C:/code/app`` once, then ``seafile_snapshot.py`` with no arguments
    at all from any directory, does the same thing again.

    A vault belongs to exactly one source directory, so a mismatch here is a
    mistake worth stopping for rather than silently snapshotting the wrong tree
    into it.  Passing ``--vault`` and ``--source`` together *is* that mismatch,
    so the refusal names both and the flag that fixes it.
    """
    git_dir = vault / ".git"
    if not git_dir.is_dir():
        return {}

    config = _read_local_config_map(vault)

    def first(key: str) -> str:
        values = config.get(key)
        return values[0] if values else ""

    remembered_source = first("snapshot.source")
    if remembered_source:
        resolved = Path(remembered_source).expanduser().resolve()
        if resolved != source.resolve():
            raise SnapshotError(
                f"this vault belongs to a different source directory:\n"
                f"  vault    : {vault}\n"
                f"  remembers: {resolved}\n"
                f"  this run : {source.resolve()}\n"
                f"A vault holds one linear chain of snapshots of one tree; "
                f"snapshotting a second tree into it would mix the two.\n"
                f"Point --source at the remembered directory, or give this tree "
                f"its own --vault."
            )

    remembered: _Remembered = {}
    remote = first("snapshot.remote")
    if remote:
        remembered["remote"] = remote
    code_remote = first("snapshot.coderemote")
    if code_remote:
        remembered["code_remote"] = code_remote
    branch = first("snapshot.branch")
    if branch:
        remembered["branch"] = branch
    excludes = [v for v in config.get("snapshot.exclude", []) if v]
    if excludes:
        remembered["exclude"] = excludes
    return remembered


def _vault_remembers(vault: Path, source: Path) -> bool:
    """Whether ``vault`` already exists and records exactly ``source`` as its tree."""
    if not (vault / ".git").is_dir():
        return False
    stored = _read_local_config_map(vault).get("snapshot.source") or []
    return bool(stored) and _same_config_value(stored[0], str(source))


def remember_settings(
    vault: Path,
    source: Path,
    *,
    remote: object,
    code_remote: object,
    exclude: list[str],
    branch: str,
    vault_config: dict[str, list[str]] | None = None,
) -> None:
    """Persist the resolved choices into the vault's own config.

    Written with ``git config --local``, so it lives and travels with the vault
    and nothing is put in the user's global config.

    ``remote`` and ``code_remote`` accept three kinds of value: a string (the
    setting to remember), ``None`` (the setting is empty for this run -- clear
    what was remembered), or :data:`_UNSET` (the setting was *suppressed* for
    this run, e.g. by ``--no-push`` -- leave what the vault remembers alone, so
    a one-off local run does not unhook a scheduled job from its remote).

    Only a value that actually *changed* is written, and a value that is being
    cleared is ``--unset-all``'d.  Re-writing every key on every run would be
    correct but wasteful: each ``git config`` is its own process, and a routine
    run has nothing new to say.  The five-and-more writes a first run makes
    collapse to none once the vault is settled -- measured on this machine, the
    config plumbing was a little over half of a run's subprocess calls.

    A single-valued key is set with a plain ``git config <key> <value>``, which
    replaces whatever was there; only a key that is being *emptied* needs the
    explicit ``--unset-all``.
    """
    settings: list[tuple[str, list[str], bool]] = [
        # (key, values this run, compare path-normalised)
        ("snapshot.source", [str(source)], True),
        ("snapshot.remote", [remote] if isinstance(remote, str) and remote else [], False),
        (
            "snapshot.coderemote",
            [code_remote] if isinstance(code_remote, str) and code_remote else [],
            False,
        ),
        ("snapshot.branch", [branch], False),
        # Repeated --exclude flags would otherwise persist the same pattern
        # twice; the stored list is what --default-excludes and the printed
        # report read back, so keep it a set of unique values in first-seen
        # order (SNAP-12).
        ("snapshot.exclude", list(dict.fromkeys(exclude)), False),
    ]
    suppressed = {key for key, value in (("snapshot.remote", remote),
                                         ("snapshot.coderemote", code_remote))
                  if value is _UNSET}

    if vault_config is None:
        vault_config = _read_local_config_map(vault)

    def unchanged(stored: list[str], values: list[str], path_key: bool) -> bool:
        if len(stored) != len(values):
            return False
        if path_key:
            return all(
                _same_config_value(a, b) for a, b in zip(stored, values, strict=True)
            )
        return all(a == b for a, b in zip(stored, values, strict=True))

    def run(key: str, *args: str, ok_rc: tuple[int, ...] = (0,)) -> None:
        proc = _run(
            ["git", "-C", str(vault), "config", "--local", *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode not in ok_rc:
            # A silent failure here would leave the vault remembering a
            # previous run's settings while this run believes it has updated
            # them -- exactly the divergence the next bare run then trusts.
            raise SnapshotError(
                f"could not update the vault's memory ({key}, "
                f"rc={proc.returncode})\n{proc.stderr.strip()}"
            )

    for key, values, path_key in settings:
        if key in suppressed:
            continue
        stored = vault_config.get(key, [])
        if unchanged(stored, values, path_key):
            continue
        if not values:
            # `--unset-all` exits 5 when the key is absent; that is not an error.
            run(key, "--unset-all", key, ok_rc=(0, 5))
        elif len(values) == 1 and len(stored) <= 1:
            # A single value over a single (or absent) key: a plain set replaces
            # it.  This is the common shape, and one call instead of two.
            run(key, key, values[0])
        else:
            # Either several values now, or a multi-valued key being rewritten
            # -- git refuses to overwrite multiple values with one, so the old
            # values are cleared first or they would linger.
            run(key, "--unset-all", key, ok_rc=(0, 5))
            for value in values:
                run(key, "--add", key, value)


def source_seafile_remote(source: Path) -> str | None:
    """The one ``seafile://`` remote the source repository pushes to, if any.

    Deriving the vault's destination from the code remote is the most useful
    default available, because the two are almost always siblings -- the code
    library and its snapshot library, side by side.  It is only used when the
    *source* has exactly one such remote; two is ambiguous, and guessing wrong
    puts a whole-tree backup somewhere the user did not choose.
    """
    found: list[str] = []
    for _name, url in source_remote_urls(source):
        if url.lower().startswith("seafile://") and url not in found:
            found.append(url)
    if len(found) == 1:
        return found[0]
    return None


def derive_vault_remote(source: Path) -> str | None:
    """The natural Seafile destination for the vault of ``source``.

    ``seafile://code/myproject`` -> ``seafile://code/myproject-vault``: the same
    library and path, suffixed, so the backup sits beside the thing it backs up
    and is recognisably related to it.
    """
    # `git -C <subdir> config --local` finds the enclosing repository's remotes,
    # so a subfolder of /code/app would derive app-vault -- the vault of the
    # WHOLE tree -- adopt its chain as the base and push a snapshot of the
    # subfolder on top of it (seen: a 6807-file "deletion" diff).  The remote
    # names the repository's root, so only the root may derive from it.
    if not (source / ".git").exists():
        return None
    code = source_seafile_remote(source)
    if not code:
        return None
    return f"{code.rstrip('/') or code}-vault"


def repo_toplevel(path: Path) -> Path | None:
    """The root of the git work tree that contains ``path``, or None.

    ``rev-parse --show-toplevel`` rather than looking for ``.git`` upwards: a
    linked worktree and a submodule keep ``.git`` as a file, and for a submodule
    the answer must be the submodule's own root, not the outer repository's.
    """
    proc = _run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    top = proc.stdout.strip() if proc.returncode == 0 else ""
    if not top:
        return None
    return Path(top).resolve()
