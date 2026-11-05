

#: Less than this to hash and the serial loop is done in about a second, so a
#: search for the right parallelism would only add start-up cost.
_HASH_MIN_BYTES = 512 * 1024 * 1024

#: The most files hashed at once.  Measured on a 24-core NVMe machine (page
#: cache, so CPU only): 906, 1579, 2815, 4209 and 5140 MiB/s at 1, 2, 4, 8 and
#: 16 threads.  A disk slower than that is found by the search, not assumed.
_HASH_MAX_WORKERS = 16

#: Hashing is steady and its files are quick, so a reading needs far less time
#: than an upload's ten-second window; a 6 GiB tree is hashed in under five
#: seconds, which the upload's 14 seconds per level would never have measured.
_HASH_SETTLE_SECONDS = 1.0
_HASH_WINDOW_SECONDS = 3.0


def hash_workers() -> "tuple[int, bool]":
    """``(ceiling, typed)`` for hashing: ``SEAFILE_SNAPSHOT_HASH_WORKERS`` or the CPU count.

    ``1`` is the plain serial loop, exactly as before the search existed.
    """
    return env_workers(
        "SEAFILE_SNAPSHOT_HASH_WORKERS", os.environ.get("SEAFILE_SNAPSHOT_HASH_WORKERS", ""),
        max(1, min(_HASH_MAX_WORKERS, os.cpu_count() or 1)),
    )


def run_hashing(
    todo: "list[str]",
    sizes: "dict[str, int]",
    work: "Callable[[str], None]",
    meter: _ByteMeter,
    cancel: threading.Event,
    *,
    quiet: bool = False,
) -> None:
    """Call ``work(rel)`` for each path in *todo*, hashing several at a time where that pays.

    A first run over a large tree is hours of ``sha256`` on one core when the
    disk could feed several.  Whether it can is a property of the disk, so the
    number of files in flight is searched for (:func:`run_climbing`), starting
    at one: on a spinning disk the search finds that more only seeks, and costs
    a few ten-second windows of a run that is disk-bound anyway.  Below
    ``_HASH_MIN_BYTES``, with one file, or with ``SEAFILE_SNAPSHOT_HASH_WORKERS=1``
    this is the plain loop in the order given.  The manifest does not depend on
    any of it: the caller assembles its entries in path order afterwards.
    """
    ceiling, typed = hash_workers()
    total = sum(sizes[rel] for rel in todo)
    if ceiling < 2 or len(todo) < 2 or total < _HASH_MIN_BYTES:
        for rel in todo:
            work(rel)
        return

    def say(line: str) -> None:
        if not quiet:
            print(line, flush=True)

    ceiling = min(ceiling, len(todo))
    say(
        f"payload: hashing {len(todo)} file(s) ({human_bytes(total)}) with "
        + (f"{ceiling} parallel hashers" if typed
           else f"up to {ceiling} parallel hashers, found by measuring")
    )

    # Largest first: the last file in flight is then a small one, not a 100 GiB
    # tail that leaves every other core idle.
    run_climbing(
        sorted(todo, key=lambda rel: (-sizes[rel], rel)), work,
        ceiling=ceiling, meter=meter, exhausted=lambda *_: None,
        start=1, noun="hasher", forced=typed, say=say, abort=cancel.set,
        settle=_HASH_SETTLE_SECONDS, window_seconds=_HASH_WINDOW_SECONDS,
    )
