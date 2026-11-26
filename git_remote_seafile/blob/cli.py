"""``seafile-blob`` (``sfblob``, ``git-seafile-blob``, ``seafile blob``): the blob store's command line (STORE.md D3, CLI.md).

Plain lines by default and one JSON document with ``--json``; no prompt ever (a command is told what to do by its
flags); exit 0 success, 1 failure (a fact about the store), 2 usage error, 3 the server or the network failed (run it again).  Standard output is the answer and nothing else; every note,
warning and progress line goes to standard error.  ``offload``, ``link`` and ``read`` are not in this version.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..client import SeafileAPIError, SeafileAuthError, SeafileConnectionError
from ..units import format_bytes
from . import cache as cache_mod
from . import gcreport, pins, putlog, refs, store, tree
from .progress import Progress

PROG = "seafile-blob"
_SIZE = re.compile(r"(\d+)\s*([kmgt]?)(?:i?b)?", re.IGNORECASE)
_UNITS = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
NOT_YET = ("offload", "link", "read")
_UNPROTECTED = "Note: these objects are not pinned, so `gc` would report them as unreferenced: use --pin NAME to protect them"


class UsageError(Exception):
    pass


def parse_size(text: str) -> int:
    match = _SIZE.fullmatch(text.strip())
    if not match:
        raise UsageError(f"not a size: {text!r} (examples: 500M, 20G, 1048576)")
    return int(match.group(1)) * _UNITS[match.group(2).lower()]


def store_url(given: "str | None") -> str:
    """``--store``, else ``$SFBLOB_STORE``, else the git config ``seafile.blob.store``."""
    url = given or os.environ.get("SFBLOB_STORE")
    if not url:
        try:
            from ..git_util import get_git_config
            url = get_git_config("seafile.blob.store")
        except Exception:
            url = None
    if not url:
        raise UsageError(
            "no store: give --store seafile://<library>/<folder>, or set SFBLOB_STORE, "
            "or `git config --global seafile.blob.store seafile://<library>/<folder>`")
    return url


def open_store(url: str, cache_dir: "str | None") -> store.Store:
    from ..client import SeafileClient
    from ..url import parse_seafile_url
    try:
        parsed = parse_seafile_url(url)
    except ValueError as exc:
        raise UsageError(f"not a seafile:// URL: {exc}") from None
    if parsed.path_defaulted:
        raise UsageError(
            f"{url} names a library and no folder: a store needs one, as seafile://<library>/<folder> "
            "(a bare library would silently use the helper's /git-repo folder)")
    cache = cache_mod.Cache(Path(cache_dir) if cache_dir else None)
    server = parsed.server_url
    known: "dict[str, str]" = {}          # the library is looked up once, not once per file of a folder

    def factory() -> store.Store:
        client = SeafileClient(server_url=server)
        if "id" not in known:
            known["id"] = client.get_repo_id(parsed.library_name)
        # Decided from the server the client actually uses, not from the URL's text: ``seafile://lib/folder`` names no
        # server, and the one in the configuration may be plain http (review R6 G3, D2).
        plain_http = str(client.server_url or "").lower().startswith("http://")
        return store.Store(client, known["id"], parsed.repo_path, cache, verify_by_download=plain_http, factory=factory)

    return factory()


def _say(payload: "dict[str, Any]", args: argparse.Namespace, text: str) -> None:
    if args.json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    elif text:
        sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))


def _err(text: str) -> None:
    sys.stderr.write(text if text.endswith("\n") else text + "\n")


def quota_warning(client: Any, need_bytes: int) -> "str | None":
    """A warning when the files to put could not fit the account's quota; ``None`` when they fit or it cannot be told.

    *need_bytes* is an upper bound (objects already in the store are not uploaded again), so this is a warning and
    never a refusal. The quota that counts is the library owner's, which is not necessarily this account.
    """
    from ..snapshot import payload_guards
    try:
        quota = payload_guards._account_quota(client)
    except Exception:                      # informational only
        return None
    if quota is None:
        return None
    usage, total = quota
    free = max(total - usage, 0)
    if need_bytes <= free:
        return None
    return (f"Warning: these files are up to {format_bytes(need_bytes)} but the account has {format_bytes(free)} of quota left "
            f"({format_bytes(usage)} of {format_bytes(total)} used); objects already in the store are skipped, but a "
            f"larger upload will probably fail partway. Free space or ask for more first; stored objects are kept.")


def _bytes_to_put(paths: "list[str]", recursive: bool) -> int:
    """The total size of what a ``put`` was asked to upload, as far as it can be told without reading it."""
    total = 0
    files: "list[Path]" = []
    for item in paths:
        if item == "-":
            continue
        source = Path(item)
        if recursive and source.is_dir():
            files.extend(tree._walk(source)[0])
        else:
            files.append(source)
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def _progress(args: argparse.Namespace, verb: str) -> Progress:
    return Progress(verb, enabled=False if (args.quiet or args.json) else None)


def _refs(texts: "list[str]") -> "list[refs.Ref]":
    """Every reference of a command line, parsed before anything is done: a malformed one is a usage error."""
    try:
        return [refs.parse(text) for text in texts]
    except refs.RefError as exc:
        raise UsageError(str(exc)) from None


def _resolved(base: store.Store, ref: refs.Ref) -> refs.Ref:
    """The full reference; a short one is said in full, so what a person copies into a script is the full one."""
    full = base.resolve(ref)
    if not ref.is_full:
        _err(f"Note: {ref} is {full}; scripts and records should keep the full reference")
    return full


def _common(top: bool) -> argparse.ArgumentParser:
    """The flags every command takes.  Declared on the program and on each command, so they work on either side of
    the command's name (the command's copies have no default of their own, or they would overwrite the program's)."""
    def default(value: Any) -> Any:
        return value if top else argparse.SUPPRESS

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=default(None),
                        help="the store: seafile://<library>/<folder> (default: $SFBLOB_STORE or git config seafile.blob.store)")
    common.add_argument("--cache-dir", default=default(None), help="the local cache (default: $SFBLOB_CACHE or the user's cache folder)")
    common.add_argument("--json", action="store_true", default=default(False), help="answer with one JSON document")
    common.add_argument("--jobs", type=int, default=default(4), help="transfers at once for a folder (default 4; the server "
                                                                       "slows a client down above about that)")
    common.add_argument("-q", "--quiet", action="store_true", default=default(False), help="no progress line")
    return common


