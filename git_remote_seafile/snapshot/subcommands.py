"""The commands of the snapshot tool, and which options each one reads.

Before 0.11 the tool had one flat list of about fifty options and a mode flag (``--restore``, ``--status``,
``--maintain``, ``--payload-gc`` ...).  Every mode accepted every option, so each mode had to refuse, one
hand-kept list at a time, the options it never read (ROADMAP AA73/AA74).  Now a command names the mode and
:data:`ALLOWED` is the one place that says what the mode reads; an option outside it is refused with one
message (``cli_checks.validate_args``) and does not appear in the command's ``--help``.

The old spellings (``--restore`` ...) are translated to the commands for 0.11 and announced with a
``Notice:`` on stderr (UPGRADING); from 0.12 they are refused.
"""

from __future__ import annotations

import sys
from typing import NamedTuple


class Command(NamedTuple):
    mode: str
    words: tuple[str, ...]
    legacy: str | None  # the pre-0.11 flag that selected this mode
    summary: str


COMMANDS: tuple[Command, ...] = (
    Command("snapshot", ("snapshot",), None,
            "take a snapshot of a working tree and push it (the default: a bare run is this)"),
    Command("restore", ("restore",), "--restore",
            "restore a snapshot byte-exactly from --remote into --into (unlike a plain git clone)"),
    Command("drill", ("drill",), "--restore-drill",
            "prove the newest snapshot of --remote restores: clone it to a temporary directory, check every "
            "file byte-for-byte and run git fsck, download N randomly chosen payload files (default 10) and "
            "check their SHA-256, then delete the directory"),
    Command("status", ("status",), "--status",
            "report the last snapshot's time and size, what changed in the source since it, the settings the "
            "next run would use and the vault lock; stages, hashes and uploads nothing"),
    Command("maintain", ("maintain",), "--maintain",
            "local vault housekeeping: report on the object store, or with --full-repack consolidate it into "
            "one packfile and prune unreachable objects; never touches the source tree or any remote"),
    Command("gc", ("payload", "gc"), "--payload-gc",
            "payload retention: report the stored objects no snapshot references; with --payload-delete mark "
            "them, and delete those that stayed unreferenced for --payload-grace-days. Needs --remote"),
    Command("verify", ("payload", "verify"), "--payload-verify",
            "download N randomly chosen payload objects (default 20, or 'all') of the newest snapshot of "
            "--remote and check each against its SHA-256. Needs --remote"),
    Command("view", ("payload", "view"), "--payload-view",
            "build a browsable directory of real file names for a snapshot of --remote (a branch name, or one "
            "like snapshot~2; default 'snapshot'), by server-side copy; a convenience view, not a restore"),
)
BY_MODE = {command.mode: command for command in COMMANDS}
FIRST_WORDS = {command.words[0] for command in COMMANDS}
LEGACY = {command.legacy: command for command in COMMANDS if command.legacy}

#: The options that are a command's value rather than a switch: the optional operand of the legacy flag
#: becomes the command's positional (``--payload-verify all`` -> ``payload verify all``).
LEGACY_OPERAND = {"--restore-drill", "--payload-verify", "--payload-view"}

