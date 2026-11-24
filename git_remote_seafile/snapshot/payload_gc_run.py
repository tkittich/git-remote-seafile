from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple

from . import common
from . import gitrun
from . import profile
from . import payload_guards
from . import payload_manifest
from . import payload_upload


# -- Payload retention ---------------------------------------------------------
#
# A snapshot run never deletes an object.  This is the one place that does, and
# it is built around the one way it could lose data: an object that *looks*
# unreferenced because the reader did not see every manifest that names it, or
# because a run that is about to name it has not pushed yet.
#
# So, in order of how much each guard matters:
#
# 1. **Two phases.**  A run with ``--payload-delete`` does not delete what it
#    finds unreferenced; it *marks* it, with this machine's clock, in a small
#    state file on the server.  A later run deletes an object only if it has been
#    marked unreferenced for the whole grace period *and is still unreferenced*.
#    An object a run reused in the meantime is referenced again by the time the
#    second run looks -- unless that run never pushed, in which case nothing names
#    it and it is garbage.  No clock comparison with the server is involved.
# 2. **Every manifest.**  It reads the manifest of every commit reachable from
#    every ref of the remote -- and of the local vault, whose un-pushed snapshots
#    may name objects already uploaded.  A manifest it cannot read, or whose
#    format it does not know, stops it; it never treats "cannot parse" as "names
#    nothing".
# 3. **No refs it cannot see.**  A ref the helper cannot list (a Seafile conflict
#    copy) would hide a snapshot, so their presence stops it.
# 4. **Only exact object names**, nothing else in the directory.
#
# Deleting frees quota at once; disk is freed only after the library's history
# expires and the server's GC runs (SNAPSHOT.md section 15.1).

#: How often the deletion loop looks at the remote's refs again: every this many
#: objects, or this many seconds, whichever comes first.
_RECHECK_EVERY = 25
_RECHECK_SECONDS = 10.0

_OBJECT_NAME = re.compile(r"^[0-9a-f]{64}$")


def _grace_now(client: Any) -> float:
    """The clock the grace period runs on: the server's ``Date`` estimate.

    The mark and the delete are different runs, often on different days, and
    the wall clock of *whichever machine happens to run the delete* decided
    both ends: a machine whose clock jumped forward eight days between the
    runs deleted an object that was marked seconds before (measured,
    REVIEW.sonnet-5.5 D3).  The server's time estimate cannot jump with
    either machine's wall clock, which is the whole property the grace period
    needs.  Entries written by older versions carry local-clock stamps; the
    error is the one-off skew between that machine's clock and the server's,
    the same bounded quantity §6.1 already records as accepted.
    """
    try:
        return float(client.get_server_time())
    except Exception:
        return time.time()


#: An object, or the temporary an interrupted upload left beside it.
_COLLECTABLE_NAME = re.compile(r"^[0-9a-f]{64}(\.part-[0-9a-f]+)?$")
DEFAULT_GRACE_DAYS = 7.0
#: A grace period shorter than this is refused: it would let a retention run
#: delete what a snapshot run, started hours earlier, is about to reference.
_MIN_GRACE_DAYS = 1.0
_PENDING_NAME = "gc-pending.json"
#: The mark list holds one ~88-byte entry per unused object, so the client's
#: 16 MiB text limit stopped a store at about 190,000 of them (measured: 87.7
#: bytes per entry) with a message that named no way out.  The file is one this
#: tool wrote, so the limit only has to stop a planted one: 64 MiB is ~760,000
#: entries.
_PENDING_MAX_BYTES = 64 * 1024 * 1024
#: A mark is stamped with the server's clock estimate (or the local clock when
#: that is unavailable); a stamp more than a day ahead of "now" was not written
#: by a run of this tool.
_PENDING_FUTURE_SLACK_SECONDS = 86400


def _read_pending(client: Any, rid: str, base: str) -> str:
    """The mark list as text ('' when there is none); too large names the way out.

    A list the folder names but the file host will not serve is an error, not
    "none": the write-back would restart every grace period (review 06cc623).
    """
    from . import remote_ops
    from git_remote_seafile.client import read_listed_bytes

    try:
        data = read_listed_bytes(client, rid, f"{base}/{_PENDING_NAME}", max_bytes=_PENDING_MAX_BYTES)
    except Exception as exc:
        if "refusing to read it" in str(exc):
            raise common.SnapshotError(
                f"{base}/{_PENDING_NAME} is larger than {remote_ops.human_bytes(_PENDING_MAX_BYTES)}, more "
                "than a store of unused objects should leave. Remove it and run again: every "
                "unused object is then marked afresh and is deleted only after a full grace "
                "period from now, so nothing is lost, only delayed"
            ) from None
        raise common.SnapshotError(f"reading gc state failed: {type(exc).__name__}: {exc}") from None
    return data.decode("utf-8", errors="replace").strip() if data else ""


