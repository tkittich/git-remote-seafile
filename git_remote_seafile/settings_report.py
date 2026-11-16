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
from .git_util import _trusted_config_value, get_git_config, run_git


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
            "largest pack the helper writes, in MiB; lower it behind a proxy with a body limit "
            "(remote.<name>.seafilegcmaxpackmib sets it for one remote)"),
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


_BOOLEAN_WORDS = frozenset({"1", "true", "yes", "on", "0", "false", "no", "off"})


def _env_ignored_because(setting: Setting, raw: str) -> str | None:
    """Why the helper ignores *raw*, or ``None`` when it acts on it.

    The report printed an environment value as the one in force whatever it said:
    ``SEAFILE_GIT_TIMEOUT=abc`` was shown while the helper used 300 (review dfd2269, fable-B10).
    """
    if setting.key == "seafile.gittimeout":
        try:
            return None if int(raw) > 0 else "not a positive whole number"
        except ValueError:
            return "not a whole number"
    if setting.key in ("seafile.forcefilehost", "seafile.skipsafetychecks"):
        return None if raw.lower() in _BOOLEAN_WORDS else "not true or false"
    return None


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
        ignored = _env_ignored_because(setting, env_raw) if env_raw else None
        if ignored is not None:
            env_raw = ""
        if env_raw:
            value, source = env_raw, f"environment ({setting.env})"
        elif git_raw:
            value = _effective(setting, cfg) or git_raw
            source = "global/system git config" if setting.trusted else "git config"
        else:
            value, source = setting.default, "default"
        if ignored is not None:
            source += f"; {setting.env} is set but {ignored}, so it is ignored"
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


def learned_pack_caps() -> list[tuple[str, str]]:
    """``(remote name, MiB)`` for every ``remote.<name>.seafilegcmaxpackmib`` in this repository's config.

    A refused upload (HTTP 413) teaches a cap for that one remote; the report showed only the
    repository-wide ``seafile.gcmaxpackmib``, so a push that split because of a learned cap looked
    unexplained (review 134c8af, fable-G8.1).
    """
    out, _, code = run_git(["config", "--get-regexp", r"^remote\..*\.seafilegcmaxpackmib$"])
    if code != 0 or not out:
        return []
    found = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        key, _, value = line.partition(" ")
        name = key[len("remote."):-len(".seafilegcmaxpackmib")]
        if name and value.strip():
            found.append((name, value.strip()))
    return sorted(found)


def format_learned_caps(caps: list[tuple[str, str]]) -> str:
    """The lines ``config`` prints about the caps a remote has learned (empty when there are none)."""
    if not caps:
        return ""
    lines = ["", "Learned per-remote pack caps (a refused upload taught them; they win over seafile.gcmaxpackmib):"]
    lines += [f"  remote.{name}.seafilegcmaxpackmib  {mib}" for name, mib in caps]
    return "\n".join(lines)
