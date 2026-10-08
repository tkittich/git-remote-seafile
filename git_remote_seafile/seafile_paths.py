"""seafile_paths.py - Unified discovery of Seafile client data and database paths."""

from __future__ import annotations

from pathlib import Path


class SeafileClientNotFoundError(FileNotFoundError):
    """Raised when Seafile client configuration or data directories are not found."""
    pass


def ccnet_dir() -> Path:
    """The client's config dir. ~/ccnet on Windows, ~/.ccnet elsewhere."""
    for cand in (Path.home() / "ccnet", Path.home() / ".ccnet"):
        if cand.is_dir():
            return cand
    raise SeafileClientNotFoundError("no ccnet directory found - is the Seafile client installed?")


def seafile_data(ccnet: Path) -> Path:
    """seafile-data location is recorded in <ccnet>/seafile.ini (one line)."""
    ini = ccnet / "seafile.ini"
    if ini.is_file():
        raw = ini.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            p = Path(raw)
            if p.is_dir():
                return p
    raise SeafileClientNotFoundError(f"could not resolve seafile-data (looked in {ini})")


def get_seafile_data_dirs() -> list[Path]:
    """Return candidate directories where the Seafile desktop client stores data."""
    dirs: list[Path] = []
    for ini_path in (
        Path.home() / "ccnet" / "seafile.ini",
        Path.home() / ".ccnet" / "seafile.ini",
    ):
        if ini_path.is_file():
            try:
                raw = ini_path.read_text(encoding="utf-8", errors="replace").strip()
                if raw:
                    p = Path(raw)
                    if p not in dirs:
                        dirs.append(p)
            except Exception:
                pass

    standard_roots = [
        Path.home() / "ccnet",
        Path.home() / ".ccnet",
        Path.home() / "Seafile" / "seafile-data",
        Path.home() / ".seafile-data",
        Path.home() / "Seafile" / ".seafile-data",
        Path.home() / "Library" / "Application Support" / "Seafile",
        Path.home() / ".config" / "seafile",
    ]
    for p in standard_roots:
        if p not in dirs:
            dirs.append(p)
    return dirs


def get_candidate_db_paths(filename: str) -> list[Path]:
    """Return candidate paths for a Seafile database file (e.g. 'accounts.db' or 'repo.db')."""
    return [d / filename for d in get_seafile_data_dirs()]
