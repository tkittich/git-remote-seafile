

# -- Payload restore (see 75_payload.py) -------------------------------------------

# -- restore -------------------------------------------------------------------


def _download_verified(
    client: Any, rid: str, source_path: str, dest: Path, item: dict[str, Any]
) -> str | None:
    """Fetch one object to ``dest`` via a ``.part`` file; ``None`` on success, else why not."""
    part = dest.with_name(dest.name + ".part")
    last = ""
    for _attempt in range(3):
        try:
            if not client.download_file_to(rid, source_path, part):
                part.unlink(missing_ok=True)
                return "object not found on the server"
            if part.stat().st_size != item["size"]:
                last = f"size {part.stat().st_size}, manifest says {item['size']}"
                continue
            if _sha256_file(part) != item["sha256"]:
                last = "SHA-256 does not match the manifest"
                continue
            os.replace(part, dest)
            return None
        except SnapshotError as exc:
            last = str(exc)
        except Exception as exc:  # transient network or API error: try again
            last = f"{type(exc).__name__}: {exc}"
    part.unlink(missing_ok=True)
    return last


def restore_payload(remote: str, into: Path) -> None:
    """Download and verify the payload a restored snapshot's manifest names.

    A security problem in the manifest refuses it whole.  An entry this platform
    cannot hold, one that would overwrite something already there, or one that
    fails to download or verify is skipped and reported -- the rest is restored
    -- and the run then fails.  A file already in place that verifies is kept, so
    a rerun into the same directory picks up where it stopped.
    """
    manifest_path = into / PAYLOAD_MANIFEST
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (ValueError, RecursionError) as exc:
        raise SnapshotError(f"the snapshot's payload manifest is unreadable: {exc}") from None
    files = validate_manifest(manifest, into)
    library, base, server_url = payload_location(remote)
    client = _api(lambda: _open_client(server_url), "opening the Seafile client")
    rid = _api(lambda: client.get_repo_id(library), f"finding library {library!r}")
    print(f"restoring {len(files)} payload file(s) from {base}/objects ...", flush=True)
    problems: list[str] = []
    done = 0
    for index, item in enumerate(files, 1):
        rel, sha = item["path"], item["sha256"]
        if os.name == "nt":
            why = windows_name_problem(rel)
            if why:
                problems.append(f"{rel}: skipped, the name {why}")
                continue
        dest = into / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{rel}: skipped, cannot create its directory ({exc})")
            continue
        if dest.exists() or dest.is_symlink():
            if dest.is_file() and not dest.is_symlink() and _sha256_file(dest) == sha:
                done += 1
                continue
            problems.append(f"{rel}: skipped, something else is already there (a name that "
                            "this filesystem treats as the same as another file's)")
            continue
        print(f"downloading [{index}/{len(files)}] {rel} ({human_bytes(item['size'])})", flush=True)
        why = _download_verified(client, rid, f"{base}/{object_dir(sha)}/{sha}", dest, item)
        if why is not None:
            problems.append(f"{rel}: {why}")
            continue
        try:
            if item.get("mode") == "100755" and os.name != "nt":
                dest.chmod(dest.stat().st_mode | 0o111)
            os.utime(dest, ns=(item["mtime_ns"], item["mtime_ns"]))
        except (OSError, OverflowError, ValueError):
            pass  # content verified; timestamps and mode are best effort
        done += 1
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise SnapshotError(
            f"{len(problems)} of {len(files)} payload file(s) were not restored "
            f"({done} were, and verified):\n  {shown}{more}"
        )
    print(f"payload verified: {len(files)} file(s), SHA-256 matches the manifest")


def portable_name_warning(paths: list[str]) -> str | None:
    """A warning for payload paths a Windows restore would skip, or ``None``."""
    found = [(p, windows_name_problem(p)) for p in paths]
    bad = [(p, why) for p, why in found if why]
    if not bad:
        return None
    shown = "\n".join(f"  {p}: {why}" for p, why in bad[:10])
    more = f"\n  ... and {len(bad) - 10} more" if len(bad) > 10 else ""
    return (
        f"\nWARNING: {len(bad)} payload path(s) cannot be restored on Windows under that "
        f"name (a restore there skips and reports them; elsewhere they restore):\n{shown}{more}"
    )
