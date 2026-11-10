"""Delete a guard, watch its test: the one-line mutation check, done the same way every time.

    python tools/mutate_check.py tools/_snapshot/20_gitrun.py \
        --old "if not (value > 0)" --new "if value <= 0" \
        tests.test_snapshot_tool.TypedGitTimeoutTests

The file is changed (``--old`` must occur exactly once), the snapshot tool is rebuilt when the
file is one of its parts, the named tests run, and the file is put back byte for byte (and the
tool rebuilt) whatever happens.  Exit status: 0 KILLED (a test failed, the guard is tested),
1 SURVIVED (every test passed: say so in the commit or the review, it is a finding), 2 the
mutation could not be applied or the tests could not run.

It exists because three reviewers of the 0.9.25 round hand-rolled this, one first mutation was
wrong, and a part of the snapshot tool mutated without ``build_snapshot.py`` tests nothing
(AGENTS.md, "A guard test must go red when its guard is deleted").  It works on the working
tree, so run it on a tree whose changes are committed or backed up: a crash of the machine in
the middle leaves the mutation on disk, and ``git diff`` shows it.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _rebuild(target: Path) -> None:
    if "tools/_snapshot/" in target.as_posix():
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_snapshot.py")],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", help="the file to mutate (a part of the snapshot tool is rebuilt)")
    parser.add_argument("--old", required=True, help="text that occurs exactly once in the file")
    parser.add_argument("--new", required=True, help="what replaces it (may be empty)")
    parser.add_argument("tests", nargs="+", help="unittest targets, e.g. tests.test_x.Class.test_y")
    args = parser.parse_args(argv)

    target = (ROOT / args.file).resolve() if not Path(args.file).is_absolute() else Path(args.file)
    original = target.read_bytes()
    text = original.decode("utf-8")
    if text.count(args.old) != 1:
        print(f"mutate_check: --old occurs {text.count(args.old)} times in {args.file}, need exactly 1",
              file=sys.stderr)
        return 2
    try:
        target.write_bytes(text.replace(args.old, args.new).encode("utf-8"))
        _rebuild(target)
        proc = subprocess.run([sys.executable, "-m", "unittest", *args.tests], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        tail = "\n".join(proc.stdout.strip().splitlines()[-12:])
    finally:
        target.write_bytes(original)
        _rebuild(target)
    if "no tests ran" in tail.lower() or "_FailedTest" in proc.stdout or "Ran 0 tests" in proc.stdout:
        print(tail)
        print("mutate_check: the tests did not run as named; this says nothing about the guard", file=sys.stderr)
        return 2
    if proc.returncode != 0:
        print(tail)
        print("KILLED: a test failed with the guard removed")
        return 0
    print(tail)
    print("SURVIVED: every named test passed with the guard removed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
