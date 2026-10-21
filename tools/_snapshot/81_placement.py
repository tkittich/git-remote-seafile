

# -- Where files go: the placement report and its what-if -----------------------------
#
# The auto payload rule (PAYLOAD.md D1) sorts a tree three ways: files under 4 MiB
# stay in git, files of 64 MiB or more go to payload, and the band between is decided
# by how well a sample of the file deflates.  Which side of that last line a band
# file falls on is a *speed* question -- deflating it through git against uploading
# it as it is -- and the answer depends on this machine and this link, so the run
# says what it did and, when a lower cut-off would plainly be faster here, says so
# without doing it.  A default is not a request: nothing moves because of this text.

#: Rates used until a run has measured them (``snapshot.uplink`` / ``snapshot.uplink1``
#: in the vault).  Measured on one server: 3 uploads at once ~53 MiB/s, one ~32 MiB/s;
#: SHA-256 on one core ~2 GiB/s.
_ASSUMED_UPLOAD_MIBS = 53.0
_ASSUMED_GIT_PUSH_MIBS = 32.0
_ASSUMED_HASH_MIBS = 2000.0
#: The cut-off the what-if tries, and how much it must save to be worth a line.
_WHATIF_RATIO = 0.5
_WHATIF_MIN_SECONDS = 60.0
_WHATIF_MIN_BYTES = 1024 ** 3
_DEFLATE_PROBE_FILES = 8


def link_rates(vault_config: dict[str, list[str]]) -> tuple[float, float, bool]:
    """``(upload MiB/s with the default jobs, one stream MiB/s, assumed)``.

    Measured values are what an earlier run stored in the vault; either one
    missing or unusable falls back to the assumed rate for that number.
    """
    def stored(key: str, fallback: float) -> tuple[float, bool]:
        try:
            value = float((vault_config.get(key) or [""])[0])
        except ValueError:
            return fallback, True
        return (value, False) if value > 0 else (fallback, True)

    upload, a1 = stored("snapshot.uplink", _ASSUMED_UPLOAD_MIBS)
    single, a2 = stored("snapshot.uplink1", _ASSUMED_GIT_PUSH_MIBS)
    return upload, single, a1 or a2