def add_payload_gc_arguments(parser: Any) -> None:
    parser.add_argument(
        "--payload-delete",
        action="store_true",
        help="with payload gc: act. The first run only marks unreferenced objects; a "
        "later run deletes those still unreferenced after the grace period",
    )
    parser.add_argument(
        "--payload-keep",
        type=int,
        default=None,
        metavar="N",
        help="with payload gc: count only the newest N snapshots of each branch and tag "
        "as using objects. Objects only older snapshots need become unused and, with "
        "--payload-delete, are removed after the grace period -- those older snapshots can "
        "then no longer be restored. Default: keep every snapshot",
    )
    parser.add_argument(
        "--payload-keep-days",
        type=int,
        default=None,
        metavar="D",
        help="with payload gc: a snapshot counts as using its objects when it is the newest of a branch or tag, "
        "or among the newest --payload-keep N, or at most D days old (a commit's age comes from its committer "
        "date and from when this tool first saw it, never from a date alone). Older snapshots can then no "
        "longer be restored once their objects are deleted; the first run releases nothing for D days. "
        "Needs --payload-accept-loss with --payload-delete. Not remembered",
    )
    parser.add_argument(
        "--payload-no-vault",
        action="store_true",
        help="with payload gc --payload-delete: delete without --vault. Snapshots that "
        "exist only in a vault on some machine (not yet pushed) would then count as "
        "unreferenced, so this must be said out loud",
    )
    parser.add_argument(
        "--payload-accept-loss",
        action="store_true",
        help="with payload gc --payload-delete: go on although snapshots would become "
        "unrestorable -- the remote's history was rewritten since the last --payload-delete "
        "run (a branch or tag deleted, a force push), or --payload-keep expires older "
        "snapshots",
    )
    parser.add_argument(
        "--payload-allow-non-owner", action="store_true",
        help="with payload gc --payload-delete: delete although this account does not own "
        "the library on Seafile Professional, where a folder permission could hide snapshots "
        "from it; say so only when no folder permission applies to this account",
    )
    parser.add_argument(
        "--payload-grace-days",
        type=float,
        default=DEFAULT_GRACE_DAYS,
        metavar="DAYS",
        help=f"with payload gc: how long an object must stay unreferenced before it is "
        f"deleted (default {DEFAULT_GRACE_DAYS:g}, minimum {_MIN_GRACE_DAYS:g})",
    )


def payload_gc_conflicts(args: argparse.Namespace) -> list[str]:
    """What is wrong with the operands of ``payload verify`` and ``payload gc`` (the options they do not read are
    refused by ``cli_checks``)."""
    problems: list[str] = []
    if args.payload_verify is not None:
        if not args.remote:
            problems.append("payload verify requires --remote <url>")
        if args.payload_verify != "all" and not args.payload_verify.isdigit():
            problems.append("payload verify takes a count or 'all'")
        elif args.payload_verify != "all" and int(args.payload_verify) < 1:
            problems.append("payload verify takes a count of at least 1")
    if args.payload_gc:
        if not args.remote:
            problems.append("payload gc requires --remote <url>")
        if args.payload_grace_days < _MIN_GRACE_DAYS:
            problems.append(f"--payload-grace-days must be at least {_MIN_GRACE_DAYS:g}")
        if args.payload_keep is not None and args.payload_keep < 1:
            problems.append("--payload-keep must be at least 1")
        if args.payload_keep_days is not None and args.payload_keep_days < 1:
            problems.append("--payload-keep-days must be at least 1")
        if args.payload_delete and not args.vault and not args.payload_no_vault:
            problems.append(
                "--payload-delete needs --vault <the long-lived vault>: a snapshot that is "
                "only in a vault and was never pushed would look unreferenced. Pass "
                "--payload-no-vault if the remote really is the only copy"
            )
        if not args.payload_delete:
            for flag, value in (("--payload-no-vault", args.payload_no_vault),
                                ("--payload-allow-non-owner", args.payload_allow_non_owner),
                                ("--payload-accept-loss", args.payload_accept_loss)):
                if value:
                    problems.append(f"{flag} only applies to payload gc --payload-delete")
        elif args.payload_no_vault and args.vault:
            problems.append("--payload-no-vault and --vault contradict each other")
    return problems


