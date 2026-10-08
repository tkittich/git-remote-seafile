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

That is the serial command, and it is the one CI uses. It is also slow: the
end-to-end module shells out to a real `git` for every case, and accounts for
roughly two thirds of the ~164 s total.

For the edit/test loop, run the same suite across processes instead:
```bash
python tools/run_tests_parallel.py          # one worker per CPU
python tools/run_tests_parallel.py -j 8     # a specific worker count
python tools/run_tests_parallel.py -k e2e   # only matching classes
python tools/run_tests_parallel.py --list   # show the units of work
```

Measured on a 12-core machine: **164 s serial, 42 s parallel** (204 tests, same
result). The wall time is bounded by the single slowest test (~26 s), not by
throughput, so more workers stop helping well before the CPU count. The unit of
work is the test class — see the module docstring for why per-test scheduling is
measurably *worse* here.

It has no dependencies and is not part of the shipped package.

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
