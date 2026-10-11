#!/usr/bin/env bash
#
# Smoke-install the built distributions, before a Release attaches them.
#
# Why this exists: `python -m build` followed by upload was the whole packaging
# path, so nothing ever *opened* the artifacts.  A wheel that cannot be
# installed, that renders its metadata wrong on the Release page, or that
# silently grew a `tools/` directory would be published unchecked (D3).
#
# Checks, in order:
#   1. `twine check` on both artifacts (metadata and long-description rendering)
#   2. the wheel installs into a clean virtual environment
#   3. the installed helper runs
#   4. the wheel ships *no* top-level `tools/` -- it carries the helper only
#   5. the sdist *does* ship `tools/` -- USER_GUIDE section 15 sends users to the
#      sdist precisely because the wheel lacks it, so the two must disagree on
#      purpose rather than by accident
#
# Usage: smoke_install_wheel.sh [dist-dir]     (default: dist)
set -euo pipefail

dist_dir="${1:-dist}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

wheel="$(ls "$dist_dir"/*.whl)"
sdist="$(ls "$dist_dir"/*.tar.gz)"
echo "wheel: $wheel"
echo "sdist: $sdist"

echo "--- twine check ---"
python -m pip install --quiet --upgrade twine
python -m twine check "$wheel" "$sdist"

echo "--- install the wheel into a clean virtual environment ---"
python -m venv "$work/venv"
# POSIX layout on the runners, Scripts/ on Windows -- so this script can also be
# run by hand on a developer's machine, which is the point of it being a script.
if [ -x "$work/venv/bin/python" ]; then
  venv_bin="$work/venv/bin"
else
  venv_bin="$work/venv/Scripts"
fi
"$venv_bin/pip" install --quiet "$wheel"

echo "--- the installed helper runs ---"
"$venv_bin/git-remote-seafile" version

echo "--- the wheel ships no tools/ ---"
"$venv_bin/python" - <<'PY'
import pathlib
import sys
import sysconfig

site = pathlib.Path(sysconfig.get_paths()["purelib"])
if (site / "tools").exists():
    sys.exit(
        f"the wheel ships a top-level 'tools' directory ({site / 'tools'}); "
        "it is meant to carry the helper only"
    )
print(f"ok: no tools/ in {site}")
PY

echo "--- the sdist does ship tools/ ---"
python - "$sdist" <<'PY'
import sys
import tarfile

wanted = "tools/seafile_snapshot.py"
names = tarfile.open(sys.argv[1]).getnames()
if not any(name.endswith(wanted) for name in names):
    sys.exit(
        f"the sdist does not ship {wanted}; USER_GUIDE section 15 sends users "
        "to the sdist for exactly that file"
    )
print(f"ok: the sdist ships {wanted}")
PY

echo "smoke install passed"