class _History(NamedTuple):
    hashes: dict[str, int]       #: every referenced object and its size
    commits: int                 #: commits scanned
    manifests: int               #: distinct manifests found
    tips: dict[str, str]         #: the remote's refs now: ``refs/heads/x`` -> commit
    dropped: list[str]           #: refs seen by the last --payload-delete that lost history
    expired: int                 #: commits --payload-keep leaves out of the count
    lost_paths: tuple[str, ...] = ()  #: paths only the commits --payload-keep leaves out hold
    reachable: tuple[str, ...] = ()   #: every commit reachable from a ref (the age record is pruned to them)


def _hashes_in_history(
    remote: str | None, vault: Path | None, keep: int | None = None,
    seen: dict[str, str] | None = None, want_lost: bool = True,
    keep_days: int | None = None, commit_seen: "dict[str, float] | None" = None,
    age_now: "float | None" = None,
) -> _History:
    """Every payload object referenced from every ref of *remote* and, when given, of
    the local *vault* (which must be one).  *seen* is the ref tips the last
    --payload-delete run saw: any that the remote no longer contains are reported.
    With ``remote=None`` only the vault is read: nothing is downloaded."""
    from . import payload_history
    if vault is not None and not (vault / ".git").exists():
        raise common.SnapshotError(
            f"--vault {vault} is not a vault (it has no .git); refusing to run without the "
            "un-pushed snapshots it was meant to protect"
        )
    if vault is not None and remote is not None:
        # The vault is read below through an alternate, which no fetch walks: look for its holes here
        # (review c305b0a, fable-S2-1).  Alone (remote=None) it is walked in place further down.
        payload_guards.require_complete_history(vault / ".git", walk=True)
    with tempfile.TemporaryDirectory(prefix="grs-payload-gc-") as td:
        # Only the vault to read: use it where it is.  Fetching it into a
        # scratch repository copied the whole vault into the temp directory
        # (with no free-space check) to answer a question git can ask in place.
        in_place = remote is None and vault is not None
        repo = (vault / ".git") if in_place and vault is not None else payload_history._scratch_history_repo(td)
        if remote is not None:
            # What is deleted is decided from this fetch, so the helper must not
            # advertise a subset of the refs: a ref it can only warn about and
            # skip makes that branch's objects look unreferenced.
            previous = os.environ.get("SEAFILE_STRICT_REFS")
            os.environ["SEAFILE_STRICT_REFS"] = "1"
            try:
                gitrun.git(
                    repo, None,
                    ["fetch", "-q", "--no-tags", remote,
                     "+refs/heads/*:refs/remotes/remote/*", "+refs/tags/*:refs/remotes/remote-tags/*"],
                    cwd=Path(td), status=f"fetching the history of {remote}", stall_limit=None,
                )
            finally:
                if previous is None:
                    os.environ.pop("SEAFILE_STRICT_REFS", None)
                else:
                    os.environ["SEAFILE_STRICT_REFS"] = previous
        if vault is not None and not in_place:
            # The vault is read where it lies: with its object store as an
            # alternate the fetch below finds every object already present and
            # transfers none, where it used to copy the whole vault into the
            # temp directory (ROADMAP S7).
            alternates = repo / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_bytes((vault / ".git" / "objects").as_posix().encode("utf-8") + b"\n")   # LF: write_text wrote CRLF on Windows (fable-S2-3)
            gitrun.git(
                repo, None,
                ["fetch", "-q", "--no-tags", str(vault),
                 "+refs/heads/*:refs/remotes/local/*", "+refs/tags/*:refs/remotes/local-tags/*"],
                cwd=Path(td),
            )
        payload_guards.require_complete_history(repo, walk=in_place)
        tips: dict[str, str] = {}
        for line in gitrun.git(
            repo, None, ["for-each-ref", "--format=%(objectname) %(refname)",
                         "refs/remotes/remote/", "refs/remotes/remote-tags/"], cwd=Path(td),
        ).stdout.splitlines():
            oid, _, name = line.partition(" ")
            if name.startswith("refs/remotes/remote-tags/"):
                tips["refs/tags/" + name[len("refs/remotes/remote-tags/"):]] = oid
            elif name.startswith("refs/remotes/remote/"):
                tips["refs/heads/" + name[len("refs/remotes/remote/"):]] = oid
        dropped: list[str] = []
        for ref, old in sorted((seen or {}).items()):
            now = tips.get(ref)
            if now is None:
                dropped.append(f"{ref} was deleted")
            elif now != old and gitrun.git(
                repo, None, ["merge-base", "--is-ancestor", old, now], cwd=Path(td), check=False,
            ).returncode != 0:
                dropped.append(f"{ref} no longer contains {old[:12]}")
        expired = 0
        lost: tuple[str, ...] = ()
        everything_list = [c for c in gitrun.git(repo, None, ["rev-list", "--all"], cwd=Path(td)).stdout.split() if c]
        if keep is None and keep_days is None:
            commits = everything_list
        else:
            refs = gitrun.git(repo, None, ["for-each-ref", "--format=%(refname)"], cwd=Path(td)).stdout.split()
            kept: set[str] = set()
            if keep_days is not None:
                for ref in refs:   # rule 1: the newest snapshot of every ref (a tag is followed to its commit)
                    kept.update(gitrun.git(repo, None, ["rev-list", "--max-count=1", ref], cwd=Path(td)).stdout.split())
                if age_now is None:
                    kept.update(everything_list)     # no usable server clock: the age rule is not applied
                else:
                    kept.update(_young_commits(repo, Path(td), keep_days, commit_seen or {}, age_now))
            if keep is not None:
                for ref in refs:
                    kept.update(gitrun.git(repo, None, ["rev-list", f"--max-count={keep}", ref],
                                    cwd=Path(td)).stdout.split())
            commits = sorted(kept)
            everything = set(everything_list)
            # Only a snapshot that has a manifest can become unrestorable: one taken
            # before payload mode is whole in git and stays restorable.
            older = "".join(f"{c}:{payload_upload.PAYLOAD_MANIFEST}\n" for c in sorted(everything - kept))
            if older:
                listed = gitrun.git(repo, None, ["cat-file", "--batch-check"], cwd=Path(td),
                             stdin=older).stdout.splitlines()
                expired = sum(1 for line in listed
                              if len(line.split()) == 3 and line.split()[1] == "blob")
            # Only payload gc prints it: payload verify read every older manifest for a value it
            # never used, and one unreadable old manifest stopped it (review c305b0a, fable-S2-7).
            lost = payload_history.paths_only_in_older_snapshots(repo, Path(td), kept, everything) if expired and want_lost else ()
        if not commits:
            return _History({}, 0, 0, tips, dropped, expired, lost, tuple(everything_list))
        specs = "".join(f"{c}:{payload_upload.PAYLOAD_MANIFEST}\n" for c in commits)
        checked = gitrun.git(repo, None, ["cat-file", "--batch-check"], cwd=Path(td), stdin=specs).stdout
        blobs = sorted({
            line.split()[0] for line in checked.splitlines()
            if len(line.split()) == 3 and line.split()[1] == "blob"
        })
        hashes: dict[str, int] = {}
        for oid in blobs:
            body = gitrun.git(repo, None, ["cat-file", "blob", oid], cwd=Path(td)).stdout
            try:
                data = json.loads(body)
            except (ValueError, RecursionError):
                raise common.SnapshotError(
                    f"a payload manifest in the history ({oid[:12]}) is unreadable; refusing "
                    "to decide what is unreferenced"
                ) from None
            for item in payload_manifest.check_manifest_shape(data, f"the payload manifest {oid[:12]}"):
                hashes[item["sha256"]] = item["size"]
        return _History(hashes, len(commits), len(blobs), tips, dropped, expired, lost, tuple(everything_list))


