"""config.py - Git and environment configuration resolution for git-remote-seafile."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from .git_util import (
    get_git_config,
    get_git_config_bool,
    get_git_config_int,
    run_git,
)

#: The automatic tail merge's download budget, as a multiple of the largest
#: pack the helper creates (`seafile.gcmaxpackmib`): 2 GiB at the default 1 GiB.
#: Measured against a real server (soak, 2026-10-13): merging costs about
#: 0.05 s per MiB end to end (64 MiB/s down, 32 MiB/s up), and the push lock is
#: held throughout, so 2 GiB is roughly a minute and a half during which other
#: pushers see "locked".  It used to be a second key (`seafile.gcmaxmib`) and
#: 8 x -- a six-minute hold.
GC_BUDGET_FACTOR = 2

def remote_cap_key(remote_name: str | None) -> str | None:
    """The git config key that holds one remote's pack cap, or ``None`` without a remote name.

    ``remote.<name>.seafilegcmaxpackmib``: a refused upload (HTTP 413) is a fact about one
    server's proxy, so what the helper learns is kept beside that remote and not in the
    repository-wide ``seafile.gcmaxpackmib``, which would also lower the cap of every other
    remote and the tail merge's frozen-pack line there (ROADMAP Z31).  A push by URL has no
    name (git hands the helper the URL), and nothing is kept for it.
    """
    if not remote_name or "://" in remote_name or "\n" in remote_name:
        return None
    return f"remote.{remote_name}.seafilegcmaxpackmib"


def remote_name_for_url(url: str) -> str | None:
    """The name of the configured remote whose URL is *url*, for commands given only a URL."""
    if not url:
        return None
    out, _err, code = run_git(["config", "--get-regexp", r"^remote\..*\.url$"])
    if code != 0:
        return None
    wanted = url.rstrip("/")
    for line in out.decode("utf-8", errors="replace").splitlines():
        key, _, value = line.partition(" ")
        if value.strip().rstrip("/") == wanted and key.startswith("remote.") and key.endswith(".url"):
            return key[len("remote."):-len(".url")]
    return None


#: A pack at least this fraction of the size cap counts as full and is left
#: where it stands by the tail merge.  The cap itself cannot be the freeze
#: line: ``pack-objects --max-pack-size`` stops *before* exceeding the cap, so
#: a full pack is always a little under it and would look movable forever.
FREEZE_FRACTION = 0.5

#: The settlement window in seconds.  Not a user setting: it is a race guard,
#: and switching it off trades a correctness property for a second per push.
#: The single exception is the test suite, which runs one in-process server with
#: no concurrent writers and sets ``SEAFILE_TEST_LOCK_SETTLE=0`` so it does not
#: pay the second 600 times.
DEFAULT_LOCK_SETTLE = 1


def default_lock_settle() -> int:
    """The settlement window: :data:`DEFAULT_LOCK_SETTLE`, or the test override."""
    raw = os.environ.get("SEAFILE_TEST_LOCK_SETTLE", "").strip()
    if not raw:
        return DEFAULT_LOCK_SETTLE
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_LOCK_SETTLE


__all__ = [
    "RemoteConfig",
    "get_git_config",
    "get_git_config_bool",
    "get_git_config_int",
]


@dataclass(frozen=True)
class RemoteConfig:
    """Resolved configuration for a remote Seafile session."""

    lock_timeout: int
    lock_lease: int
    lock_settle: int
    auto_gc: bool
    gc_threshold: int
    # Defaults at the dataclass level too, so constructing a config directly
    # (tests, embedded callers) never needs to know every knob.
    # The budget is *derived* from gc_max_pack_mib by load(); the field
    # stays so a config can still be constructed directly (tests, embedded
    # callers) with an explicit value.
    gc_max_mib: int = GC_BUDGET_FACTOR * 1024
    #: The largest pack the helper writes (``pack-objects --max-pack-size``),
    #: for pushes, tail merges and full gc alike.  Seafile deployments cap a
    #: single request -- measured on one server: 1.75 GiB accepted, 2 GiB reset --
    #: and an uncapped merge produced a pack the server would not take.  A file
    #: of 1 GiB or more is uploaded in Content-Range chunks, which is what lets
    #: one object larger than the cap through as a single larger pack.
    gc_max_pack_mib: int = 1024

    @property
    def freeze_bytes(self) -> int:
        """Packs at or above this size are left alone by the tail merge."""
        return int(self.gc_max_pack_mib * 1024 * 1024 * FREEZE_FRACTION)

    @property
    def max_pack_bytes(self) -> int:
        """The size cap handed to ``--max-pack-size``."""
        return self.gc_max_pack_mib * 1024 * 1024

    @classmethod
    def load(cls, remote_name: str | None = None) -> RemoteConfig:
        """Load configuration from Git config with documented defaults.

        *remote_name* is the remote this session talks to: its own pack cap
        (:func:`remote_cap_key`) wins over ``seafile.gcmaxpackmib``.

        Values are used as the user sets them, except where they would defeat
        the mechanism outright: a zero or negative lease expires every lock
        immediately (permanent contention), a negative timeout never waits,
        and a sub-1 gc threshold compacts on every push.  Those fall back to
        the documented defaults with a warning instead of being honored --
        the config is a way to tune the protocol, not to switch it off.

        `seafile.locksettle` is the one setting where 0 is meaningful: it
        switches off the settlement re-scan (see RemoteLock.acquire), which is
        a race guard rather than the lock itself.  Only a negative value -- a
        settlement window that would elapse before it started -- is refused.
        """
        # Annotated because the literal alone infers ``dict[str, int]`` -- bool
        # is a subtype of int, so ``auto_gc`` does not widen it -- and that
        # cannot then be splatted into the dataclass's ``auto_gc: bool`` field.
        # These read every scope, repository-local included, on purpose: the
        # lease a contender honours travels in the holder's ticket (see
        # RemoteLock._is_lock_active), so a repo-local value reaches only the
        # checkout that set it. DESIGN section 4.8 records the reasoning, and
        # why gittimeout/skipsafetychecks are different (the trusted readers
        # in git_util).
        values: dict[str, Any] = {
            "lock_timeout": get_git_config_int("seafile.locktimeout", 15),
            "lock_lease": get_git_config_int("seafile.locklease", 60),
            "lock_settle": default_lock_settle(),
            "auto_gc": get_git_config_bool("seafile.autogc", True),
            "gc_threshold": get_git_config_int("seafile.gcthreshold", 20),
            # Per-pack freeze threshold for the tail merge: packs at or above
            # this size are left where they stand, so each cycle moves only the
            # small new packs and cost stays bounded regardless of history.
            "gc_max_pack_mib": get_git_config_int("seafile.gcmaxpackmib", 1024),
        }
        cap_key = "seafile.gcmaxpackmib"
        per_remote = remote_cap_key(remote_name)
        if per_remote is not None and get_git_config(per_remote) is not None:
            cap_key = per_remote
            values["gc_max_pack_mib"] = get_git_config_int(per_remote, values["gc_max_pack_mib"])

        fallbacks: list[str] = []
        if values["lock_timeout"] < 0:
            fallbacks.append(f"seafile.locktimeout={values['lock_timeout']} -> 15")
            values["lock_timeout"] = 15
        if values["lock_lease"] <= 0:
            fallbacks.append(f"seafile.locklease={values['lock_lease']} -> 60")
            values["lock_lease"] = 60
        if values["gc_threshold"] < 1:
            fallbacks.append(f"seafile.gcthreshold={values['gc_threshold']} -> 20")
            values["gc_threshold"] = 20
        # A sub-1 MiB freeze threshold would make every pack -- including old,
        # large consolidated ones -- movable, defeating the bounded-cost design.
        if values["gc_max_pack_mib"] < 1:
            fallbacks.append(f"{cap_key}={values['gc_max_pack_mib']} -> 1024")
            values["gc_max_pack_mib"] = 1024
        values["gc_max_mib"] = GC_BUDGET_FACTOR * values["gc_max_pack_mib"]
        if fallbacks:
            sys.stderr.write(
                "Warning: implausible seafile.* config ignored ("
                + "; ".join(fallbacks)
                + ").\n"
            )
            sys.stderr.flush()

        return cls(**values)
