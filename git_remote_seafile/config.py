"""config.py - Git and environment configuration resolution for git-remote-seafile."""

from __future__ import annotations

import sys
from dataclasses import dataclass

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
    auto_gc: bool
    gc_threshold: int

    @classmethod
    def load(cls) -> RemoteConfig:
        """Load configuration from Git config with documented defaults.

        Values are used as the user sets them, except where they would defeat
        the mechanism outright: a zero or negative lease expires every lock
        immediately (permanent contention), a negative timeout never waits,
        and a sub-1 gc threshold compacts on every push.  Those fall back to
        the documented defaults with a warning instead of being honored --
        the config is a way to tune the protocol, not to switch it off.
        """
        values = {
            "lock_timeout": get_git_config_int("seafile.locktimeout", 15),
            "lock_lease": get_git_config_int("seafile.locklease", 60),
            "auto_gc": get_git_config_bool("seafile.autogc", False),
            "gc_threshold": get_git_config_int("seafile.gcthreshold", 20),
        }

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
        if fallbacks:
            sys.stderr.write(
                "Warning: implausible seafile.* config ignored ("
                + "; ".join(fallbacks)
                + ").\n"
            )
            sys.stderr.flush()

        return cls(**values)