def _parser() -> argparse.ArgumentParser:
    from .. import __version__
    parser = argparse.ArgumentParser(
        prog=PROG, parents=[_common(True)],
        description="Keep large, immutable files on a Seafile library under their SHA-256, and get them back verified.",
        epilog=("What each command writes, and who may: `put` creates objects (blobs/<xx>/<sha256>) and, once, the store "
                "marker; an object is never changed after it is stored (write-once). `get`, `path`, `cat`, `exists` and "
                "`stat` write nothing on the server; `get` and `path` write only inside the local cache (and `get --to` "
                "and `-o` only to a path that did not exist). `--dry-run` changes nothing at all. Nothing here deletes an "
                "object on the server. Notes, warnings and progress go to standard error; standard output is the answer."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"{PROG} (git-remote-seafile {__version__})")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    shared = [_common(False)]

    put = sub.add_parser("put", parents=shared, help="upload files (or a folder with -r) and print their references")
    put.add_argument("paths", nargs="+", help="files, a folder with -r, or - for standard input")
    put.add_argument("-r", "--recursive", action="store_true", help="upload a folder")
    put.add_argument("--record", metavar="FILE", help="with -r: write path -> reference to FILE (one sorted line per entry, for git)")
    put.add_argument("--dry-run", action="store_true", help="say what would be uploaded; change nothing (not even the record file)")
    put.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                     help="with -r: leave out paths matching PATTERN (shell-style, matched on the relative path; repeatable; "
                          "'.git/*' leaves a repository's own folder out)")
    put.add_argument("--pin", metavar="NAME", help="keep these objects from `gc`: write a pin of that name first (see `pin`)")
    put.add_argument("--no-log", action="store_true",
                     help="do not note this put in the local log `log` searches (it holds local paths)")

    get = sub.add_parser("get", parents=shared, help="fetch objects into the cache, verified, and print their paths")
    get.add_argument("refs", nargs="*", help="sha256:<hex> (a prefix of 6 or more hex characters is enough when it is unique)")
    get.add_argument("-o", "--output", metavar="FILE", help="copy the one object to FILE, which must not exist")
    get.add_argument("--to", metavar="DIR", help="copy each object into DIR as its reference (or, with --from, restore the paths there)")
    get.add_argument("--from", dest="record", metavar="FILE", help="restore every path of a record file under --to")
    get.add_argument("--dry-run", action="store_true", help="with --from: say what would be restored; change nothing")
    get.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                     help="with --from: leave out paths matching PATTERN (shell-style; repeatable)")

    path = sub.add_parser("path", parents=shared, help="print the cache path of an object, fetching it first")
    path.add_argument("ref")

    cat = sub.add_parser("cat", parents=shared, help="write an object to standard output once it is proved "
                                                      "(needs the object's size free for a moment in the temporary folder)")
    cat.add_argument("ref")

    exists = sub.add_parser("exists", parents=shared, help="say whether objects are in the store (exit 1 when one is not)")
    exists.add_argument("refs", nargs="+")

    stat = sub.add_parser("stat", parents=shared, help="the size of an object in the store")
    stat.add_argument("ref")

    cache = sub.add_parser("cache", parents=shared, help="show, bound or empty the local cache")
    cache.add_argument("--size", metavar="N",
                       help="keep the cache to N bytes from now on, evicting the least recently used (e.g. 20G; `off` removes the bound)")
    cache.add_argument("--clear", action="store_true", help="remove every cached object")

    pin = sub.add_parser("pin", parents=shared, help="keep objects from `gc` under a pin name (`pin ls` lists the pins)")
    pin.add_argument("name", help="the pin's name, or `ls` to list pins")
    pin.add_argument("refs", nargs="*", help="full references to keep (with `ls`: the one pin to list)")
    pin.add_argument("--from-file", metavar="FILE", help="read references from FILE, one a line (- for standard input)")
    pin.add_argument("--from-record", metavar="FILE", help="pin every object a record file names (so the roots live on the server, "
                     "where every machine's `gc` sees them)")

    unpin = sub.add_parser("unpin", parents=shared, help="remove a pin (objects only it kept become unused)")
    unpin.add_argument("name")

    gc = sub.add_parser("gc", parents=shared, help="report what no pin keeps, and the upload temporaries; deletes nothing")
    gc.add_argument("--apply", action="store_true", help="(not in this version: object removal comes later)")

    verify = sub.add_parser("verify", parents=shared, help="check that objects are in the store (--deep also reads and hashes them)")
    verify.add_argument("refs", nargs="*", help="references (default: every object, or those in --from)")
    verify.add_argument("--from", dest="record", metavar="FILE", help="check every entry of a record file, at its recorded size")
    verify.add_argument("--deep", action="store_true", help="download each object and check its hash (reads every byte)")
    verify.add_argument("--subset", metavar="K/N", help="only the objects whose fan-out folder falls in part K of N (1 <= K <= N): "
                        "run 1/7 to 7/7 on seven days to check every object, with no state file")

    for name in NOT_YET:          # known words, so a global flag before one answers "not yet" and not a usage error
        sub.add_parser(name, parents=shared, add_help=False).add_argument("rest", nargs=argparse.REMAINDER)

    sub.add_parser("status", parents=shared, help="is this store all right? marker, counts, pins, cache, and what to run next")
    sub.add_parser("ls", parents=shared, help="list the objects in the store, with their sizes")
    sub.add_parser("usage", parents=shared, help="how many objects the store holds, how many bytes, and the local cache")

    log = sub.add_parser("log", parents=shared,
                         help="find a reference again by the file's name, from what this machine put (local; no network)")
    log.add_argument("pattern", nargs="?", help="part of a path, or a shell pattern such as '*survey*.tif'")

    init = sub.add_parser("init", parents=shared,
                          help="check the login and the store, create it, and remember it so --store is not needed again")
    init.add_argument("url", nargs="?", help="seafile://<library>/<folder> (default: the store already configured)")
    init.add_argument("--global", dest="global_scope", action="store_true",
                      help="save it for every repository (the default outside a repository; inside one it is saved there)")
    return parser


