"""git-remote-seafile - Transparent Git remote helper for Seafile servers."""

from __future__ import annotations

import sys

# The advertised minimum. The code uses str.removeprefix/str.removesuffix and
# pathlib.Path.is_relative_to, which were all added in Python 3.9.
# ``from __future__ import annotations`` defers annotation evaluation but cannot
# backfill these runtime APIs, so 3.8 is genuinely unsupported -- just not at
# import time. See check_python_version() below.
MINIMUM_PYTHON: tuple[int, int] = (3, 9)


def check_python_version(version_info: tuple[int, int] | None = None) -> None:
    """Raise ``RuntimeError`` if the interpreter is below the supported minimum.

    ``version_info`` defaults to the running interpreter. A two-element
    ``(major, minor)`` tuple may be passed explicitly so the check is testable
    without re-launching an older interpreter.
    """
    if version_info is None:
        current = tuple(sys.version_info[:2])
        human = sys.version.split()[0]
    else:
        current = tuple(version_info[:2])
        human = ".".join(str(part) for part in current)

    if current < MINIMUM_PYTHON:
        minimum = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise RuntimeError(
            f"git-remote-seafile requires Python {minimum} or newer "
            f"(running {human}). Python 3.8 and older are not supported: the "
            "code relies on str.removeprefix/str.removesuffix and "
            "pathlib.Path.is_relative_to, which do not exist before 3.9."
        )


# Enforced at import time so a source checkout fails loudly and immediately
# instead of raising a cryptic AttributeError in the middle of a git operation.
check_python_version()

__version__ = "0.4.3"
