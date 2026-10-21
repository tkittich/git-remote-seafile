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

That is the baseline command, and it is the slow path: the end-to-end and
snapshot modules shell out to a real `git` for case after case, and the suite has
grown well past what a serial run is pleasant for.

For the edit/test loop — and in CI — run the same suite across processes
instead:
```bash
python tools/run_tests_parallel.py          # one worker per CPU
python tools/run_tests_parallel.py -j 8     # a specific worker count
python tools/run_tests_parallel.py -k e2e   # only matching classes
python tools/run_tests_parallel.py --list   # show the units of work
python tools/run_tests_parallel.py --timeout 600  # kill a target after 10 minutes (default 1800)
```

Wall time depends on the machine far more than on the suite, so no figure is
published here -- it would be stale by the next change (earlier versions of this
page listed 339 s and 272 s for numbers this suite now finishes in well under a
minute on a 24-thread desktop). The runner prints its own wall time; run it, or
link a dated CI run if you need a number.

The *shape* is stable and is what to remember:

* The wall time is bounded by the slowest single unit of work, not by
  throughput -- today one of the `test_snapshot_payload` classes (each of the
  five runs one to two minutes; a single 109-test class once made the whole suite
  six minutes, which is why it was split). More workers than there are heavy
  classes buy nothing, and a new test class should be sized with that in mind.
* The unit of work is the test class. Per-test scheduling is measurably worse
  (many more process start-ups for the same work).
* `-j 1` is slower than `python -m unittest discover tests`, because it pays an
  interpreter start-up per class. Use the serial command for a baseline.

Because the unit of work is the class, a **class that defines `test_*` methods
must not be subclassed**: `unittest` inherits tests, so every case would run
twice — once under the base's own name and once under the subclass's. Put shared
`setUp` and helpers in a base with no test methods (as `_SnapshotFixture` does in
`test_snapshot_tool.py`); `tests/test_suite_shape.py` fails the suite otherwise.

It has no dependencies. It is not in the installed package — it travels in the
sdist, for someone building from source.

---

## Testing against a real Seafile server (opt-in)

Everything above validates the helper against an in-process stub, which encodes
the authors' *beliefs* about the server. `tests/test_realserver.py` is the check
on those beliefs. It is skipped unless you name a scratch library:

```bash
SEAFILE_TEST_LIBRARY=test python -m unittest tests.test_realserver -v   # ~70 s
```

Credentials come from the usual places (desktop client, `~/.git-seafile.json`,
`SEAFILE_SERVER` + `SEAFILE_TOKEN`). Each run writes under one unique top-level
folder of that library and removes it afterwards; nothing else is read or
written. It covers the API semantics the stub copies, the SHA-1 and SHA-256
push / clone / fetch / delete / tail-merge round trip, concurrent pushers through
the lock, and the observation that simultaneous same-path uploads leave
`(SFConflict ...)` copies -- the reason the lock exists. Run it before a release,
and whenever the stub's `do_DELETE`/`do_POST` semantics are changed.

---

## The snapshot tool is one module to users and several parts to maintainers

`git_remote_seafile/snapshot_tool.py` is **generated**. Its source is a handful of
cohesive parts in `tools/_snapshot/` (`10_setup.py`, `20_gitrun.py`, ...),
concatenated in name order by `tools/build_snapshot.py` -- no import rewriting, no
bundler. It lives in the package so it ships in the wheel and installs as
`git-seafile-snapshot` (and `git-remote-seafile snapshot`); maintainers get units a
reader can hold. `tools/seafile_snapshot.py` is a hand-written wrapper that runs it
from a checkout and stands in for it when imported.

```bash
# edit tools/_snapshot/<part>.py, then:
python tools/build_snapshot.py           # rewrite git_remote_seafile/snapshot_tool.py
python tools/build_snapshot.py --check   # exit 1 if it is stale (the test suite does this too)
```

Rules for the parts: they are *fragments of one module* sharing a flat namespace
(no `from ._x import y`); all imports live in `10_setup.py`; a top-level name is
defined in exactly one part; anything evaluated at import time must come from an
earlier part; no part grows past 700 lines. `tests/test_snapshot_build.py`
enforces each of these. Ruff and mypy check the generated file (the parts cannot
be linted alone -- each uses names another defines). **Never edit
`git_remote_seafile/snapshot_tool.py` directly**: the next build overwrites it.

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