def measure_deflate_mibs(source: Path, band: list[tuple[int, float, str]]) -> float | None:
    """Deflate speed (zlib level 6, git's default) on real data from *band*.

    Reads the first 256 KiB of up to eight files spread across the band, so the
    figure is for the data in question and not for a synthetic block; about 2 MiB
    and a few hundredths of a second.  ``None`` when nothing could be read.
    """
    if not band:
        return None
    step = max(1, len(band) // _DEFLATE_PROBE_FILES)
    raw = 0
    spent = 0.0
    for _size, _ratio, rel in band[::step][:_DEFLATE_PROBE_FILES]:
        try:
            with open(source / rel, "rb") as fh:
                chunk = fh.read(_AUTO_SAMPLE_CHUNK)
        except OSError:
            continue
        started = time.perf_counter()
        zlib.compress(chunk, 6)
        spent += time.perf_counter() - started
        raw += len(chunk)
    if not raw or spent <= 0:
        return None
    return raw / 1024 ** 2 / spent


def placement_lines(
    survey: dict[str, Any], payload: dict[str, Candidate], ratio: float, source: Path,
    vault_config: dict[str, list[str]],
) -> list[str]:
    """What the auto rule did with this tree, and a cheaper cut-off if there is one."""
    small_n, small_b = survey["small"]
    band = survey["mid"]
    in_git = [m for m in band if m[1] < ratio]
    git_n = small_n + len(in_git)
    git_b = small_b + sum(m[0] for m in in_git)
    pay_b = sum(item[0] for item in payload.values())
    lines = [
        f"placement: {len(payload)} file(s) ({human_bytes(pay_b)}) go to payload; {git_n} "
        f"({human_bytes(git_b)}) stay in git -- {small_n} under {human_bytes(_AUTO_LOWER)} and "
        f"{len(in_git)} of {human_bytes(_AUTO_LOWER)}-{human_bytes(_AUTO_UPPER)} that deflate "
        f"by more than {1 - ratio:.0%}"
    ]
    if ratio <= _WHATIF_RATIO:
        return lines
    moved = [m for m in band if _WHATIF_RATIO <= m[1] < ratio]
    moved_b = sum(m[0] for m in moved)
    if moved_b < _WHATIF_MIN_BYTES:
        return lines
    deflate = measure_deflate_mibs(source, in_git)
    if not deflate:
        return lines
    upload, single, assumed = link_rates(vault_config)
    mib = 1024 ** 2
    # Per MiB: through git it is deflated and the compressed bytes go up one stream
    # at a time; as payload it is hashed and goes up as it is, jobs at once.
    seconds = sum(
        (size / mib) * (1 / deflate + r / single - 1 / upload - 1 / _ASSUMED_HASH_MIBS)
        for size, r, _rel in moved
    )
    if seconds < _WHATIF_MIN_SECONDS:
        return lines
    extra = sum(size * (1 - r) for size, r, _rel in moved)
    lines.append(
        f"Hint: --payload-auto-ratio {_WHATIF_RATIO:g} would move {len(moved)} file(s) "
        f"({human_bytes(moved_b)}) from git to payload: about {seconds / 60:.0f} min faster "
        f"here ({deflate:.0f} MiB/s deflate, {upload:.0f} MiB/s upload"
        f"{', assumed' if assumed else ''}), for about {human_bytes(int(extra))} more on the "
        f"server and without git's delta between snapshots. Not applied."
    )
    return lines


# -- Measuring this machine and its link, once, for a new vault ------------------------
#
# Which cut-off is faster depends on two speeds the tool can measure: how fast this
# machine deflates the data that would stay in git, and how fast the link takes an
# upload.  A *new* vault with a gigabyte or more of such data measures both on its
# first real run and remembers the result (``snapshot.payloadratio``), so the choice
# is made once and stays: a cut-off that changed from run to run would move files
# between git and payload.  An established vault is never re-tuned on its own -- a
# default is not a request -- and neither is one whose remote already holds payload
# objects, because two machines on different cut-offs would move files back and
# forth.  ``--payload-auto-ratio measure`` measures again on request.

#: Each probe file is this big, and the aggregate probe runs this many at once -- the
#: default ``--payload-jobs``.  On the real server 8 MiB was too short: a transfer of
#: ~0.25 s is mostly request latency, and three streams read *slower* than one.
_PROBE_BYTES = 16 * 1024 ** 2
_PROBE_STREAMS = 3


def tuned_ratio(deflate: float, upload: float, single: float) -> float:
    """The cut-off at which payload and git take equally long, kept in [0.5, 0.9].

    Per MiB, git deflates it (``1/deflate``) and sends the compressed share down
    one stream (``ratio/single``); payload hashes it and sends all of it, *jobs* at
    once (``1/upload``).  Payload is faster when ``ratio`` exceeds
    ``single * (1/upload + 1/hash - 1/deflate)``.  On a fast link that is zero or
    negative, which would send everything of 4 MiB or more to payload; the floor
    keeps the highly compressible files in git, where they cost little to store and
    deltas apply, and the ceiling is the old fixed default.
    """
    breakeven = single * (1 / upload + 1 / _ASSUMED_HASH_MIBS - 1 / deflate)
    return min(_AUTO_INCOMPRESSIBLE, max(_WHATIF_RATIO, breakeven))


def probe_link(remote: str, *, force: bool = False) -> dict[str, Any] | None:
    """Upload speed to the payload store: ``{"single", "upload", "in_use"}`` in MiB/s.

    One stream, then ``_PROBE_STREAMS`` at once, each of ``_PROBE_BYTES`` random
    bytes named ``probe-*`` directly in ``objects/``, outside the 256 fan-out
    directories that gc, verify, view and restore read, so no payload tool ever
    sees one; all are deleted again (a crash can leave one, which is safe to delete).
    ``in_use`` is whether the store already holds objects; unless *force*, that
    is reported without uploading anything.  Any failure is ``None``: a
    measurement must never fail a run.
    """
    try:
        library, base, server_url = payload_location(remote)
        client = _open_client(server_url)
        rid = client.get_repo_id(library)
        directory = f"{base}/objects"
        in_use = any(e.get("type") == "dir" for e in client.list_dir(rid, directory))
        if in_use and not force:
            return {"in_use": True}
        client.mkdir_p(rid, directory)
    except Exception:
        return None
    names: list[str] = []
    mib = _PROBE_BYTES / 1024 ** 2
    try:
        data = os.urandom(_PROBE_BYTES)
        nonce = os.urandom(4).hex()

        def push(index: int, worker: Any) -> bool:
            name = f"probe-{nonce}-{index}{_PART_SUFFIX}{nonce}"
            names.append(name)
            return bool(worker.upload_file(rid, directory, name, data))

        started = time.perf_counter()
        if not push(0, client):
            return None
        single = mib / (time.perf_counter() - started)
        workers = [_open_client(server_url) for _ in range(_PROBE_STREAMS)]
        for worker in workers:
            worker.list_dir(rid, directory)  # connect now, so setup is not timed as upload
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=_PROBE_STREAMS) as pool:
            done = list(pool.map(lambda i: push(i + 1, workers[i]), range(_PROBE_STREAMS)))
        if not all(done):
            return None
        # Never below one stream: if the server serialises uploads, one is what we get.
        upload = max(single, _PROBE_STREAMS * mib / (time.perf_counter() - started))
        return {"single": single, "upload": upload, "in_use": in_use}
    except Exception:
        return None
    finally:
        for name in names:
            try:
                client.delete_entry(rid, f"{directory}/{name}")
            except Exception:
                pass  # a probe file left behind is ignored by every payload tool


