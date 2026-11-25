"""``seafile-blob`` (``sfblob``, ``git-seafile-blob``, ``seafile blob``): the blob store's command line (STORE.md D3, CLI.md).

Plain lines by default and one JSON document with ``--json``; no prompt ever (a command is told what to do by its
flags); exit 0 success, 1 failure, 2 usage error.  Standard output is the answer and nothing else; every note,
warning and progress line goes to standard error.  ``gc``, ``pin``, ``offload`` and ``verify`` are not in this version.
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

from ..client import SeafileAPIError, SeafileAuthError
from ..units import format_bytes
from . import cache as cache_mod
from . import refs, store, tree
from .progress import Progress

PROG = "seafile-blob"
_SIZE = re.compile(r"(\d+)\s*([kmgt]?)(?:i?b)?", re.IGNORECASE)
_UNITS = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
NOT_YET = ("gc", "pin", "unpin", "offload", "verify", "link", "read", "usage")
_UNPROTECTED = "Note: these objects are not protected from `gc` (pins come with a later version)"


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
    plain_http = bool(server) and str(server).lower().startswith("http://")
    known: "dict[str, str]" = {}          # the library is looked up once, not once per file of a folder

    def factory() -> store.Store:
        client = SeafileClient(server_url=server)
        if "id" not in known:
            known["id"] = client.get_repo_id(parsed.library_name)
        return store.Store(client, known["id"], parsed.repo_path, cache, verify_by_download=plain_http, factory=factory)

    return factory()


def _say(payload: "dict[str, Any]", args: argparse.Namespace, text: str) -> None:
    if args.json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    elif text:
        sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))


def _err(text: str) -> None:
    sys.stderr.write(text if text.endswith("\n") else text + "\n")


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
    common.add_argument("--jobs", type=int, default=default(1), help="transfers at once for a folder (default 1)")
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
    cache.add_argument("--size", metavar="N", help="evict the least recently used until the cache fits (e.g. 20G)")
    cache.add_argument("--clear", action="store_true", help="remove every cached object")
    return parser


def _put(args: argparse.Namespace) -> int:
    for item in args.paths:
        if item != "-" and Path(item).is_dir() and not args.recursive:
            raise UsageError(f"{item} is a folder: use `{PROG} put -r {item} --record FILE` to upload what is in it")
    base = open_store(store_url(args.store), args.cache_dir)
    spooled: "list[Path]" = []
    try:
        if args.recursive:
            if len(args.paths) != 1:
                raise UsageError("put -r takes one folder")
            report = tree.put_tree(base, Path(args.paths[0]), Path(args.record) if args.record else None, args.jobs,
                                   dry_run=args.dry_run, exclude=args.exclude)
            for note in report.skipped:
                _err(f"Warning: skipped {note}")
            for problem in report.problems:
                _err(f"{PROG}: {problem}")
            if not args.record and not args.json:
                for e in report.entries:                # no record file: the references are the answer
                    sys.stdout.write(f"{e.ref}  {e.size}  {e.path}\n")
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
            if report.entries and not report.dry_run and not report.problems:
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
                done = base.would_put(source) if args.dry_run else base.put(source, progress=progress)
            except store.StoreError as exc:
                progress.finish()
                _err(f"{PROG}: {exc}")
                failed = True
                results.append({"path": item, "error": str(exc)})
                continue
            progress.finish()
            results.append({"path": item, "ref": str(done.ref), "size": done.size, "uploaded": done.uploaded})
        if args.json:
            _say({"files": results}, args, "")
        else:
            for row in results:
                if "ref" in row:
                    sys.stdout.write(f"{row['ref']}  {row['size']}  {row['path']}\n")
            if any("ref" in row for row in results) and not args.dry_run:
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
    if args.clear:
        removed = cache.clear()
    elif args.size:
        removed = len(cache.evict(parse_size(args.size)))
    used = cache.used()
    held = len(cache.objects())
    _say({"removed": removed, "used": used, "objects": held, "directory": str(cache.root)}, args,
         (f"removed {removed} object(s); " if (args.clear or args.size) else "")
         + f"the cache holds {held} object(s), {format_bytes(used)}, in {cache.root}")
    return 0


_COMMANDS = {"put": _put, "get": _get, "path": _path, "cat": _cat, "exists": _exists, "stat": _stat, "cache": _cache}


def main(argv: "list[str] | None" = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if args_in and args_in[0] in NOT_YET:
        _err(f"{PROG}: `{args_in[0]}` is not in this version yet (put, get, path, cat, exists, stat and cache are)")
        return 1          # not a mistake in how it was called, so not the usage code
    try:
        args = parser.parse_args(args_in)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    try:
        return _COMMANDS[args.command](args)
    except UsageError as exc:
        _err(f"{PROG}: {exc}")
        return 2
    except SeafileAPIError as exc:
        _err(f"{PROG}: the server said: {exc}")
        return 1
    except SeafileAuthError as exc:
        _err(f"{PROG}: {exc}")
        return 1
    except (store.StoreError, refs.RefError, OSError) as exc:
        _err(f"{PROG}: {exc}")
        return 1
    except KeyboardInterrupt:
        _err("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
