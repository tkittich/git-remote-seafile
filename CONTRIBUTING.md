# Contributing to git-remote-seafile

Thank you for your interest in improving `git-remote-seafile`! We welcome bug reports, feature requests, documentation improvements, and pull requests.

---

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/tkittich/git-remote-seafile.git
   cd git-remote-seafile
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install in editable mode** (there are no development dependencies — the runner deliberately needs nothing beyond the package):
   ```bash
   pip install -e .
   ```

---

## Running Tests

Run the test suite using Python's standard `unittest`:
```bash
python -m unittest discover tests
```

That is the baseline command. It is also slow: the end-to-end module shells out
to a real `git` for every case, and accounts for 117.8 s of the 204 s total —
close to three fifths.

For the edit/test loop — and in CI — run the same suite across processes
instead:
```bash
python tools/run_tests_parallel.py          # one worker per CPU
python tools/run_tests_parallel.py -j 8     # a specific worker count
python tools/run_tests_parallel.py -k e2e   # only matching classes
python tools/run_tests_parallel.py --list   # show the units of work
```

Measured on a 12-core AMD 5900X — 268 tests when this table was first taken,
539 after v0.8.0 added the snapshot module — same result either way:

| workers | wall time |
| ------- | --------- |
| serial (`python -m unittest discover tests`) | 204.4 s (268 tests) |
| 2 | 141.2 s |
| 4 | 84.6 s |
| 6 | 66.1 s |
| 12 (the default here) | 52.7 s |

The 268-test rows are the original measurement, kept because the *shape* of the
curve is the point; with the snapshot module the absolute numbers are larger
(the module alone was 587 s serial before its fixture split), and the
class-granularity argument only got stronger.

The wall time is bounded by the slowest single unit of work (37.0 s), not by
throughput, and the curve flattens early: twelve workers are 1.6x four, not 3x,
because the heavy cases are themselves multi-process. Repeated twelve-worker
runs land between 52.7 s and 56.5 s, so treat the column as indicative rather
than exact. The unit of work is the test class — see the module docstring for
why per-test scheduling is measurably *worse* here.

Because the unit of work is the class, a **class that defines `test_*` methods
must not be subclassed**: `unittest` inherits tests, so every case would run
twice — once under the base's own name and once under the subclass's. Put shared
`setUp` and helpers in a base with no test methods (as `_SnapshotFixture` does in
`test_snapshot_tool.py`); `tests/test_suite_shape.py` fails the suite otherwise.

Note that `-j 1` is *slower* than the serial command above, because it pays an
interpreter start-up per class. Use the serial command for a baseline.

It has no dependencies. It is not in the installed package — it travels in the
sdist, for someone building from source.

---

## Linting

CI runs [ruff](https://docs.astral.sh/ruff/) as its own job, alongside the test
matrix:

```bash
pip install ruff==0.16.10     # the version CI pins
ruff check .
```

The rules live in `pyproject.toml` under `[tool.ruff]`, not in the workflow, so
a local run and CI cannot disagree about what is being checked. The selection is
deliberately narrow — pyflakes (`F`), syntax errors (`E9`) and bugbear (`B`) —
which is the set that catches *mistakes*: an unused import, an undefined name, a
loop variable that is never read, an exception re-raised without its cause.
Style rules are left off; the codebase already has a house style, and a linter
that argues about line length is one people learn to filter out.

The pin is deliberate. An unpinned linter turns a green branch red overnight
when a new rule ships, with a failure that says nothing about what changed here.
Bumping it is a one-line diff.

`target-version` is `py310`, matching `requires-python`, and it is enforced
rather than decorative: an `except*` clause (Python 3.11 syntax) anywhere in
this repository fails the lint job.

---

## Releasing

**A release is a pushed tag.** There is no separate publish step: pushing a tag
matching `v*` runs `.github/workflows/release.yml`, which runs the suite and
ruff, checks that the tag agrees with the package version, builds the wheel and
sdist, and creates the GitHub Release with those artifacts attached. The job
builds the release body from `.github/release-notes/<tag>.md` when that file
exists and falls back to GitHub's generated commit list when it does not — which
is why the notes file is part of a release rather than an optional extra.

### Before tagging

1. **Bump the version in one place only.** `pyproject.toml` reads the version
   dynamically (`dynamic = ["version"]` with `version = {attr =
   "git_remote_seafile.__version__"}`), so `git_remote_seafile/__init__.py` is
   the only file that names it. `tests/test_version_consistency.py` fails if a
   version literal turns up anywhere else.

2. **Add the changelog entry.** A `## [x.y.z] - YYYY-MM-DD` section in
   `CHANGELOG.md`, plus its link in the reference block at the foot of the file:
   `[x.y.z]: https://github.com/tkittich/git-remote-seafile/compare/v<previous>...v<x.y.z>`.
   A missing link leaves that heading rendering as literal brackets. Move the
   `[Unreleased]` link forward to the new tag at the same time.

3. **Write the release notes.** `.github/release-notes/v<x.y.z>.md` *is* the
   published release body, so it is a contract rather than a draft:

   - the first line is `# v<x.y.z> - <summary>`, which becomes the release title;
   - the body starts at `## What's Changed in v<x.y.z>`;
   - the file ends with `---` followed by the
     `**Full Changelog**: …/compare/v<previous>...v<x.y.z>` link.

   `TestReleaseNotesFormat` checks that shape, and `TestEveryTagHasReleaseNotes`
   fails the suite when a merged tag has no notes file — the gap that let
   `v0.6.2` ship with a bare commit list. Only the compare link's *base* tag has
   to exist, so the notes file can be committed before the tag is cut.

4. **Run the suite and the linter locally** (see above). The release job runs
   both again; a red tag is a public red.

### Tagging

```bash
git tag -a v<x.y.z> -m "v<x.y.z>"
git push origin v<x.y.z>
```

The tag is the whole release. The workflow compares it against the package
version (`v$(__version__) == $GITHUB_REF_NAME`) and stops before building
anything if they disagree, so a tag that does not match `__init__.py` fails
loudly instead of publishing a mislabelled artifact.

### Distribution

Releases ship **wheel and sdist assets on GitHub Releases only**. PyPI
publication is deliberately deferred to the official Seafile repository once
this code is merged upstream, so the workflow carries no PyPI job to maintain or
misfire.

---

## Guidelines for Pull Requests

1. **Keep dependencies minimal**:
   - `git-remote-seafile` intentionally avoids heavy dependencies to remain lightweight and embeddable.
   - Use standard library modules wherever possible. `requests` is the primary external dependency.

2. **Cross-Platform Compatibility**:
   - The tool must run seamlessly on **Windows, Linux, and macOS**.
   - Use `pathlib.Path` or `os.path` rather than hardcoded slash separators.
   - Do not rely on POSIX-specific system calls (avoid `fcntl`, Unix sockets, etc.).

3. **Code Style**:
   - Follow [PEP 8](https://peps.python.org/pep-0008/) conventions.
   - Include type annotations on all function and method signatures.

4. **License Agreement**:
   - All contributions to this project are submitted under the terms of the **Apache License 2.0**.

---

## Submitting Upstream to Seafile / Haiwen

When contributing changes intended for the official Seafile organization:
- Ensure all existing unit tests pass.
- Maintain compatibility with both **Seafile Community Edition (CE)** and **Seafile Professional (Pro)**.
- Document any new CLI options or configuration keys in [USER_GUIDE.md](USER_GUIDE.md).
