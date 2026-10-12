"""config.py - Git and environment configuration resolution for git-remote-seafile."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from .git_util import (
    get_git_config,
    get_git_config_bool,
    get_git_config_int,
)

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
    gc_max_mib: int = 8192
    gc_max_pack_mib: int = 1024

    @classmethod
    def load(cls) -> RemoteConfig:
        """Load configuration from Git config with documented defaults.

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
        values: dict[str, Any] = {
            "lock_timeout": get_git_config_int("seafile.locktimeout", 15),
            "lock_lease": get_git_config_int("seafile.locklease", 60),
            "lock_settle": get_git_config_int("seafile.locksettle", 1),
            "auto_gc": get_git_config_bool("seafile.autogc", False),
            "gc_threshold": get_git_config_int("seafile.gcthreshold", 20),
            # Download budget for the automatic tail merge: when the movable
            # packs (those below gc_max_pack_mib) total more than this, auto-GC
            # defers to a manual off-hours run instead of round-tripping that
            # much mid-push.
            "gc_max_mib": get_git_config_int("seafile.gcmaxmib", 8192),
            # Per-pack freeze threshold for the tail merge: packs at or above
            # this size are left where they stand, so each cycle moves only the
            # small new packs and cost stays bounded regardless of history.
            "gc_max_pack_mib": get_git_config_int("seafile.gcmaxpackmib", 1024),
        }

        fallbacks: list[str] = []
        if values["lock_timeout"] < 0:
            fallbacks.append(f"seafile.locktimeout={values['lock_timeout']} -> 15")
            values["lock_timeout"] = 15
        if values["lock_lease"] <= 0:
            fallbacks.append(f"seafile.locklease={values['lock_lease']} -> 60")
            values["lock_lease"] = 60
        if values["lock_settle"] < 0:
            fallbacks.append(f"seafile.locksettle={values['lock_settle']} -> 1")
            values["lock_settle"] = 1
        if values["gc_threshold"] < 1:
            fallbacks.append(f"seafile.gcthreshold={values['gc_threshold']} -> 20")
            values["gc_threshold"] = 20
        # A sub-1 MiB budget would defer every tail merge, defeating the setting.
        if values["gc_max_mib"] < 1:
            fallbacks.append(f"seafile.gcmaxmib={values['gc_max_mib']} -> 8192")
            values["gc_max_mib"] = 8192
        # A sub-1 MiB freeze threshold would make every pack -- including old,
        # large consolidated ones -- movable, defeating the bounded-cost design.
        if values["gc_max_pack_mib"] < 1:
            fallbacks.append(f"seafile.gcmaxpackmib={values['gc_max_pack_mib']} -> 1024")
            values["gc_max_pack_mib"] = 1024
        if fallbacks:
            sys.stderr.write(
                "Warning: implausible seafile.* config ignored ("
                + "; ".join(fallbacks)
                + ").\n"
            )
            sys.stderr.flush()

        return cls(**values)
