

def _scratch_history_repo(td: str) -> Path:
    """A bare scratch repository in *td* that git will not run maintenance in.

    ``git fetch`` otherwise may start ``gc --auto`` / ``maintenance`` in the
    background, which races with the directory being removed (seen on Linux and
    macOS as "Directory not empty").
    """
    repo = Path(td) / "history.git"
    made = _run(
        ["git", "init", "-q", "--bare", str(repo)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if made.returncode != 0:
        raise SnapshotError(f"could not create a scratch repository: {made.stderr.strip()}")
    for key, value in (("gc.auto", "0"), ("maintenance.auto", "false")):
        _run(
            ["git", f"--git-dir={repo}", "config", key, value],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    return repo


def _manifest_paths(repo: Path, cwd: Path, commits: "set[str]") -> "set[str]":
    """Every payload path named by the manifests of *commits* (empty for a snapshot without one)."""
    if not commits:
        return set()
    specs = "".join(f"{c}:{PAYLOAD_MANIFEST}\n" for c in sorted(commits))
    checked = git(repo, None, ["cat-file", "--batch-check"], cwd=cwd, stdin=specs).stdout
    paths: set[str] = set()
    for oid in sorted({line.split()[0] for line in checked.splitlines()
                       if len(line.split()) == 3 and line.split()[1] == "blob"}):
        try:
            data = json.loads(git(repo, None, ["cat-file", "blob", oid], cwd=cwd).stdout)
        except (ValueError, RecursionError):
            raise SnapshotError(
                f"a payload manifest in the history ({oid[:12]}) is unreadable; refusing "
                "to decide what is unreferenced"
            ) from None
        paths.update(item["path"] for item in check_manifest_shape(data, f"the payload manifest {oid[:12]}"))
    return paths


def paths_only_in_older_snapshots(repo: Path, cwd: Path, kept: "set[str]", everything: "set[str]") -> "tuple[str, ...]":
    """Payload paths that some snapshot ``--payload-keep`` leaves out names and no kept one does.

    Their stored versions stop being referenced when the older snapshots go, so retention deletes
    them: a file deleted on purpose long ago, and also one that ``--payload-skip-changing`` has
    kept out of every snapshot since (its only stored version is in an older one; review 9fe723f,
    gemini D1).  Naming the paths turns "N older snapshots become unrestorable" into what it costs.
    """
    return tuple(sorted(_manifest_paths(repo, cwd, everything - kept) - _manifest_paths(repo, cwd, kept)))


def lost_paths_line(paths: "tuple[str, ...]") -> str:
    """The sentence ``--payload-gc --payload-keep`` prints about *paths* (empty when there are none)."""
    if not paths:
        return "no payload path is held only by the older snapshots"
    shown = ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")
    return (f"{len(paths)} payload path(s) are in no kept snapshot, so their last stored version "
            f"goes with the older ones: {shown}.  A file deleted long ago is among them, and so "
            "is one that --payload-skip-changing has left out of every snapshot since")


def release_reused_marks(client: Any, rid: str, base: str, reused: "set[str]") -> int:
    """Drop the retention mark of every object a snapshot found already stored and now names.

    ``--payload-gc --payload-delete`` counts its grace period from the *mark*, so an object
    marked on day 0 that another machine's snapshot starts depending on on day 6.9 was deleted
    a tenth of a day later, before that snapshot was pushed (review 134c8af, fable-D1).  A
    snapshot that names an object has used it: the mark goes, and a later run marks it afresh if
    it is unused again.  A mark dropped too early only delays a deletion.  Best effort: a
    failure here changes nothing the run depends on, so it is never an error.  Returns how many
    marks were dropped.
    """
    if not reused:
        return 0
    try:
        text = _read_pending(client, rid, base)
        marks = json.loads(text) if text else {}
        if not isinstance(marks, dict):
            return 0
        kept = {sha: stamp for sha, stamp in marks.items() if sha not in reused}
        if len(kept) == len(marks):
            return 0
        client.upload_file(rid, base, _PENDING_NAME, encode_pending(kept))
        return len(marks) - len(kept)
    except Exception:
        return 0
