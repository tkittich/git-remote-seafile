

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
        f"{len(in_git)} of {human_bytes(_AUTO_LOWER)}-{human_bytes(_AUTO_UPPER)} that compress "
        f"(sample keeps under {ratio:g} of its size)"
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
        f"tip: --payload-auto-ratio {_WHATIF_RATIO:g} would move {len(moved)} file(s) "
        f"({human_bytes(moved_b)}) from git to payload: about {seconds / 60:.0f} min faster "
        f"here ({deflate:.0f} MiB/s deflate, {upload:.0f} MiB/s upload"
        f"{', assumed' if assumed else ''}), for about {human_bytes(int(extra))} more on the "
        f"server and without git's delta between snapshots. Not applied."
    )
    return lines