def _put(args: argparse.Namespace) -> int:
    for item in args.paths:
        if item != "-" and Path(item).is_dir() and not args.recursive:
            raise UsageError(f"{item} is a folder: use `{PROG} put -r {item} --record FILE` to upload what is in it")
    url = store_url(args.store)
    base = open_store(url, args.cache_dir)
    spooled: "list[Path]" = []
    if not args.dry_run:
        note = quota_warning(base.client, _bytes_to_put(args.paths, args.recursive))
        if note:
            _err(note)
    try:
        if args.recursive:
            if len(args.paths) != 1:
                raise UsageError("put -r takes one folder")
            report = tree.put_tree(base, Path(args.paths[0]), Path(args.record) if args.record else None, args.jobs,
                                   dry_run=args.dry_run, exclude=args.exclude, pin=args.pin)
            for note in report.skipped:
                _err(f"Warning: not uploaded ({note})")
            for problem in report.problems:
                _err(f"{PROG}: {problem}")
            if report.dry_run and not args.json:
                for e in report.entries:                # the plan, one line per file
                    sys.stdout.write(f"{'+ would upload' if e.path in report.new else '= already there'}  {e.size}  {e.path}\n")
            elif not args.record and not args.json:
                for e in report.entries:                # no record file: the references are the answer
                    sys.stdout.write(f"{e.ref}  {e.size}  {e.path}\n")
            if not args.dry_run and not args.no_log:
                root = os.path.abspath(args.paths[0])
                putlog.append(base.cache, url, [(str(e.ref), e.size, os.path.join(root, *e.path.split("/")))
                                                for e in report.entries])
            summary = (f"dry run, nothing changed: {len(report.entries)} file(s), {report.uploaded} would be uploaded, "
                       f"{report.present} already there" if report.dry_run else
                       f"{len(report.entries)} file(s): {report.uploaded} uploaded, {report.present} already there")
            summary += (f", {report.excluded} excluded" if report.excluded else "")
            summary += (f"; record written to {args.record}" if args.record and not report.problems and not report.dry_run else "")
            if args.record or args.json:
                _say({"entries": [{"path": e.path, "size": e.size, "ref": str(e.ref)} for e in report.entries],
                      "uploaded": report.uploaded, "present": report.present, "skipped": report.skipped,
                      "problems": report.problems, "record": args.record, "excluded": report.excluded,
                      "dry_run": report.dry_run}, args, summary)
            else:
                _err(summary)
            if report.entries and not report.dry_run and not report.problems and not args.pin:
                _err(_UNPROTECTED)
            return 1 if report.problems else 0
        if args.record:
            raise UsageError("--record needs -r (a folder)")
        if args.exclude:
            raise UsageError("--exclude needs -r (a folder)")
        results: "list[dict[str, Any]]" = []
        failed = False
        for item in args.paths:
            source = Path(item)
            if item == "-":
                handle, name = tempfile.mkstemp(prefix="sfblob-stdin-")
                with os.fdopen(handle, "wb") as spool:
                    shutil.copyfileobj(sys.stdin.buffer, spool)
                source = Path(name)
                spooled.append(source)
            progress = _progress(args, "uploaded")
            try:
                done = base.would_put(source) if args.dry_run else base.put(source, progress=progress, pin=args.pin)
            except store.StoreError as exc:
                progress.finish()
                _err(f"{PROG}: {exc}")
                failed = True
                results.append({"path": item, "error": str(exc)})
                continue
            progress.finish()
            results.append({"path": item, "ref": str(done.ref), "size": done.size, "uploaded": done.uploaded})
            if not args.dry_run and not args.no_log and item != "-":
                putlog.append(base.cache, url, [(str(done.ref), done.size, os.path.abspath(item))])
        if args.json:
            _say({"files": results}, args, "")
        else:
            for row in results:
                if "ref" in row:
                    sys.stdout.write(f"{row['ref']}  {row['size']}  {row['path']}\n")
            if any("ref" in row for row in results) and not args.dry_run and not args.pin:
                _err(_UNPROTECTED)
        return 1 if failed else 0
    finally:
        for temp in spooled:
            temp.unlink(missing_ok=True)


