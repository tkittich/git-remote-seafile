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

3. **Install in editable mode with development dependencies**:
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

Measured on a 12-core AMD 5900X, 268 tests, same result either way:

| workers | wall time |
| ------- | --------- |
| serial (`python -m unittest discover tests`) | 204.4 s |
| 2 | 141.2 s |
| 4 | 84.6 s |
| 6 | 66.1 s |
| 12 (the default here) | 52.7 s |

The wall time is bounded by the slowest single unit of work (37.0 s), not by
throughput, and the curve flattens early: twelve workers are 1.6x four, not 3x,
because the heavy cases are themselves multi-process. Repeated twelve-worker
runs land between 52.7 s and 56.5 s, so treat the column as indicative rather
than exact. The unit of work is the test class — see the module docstring for
why per-test scheduling is measurably *worse* here.

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

`target-version` is `py39`, matching `requires-python`, and it is enforced
rather than decorative: a `match` statement anywhere in this repository fails
the lint job with "Cannot use `match` statement on Python 3.9".

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