def _young_commits(
    repo: Path, work: Path, days: int, first_seen: "dict[str, float]", now: float,
) -> "set[str]":
    """The commits at most *days* old, by the committer date and by when this tool first saw them.

    The committer date is written by the machine that committed and a rebase changes only it, so it is
    believed only as far as the first-seen time allows: a date more than a day after the commit was first
    seen is aged from the first-seen time (a clock set to 2036 does not keep its snapshots until 2036), and
    a date in the past is believed only when the tool has also known the commit that long.  Compared in code
    over every commit: ``rev-list --since`` stops at the first old one and would hide newer ancestors.
    """
    young: set[str] = set()
    listing = gitrun.git(repo, None, ["log", "--all", "--format=%H %ct"], cwd=work).stdout.splitlines()
    for line in listing:
        commit, _, stamp = line.partition(" ")
        if not commit or not stamp.strip().lstrip("-").isdigit():
            continue
        committed = float(stamp)
        first = first_seen.get(commit, now)
        effective = first if committed > first + 86400 else max(committed, first)
        if now - effective <= days * 86400:
            young.add(commit)
    return young


def _clock_usable(client: Any) -> bool:
    """Whether the server's clock can time the age rule: it sent a ``Date`` and it is near this machine's."""
    if not getattr(client, "server_time_measured", True):
        return False
    try:
        return abs(float(client.get_server_time()) - time.time()) <= payload_guards._MAX_CLOCK_SKEW_SECONDS
    except Exception:
        return False