def _get(args: argparse.Namespace) -> int:
    if args.output and (args.to or args.record):
        raise UsageError("-o names one file; it does not go with --to or --from")
    parsed = _refs(args.refs)
    if args.output and len(parsed) != 1:
        raise UsageError("-o FILE takes exactly one reference")
    base = open_store(store_url(args.store), args.cache_dir)
    if args.record:
        if not args.to:
            raise UsageError("--from needs --to DIR")
        report = tree.get_tree(base, Path(args.record), Path(args.to), args.jobs, dry_run=args.dry_run, exclude=args.exclude)
        for warning in report.warnings:
            _err(f"Warning: {warning}")
        for problem in report.problems:
            _err(f"{PROG}: {problem}")
        _say({"restored": report.restored, "in_place": report.in_place, "problems": report.problems,
              "warnings": report.warnings, "excluded": report.excluded, "dry_run": report.dry_run}, args,
             (f"dry run, nothing changed: {report.restored} would be restored" if report.dry_run else f"{report.restored} restored")
             + f", {report.in_place} already in place"
             + (f", {report.excluded} excluded" if report.excluded else "")
             + (f", {len(report.problems)} problem(s)" if report.problems else ""))
        return 1 if report.problems else 0
    if args.dry_run or args.exclude:
        raise UsageError("--dry-run and --exclude need --from FILE")
    if not parsed:
        raise UsageError("get needs a reference or --from FILE")
    rows: "list[dict[str, Any]]" = []
    failed = False
    for text, ref in zip(args.refs, parsed, strict=True):
        progress = _progress(args, "downloaded")
        try:
            ref = _resolved(base, ref)
            if args.output:
                where = base.get_to(ref, Path(args.output), progress=progress)
            elif args.to:
                folder = Path(args.to)
                folder.mkdir(parents=True, exist_ok=True)
                where = base.get_to(ref, folder / ref.hex, progress=progress)
            else:
                where, _ = base.get(ref, progress=progress)
        except (store.StoreError, OSError) as exc:
            progress.finish()
            _err(f"{PROG}: {exc}")
            failed = True
            rows.append({"ref": text, "error": str(exc)})
            continue
        progress.finish()
        rows.append({"ref": str(ref), "path": str(where)})
    if args.json:
        _say({"objects": rows}, args, "")
    else:
        for row in rows:
            if "path" in row:
                sys.stdout.write(row["path"] + "\n")
    return 1 if failed else 0


