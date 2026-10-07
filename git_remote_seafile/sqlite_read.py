"""sqlite_read.py - open a Seafile client database without disturbing it.

The Seafile desktop client keeps ``repo.db``/``accounts.db`` open and may be
writing to them, so opening those files in place risks blocking the client or
reading a torn state.  We copy them to a private temp directory and open the
copy read-only.

The copy must include SQLite's sidecar files.  In WAL mode -- the default for
modern SQLite -- committed transactions live in ``<db>-wal`` until a
checkpoint, and a copy of the main file alone can contain *no rows at all*:
measured against a populated WAL database, copying just ``repo.db`` raises
``no such table`` because the table itself is still in the log.  Copying the
sidecars is harmless in the other journal modes.

Do **not** add ``immutable=1`` to the connection URI.  It tells SQLite the file
cannot change, which makes it ignore the WAL -- reintroducing the exact bug this
module exists to prevent (also measured).
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Everything SQLite may keep beside the database.  ``-wal``/``-shm`` for WAL
# mode, ``-journal`` for the rollback journal modes.
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _copy_with_sidecars(src: Path, dest_dir: Path) -> Path:
    """Copy *src* and any SQLite sidecar files into *dest_dir*; return the copy."""
    dest = dest_dir / src.name
    shutil.copy2(src, dest)
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = src.with_name(src.name + suffix)
        if not sidecar.is_file():
            continue
        try:
            shutil.copy2(sidecar, dest_dir / sidecar.name)
        except OSError:
            # A locked or vanished sidecar is not fatal: the main copy is still
            # a valid -- if slightly older -- database.
            pass
    return dest


@contextmanager
def open_live_sqlite_ro(db_path: Path) -> Iterator[sqlite3.Connection | None]:
    """Yield a read-only connection to a copy of *db_path*, or ``None``.

    ``None`` means "could not read this candidate" (absent, unreadable, or not
    a database), which lets callers keep walking their candidate list.  Errors
    raised by the caller's own body propagate normally.
    """
    path = Path(db_path)
    if not path.is_file():
        yield None
        return

    with tempfile.TemporaryDirectory(prefix="grs-sqlite-") as tmp:
        try:
            copy = _copy_with_sidecars(path, Path(tmp))
            con = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        except (OSError, sqlite3.Error):
            yield None
            return
        try:
            yield con
        finally:
            con.close()
