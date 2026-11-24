"""The ``seafile`` command: one name for the tools of this project (CLI.md).

``seafile snapshot ...`` (or ``seafile snap ...``) is ``git-seafile-snapshot ...``, ``seafile blob ...`` is the
blob store, and the helper's account commands (``seafile login``, ``seafile doctor``, ...) are
``git-remote-seafile ...``.  It forwards to the existing ``main`` functions and holds no logic of its own, so a
tool behaves the same whichever name started it.
"""

from __future__ import annotations

import sys

#: The first words that name the snapshot tool; everything after goes to it unchanged.
SNAPSHOT_WORDS = ("snapshot", "snap")
BLOB_WORD = "blob"

_HELP = """usage: seafile <tool> [arguments]

One name for the tools of git-remote-seafile.

  seafile snapshot ...   whole-tree snapshots (same as git-seafile-snapshot, sfsnap); `seafile snap` too
  seafile blob ...       the blob store (same as seafile-blob, sfblob)
  seafile <command> ...  the helper's own commands, e.g. login, doctor, config, repair, version
                         (same as git-remote-seafile <command>)

`seafile <tool> --help` shows that tool's own help.
"""


def _helper_commands() -> "frozenset[str]":
    from . import cli
    return frozenset(cli._HANDLERS) - {"snapshot"}


def main(argv: "list[str] | None" = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(_HELP)
        return 0 if args else 2
    first, rest = args[0], args[1:]
    if first in SNAPSHOT_WORDS:
        from .snapshot import cli as snapshot_cli
        return snapshot_cli.main(rest)
    if first == BLOB_WORD:
        sys.stderr.write("seafile: the blob store is not part of this version yet\n")
        return 2
    if first in _helper_commands():
        from . import cli
        sys.argv = ["git-remote-seafile", first, *rest]
        return cli.main()
    sys.stderr.write(f"seafile: unknown tool or command '{first}'\n\n{_HELP}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