def _path(args: argparse.Namespace) -> int:
    (parsed,) = _refs([args.ref])
    base = open_store(store_url(args.store), args.cache_dir)
    progress = _progress(args, "downloaded")
    where, _ = base.get(_resolved(base, parsed), progress=progress)
    progress.finish()
    _say({"path": str(where)}, args, str(where))
    return 0


def _cat(args: argparse.Namespace) -> int:
    (parsed,) = _refs([args.ref])
    base = open_store(store_url(args.store), args.cache_dir)
    progress = _progress(args, "downloaded")
    base.cat(_resolved(base, parsed), sys.stdout.buffer, progress=progress)
    progress.finish()
    sys.stdout.buffer.flush()
    return 0


def _exists(args: argparse.Namespace) -> int:
    parsed = _refs(args.refs)
    base = open_store(store_url(args.store), args.cache_dir)
    rows = []
    for text, ref in zip(args.refs, parsed, strict=True):
        try:
            full = _resolved(base, ref)
            rows.append({"ref": str(full), "exists": base.exists(full)})
        except store.StoreError as exc:
            rows.append({"ref": text, "exists": False, "error": str(exc)})
    if args.json:
        _say({"objects": rows}, args, "")
    else:
        for row in rows:
            sys.stdout.write(f"{row['ref']}  {'present' if row['exists'] else 'missing'}\n")
    return 0 if all(r["exists"] for r in rows) else 1


def _stat(args: argparse.Namespace) -> int:
    (parsed,) = _refs([args.ref])
    base = open_store(store_url(args.store), args.cache_dir)
    ref = _resolved(base, parsed)
    size = base.stat(ref)
    if size is None:
        raise store.StoreError(f"{ref} is not in the store")
    _say({"ref": str(ref), "size": size}, args, f"{ref}  {size}  ({format_bytes(size)})")
    return 0


def _cache(args: argparse.Namespace) -> int:
    cache = cache_mod.Cache(Path(args.cache_dir) if args.cache_dir else None)
    removed = 0
    if args.clear and args.size:
        raise UsageError("--clear and --size together: say one (--clear empties the cache, --size bounds it)")
    if args.clear:
        removed = cache.clear()
    elif args.size:
        if args.size.strip().lower() == "off":
            cache.set_limit(None)
        else:
            limit = parse_size(args.size)
            cache.set_limit(limit)
            removed = len(cache.evict(limit, grace=0))          # asked for now: nothing is "just handed out"
    used = cache.used()
    held = len(cache.objects())
    bound = f", kept to {format_bytes(cache.size_limit)}" if cache.size_limit is not None else ", no size bound (--size sets one)"
    _say({"removed": removed, "used": used, "objects": held, "directory": str(cache.root), "limit": cache.size_limit}, args,
         (f"removed {removed} object(s); " if (args.clear or args.size) else "")
         + f"the cache holds {held} object(s), {format_bytes(used)}{bound}, in {cache.root}")
    return 0