#: What a command reads beyond its own operands.  A snapshot run reads everything except the options below.
#: Found by running every option against every mode (``tests/test_snapshot_flag_coverage.py`` does it again,
#: so a new option fails there until it is placed here).
EVERY_COMMAND = frozenset({"--json", "--exit-warnings"})
ALLOWED: dict[str, frozenset[str]] = {
    "restore": EVERY_COMMAND | {
        "--remote", "--into", "--branch", "--ref", "--allow-synced", "--allow-low-disk", "--verify", "--only",
    },
    "drill": EVERY_COMMAND | {"--remote", "--branch", "--ref", "--allow-synced", "--allow-low-disk"},
    "status": EVERY_COMMAND | {
        "SOURCE", "--source", "--vault", "--branch", "--remote", "--code-remote", "--exclude",
        "--default-excludes", "--allow-synced", "--payload-min-size", "--no-payload", "--payload-auto-ratio",
    },
    "maintain": EVERY_COMMAND | {
        "SOURCE", "--source", "--vault", "--allow-synced", "--no-object-compression", "--full-repack",
    },
    "gc": EVERY_COMMAND | {
        "--remote", "--vault", "--allow-low-disk", "--allow-shared-remote", "--allow-synced",
        "--payload-grace-days", "--payload-keep", "--payload-keep-days", "--payload-delete", "--payload-no-vault",
        "--payload-accept-loss", "--payload-allow-non-owner",
    },
    "verify": EVERY_COMMAND | {
        "--remote", "--vault", "--allow-low-disk", "--allow-shared-remote", "--allow-synced", "--exclude",
        "--payload-jobs", "--payload-rehash", "--payload-report-days", "--payload-skip-changing",
    },
    "view": EVERY_COMMAND | {
        "--remote", "--vault", "--allow-shared-remote", "--allow-synced", "--payload-view-name",
        "--payload-view-replace",
    },
}
#: What only some other command reads, so a snapshot run does not.
NOT_SNAPSHOT = frozenset({
    "--into", "--ref", "--verify", "--only", "--full-repack", "--payload-delete", "--payload-keep", "--payload-keep-days", "--payload-no-vault",
    "--payload-accept-loss", "--payload-allow-non-owner", "--payload-grace-days", "--payload-view-name",
    "--payload-view-replace",
})


def allowed(mode: str, every_flag: frozenset[str]) -> frozenset[str]:
    """The options *mode* reads; *every_flag* is every option the tool defines."""
    return (every_flag - NOT_SNAPSHOT) if mode == "snapshot" else ALLOWED[mode]


def name(mode: str) -> str:
    """The command as typed: ``restore``, ``payload gc``."""
    return " ".join(BY_MODE[mode].words)


def split_command(argv: list[str]) -> tuple[str, list[str], list[str]]:
    """``(mode, the rest of the command line, the pre-0.11 mode flags that were typed)``.

    A command word comes first: ``restore --remote r --into d``, ``payload verify all --remote r``.  With
    none, the run is a snapshot -- unless a pre-0.11 mode flag is present, which still chooses its mode
    for 0.11 (and is reported by the caller).  A directory called ``restore`` is snapshotted as
    ``snapshot ./restore`` or ``--source restore``.
    """
    if argv and argv[0] in FIRST_WORDS | {"snapshot"}:
        first = argv[0]
        if first == "payload":
            if len(argv) < 2 or argv[1] not in ("gc", "verify", "view"):
                raise UsageError("payload needs one of: gc, verify, view")
            return argv[1], argv[2:], []
        return first, argv[1:], []
    found: list[tuple[int, str]] = []
    for index, token in enumerate(argv):
        if token == "--":
            break
        flag = token.split("=", 1)[0]
        if flag in LEGACY:
            found.append((index, flag))
    if not found:
        return "snapshot", list(argv), []
    flags = sorted({flag for _, flag in found})
    flag = flags[0]
    rest = list(argv)
    operand: str | None = None
    for index, _ in reversed(found):
        token = rest.pop(index)
        if "=" in token:
            operand = token.split("=", 1)[1]
        elif len(flags) == 1 and flag in LEGACY_OPERAND and index < len(rest) and not rest[index].startswith("-"):
            operand = rest.pop(index)
    if operand is not None:
        rest.append(operand)
    return LEGACY[flag].mode, rest, flags


class UsageError(Exception):
    """A command line that names no command or two of them: exit 2, like any other wrong command line."""


def legacy_notice(flag: str) -> str:
    command = LEGACY[flag]
    return (
        f"Notice: {flag} is now the command '{name(command.mode)}' ('git seafile-snapshot {name(command.mode)} "
        f"...'). The flag still works in 0.11 and is refused from 0.12."
    )


def say_legacy(flags: list[str]) -> None:
    for flag in flags[:1]:
        print(legacy_notice(flag), file=sys.stderr, flush=True)
