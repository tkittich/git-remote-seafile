

def current_tip(git_dir: Path, work_tree: Path, branch: str) -> str | None:
    proc = git(
        git_dir,
        work_tree,
        ["rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
        cwd=work_tree,
        check=False,
    )
    return proc.stdout.strip() or None


def is_ancestor(git_dir: Path, work_tree: Path, older: str, newer: str) -> bool:
    """True iff ``older`` is an ancestor of ``newer`` (equal counts).

    ``merge-base --is-ancestor`` answers 0 (yes) or 1 (no); anything else is git
    failing -- an unknown commit, a damaged object -- and is *not* "no".  Reading
    it as "no" told users their history had diverged and pointed them at
    ``--reset-to-remote``, the one flag that drops snapshots.
    """
    proc = git(
        git_dir,
        work_tree,
        ["merge-base", "--is-ancestor", older, newer],
        cwd=work_tree,
        check=False,
    )
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        raise SnapshotError(
            f"could not compare {older[:12]} with {newer[:12]}: {detail}"
        )
    return proc.returncode == 0


def set_branch(
    git_dir: Path, work_tree: Path, branch: str, commit: str, expected: str | None
) -> None:
    """Point ``refs/heads/<branch>`` at ``commit``.

    ``expected`` is the compare-and-swap guard: when given, the update only
    happens if the branch still points there, so a racing writer loses loudly
    instead of being clobbered.  ``None`` means an unconditional update, used
    when deliberately adopting the remote's history.
    """
    args = ["update-ref", f"refs/heads/{branch}", commit]
    if expected:
        args.append(expected)
    git(git_dir, work_tree, args, cwd=work_tree)


def delete_branch(git_dir: Path, work_tree: Path, branch: str, expected: str) -> None:
    git(
        git_dir,
        work_tree,
        ["update-ref", "-d", f"refs/heads/{branch}", expected],
        cwd=work_tree,
        check=False,
    )


def fetch_remote_tip(
    git_dir: Path, work_tree: Path, remote: str, branch: str
) -> str | None:
    """Fetch ``refs/heads/<branch>`` from ``remote`` and return its commit.

    ``None`` means the remote simply has no such branch yet -- a brand-new
    vault remote, which is not an error.  Any *other* fetch failure is raised:
    carrying on after a network error would build a snapshot on a stale base
    and have it rejected at push time, with the reason buried in the push.
    """
    proc = git(
        git_dir,
        work_tree,
        ["fetch", "--quiet", "--no-tags", remote, f"refs/heads/{branch}"],
        cwd=work_tree,
        check=False,
    )
    if proc.returncode != 0:
        # "No such branch on the remote" is the not-an-error case, but it
        # is decided by asking the remote, not by matching words in git's
        # output: the rejection text is locale-dependent (and differs
        # between git transports), the same reason push_snapshot refuses
        # to sniff it.  An ls-remote that succeeds and finds no ref names
        # the branch as absent; an ls-remote failure is a transport
        # problem and is raised.
        listing = git(
            git_dir,
            work_tree,
            ["ls-remote", "--heads", remote, f"refs/heads/{branch}"],
            cwd=work_tree,
            check=False,
        )
        if listing.returncode == 0 and not listing.stdout.strip():
            return None
        raise SnapshotError(
            f"could not fetch '{branch}' from {remote} (rc={proc.returncode})\n"
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    out = git(
        git_dir,
        work_tree,
        ["rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        cwd=work_tree,
        check=False,
    )
    return out.stdout.strip() or None


DIVERGENCE_HELP = (
    "the local vault and the remote have diverged: each holds snapshots the "
    "other does not.\n"
    "That happens when this machine snapshot while offline and another machine "
    "pushed to the same remote in the meantime.\n"
    "Nothing has been changed.  To resolve, either:\n"
    "  * keep the remote chain and re-snapshot this tree on top of it --\n"
    "    re-run with --reset-to-remote (the local-only snapshots are dropped,\n"
    "    but the current tree is captured again by the new snapshot); or\n"
    "  * keep this machine's chain -- push it to a different --remote, or to a\n"
    "    different --branch."
)


def reconcile_with_remote(
    git_dir: Path,
    work_tree: Path,
    branch: str,
    tip: str | None,
    *,
    reset_to_remote: bool,
    move: bool,
) -> str | None:
    """Decide which commit the next snapshot should extend, after a fetch.

    The vault is an append-only log of tree states, so the correct base is the
    *remote* tip whenever this machine can fast-forward to it.  Building on the
    local branch instead is what breaks a shared vault: a second machine starts
    from its own unrelated genesis, its push is rejected as non-fast-forward,
    and because the rejection never moves its branch back it is rejected again
    on every later run.

    Returns the commit to use as the new snapshot's parent, and (unless ``move``
    is false, as in a dry run) brings the local branch in line with it.
    """
    local = current_tip(git_dir, work_tree, branch)

    if tip is None:
        # Nothing on the remote yet: the local branch is all there is.
        return local

    if local is None or local == tip:
        if move and local is None:
            set_branch(git_dir, work_tree, branch, tip, None)
        return tip

    if is_ancestor(git_dir, work_tree, local, tip):
        # This machine is behind: fast-forward, then extend the remote chain.
        if move:
            set_branch(git_dir, work_tree, branch, tip, local)
        return tip

    if is_ancestor(git_dir, work_tree, tip, local):
        # This machine is ahead -- snapshots taken while offline.  Keep them;
        # the push fast-forwards.
        return local

    if not reset_to_remote:
        raise SnapshotError(DIVERGENCE_HELP)
    if move:
        set_branch(git_dir, work_tree, branch, tip, None)
    return tip


def commit_tree(
    git_dir: Path, work_tree: Path, tree: str, parent: str | None, message: str
) -> str:
    """Write a commit object for ``tree`` on top of ``parent``."""
    args = ["commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", message]
    return git(git_dir, work_tree, args, cwd=work_tree).stdout.strip()