def _ls(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    rows = base.objects()
    if args.json:
        _say({"objects": [{"ref": str(r), "size": s} for r, s in rows]}, args, "")
    else:
        for ref, size in rows:
            sys.stdout.write(f"{ref}  {size}\n")
    return 0


def _usage(args: argparse.Namespace) -> int:
    url = store_url(args.store)
    base = open_store(url, args.cache_dir)
    rows = base.objects()
    total = sum(s for _, s in rows)
    cache = base.cache
    _say({"store": url, "objects": len(rows), "bytes": total, "cache_objects": len(cache.objects()),
          "cache_bytes": cache.used(), "cache_limit": cache.size_limit}, args,
         f"{url}: {len(rows)} object(s), {format_bytes(total)}\n"
         f"cache {cache.root}: {len(cache.objects())} object(s), {format_bytes(cache.used())}"
         + (f", kept to {format_bytes(cache.size_limit)}" if cache.size_limit is not None else ""))
    return 0


def _status(args: argparse.Namespace) -> int:
    """One read-only look: which store, what it holds, what no pin names, what a pin names that is gone, and the next command."""
    url = store_url(args.store)
    base = open_store(url, args.cache_dir)
    base.check_root(create=False)
    report = gcreport.scan(base)
    cache = base.cache
    features = {key: list(base.marker.get(key, [])) for key in ("incompat", "ro_compat", "compat") if base.marker.get(key)}
    next_steps: "list[str]" = []
    if report.missing:
        next_steps.append("sfblob put <the file> --pin <name>   (a pinned object is not in the store: store it again)")
    if report.unused:
        next_steps.append("sfblob pin <name> <reference>   (objects no pin names would be collected: keep the ones you need)")
    if report.foreign:
        next_steps.append("look at the entries `sfblob gc` calls foreign: this version leaves them alone")
    if base._read_only_because:
        next_steps.append("use a newer sfblob to write to this store")
    rows: "dict[str, Any]" = {
        "store": url, "format": base.marker.get("format"), "features": features, "writable": not base._read_only_because,
        "objects": len(report.objects), "bytes": sum(report.objects.values()), "pins": len(report.pins),
        "pinned_objects": len(report.pinned), "unused": len(report.unused), "missing": len(report.missing),
        "foreign": len(report.foreign), "newer": len(report.newer), "temporaries": len(report.temporaries),
        "cache_objects": len(cache.objects()), "cache_bytes": cache.used(), "cache_limit": cache.size_limit, "next": next_steps}
    lines = [
        f"store    : {url} (format {rows['format']}{', ' + ', '.join(f'{k}: {v}' for k, v in features.items()) if features else ''}"
        f"{'' if rows['writable'] else '; read-only for this version'})",
        f"objects  : {rows['objects']}, {format_bytes(rows['bytes'])}",
        f"pins     : {rows['pins']} pin(s) keep {rows['pinned_objects']} object(s); {rows['unused']} object(s) no pin names; "
        f"{rows['missing']} pinned object(s) not in the store",
        f"cache    : {rows['cache_objects']} object(s), {format_bytes(rows['cache_bytes'])}"
        + (f", kept to {format_bytes(cache.size_limit)}" if cache.size_limit is not None else ""),
    ]
    if rows["foreign"] or rows["newer"] or rows["temporaries"]:
        lines.append(f"other    : {rows['foreign']} foreign, {rows['newer']} from a newer version, {rows['temporaries']} interrupted upload(s)")
    lines += ["next     : " + step for step in next_steps] or ["next     : nothing to do"]
    _say(rows, args, "\n".join(lines))
    return 1 if report.missing else 0


def _log(args: argparse.Namespace) -> int:
    base_cache = cache_mod.Cache(Path(args.cache_dir) if args.cache_dir else None)
    rows = putlog.search(base_cache, args.pattern)
    if args.json:
        _say({"puts": rows}, args, "")
    else:
        for row in rows:
            sys.stdout.write(f"{row.get('ref')}  {row.get('size')}  {row.get('path')}\n")
        if not rows:
            _err("Note: nothing logged on this machine matches" if args.pattern else
                 "Note: nothing has been put from this machine yet (or --no-log / SFBLOB_LOG=off was used)")
    return 0 if rows else 1


def _init(args: argparse.Namespace) -> int:
    import subprocess
    url = args.url
    if not url:
        try:
            url = store_url(None)
        except UsageError:
            raise UsageError("init needs the store: `sfblob init seafile://<library>/<folder>`") from None
    base = open_store(url, args.cache_dir)
    entries = base._list(base.root)
    existed = any(e.get("name") == store.MARKER for e in entries)
    base.check_root(create=True)
    saved = ""
    if args.url:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True).returncode == 0
        scope = "--global" if (args.global_scope or not inside) else "--local"
        done = subprocess.run(["git", "config", scope, "seafile.blob.store", url], capture_output=True, text=True)
        saved = scope[2:] if done.returncode == 0 else ""
        if not saved:
            _err("Note: could not save the store with git config; set SFBLOB_STORE instead")
    lines = [f"store ready: {url}" + (" (it already was)" if existed else " (created)"),
             f"saved as git config seafile.blob.store ({saved})" if saved else "",
             "next: `sfblob put FILE` stores a file and prints its reference; `sfblob get REF -o FILE` brings it back; "
             "`sfblob log NAME` finds a reference again"]
    _say({"store": url, "created": not existed, "saved": saved}, args, "\n".join(line for line in lines if line))
    return 0