## Type checking

CI runs [mypy](https://mypy-lang.org/) as its own job, alongside the test matrix
and the linter:

```bash
pip install mypy==2.4.0 types-requests   # the versions CI pins
mypy
```

Bare `mypy`: the files to check, the interpreter to check against and the
strictness all come from `[tool.mypy]` in `pyproject.toml`, so there is no second
place to keep in step with it. Two choices there are worth knowing:

- **`tests/` is out of scope.** It is a namespace package (no `__init__.py`), so
  mypy resolves each module under two names and stops before checking anything;
  and its test doubles are `MagicMock`s whose attributes no annotation can
  describe. The scope is the code that ships.
- **`warn_unused_ignores` is on.** With the annotations enforced, a
  `# type: ignore` that has stopped being necessary is itself a defect: it would
  silently absorb the next real error on that line.

`types-requests` is not optional. Without a stub for `requests`, the checker
reports the *missing stub* as errors inside this code, which is noise rather than
a finding.

---

## Coverage

`[tool.coverage.*]` in `pyproject.toml` configures a `coverage` job that runs the
suite and publishes a per-file table to the job summary:

```bash
pip install coverage==7.16.2
coverage run tools/run_tests_parallel.py -j 0
coverage combine
coverage report --show-missing
```

`parallel` and `patch = ["subprocess"]` are load-bearing rather than tidiness.
The suite runs in worker processes and the end-to-end tests exercise the helper
as a child of `git`, so measuring the driver alone would produce a number that
looks like coverage and is not: `parallel` keeps each process's data in its own
file instead of letting the last one overwrite it, and `patch` installs a startup
hook and puts the setting in the environment — the hook lives in site-packages
and the variable is inherited through `git`, so the helper is measured too.

There is deliberately **no threshold**. A `fail_under` chosen to make today's
build green is one that gets raised for the wrong reason or lowered for a worse
one; what the job is for is that a *drop* is visible in a review.

At v0.8.7 the suite covered **91%** of the package — 2944 statements, 264 missed
— with `cli.py` at 88%, `client.py` 92%, `helper.py` 96% and `lock.py` 84%. Read
that as a dated observation rather than a target: it is here so a later drop is
visible, and it moves whenever the suite does.

The report is a measurement, not a promise. It counts the paths a stub exercises,
and `DESIGN.md` §4.3.1 says plainly that none of this has been validated against
a real Seafile instance.

---

## Continuous Integration

`.github/workflows/ci.yml` runs on every push to `main` and on every pull
request, in five jobs:

| Job | What it does |
| :--- | :--- |
| `test` | The whole suite on a 3 × 5 matrix — Ubuntu, Windows and macOS × Python 3.10–3.14 — with `fail-fast: false`, so one platform's failure never hides another's. |
| `lint` | `ruff check --output-format github`, then the documentation guards *and* the drift scenarios that prove they still work. |
| `typecheck` | `mypy`, scoped and configured by `[tool.mypy]` in `pyproject.toml`. |
| `min-deps` | Installs `requests` at exactly the version `pyproject.toml` declares as its floor, with `--no-deps`, and runs the whole suite against it. |
| `coverage` | Runs the whole suite under `coverage` and publishes the per-file table to the job summary. |

Four properties of that workflow are deliberate, and worth knowing before you
change it:

- **The worker count is derived, not pinned.** Each leg runs
  `tools/run_tests_parallel.py -j 0`, which means one worker per CPU the runner
  reports. It used to be a literal `-j 4`, justified by "the runner has four
  vCPUs" — a number that is not the same on every OS in the matrix, and changes
  whenever GitHub resizes a runner image.
- **The dependency floor is a tested claim.** Every leg except `min-deps`
  installs the *latest* `requests`, so `min-deps` is the only place the floor in
  `pyproject.toml` is exercised. Raise the pin in `ci.yml` in the same commit as
  the floor; `tests/test_release_workflow.py` fails if the two disagree, and
  fails again if the floor drops below 2.32.4 (see `SECURITY.md` for the three
  CVEs behind that number).
- **The token is scoped.** The workflow declares `permissions: contents: read`
  rather than inheriting the repository's default scope, which is read/write on
  older repository settings.
- **Superseded runs are cancelled.** A `concurrency` group keyed on the workflow
  and ref cancels an in-flight run when a newer push to the same branch arrives.
  Fifteen jobs per push is a lot of runner time to spend on a commit that has
  already been replaced.

`.github/dependabot.yml` opens a weekly pull request when a pinned action has a
newer release. Every `uses:` is pinned to a full commit SHA with the release in
a trailing comment; Dependabot rewrites both halves, which is the only
mechanical way those pins stay current.

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

First run `python tools/release_check.py`. It answers locally what the release job
answers after the tag is public: `__version__` against the tag, the notes file's
shape and compare-link base, the CHANGELOG section and its reference link, a clean
tree. It prints the exact `git tag` command to use, and warns when HEAD is on no
remote branch (it cannot see CI; wait for green before tagging).

```bash
git tag -a v<x.y.z> -m "v<x.y.z> - <the release-notes title>"
git push origin v<x.y.z>   # naming the tag is what carries it
git push origin            # the branch
git push mirror            # the mirror carries heads and tags
```

**The tag message is the notes file's `# ` title, with the hash dropped.** Every
tag in this history is named that way (`v0.8.6 - The Hardening Pass: a Token
Scope, a Tested Floor, an Attested Release`), and the message is what
`git describe` and the repository's tag list show. A bare `-m "v0.8.7"` is not
wrong in a way any check catches — it just quietly makes one tag in the list look
different from all the others.

**A bare `git push origin` does not carry tags.** `push.followTags` is unset and
`origin` has no tag refspec, so the branch goes up and the tag stays local: a
release that looks pushed and is not. The command above names the tag explicitly,
which is why it works. `mirror` is a second remote whose refspecs cover heads
*and* tags, so it needs the tag too — a release is not complete until both have
it.

The tag is the whole release. The workflow compares it against the package
version (`v$(__version__) == $GITHUB_REF_NAME`) and stops before building
anything if they disagree, so a tag that does not match `__init__.py` fails
loudly instead of publishing a mislabelled artifact.

### Distribution

Releases ship **wheel and sdist assets on GitHub Releases only**. PyPI
publication is deliberately deferred to the official Seafile repository once
this code is merged upstream, so the workflow carries no PyPI job to maintain or
misfire.

Alongside the two distributions a release carries a **`SHA256SUMS`** file, and
each artifact has a **build-provenance attestation** generated by the `build`
job and signed against the workflow's own OIDC identity — which is why that job
widens its token to `id-token: write` and `attestations: write` while every
other job stays at `contents: read`.

The order of those two steps is load-bearing: the attestation's subject is the
glob `dist/*`, so a checksum file written first would be swept up and attested
as though it were an artifact. `tests/test_release_workflow.py` pins the order.

`SECURITY.md` is what tells a user to verify either of them:

```bash
sha256sum --check SHA256SUMS
gh attestation verify dist/<artifact> --repo tkittich/git-remote-seafile
```

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

4. **User-visible text**:
   - A notification opens with `Warning:` (needs attention), `Note:` (FYI), `Notice:`
     (a default changed what this run does), `Hint:` (advice that changes nothing) or,
     for a fatal error, `Error:`. Title case: `WARNING:`, `tip:` and `error:` fail
     `tests/test_message_style.py`.
   - Byte counts are binary and labelled so (`KiB`, `MiB`, `GiB`). Use
     `git_remote_seafile.units.format_bytes` in the package and `human_bytes` in the
     snapshot tool (a copy, since that tool runs without the package; a test fails if
     they differ). A hand-written `KB`, `MB` or `GB` next to a number fails the same test.
   - A message that names a problem names the flag that fixes it.
   - A new message comes with a test that triggers it and quotes three words in a row
     of its text; the check lists the ones that do not. `tests/untested_messages.txt` is
     the older backlog and may only shrink.

5. **License Agreement**:
   - All contributions to this project are submitted under the terms of the **Apache License 2.0**.

---

## Submitting Upstream to Seafile / Haiwen

When contributing changes intended for the official Seafile organization:
- Ensure all existing unit tests pass.
- Maintain compatibility with both **Seafile Community Edition (CE)** and **Seafile Professional (Pro)**.
- Document any new CLI options or configuration keys in [USER_GUIDE.md](USER_GUIDE.md).
