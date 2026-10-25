

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


_WARNING_LIMIT = 500


class _LineTap:
    """A text stream that passes everything through and remembers some of its lines.

    ``Warning:`` and ``Error:`` are printed from dozens of places, some after a
    blank line and some from a child process's captured output; reading them where
    they leave the process finds all of them without a second path to keep in step.
    """

    def __init__(
        self, stream: Any, warnings: list[str], errors: list[str], seen: set[str]
    ) -> None:
        self._stream = stream
        self._warnings = warnings
        self._errors = errors
        self._seen = seen
        self._partial = ""
        self._open: int | None = None
        self._open_error: int | None = None

    def write(self, text: str) -> int:
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            self._see(line)
        return self._stream.write(text)

    def _see(self, line: str) -> None:
        stripped = line.strip()
        if self._open is not None:
            # A warning is wrapped over several lines (and may list paths); it ends
            # at the blank line that follows it.  Capped, so a stray never swallows
            # the run's output.
            if stripped and len(self._warnings[self._open]) < _WARNING_LIMIT:
                self._warnings[self._open] += " " + stripped
                return
            self._open = None
        if self._open_error is not None:
            # An error is wrapped the same way -- the house style lists the
            # affected paths on the following lines -- and `.error` carrying
            # only the headline loses exactly the part a scheduler would act
            # on (REVIEW.iq3_s E4, reproduced).
            if stripped and len(self._errors[self._open_error]) < _WARNING_LIMIT:
                self._errors[self._open_error] += " " + stripped
                return
            self._open_error = None
        if stripped.startswith("Warning:") and stripped not in self._seen:
            self._seen.add(stripped)
            self._warnings.append(stripped)
            self._open = len(self._warnings) - 1
        elif stripped.startswith("Error:"):
            self._errors.append(stripped)
            self._open_error = len(self._errors) - 1

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
    seen: set[str] = set()
    sys.stdout = _LineTap(real_err if args.json else real_out, warnings, errors, seen)
    sys.stderr = _LineTap(real_err, warnings, errors, seen)
    started = time.monotonic()
    rc: int | None = None
    try:
        rc = body(args)
    except KeyboardInterrupt:
        # A scheduler's SIGINT is a run outcome, not a reason to die with a
        # traceback and no document at all: the contract says stdout is then
        # exactly one JSON line, and it is precisely the interrupted runs a
        # scheduler most needs to classify (REVIEW.iq3_s D7/E5, reproduced:
        # rc=-2, empty stdout).
        sys.stderr.write("Error: interrupted by a signal\n")
        sys.stderr.flush()
        rc = EXIT_FAILED
    except BaseException as exc:
        # run_command catches SnapshotError; anything else (an OSError from a
        # raw file operation, a GitError that escaped a wrapper) would leave
        # the same document-less traceback.  Reported, then re-raised: an
        # unexpected exception is a bug, and hiding it behind status "failed"
        # would hide the traceback from the person who can fix it.
        sys.stderr.write(f"Error: {type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        rc = EXIT_FAILED
        raise
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        if rc == EXIT_OK and warnings and args.exit_warnings:
            rc = EXIT_WARNINGS
        if args.json:
            status = (
                "failed" if rc in (EXIT_FAILED, EXIT_USAGE)
                else "warnings" if warnings else "ok"
            )
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