def _remote_tips(remote: str) -> dict[str, str]:
    """``{ref: object id}`` for every branch and tag of *remote*, via the helper's listing.

    Strict, like the history fetch: a listing that would have skipped a ref is
    an error here too.
    """
    previous = os.environ.get("SEAFILE_STRICT_REFS")
    os.environ["SEAFILE_STRICT_REFS"] = "1"
    try:
        proc = gitrun._run(
            ["git", "ls-remote", "--heads", "--tags", remote],
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    finally:
        if previous is None:
            os.environ.pop("SEAFILE_STRICT_REFS", None)
        else:
            os.environ["SEAFILE_STRICT_REFS"] = previous
    if proc.returncode != 0:
        raise common.SnapshotError(
            f"could not list the refs of {remote} to re-check them before deleting: "
            f"{proc.stderr.strip()}"
        )
    tips: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        oid, _, name = line.partition("	")
        if name and not name.endswith("^{}"):
            tips[name] = oid
    return tips


def _refs_the_helper_cannot_see(client: Any, rid: str, repo_path: str) -> list[str]:
    """Names under ``refs/`` that are Seafile conflict copies (invisible to git)."""
    found: list[str] = []
    stack = [f"{repo_path}/refs"]
    while stack:
        directory = stack.pop()
        for entry in client.list_dir(rid, directory):
            name = entry.get("name", "")
            full = f"{directory}/{name}"
            if "(SFConflict" in name:
                found.append(full)
            if entry.get("type") == "dir":
                stack.append(full)
    return found


def payload_verify(
    remote: str, vault: Path | None, *, count: str, allow_low_disk: bool = False
) -> int:
    """Download a sample of the newest snapshot's objects and check their SHA-256."""
    from . import payload_restore, runreport
    library, base, server_url = payload_upload.payload_location(remote)
    client = payload_upload._api(lambda: payload_upload._open_client(server_url), "opening the Seafile client")
    rid = payload_upload._api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    _require_room_for_history(client, rid, base[: -len("/payload")], allow_low_disk)
    history = _hashes_in_history(remote, vault, 1, want_lost=False)
    sizes, manifests = history.hashes, history.manifests
    if manifests == 0:
        raise common.SnapshotError("found no payload manifest in the newest snapshot of the remote")
    shas = sorted(sizes)
    chosen = shas if count == "all" else sorted(random.sample(shas, min(int(count), len(shas))))
    print(f"verifying {len(chosen)} of {len(shas)} object(s) of the newest snapshot ...", flush=True)
    bad: list[str] = []
    total_bytes = sum(sizes[sha] for sha in chosen)
    checked = 0
    started = last_pulse = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="grs-payload-verify-") as td:
        for index, sha in enumerate(chosen, 1):
            why = payload_restore._download_verified(
                client, rid, f"{base}/{payload_upload.object_dir(sha)}/{sha}", Path(td) / sha,
                {"size": sizes[sha], "sha256": sha}, Path(td),
            )
            (Path(td) / sha).unlink(missing_ok=True)
            if why is not None:
                bad.append(f"{sha}: {why}")
            checked += sizes[sha]
            now = time.monotonic()
            if index % 10 == 0 or index == len(chosen):
                print(f"checked [{index}/{len(chosen)}]", flush=True)
            if now - last_pulse >= payload_upload._PULSE_SECONDS:
                last_pulse = now
                print(payload_upload._progress_line(checked, total_bytes, now - started, "checked"), flush=True)
    if bad:
        shown = "\n  ".join(bad[:20])
        raise common.SnapshotError(f"{len(bad)} of {len(chosen)} sampled object(s) are damaged:\n  {shown}")
    print(f"verified {len(chosen)} object(s): every sampled object matches its SHA-256")
    payload_guards.record_verification(client, rid, base, len(chosen), len(shas))
    runreport.RUN_FACTS.update(checked=len(chosen), objects=len(shas), damaged=0)
    return 0