def _pin(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    if args.name == "ls":
        if args.from_file or args.from_record or len(args.refs) > 1:
            raise UsageError("`pin ls` takes at most one pin name; to pin references, give the pin another name")
        base.check_root(create=False)
        held = pins.read_all(base)
        if args.refs:
            if args.refs[0] not in held:
                raise store.StoreError(f"there is no pin {args.refs[0]!r}")
            held = {args.refs[0]: held[args.refs[0]]}
        if args.json:
            _say({"pins": {name: [str(r) for r in wanted] for name, wanted in held.items()}}, args, "")
        else:
            for name, wanted in held.items():
                sys.stdout.write(f"{name}  {len(set(r.hex for r in wanted))} object(s)\n")
        return 0
    texts = list(args.refs)
    if args.from_file:
        try:
            raw = sys.stdin.read() if args.from_file == "-" else Path(args.from_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read {args.from_file}: {exc}") from None
        texts += [line.strip() for line in raw.splitlines() if line.strip()]
    if args.from_record:
        from . import record
        try:
            texts += [str(e.ref) for e in record.parse(Path(args.from_record).read_text(encoding="utf-8"))]
        except (OSError, record.RecordError) as exc:
            raise UsageError(f"{args.from_record}: {exc}") from None
    if not texts:
        raise UsageError("pin needs references, or --from-file FILE, or --from-record FILE")
    wanted = _refs(texts)
    if any(not r.is_full for r in wanted):
        raise UsageError("a pin names full references (sha256:<64 hex>), not prefixes")
    base.check_root(create=False)
    pins.write(base, args.name, wanted)          # the pin first, then look: a pinner lists after its pin is readable
    absent = [r for r in wanted if not base.exists(r)]
    if absent:
        _err(f"{PROG}: {len(absent)} object(s) are not in the store (first: {absent[0]}); the pin names them anyway, "
             "and `put` of the file stores them")
    _say({"pin": args.name, "references": len({r.hex for r in wanted}), "absent": [str(r) for r in absent]}, args,
         f"pinned {len({r.hex for r in wanted})} object(s) as {args.name}")
    return 1 if absent else 0


def _unpin(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    base.check_root(create=False)
    removed = pins.remove(base, args.name)
    _say({"pin": args.name, "removed": removed}, args,
         f"removed the pin {args.name}" if removed else f"there was no pin {args.name}")
    return 0


def _gc(args: argparse.Namespace) -> int:
    if args.apply:
        raise UsageError("gc --apply is not in this version: this version reports, and removes nothing")
    base = open_store(store_url(args.store), args.cache_dir)
    risk = None
    try:
        risk = base.client.hidden_history_risk(base.repo_id, noun="pins")
    except Exception:
        risk = None
    if risk:
        _err("Warning: " + risk)
    report = gcreport.scan(base)
    unused = report.unused
    rows: "dict[str, Any]" = {"objects": len(report.objects), "bytes": sum(report.objects.values()), "pins": report.pins,
            "unused": len(unused), "unused_list": [f"sha256:{h}" for h in unused], "unused_bytes": report.bytes_of(unused), "temporaries": len(report.temporaries),
            "temporary_bytes": sum(s for _, s in report.temporaries), "copies": len(report.copies),
            "copy_bytes": sum(s for _, s in report.copies), "foreign": report.foreign, "newer": report.newer, "missing": len(report.missing),
            "missing_list": [f"sha256:{h}" for h in report.missing],
            "removed": 0}
    exit_code = 0
    lines = [
        f"stored : {rows['objects']} object(s), {format_bytes(rows['bytes'])}",
        f"pinned : {len(report.pinned)} object(s) by {len(report.pins)} pin(s)",
        f"unused : {rows['unused']} object(s), {format_bytes(rows['unused_bytes'])} (no pin names them)",
        f"temporaries : {rows['temporaries']}, {format_bytes(rows['temporary_bytes'])} (interrupted uploads)",
        f"copies : {rows['copies']}, {format_bytes(rows['copy_bytes'])} (`(N)` or conflict copies of an object)",
    ]
    if report.newer:
        lines.append(f"newer  : {len(report.newer)} entry(ies) written by a newer version of sfblob (left alone)")
    if report.foreign:
        lines.append(f"other  : {len(report.foreign)} entry(ies) that are not the store's (left alone)")
    if report.missing:
        shown = ", ".join(f"sha256:{h}" for h in report.missing[:3]) + (" ..." if len(report.missing) > 3 else "")
        _err(f"Warning: {len(report.missing)} pinned object(s) are not in the store ({shown}); `put` the file with --pin to restore "
             "one, or `unpin` the pin that names it")          # stdout is the answer only
        exit_code = 1          # the store is not what its pins say: 0 means consistent
    lines.append("nothing changed: this version reports and removes nothing. Deleting an object frees the library's quota, "
                 "and disk space only after the server's history is truncated and its garbage collector has run.")
    _say(rows, args, "\n".join(lines))
    return exit_code


def _subset(text: str) -> "tuple[int, int]":
    """``K/N`` -> (K, N): part K of N, by the object's two-hex fan-out folder (deterministic, so no state file is needed)."""
    match = re.fullmatch(r"(\d+)/(\d+)", text.strip())
    if not match or not 1 <= int(match.group(1)) <= int(match.group(2)) <= 256:
        raise UsageError(f"--subset {text!r}: write K/N with 1 <= K <= N <= 256, as 3/7")
    return int(match.group(1)), int(match.group(2))


def _verify(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    base.check_root(create=False)
    wanted: "list[tuple[refs.Ref, int | None]]" = []
    if args.record:
        from . import record
        try:
            wanted += [(e.ref, e.size) for e in record.parse(Path(args.record).read_text(encoding="utf-8"))]
        except (OSError, record.RecordError) as exc:
            raise UsageError(f"{args.record}: {exc}") from None
    parsed = _refs(args.refs)
    wanted += [(_resolved(base, r), None) for r in parsed]
    if not wanted and not args.record:
        wanted = [(r, s) for r, s in base.objects()]          # an empty record names nothing: it verifies nothing
    if args.subset:
        part, parts = _subset(args.subset)
        wanted = [(r, s) for r, s in wanted if int(r.hex[:2], 16) % parts == part - 1]
    rows: "list[dict[str, Any]]" = []
    bad = 0
    for ref, size in wanted:
        problem = None
        listed = base.stat(ref)
        if listed is None:
            problem = "missing"
        elif size is not None and listed != size:
            problem = f"stored at {listed} bytes, expected {size}"
        elif args.deep:
            progress = _progress(args, "downloaded")
            try:
                path, cached = base.get(ref, keep=False, use_cache=False, progress=progress)
                if not cached:
                    path.unlink(missing_ok=True)
            except store.StoreError as exc:
                problem = str(exc)
            progress.finish()
        rows.append({"ref": str(ref), "ok": problem is None, **({"problem": problem} if problem else {})})
        if problem:
            bad += 1
            _err(f"{PROG}: {ref}: {problem}")
    if args.json:
        _say({"objects": rows, "problems": bad}, args, "")
    else:
        _say({}, args, f"{len(rows) - bad} of {len(rows)} object(s) verified" + (" (read and hashed)" if args.deep else " (present; add --deep to read them)"))
    return 1 if bad else 0


_COMMANDS = {"put": _put, "get": _get, "path": _path, "cat": _cat, "exists": _exists, "stat": _stat, "cache": _cache,
             "ls": _ls, "usage": _usage, "status": _status, "log": _log, "init": _init, "pin": _pin, "unpin": _unpin, "gc": _gc,
             "verify": _verify}


def main(argv: "list[str] | None" = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    try:
        args = parser.parse_args(args_in)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    if args.command in NOT_YET:
        _err(f"{PROG}: `{args.command}` is not in this version yet (put, get, path, cat, exists, stat, cache, ls, usage, status, log, init, pin, unpin, gc and verify are)")
        return 1          # not a mistake in how it was called, so not the usage code
    def failed(code: str, status: int, message: str, retryable: bool = False) -> int:
        """Say it on standard error as ever; with ``--json`` also as one document on standard output, so a program can tell "the
        network failed, run it again" from "that object is not in the store" without reading prose."""
        _err(f"{PROG}: {message}")
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps({"ok": False, "error": {"code": code, "message": message, "retryable": retryable,
                                                                "exit": status}}, sort_keys=True) + "\n")
        return status

    try:
        return _COMMANDS[args.command](args)
    except UsageError as exc:
        return failed("usage", 2, str(exc))
    except SeafileConnectionError as exc:
        return failed("network", 3, f"the server or the network failed ({exc}); run it again", retryable=True)
    except SeafileAPIError as exc:
        return failed("server", 1, f"the server said: {exc}")
    except SeafileAuthError as exc:
        return failed("auth", 1, str(exc))
    except store.StoreTransportError as exc:
        return failed("network", 3, f"{exc} (the server or the network failed: run it again)", retryable=True)
    except (store.StoreError, refs.RefError, OSError) as exc:
        return failed("store" if isinstance(exc, (store.StoreError, refs.RefError)) else "io", 1, str(exc))
    except KeyboardInterrupt:
        _err("\nInterrupted.")
        return 130

if __name__ == "__main__":
    sys.exit(main())
