"""``seafile-blob`` (``sfblob``, ``git-seafile-blob``, ``seafile blob``): the blob store's command line (STORE.md D3, CLI.md).

Plain lines by default and one JSON document with ``--json``; no prompt ever (a command is told what to do by its
flags); exit 0 success, 1 failure, 2 usage error.  ``gc``, ``pin``, ``offload`` and ``verify`` are not in this
version.
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

from ..client import SeafileAuthError
from ..units import format_bytes
from . import cache as cache_mod
from . import refs, store, tree

PROG = "seafile-blob"
_SIZE = re.compile(r"(\d+)\s*([kmgt]?)(?:i?b)?", re.IGNORECASE)
_UNITS = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
NOT_YET = ("gc", "pin", "unpin", "offload", "verify", "link", "read", "usage")


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
    cache = cache_mod.Cache(Path(cache_dir) if cache_dir else None)
    server = parsed.server_url
    plain_http = bool(server) and str(server).lower().startswith("http://")

    def factory() -> store.Store:
        client = SeafileClient(server_url=server)
        rid = client.get_repo_id(parsed.library_name)
        return store.Store(client, rid, parsed.repo_path, cache, verify_by_download=plain_http, factory=factory)

    return factory()


def _say(payload: "dict[str, Any]", args: argparse.Namespace, text: str) -> None:
    if args.json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    elif text:
        sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", help="the store: seafile://<library>/<folder> (default: $SFBLOB_STORE or git config seafile.blob.store)")
    common.add_argument("--cache-dir", help="the local cache (default: $SFBLOB_CACHE or the user's cache folder)")
    common.add_argument("--json", action="store_true", help="answer with one JSON document")
    common.add_argument("--jobs", type=int, default=1, help="transfers at once for a folder (default 1)")

    parser = argparse.ArgumentParser(
        prog=PROG, description="Keep large, immutable files on a Seafile library under their SHA-256, and get them back verified.")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    put = sub.add_parser("put", parents=[common], help="upload files (or a folder with -r) and print their references")
    put.add_argument("paths", nargs="+", help="files, a folder with -r, or - for standard input")
    put.add_argument("-r", "--recursive", action="store_true", help="upload a folder")
    put.add_argument("--record", metavar="FILE", help="with -r: write path -> reference to FILE (one sorted line per entry, for git)")

    get = sub.add_parser("get", parents=[common], help="fetch objects into the cache, verified, and print their paths")
    get.add_argument("refs", nargs="*", help="sha256:<hex> (a prefix of 6 or more hex characters is enough when it is unique)")
    get.add_argument("--to", metavar="DIR", help="copy each object into DIR as its reference (or, with --from, restore the paths there)")
    get.add_argument("--from", dest="record", metavar="FILE", help="restore every path of a record file under --to")

    path = sub.add_parser("path", parents=[common], help="print the cache path of an object, fetching it first")
    path.add_argument("ref")

    cat = sub.add_parser("cat", parents=[common], help="write an object to standard output once it is proved")
    cat.add_argument("ref")

    exists = sub.add_parser("exists", parents=[common], help="say whether objects are in the store (exit 1 when one is not)")
    exists.add_argument("refs", nargs="+")

    stat = sub.add_parser("stat", parents=[common], help="the size of an object in the store")
    stat.add_argument("ref")

    cache = sub.add_parser("cache", parents=[common], help="bound or empty the local cache")
    cache.add_argument("--size", metavar="N", help="evict the least recently used until the cache fits (e.g. 20G)")
    cache.add_argument("--clear", action="store_true", help="remove every cached object")
    return parser


def _put(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    spooled: "list[Path]" = []
    try:
        if args.recursive:
            if len(args.paths) != 1:
                raise UsageError("put -r takes one folder")
            report = tree.put_tree(base, Path(args.paths[0]), Path(args.record) if args.record else None, args.jobs)
            for note in report.skipped:
                sys.stderr.write(f"skipped {note}\n")
            for problem in report.problems:
                sys.stderr.write(f"{PROG}: {problem}\n")
            _say({"entries": [{"path": e.path, "size": e.size, "ref": str(e.ref)} for e in report.entries],
                  "uploaded": report.uploaded, "present": report.present, "skipped": report.skipped,
                  "problems": report.problems, "record": args.record},
                 args, f"{len(report.entries)} file(s): {report.uploaded} uploaded, {report.present} already there"
                 + (f"; record written to {args.record}" if args.record and not report.problems else ""))
            return 1 if report.problems else 0
        if args.record:
            raise UsageError("--record needs -r (a folder)")
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
            try:
                done = base.put(source)
            except store.StoreError as exc:
                sys.stderr.write(f"{PROG}: {exc}\n")
                failed = True
                continue
            results.append({"path": item, "ref": str(done.ref), "size": done.size, "uploaded": done.uploaded})
        if not args.json:
            for row in results:
                sys.stdout.write(f"{row['ref']}  {row['size']}  {row['path']}\n")
            if results:
                sys.stdout.write("Note: these objects are not protected from `gc` (pins come with a later version)\n")
        else:
            _say({"files": results}, args, "")
        return 1 if failed else 0
    finally:
        for temp in spooled:
            temp.unlink(missing_ok=True)


def _get(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    if args.record:
        if not args.to:
            raise UsageError("--from needs --to DIR")
        report = tree.get_tree(base, Path(args.record), Path(args.to), args.jobs)
        for problem in report.problems:
            sys.stderr.write(f"{PROG}: {problem}\n")
        _say({"restored": report.restored, "in_place": report.in_place, "problems": report.problems}, args,
             f"{report.restored} restored, {report.in_place} already in place" + (f", {len(report.problems)} problem(s)" if report.problems else ""))
        return 1 if report.problems else 0
    if not args.refs:
        raise UsageError("get needs a reference or --from FILE")
    rows: "list[dict[str, Any]]" = []
    failed = False
    for text in args.refs:
        try:
            ref = base.resolve(refs.parse(text))
            if args.to:
                folder = Path(args.to)
                folder.mkdir(parents=True, exist_ok=True)
                where = base.get_to(ref, folder / ref.hex)
            else:
                where, _ = base.get(ref)
        except (refs.RefError, store.StoreError) as exc:
            sys.stderr.write(f"{PROG}: {exc}\n")
            failed = True
            continue
        rows.append({"ref": str(ref), "path": str(where)})
    if args.json:
        _say({"objects": rows}, args, "")
    else:
        for row in rows:
            sys.stdout.write(row["path"] + "\n")
    return 1 if failed else 0


def _path(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    where, _ = base.get(base.resolve(refs.parse(args.ref)))
    _say({"path": str(where)}, args, str(where))
    return 0


def _cat(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    base.cat(base.resolve(refs.parse(args.ref)), sys.stdout.buffer)
    sys.stdout.buffer.flush()
    return 0


def _exists(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    rows = []
    for text in args.refs:
        try:
            ref = base.resolve(refs.parse(text))
            rows.append({"ref": str(ref), "exists": base.exists(ref)})
        except (refs.RefError, store.StoreError) as exc:
            rows.append({"ref": text, "exists": False, "error": str(exc)})
    if args.json:
        _say({"objects": rows}, args, "")
    else:
        for row in rows:
            sys.stdout.write(f"{row['ref']}  {'present' if row['exists'] else 'missing'}\n")
    return 0 if all(r["exists"] for r in rows) else 1


def _stat(args: argparse.Namespace) -> int:
    base = open_store(store_url(args.store), args.cache_dir)
    ref = base.resolve(refs.parse(args.ref))
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
    else:
        raise UsageError("cache needs --size N or --clear")
    used = cache.used()
    _say({"removed": removed, "used": used, "directory": str(cache.root)}, args,
         f"removed {removed} object(s); the cache holds {format_bytes(used)} in {cache.root}")
    return 0


_COMMANDS = {"put": _put, "get": _get, "path": _path, "cat": _cat, "exists": _exists, "stat": _stat, "cache": _cache}


def main(argv: "list[str] | None" = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if args_in and args_in[0] in NOT_YET:
        sys.stderr.write(f"{PROG}: `{args_in[0]}` is not in this version yet (put, get, path, cat, exists, stat and cache are)\n")
        return 2
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
        sys.stderr.write(f"{PROG}: {exc}\n")
        return 2
    except SeafileAuthError as exc:
        sys.stderr.write(f"{PROG}: {exc}\n")
        return 1
    except (store.StoreError, refs.RefError) as exc:
        sys.stderr.write(f"{PROG}: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