def history_fetch_space_problem(
    client: Any, rid: str, repo_path: str, scratch: Path, *, allow_low_disk: bool
) -> str | None:
    """A reason the remote's history would not fit in *scratch*, or ``None``.

    The history is read by fetching the remote into a scratch repository, which
    downloads every pack; a full volume stops the run mid-fetch with whatever
    git says about it.  The packs' listed sizes are what the fetch will write.
    """
    from . import remote_ops
    need = sum(
        e.get("size", 0) for e in client.list_dir(rid, f"{repo_path}/objects/pack")
        if e.get("type") == "file" and isinstance(e.get("size"), int)
    )
    try:
        free = shutil.disk_usage(scratch).free
    except OSError:
        return None
    if need + profile._FREE_SPACE_MARGIN_BYTES <= free or allow_low_disk:
        return None
    return (
        f"refusing to fetch the history: the remote's packs are {remote_ops.human_bytes(need)} and "
        f"{scratch} has {remote_ops.human_bytes(free)} free ({remote_ops.human_bytes(profile._FREE_SPACE_MARGIN_BYTES)} is "
        "kept in reserve). Point TMPDIR (TEMP on Windows) at a larger volume, or pass "
        "--allow-low-disk to try anyway"
    )


def _require_room_for_history(client: Any, rid: str, repo_path: str, allow_low_disk: bool) -> None:
    """Refuse before a command that fetches the remote's history into the temp volume.

    ``payload verify`` reads the same history as retention does and is the
    command run after something looked wrong, which is when the volume is
    least likely to have room (REVIEW.af9e9e1 T1.1).
    """
    problem = payload_upload._api(
        lambda: history_fetch_space_problem(
            client, rid, repo_path, Path(tempfile.gettempdir()), allow_low_disk=allow_low_disk
        ),
        "listing the remote's packs",
    )
    if problem:
        raise common.SnapshotError(problem)


