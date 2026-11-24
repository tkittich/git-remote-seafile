from __future__ import annotations

import argparse
import json
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from . import common
from . import gitrun
from . import vault_store
from . import payload_guards
from . import payload_manifest
from . import payload_upload
from . import payload_gc_run


# -- Payload views -------------------------------------------------------------
#
# A snapshot's payload is stored by hash, which is right for the store and useless
# to a person who wants to look.  A *view* is a directory of real file names built
# on the server by copying and renaming objects: no data moves (a server-side copy
# shares blocks; measured at 20-30 ms for 1,500 entries and 0 bytes on disk), and
# it is a convenience that can be deleted and rebuilt at any time -- the objects
# and the manifest stay the source of truth.
#
# Views are *not* byte-faithful restores: they carry no mode and no mtime, and the
# server's name rules apply (NFC, a double quote becomes %22, 255 bytes), so a file
# whose name cannot be held is reported and left out.  `restore` is the faithful
# path.

_VIEW_NAME_BYTES = 255


def add_payload_view_arguments(parser: Any) -> None:
    parser.add_argument(
        "--payload-view-name",
        default=None,
        metavar="NAME",
        help="with payload view: the directory name under <path>-view (default: the "
        "snapshot id, which is stable for identical content)",
    )
    parser.add_argument(
        "--payload-view-replace",
        action="store_true",
        help="with payload view: delete an existing view of that name first, so "
        "'current' can be rebuilt in place",
    )


def payload_view_conflicts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.payload_view is not None:
        if not args.remote:
            problems.append("payload view requires --remote <url>")
        name = args.payload_view_name
        if name is not None and (not name or "/" in name or "\\" in name or name in (".", "..")):
            problems.append("--payload-view-name must be a plain directory name")
    return problems


def _manifest_at(remote: str, ref: str) -> dict[str, Any]:
    """The manifest of snapshot *ref* (``branch`` plus an optional ``~N``/``^``)."""
    from . import payload_history
    cut = min((i for i, ch in enumerate(ref) if ch in "~^@:"), default=len(ref))
    branch, suffix = ref[:cut] or "snapshot", ref[cut:]
    with tempfile.TemporaryDirectory(prefix="grs-payload-view-") as td:
        repo = payload_history._scratch_history_repo(td)
        gitrun.git(
            repo, None,
            ["fetch", "-q", "--no-tags", remote, "+refs/heads/*:refs/remotes/remote/*"],
            cwd=Path(td), status=f"fetching the history of {remote}", stall_limit=None,
        )
        rev = gitrun.git(
            repo, None,
            ["rev-parse", "--verify", f"refs/remotes/remote/{branch}{suffix}^{{commit}}"],
            cwd=Path(td), check=False,
        )
        if rev.returncode != 0:
            raise common.SnapshotError(f"cannot resolve {ref!r} in the history of {remote}")
        body = gitrun.git(
            repo, None, ["show", f"{rev.stdout.strip()}:{payload_upload.PAYLOAD_MANIFEST}"],
            cwd=Path(td), check=False,
        )
        if body.returncode != 0:
            raise common.SnapshotError(f"{ref!r} is a snapshot with no payload manifest; nothing to view")
        try:
            data = json.loads(body.stdout)
        except (ValueError, RecursionError):
            raise common.SnapshotError(f"the payload manifest of {ref!r} is unreadable") from None
    payload_manifest.check_manifest_shape(data, f"the payload manifest of {ref!r}")
    return data


def _view_name(rel: str) -> str:
    """The name the server will hold for a source path (NFC, quote escaped)."""
    return "/".join(
        unicodedata.normalize("NFC", part).replace('"', "%22") for part in rel.split("/")
    )


def _place_object(client: Any, rid: str, source_dir: str, sha: str, target: str, name: str) -> None:
    """Copy one object into *target* (it keeps its hash name) and rename it."""
    if not client.copy_entries(rid, source_dir, [sha], target):
        raise common.SnapshotError(f"the server refused to copy object {sha[:12]} into {target}")
    # A file named by its own hash (a content-addressed store) is already in place:
    # renaming it onto its own name made Seafile number it "<sha> (1)" (review
    # 06cc623, gemini G3.1b).
    if name == sha:
        return
    if not client.rename_entry(rid, f"{target}/{sha}", name):
        raise common.SnapshotError(f"the server would not rename an object to {name!r}")


def view_order(made: "list[tuple[str, str, str]]") -> "list[tuple[str, str, str]]":
    """The order a view is built in: within a folder, a file named by its own hash goes last.

    A server-side copy lands under the object's hash and is then renamed.  The file named by its own hash
    needs no rename, so if it were placed first, the copy for a same-bytes file after it would find the name
    taken, be numbered ``<sha> (1)``, and the rename would move the first file instead (review c305b0a
    fable-S3-2).  Placed last, its name is free when the others are done.
    """
    return sorted(made, key=lambda entry: (entry[0], entry[1] == entry[2], entry[1], entry[2]))


