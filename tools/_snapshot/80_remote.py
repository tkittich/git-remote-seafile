

def source_remote_urls(source: Path) -> list[tuple[str, str]]:
    """``(remote_name, url)`` for every URL the source repository can push to.

    Read with ``--local``: a global ``insteadOf`` or a credential helper is not
    what the user means by "my code remote", and a repository that is not a git
    repository at all simply has no remotes.
    """
    proc = _run(
        [
            "git",
            "-C",
            str(source),
            "config",
            "--local",
            "--get-regexp",
            r"^remote\..*\.(url|pushurl)$",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    found: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        value = value.strip()
        if not value or not key.startswith("remote."):
            continue
        name = key[len("remote.") :].rsplit(".", 1)[0]
        found.append((name, value))
    return found


def normalise_remote(url: str) -> str:
    """Reduce a remote URL to something two spellings of it agree on.

    Deliberately loose.  A trailing slash, a trailing ``.git`` and the case of
    the scheme are all things that differ between two spellings of one remote,
    and the two error directions are not symmetric: a false *match* produces a
    clear message plus an override flag, while a false *miss* publishes the
    secrets.  So it errs towards matching.
    """
    text = url.strip().rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    scheme, sep, rest = text.partition("://")
    if sep:
        text = f"{scheme.lower()}://{rest}"
    return text.rstrip("/").lower()


def check_remote_is_not_a_code_remote(
    source: Path, remote: str, *, allowed: bool
) -> None:
    """Refuse to push the vault to a remote the source repository already uses.

    A separate *local* repository keeps the snapshot out of the code
    repository's object database.  It does **not** keep it off the code remote's
    *server*: point the vault at the same library and one push publishes
    ``.env``, keys and everything else the code repository is careful to
    exclude.  Nothing else in the design prevents that -- this check is the only
    thing standing between "a separate repository" and "the same backup".
    """
    if allowed:
        return
    target = normalise_remote(remote)
    for name, url in source_remote_urls(source):
        if normalise_remote(url) != target:
            continue
        raise SnapshotError(
            f"refusing to push the vault to '{remote}': that is the same place "
            f"as the source repository's '{name}' remote.\n"
            f"A snapshot contains the files .gitignore keeps *out* of the code "
            f"repository -- .env, keys, credentials -- so pushing it there "
            f"would publish them.\n"
            f"Point the vault at a different library, or pass "
            f"--allow-shared-remote if that is genuinely what you want."
        )


def diff_stat(git_dir: Path, work_tree: Path, parent: str, tree: str) -> str:
    proc = git(
        git_dir,
        work_tree,
        ["diff", "--stat", "--stat-count=35", "--no-color", parent, tree],
        cwd=work_tree,
        check=False,
    )
    return proc.stdout.rstrip()


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def push_snapshot(
    git_dir: Path,
    work_tree: Path,
    remote: str,
    branch: str,
    commit: str,
    tree: str,
    base: str | None,
    message: str,
) -> None:
    """Push the snapshot, re-parenting once if the remote moved under us.

    Two machines snapshotting the same vault can race between this run's fetch
    and its push; the helper rejects the loser with a non-fast-forward.  Since
    a snapshot is just a tree, the loser re-commits that tree on top of the new
    remote tip and pushes again -- no data is lost and the chain stays linear.
    Whether the remote actually moved is decided by *fetching the tip again*,
    not by matching words in git's output: the rejection text is
    locale-dependent, and any other failure deserves the same check.

    On any failure the local branch is rolled back to what the remote actually
    has, so the vault never claims a snapshot the remote never took.  That
    rollback is also what makes a plain re-run the correct remedy: the next
    run fetches the true tip and appends to it.  It runs even when the retry
    itself fails -- a fetch dying mid-retry is no reason to leave the branch
    advanced past a commit the server refused.
    """
    spec = f"refs/heads/{branch}:refs/heads/{branch}"
    current = commit
    try:
        for attempt in (1, 2):
            print(f"pushing to {remote} ...")
            # live_stderr: the first push of a large vault runs for hours, and
            # after it succeeds the helper auto-compacts the repository -- its
            # notice goes to stderr, which we stream instead of buffering so a
            # second long silence does not read as a dead run.
            proc = git(
                git_dir,
                work_tree,
                ["push", remote, spec],
                cwd=work_tree,
                check=False,
                status=f"pushing to {remote}",
                live_stderr=True,
                stall_limit=None,
            )
            if proc.returncode == 0:
                print("push complete")
                return

            sys.stdout.write(proc.stdout or "")
            if proc.stderr is not None and proc.stderr:
                sys.stderr.write(proc.stderr)

            if attempt == 1:
                try:
                    tip = fetch_remote_tip(git_dir, work_tree, remote, branch)
                except SnapshotError as fetch_failure:
                    # The remote cannot be re-read; the rollback below still
                    # restores the last state the server is known to have.
                    sys.stderr.write(
                        f"could not re-check the remote tip ({fetch_failure}); "
                        f"rolling the local branch back\n"
                    )
                    break
                if tip and tip != base:
                    print(
                        f"remote moved to {tip[:12]}; re-parenting the snapshot "
                        f"and retrying"
                    )
                    rebased = commit_tree(git_dir, work_tree, tree, tip, message)
                    set_branch(git_dir, work_tree, branch, rebased, current)
                    current, base = rebased, tip
                    continue
            break
    except SnapshotError as retry_failure:
        # A failure while re-parenting (the fetch, commit-tree, or the branch
        # move) must not skip the rollback: the push failed, so the branch is
        # pointing at a commit the remote never took.
        sys.stderr.write(
            f"the push retry failed ({retry_failure}); rolling the local "
            f"branch back\n"
        )

    try:
        if base:
            set_branch(git_dir, work_tree, branch, base, current)
            rolled_back = base[:12]
        else:
            if delete_branch(git_dir, work_tree, branch, current):
                rolled_back = "(deleted)"
            else:
                rolled_back = f"(not deleted: the branch moved and is no longer at {current[:12]})"
    except SnapshotError as rollback_failure:
        raise SnapshotError(
            f"push to {remote} failed, and the rollback failed too "
            f"({rollback_failure}).  The vault branch may point at a commit "
            f"the remote does not have; re-run with --reset-to-remote to "
            f"adopt the remote chain."
        ) from rollback_failure
    raise SnapshotError(
        f"push to {remote} failed.\n"
        f"The local branch was rolled back to {rolled_back} -- the snapshot "
        f"this run attempted was discarded.  The remote may still be ahead "
        f"of that point (it moved during the retry), so re-run to reconcile "
        f"with its tip; the tree itself was never modified."
    )