def tuning_mode(
    typed: float | str | None, vault: Path, *, dry_run: bool, auto_rule: bool,
    remote: str | None, has_history: bool,
) -> str | None:
    """``"forced"`` (asked for), ``"new"`` (a vault with nothing remembered yet) or None."""
    if dry_run or not auto_rule or not remote:
        return None
    if typed == "measure":
        return "forced"
    if typed is not None or has_history:
        return None
    if _config(vault, "snapshot.payload") or _config(vault, "snapshot.payloadratio"):
        return None
    return "new"


def tune_payload_ratio(
    remote: str, source: Path, survey: dict[str, Any], *, forced: bool
) -> dict[str, Any] | None:
    """Measure and choose, or ``None`` when there is nothing to decide.

    The result carries ``ratio``, the two measured rates and the deflate speed, and
    a ``message`` to print; with no ``ratio`` the message says why the default was
    kept.  Nothing is written here -- :func:`remember_tuning` does that after a
    complete run.
    """
    in_git = [m for m in survey["mid"] if m[1] < _AUTO_INCOMPRESSIBLE]
    if sum(m[0] for m in in_git) < _WHATIF_MIN_BYTES:
        return None  # too little at stake to touch the server for
    deflate = measure_deflate_mibs(source, in_git)
    if not deflate:
        return None
    probe = probe_link(remote, force=forced)
    if probe is None:
        return {"message": "could not measure the link (payload cut-off stays "
                           f"{_AUTO_INCOMPRESSIBLE:g}); --payload-auto-ratio measure tries again"}
    if probe.get("in_use") and "single" not in probe:
        return {"message": "this remote already holds payload objects, so its cut-off "
                           f"({_AUTO_INCOMPRESSIBLE:g} unless set) is kept: machines must agree. "
                           "--payload-auto-ratio measure chooses one for this vault"}
    ratio = tuned_ratio(deflate, probe["upload"], probe["single"])
    verdict = (f"the default cut-off {ratio:.2f} stands" if ratio >= _AUTO_INCOMPRESSIBLE
               else f"cut-off {ratio:.2f}")
    return {
        "ratio": ratio, "deflate": deflate, "upload": probe["upload"], "single": probe["single"],
        "message": (
            f"tuned: {verdict} -- 4-64 MiB files that deflate by less than {1 - ratio:.0%} go "
            f"to payload (deflate {deflate:.0f} MiB/s here; "
            f"uploads {probe['upload']:.0f} MiB/s with {_PROBE_STREAMS} at once, "
            f"{probe['single']:.0f} MiB/s with one). Remembered for this vault; "
            "--payload-auto-ratio R sets it, --payload-auto-ratio measure measures again"
        ),
    }


def remember_tuning(vault: Path, tuning: dict[str, Any] | None, *, dry_run: bool) -> None:
    """Keep a measured cut-off and the link rates after a *complete* real run."""
    if not tuning or tuning.get("ratio") is None or dry_run:
        return
    for key, value in (("snapshot.payloadratio", repr(float(tuning["ratio"]))),
                       ("snapshot.payloadratiosource", "measured"),
                       ("snapshot.uplink", f"{tuning['upload']:.1f}"),
                       ("snapshot.uplink1", f"{tuning['single']:.1f}")):
        _run(
            ["git", "-C", str(vault), "config", key, value],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