def payload_view(
    remote: str, ref: str, *, name: str | None, replace: bool, vault: Path | None = None
) -> int:
    from . import remote_ops
    manifest = _manifest_at(remote, ref)
    files = payload_manifest.validate_manifest(manifest, Path(tempfile.gettempdir()) / "grs-view-validate")
    library, base, server_url = payload_upload.payload_location(remote)
    repo_path = base[: -len("/payload")]
    client = payload_upload._api(lambda: payload_upload._open_client(server_url), "opening the Seafile client")
    rid = payload_upload._api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    view_name = name or manifest.get("snapshot")
    if not view_name:
        raise common.SnapshotError("the manifest names no snapshot id; give the view a name")
    root = f"{repo_path}-view/{view_name}"

    held = payload_upload._remote_objects(client, rid, base, {item["sha256"] for item in files})
    absent = [item["path"] for item in files if held.get(item["sha256"]) != item["size"]]
    if absent:
        raise common.SnapshotError(
            f"{len(absent)} object(s) of this snapshot are missing from the server (first: "
            f"{absent[0]}); a view of it would be incomplete"
        )
    if payload_upload._api(lambda: client.dir_exists(rid, root), f"checking {root}"):
        if not replace:
            raise common.SnapshotError(
                f"{root} already exists; pick another --payload-view-name or pass "
                "--payload-view-replace"
            )
        if not payload_upload._api(lambda: client.delete_entry(rid, root), f"removing the old {root}"):
            # delete_entry answers False for a refusal the server reports as a
            # status, and _api only converts exceptions: swallowing that built
            # the new view into the old one's remains.
            raise common.SnapshotError(
                f"the server would not remove the old view {root}; delete it by hand or "
                "pick another --payload-view-name"
            )

    skipped: list[str] = []
    seen: set[str] = set()
    made: list[tuple[str, str, str]] = []   # (directory, shown name, sha)
    for item in files:
        shown = _view_name(item["path"])
        parts = shown.split("/")
        if any(len(part.encode("utf-8")) > _VIEW_NAME_BYTES for part in parts):
            skipped.append(f"{item['path']}: a name over {_VIEW_NAME_BYTES} bytes cannot be held")
            continue
        if shown in seen:
            skipped.append(f"{item['path']}: becomes the same name as another file")
            continue
        seen.add(shown)
        made.append(("/".join(parts[:-1]), parts[-1], item["sha256"]))

    # A server-side copy adds no blocks, but the library's quota counts each copy
    # at its full size (measured, SNAPSHOT.md 15.1, and again on a real server:
    # a view of one 70 MiB object grew the library by 70.0 MiB): a view of a
    # large snapshot can use what the next backup needs.  Said before anything
    # is built, and again in the closing line.
    sizes = {item["sha256"]: item["size"] for item in files}
    counted = sum(sizes[sha] for _d, _n, sha in made)
    payload_guards.payload_preflight(client, rid, counted, action="copies (counted at full size)")
    print(f"building {root}: {len(made)} file(s) ...", flush=True)
    payload_upload._api(lambda: client.mkdir_p(rid, root), f"creating {root}")
    done_dirs: set[str] = set()
    for index, (directory, shown_name, sha) in enumerate(view_order(made), 1):
        target = root + ("/" + directory if directory else "")
        if target not in done_dirs:
            payload_upload._api(lambda t=target: client.mkdir_p(rid, t), f"creating {target}")
            done_dirs.add(target)
        source_dir = f"{base}/{payload_upload.object_dir(sha)}"
        payload_upload._api(
            lambda s=source_dir, h=sha, t=target, n=shown_name:
            _place_object(client, rid, s, h, t, n),
            f"placing {shown_name}",
        )
        if index % 100 == 0 or index == len(made):
            print(f"built [{index}/{len(made)}]", flush=True)
    print(
        f"view ready: {library}:{root} ({len(made)} file(s), {remote_ops.human_bytes(counted)}; the server "
        "copied them without moving data, but the library's quota and size now count each "
        "copy at its full size -- delete the view when you are done with it)"
    )
    if skipped:
        shown = "\n  ".join(skipped[:20])
        more = f"\n  ... and {len(skipped) - 20} more" if len(skipped) > 20 else ""
        print(f"Warning: {len(skipped)} file(s) were left out of the view:\n  {shown}{more}")
    return 0


def payload_command(args: argparse.Namespace) -> int | None:
    """Run a payload sub-command (verify, view, retention); ``None`` for a snapshot run."""
    vault = vault_store.expand_path(args.vault) if args.vault else None
    if args.payload_verify is not None:
        return payload_gc_run.payload_verify(
            args.remote, vault, count=args.payload_verify, allow_low_disk=args.allow_low_disk
        )
    if args.payload_view is not None:
        return payload_view(
            args.remote, args.payload_view, name=args.payload_view_name,
            replace=args.payload_view_replace, vault=vault,
        )
    if args.payload_gc:
        return payload_gc_run.payload_gc(
            args.remote, vault, delete=args.payload_delete,
            grace_days=args.payload_grace_days, keep=args.payload_keep, keep_days=args.payload_keep_days,
            accept_loss=args.payload_accept_loss, allow_low_disk=args.allow_low_disk,
            allow_non_owner=args.payload_allow_non_owner,
        )
    return None
