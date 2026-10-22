

# -- Run report: exit-code classes and --json (SNAPSHOT.md, "Exit codes and --json") ----

#: The exit codes a script can rely on.  1 and 2 are what the tool always did; 3 is
#: new and opt-in (``--exit-warnings``), because a run that exited 0 with a warning
#: must not start failing a scheduled job that was working.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_WARNINGS = 3

#: Facts a run records for the ``--json`` document, next to ``END_OF_RUN_NOTES``:
#: the code that knows them says them, and nothing reads them unless asked.
RUN_FACTS: dict[str, Any] = {}


class _LineTap:
    """A text stream that passes everything through and remembers some of its lines.

    ``Warning:`` and ``Error:`` are printed from dozens of places, some after a
    blank line and some from a child process's captured output; reading them where
    they leave the process finds all of them without a second path to keep in step.
    """

    def __init__(self, stream: Any, warnings: list[str], errors: list[str]) -> None:
        self._stream = stream
        self._warnings = warnings
        self._errors = errors
        self._partial = ""

    def write(self, text: str) -> int:
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            self._see(line)
        return self._stream.write(text)

    def _see(self, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith("Warning:") and stripped not in self._warnings:
            self._warnings.append(stripped)
        elif stripped.startswith("Error:"):
            self._errors.append(stripped)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def run_mode(args: argparse.Namespace) -> str:
    for flag, mode in (
        ("restore_drill", "restore-drill"), ("restore", "restore"), ("maintain", "maintain"),
        ("payload_verify", "payload-verify"), ("payload_view", "payload-view"),
        ("payload_gc", "payload-gc"),
    ):
        if getattr(args, flag, None) not in (None, False):
            return mode
    return "snapshot"


def run_reported(
    args: argparse.Namespace, body: Callable[[argparse.Namespace], int]
) -> int:
    """Run *body*; apply ``--exit-warnings`` and write the ``--json`` document.

    With neither flag this is ``body(args)`` and nothing else, so the default
    behaviour (streams, exit codes) is exactly what it was.  With ``--json`` the
    human text moves to stderr and stdout carries one line: the document.
    """
    if not (args.json or args.exit_warnings):
        return body(args)
    RUN_FACTS.clear()
    warnings: list[str] = []
    errors: list[str] = []
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _LineTap(real_err if args.json else real_out, warnings, errors)
    sys.stderr = _LineTap(real_err, warnings, errors)
    started = time.monotonic()
    try:
        rc = body(args)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    if rc == EXIT_OK and warnings and args.exit_warnings:
        rc = EXIT_WARNINGS
    if args.json:
        status = "failed" if rc in (EXIT_FAILED, EXIT_USAGE) else "warnings" if warnings else "ok"
        document = {
            "schema": 1,
            "tool": "git-seafile-snapshot",
            "version": tool_version(),
            "mode": run_mode(args),
            "status": status,
            "exit_code": rc,
            "seconds": round(time.monotonic() - started, 1),
            "warnings": warnings,
            "error": errors[-1].removeprefix("Error:").strip() if errors else None,
            "result": dict(RUN_FACTS),
        }
        real_out.write(json.dumps(document, sort_keys=True) + "\n")
        real_out.flush()
    return rc
