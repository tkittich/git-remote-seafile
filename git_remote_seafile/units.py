"""How the tool writes a byte count.

Binary units, labelled as binary: the divisor is 1024, so ``1536`` is
``1.5 KiB``, never ``1.5 KB``.  The snapshot tool carries a copy of this function
(``human_bytes``, because it must run without the package installed), and
``tests/test_message_style.py`` fails if the two ever give different answers.
"""

from __future__ import annotations


def format_bytes(n: float) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"