def payload_gc(
    remote: str, vault: Path | None, *, delete: bool, grace_days: float, keep: int | None = None,
    accept_loss: bool = False, from_vault: bool = False, allow_low_disk: bool = False,
    allow_non_owner: bool = False, keep_days: int | None = None,
) -> int:
    """Report, and with *delete* mark and later remove, unreferenced payload objects.

    ``from_vault`` is for the routine report that follows a push: the history is
    read from the local vault (which a pushing run has just made equal to the
    remote's tip) instead of being downloaded again, and nothing is marked or
    deleted.  It must not be combined with *delete*.
    """
    from . import remote_ops, placement, payload_history
    if from_vault and delete:
        raise common.SnapshotError("internal error: a vault-only history cannot back a deletion")
    if grace_days < _MIN_GRACE_DAYS:
        raise common.SnapshotError(f"the grace period must be at least {_MIN_GRACE_DAYS:g} day(s)")
    library, base, server_url = payload_upload.payload_location(remote)
    repo_path = base[: -len("/payload")]
    client = payload_upload._api(lambda: payload_upload._open_client(server_url), "opening the Seafile client")
    rid = payload_upload._api(lambda: client.get_repo_id(library), f"finding library {library!r}")

    conflicts = payload_upload._api(lambda: _refs_the_helper_cannot_see(client, rid, repo_path), "listing refs")
    if conflicts:
        raise common.SnapshotError(
            f"{len(conflicts)} ref(s) are Seafile conflict copies the history reader cannot "
            f"see (first: {conflicts[0]}); a snapshot reachable only from one would look "
            "unreferenced. Resolve them (keep one ref, delete the copies; git-remote-seafile doctor on the "
            "vault's URL lists every one) and run again"
        )
    partial_view = payload_guards._require_whole_history(client, rid, delete, check=not (allow_non_owner or from_vault))
    if not from_vault:
        _require_room_for_history(client, rid, repo_path, allow_low_disk)
    seen = {} if from_vault else payload_guards.read_seen_tips(client, rid, base)
    if from_vault and keep_days is not None:
        raise common.SnapshotError("internal error: a vault-only history cannot apply the age rule")
    # The age of a commit is judged on the server's clock, so its check runs before the history walk.  Every
    # delete run keeps the first-seen record (so the option, enabled later, finds its history ready); a
    # report reads it only for the age rule.
    commit_seen: dict[str, float] = {}
    seen_copies: list[str] = []
    read_now = _grace_now(client)
    age_now: float | None = None
    if keep_days is not None:
        payload_guards._require_server_clock(client, delete)
        age_now = read_now if _clock_usable(client) else None
        if age_now is None:
            print(f"Note: the server sent no usable Date, so the age rule of --payload-keep-days {keep_days} was "
                  "not applied and every snapshot is kept")
    if delete or keep_days is not None:
        commit_seen, seen_copies = payload_guards.read_commit_seen(client, rid, base, read_now)
    history = _hashes_in_history(None if from_vault else remote, vault, keep, seen,
                                 keep_days=keep_days, commit_seen=commit_seen, age_now=age_now)
    sizes_by_hash, commits, manifests = history.hashes, history.commits, history.manifests
    referenced = set(sizes_by_hash)
    if keep is not None:
        print(f"--payload-keep {keep}: only the newest {keep} snapshot(s) of each branch and "
              f"tag count as using objects; {history.expired} older snapshot(s) become "
              f"unrestorable once their objects are deleted")
        print(payload_history.lost_paths_line(history.lost_paths))
    if keep_days is not None and age_now is not None:
        print(f"--payload-keep-days {keep_days}: a snapshot counts as using objects when it is the newest of a "
              f"branch or tag"
              + (f", among the newest {keep}" if keep is not None else "")
              + f", or at most {keep_days} day(s) old; {history.expired} older snapshot(s) become "
              "unrestorable once their objects are deleted")
        if not commit_seen:
            print(f"first run of the age rule: every commit is first seen now, so nothing is released for "
                  f"{keep_days} day(s)")
        print(payload_history.lost_paths_line(history.lost_paths))
    if history.dropped:
        print("Warning: the remote's history changed since the last --payload-delete run:\n  "
              + "\n  ".join(history.dropped)
              + "\n  snapshots only reachable from there no longer count as using objects")
    print(f"history: {commits} commit(s), {manifests} distinct manifest(s), "
          f"{len(referenced)} referenced object(s)")

    stored: dict[str, int] = {}
    others = 0
    for prefix in (f"{n:02x}" for n in range(256)):
        entries = payload_upload._api(
            lambda p=prefix: client.list_dir(rid, f"{base}/objects/{p}"),
            f"listing {base}/objects/{prefix}",
        )
        for entry in entries:
            name = entry.get("name", "")
            if (entry.get("type") == "dir" or not isinstance(name, str)
                    or not _COLLECTABLE_NAME.match(name)):
                others += 1
                continue
            size = entry.get("size", 0)
            stored[name] = size if isinstance(size, int) and not isinstance(size, bool) else 0

    # A temporary is never referenced; the grace period keeps one that a run in
    # progress is still writing.
    unused = {name for name in stored if name[:64] not in referenced or len(name) > 64}
    missing = referenced - {name for name in stored if len(name) == 64}
    pending_text = _read_pending(client, rid, base)
    try:
        pending_raw = json.loads(pending_text) if pending_text else {}
    except (ValueError, RecursionError):
        raise common.SnapshotError(f"{base}/{_PENDING_NAME} is unreadable; remove it and run again") from None
    if not isinstance(pending_raw, dict):
        raise common.SnapshotError(f"{base}/{_PENDING_NAME} is not an object; remove it and run again")
    payload_guards._require_server_clock(client, delete)
    now = _grace_now(client)
    # A stamp that cannot be a time this tool wrote (zero, negative, in the future,
    # a boolean, NaN) is not trusted: "now - stamp" would call the grace period over
    # at once, which is the one thing a mark list exists to prevent.  The object is
    # marked afresh, which only delays its deletion.
    pending = {
        sha: float(ts) for sha, ts in pending_raw.items()
        if isinstance(sha, str) and _COLLECTABLE_NAME.match(sha)
        and isinstance(ts, (int, float)) and not isinstance(ts, bool)
        and 0 < ts <= now + _PENDING_FUTURE_SLACK_SECONDS
    }
    unusable = sorted(sha for sha in pending_raw if sha in unused and sha not in pending)
    if unusable:
        print(f"Warning: {len(unusable)} mark(s) in {base}/{_PENDING_NAME} hold no usable time; "
              "those object(s) are marked afresh and wait out the grace period again")
    due = {sha for sha in unused if sha in pending and now - pending[sha] >= grace_days * 86400}
    waiting = unused - due

    def total(shas: Any) -> str:
        return remote_ops.human_bytes(sum(stored[s] for s in shas))

    print(f"stored : {len(stored)} object(s), {total(stored)} under {base}/objects")
    print(f"in use : {len(stored) - len(unused)} object(s), {total(set(stored) - unused)}")
    print(f"unused : {len(unused)} object(s), {total(unused)}")
    print(f"  unreferenced for at least {grace_days:g} day(s) (deletable): {len(due)}, {total(due)}")
    print(f"  not yet unreferenced for that long (kept): {len(waiting)}, {total(waiting)}")
    if others:
        print(f"other  : {others} entry(ies) that are not objects (left alone)")
    if missing:
        print(f"Warning: {len(missing)} object(s) named by a manifest are NOT on the server; "
              f"the snapshots that need them cannot be restored")
    print(payload_guards.verification_note(client, rid, base))

    if not delete:
        # Seed the history-rewrite fence: a report that read the *remote's*
        # history and found no baseline writes the tips it saw, so the first
        # delete run has something to compare against (ROADMAP S2).  Only
        # when none exists — rewriting an existing baseline to current tips
        # would mask a rewrite that happened since the last delete run — and
        # never on the vault-only weekly report, whose tips may be stale.
        if not seen and not from_vault and history.tips and not partial_view:
            payload_guards.write_seen_tips(client, rid, base, history.tips)
            print("fence baseline: recorded the ref tips this report saw (the "
                  "first --payload-delete run compares against them)")
        if unused:
            print("\nnothing changed (report only); --payload-delete marks unused objects now "
                  "and deletes them once they have stayed unused for the grace period")
        return 0
    if manifests == 0:
        raise common.SnapshotError(
            "found no payload manifest anywhere in the history, so every object would look "
            "unreferenced; refusing to mark or delete (wrong remote, or a fetch that "
            "returned nothing?)"
        )
    ruled = keep is not None or (keep_days is not None and age_now is not None)
    if (history.dropped or (ruled and history.expired)) and not accept_loss:
        raise common.SnapshotError(
            "refusing to mark or delete: "
            + ("the remote's history was rewritten since the last --payload-delete run "
               f"({len(history.dropped)} ref(s)); " if history.dropped else "")
            + (f"{'--payload-keep ' + str(keep) if keep is not None else '--payload-keep-days ' + str(keep_days)} "
               f"would make {history.expired} older snapshot(s) "
               "unrestorable; " if ruled and history.expired else "")
            + "if that is intended, run again with --payload-accept-loss. Objects that "
            "only those snapshots need would otherwise be deleted after the grace period"
        )
    if due:
        # The decision above is minutes old.  A snapshot pushed since could name
        # an object that was unreferenced when the history was read, so the refs
        # are read once more immediately before the first deletion and a change
        # stops the run with nothing deleted (the next run re-decides).
        now_tips = _remote_tips(remote)
        if now_tips != {k: v for k, v in history.tips.items()}:
            raise common.SnapshotError(
                "the remote's branches or tags changed while this run was deciding what is "
                "unused (another machine pushed); nothing was deleted. Run it again"
            )
    deleted = 0
    gone: set[str] = set()
    stopped_early = False
    last_check = time.monotonic()
    for index, sha in enumerate(sorted(due), 1):
        # The loop can run for many minutes.  A push that lands in it is noticed
        # at the next look and stops the run, so the damage is bounded to what
        # was deleted before the push's ref became visible (and a snapshot
        # taken later re-uploads anything missing while its file still exists).
        if index > 1 and (index % _RECHECK_EVERY == 0
                          or time.monotonic() - last_check >= _RECHECK_SECONDS):
            if _remote_tips(remote) != {k: v for k, v in history.tips.items()}:
                stopped_early = True
                break
            last_check = time.monotonic()
        if not client.delete_entry(rid, f"{base}/{payload_upload.object_dir(sha)}/{sha}"):
            raise common.SnapshotError(f"the server refused to delete {sha}")
        deleted += 1
        gone.add(sha)
        if index % 100 == 0 or index == len(due):
            print(f"deleted [{index}/{len(due)}]", flush=True)
    # The new state: everything still unused keeps its first-seen time (or gets
    # now); anything that became referenced again, or was deleted, drops out.
    fresh = {sha: pending.get(sha, now) for sha in waiting | (due - gone)}
    marked = len(set(fresh) - set(pending))
    if fresh != pending:
        payload_upload._api(lambda: client.mkdir_p(rid, base), f"creating {base}")
        payload_upload._api(lambda: client.upload_file(
            rid, base, _PENDING_NAME, payload_guards.encode_pending(fresh)
        ), "writing gc state")
    if stopped_early:
        raise common.SnapshotError(
            f"the remote's branches or tags changed while objects were being deleted (another "
            f"machine pushed); stopped after {deleted} of {len(due)}. Run it again; if a "
            "restore then names a missing object, take a new snapshot from a tree that still "
            "has the file, or run payload verify"
        )
    payload_guards.write_seen_tips(client, rid, base, history.tips)
    payload_guards.write_commit_seen(client, rid, base, commit_seen, history.reachable, read_now, seen_copies)
    swept = placement.sweep_probe_debris(client, rid, f"{base}/objects")
    if swept:
        print(f"removed {swept} file(s) a killed link probe left in {base}/objects")
    print(f"marked {marked} newly unreferenced object(s); deleted {deleted} object(s), "
          f"{total(due)}; quota is released at once, disk after the library's history "
          f"expires and the server's GC runs")
    return 0
