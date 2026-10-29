"""settings_report.py - ``git-remote-seafile config``: every setting, its value, its source.

The behavioural settings are git config keys and environment variables, documented
in one table in the User Guide.  Nothing in the tool said which of them were in
force, so finding out why a push waited 15 seconds meant reading that table and
running ``git config --get`` for each key.  This reports the effective value of
each one and where it came from, using the same readers the helper uses.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from .config import RemoteConfig
from .git_util import _trusted_config_value, get_git_config


class Setting(NamedTuple):
    key: str
    env: str | None
    default: str
    #: Read from the machine owner's scopes only (a repository's own config is ignored).
    trusted: bool
    meaning: str


#: In the order the User Guide lists them.  ``default`` is what the code uses when
#: nothing is set; a test compares it with :meth:`RemoteConfig.load` so the two
#: cannot drift.
SETTINGS: tuple[Setting, ...] = (
    Setting("seafile.autogc", None, "true", False,
            "merge the remote's small packs after a push"),
    Setting("seafile.gcthreshold", None, "20", False,
            "small packs at which the merge (or the gc tip) triggers"),
    Setting("seafile.gcmaxpackmib", None, "1024", False,
            "largest pack the helper writes, in MiB; lower it behind a proxy with a body limit"),
    Setting("seafile.locktimeout", None, "15", False,
            "seconds to wait for the remote lock"),
    Setting("seafile.locklease", None, "60", False,
            "seconds before an idle lock lease expires"),
    Setting("seafile.forcefilehost", "SEAFILE_FORCE_FILE_HOST", "false", False,
            "rewrite transfer links onto the configured server's host"),
    Setting("seafile.gittimeout", "SEAFILE_GIT_TIMEOUT", "300", True,
            "seconds one git subprocess may run before it is killed"),
    Setting("seafile.skipsafetychecks", "SEAFILE_SKIP_SAFETY_CHECKS", "false", True,
            "bypass the pre-flight guardrails"),
)


def _effective(setting: Setting, cfg: RemoteConfig) -> str:
    """The value the helper acts on, for the keys ``RemoteConfig`` resolves."""
    attr = {
        "seafile.autogc": "auto_gc",
        "seafile.gcthreshold": "gc_threshold",
        "seafile.gcmaxpackmib": "gc_max_pack_mib",
        "seafile.locktimeout": "lock_timeout",
        "seafile.locklease": "lock_lease",
    }.get(setting.key)
    if attr is None:
        return ""
    value = getattr(cfg, attr)
    return str(value).lower() if isinstance(value, bool) else str(value)


def effective_settings() -> list[tuple[Setting, str, str]]:
    """``(setting, value, source)`` for every setting; source is where the value came from."""
    cfg = RemoteConfig.load()
    rows: list[tuple[Setting, str, str]] = []
    for setting in SETTINGS:
        env_raw = os.environ.get(setting.env, "").strip() if setting.env else ""
        git_raw = (
            _trusted_config_value(setting.key) if setting.trusted
            else get_git_config(setting.key)
        )
        if env_raw:
            value, source = env_raw, f"environment ({setting.env})"
        elif git_raw:
            value = _effective(setting, cfg) or git_raw
            source = "global/system git config" if setting.trusted else "git config"
        else:
            value, source = setting.default, "default"
        rows.append((setting, value, source))
    return rows


def format_report(rows: list[tuple[Setting, str, str]]) -> str:
    key_w = max(len(s.key) for s, _, _ in rows)
    val_w = max(len(v) for _, v, _ in rows)
    src_w = max(len(src) for _, _, src in rows)
    lines = []
    for setting, value, source in rows:
        lines.append(
            f"{setting.key:<{key_w}}  {value:<{val_w}}  {source:<{src_w}}  {setting.meaning}"
        )
    lines.append("")
    lines.append(
        "Hint: change one with 'git config [--global] <key> <value>'; "
        "a source that says 'default' means nothing is set. "
        "seafile.gittimeout and seafile.skipsafetychecks read only the environment "
        "and your global/system git config, never a repository's own."
    )
    return "\n".join(lines)
