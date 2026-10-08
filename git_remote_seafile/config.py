"""config.py - Git and environment configuration resolution for git-remote-seafile."""

from __future__ import annotations

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
        """Load configuration from Git config with documented defaults."""
        return cls(
            lock_timeout=get_git_config_int("seafile.locktimeout", 15),
            lock_lease=get_git_config_int("seafile.locklease", 60),
            auto_gc=get_git_config_bool("seafile.autogc", False),
            gc_threshold=get_git_config_int("seafile.gcthreshold", 20),
        )
