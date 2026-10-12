@echo off
setlocal
set "DIR=%~dp0.."
rem An explicit bootstrap, rather than `-m`.  `python -m package` prepends the
rem *current directory* to sys.path, and a remote helper runs with the cwd set
rem to the repository git is operating on -- which may be a tree the user has
rem just cloned and has not read.  A `requests.py` sitting there shadowed the
rem real module, so `git clone seafile://host/lib/path` in such a tree ran
rem whatever that file contained, as the user.  The bootstrap filters the cwd
rem out of sys.path itself, then adds this checkout explicitly.
rem
rem `-E`, deliberately not `-I`: `-I` implies `-s`, which disables the per-user
rem site directory, so a user who ran `pip install --user requests` gets
rem `ModuleNotFoundError: requests` from inside a git command.  That is why this
rem file no longer sets PYTHONPATH -- `-E` ignores it -- and why the filter
rem below is what keeps the cwd out.  See tests/test_launcher.py.
set "GRS_BOOT=import sys; sys.path = [p for p in sys.path if p not in ('', '.')]; sys.path.insert(0, sys.argv[1]); sys.argv = ['git-remote-seafile'] + sys.argv[2:]; from git_remote_seafile.cli import main; sys.exit(main())"

rem `where python` succeeds on machines where `python` is the Microsoft Store
rem alias -- which then fails with the Store's own message.  --version is the
rem probe that actually runs the interpreter.
set "GRS_PY=python"
python --version >nul 2>nul
if errorlevel 1 set "GRS_PY=py -3"

%GRS_PY% --version >nul 2>nul
if errorlevel 1 (
    echo git-remote-seafile: neither python nor py -3 could run on PATH >&2
    exit /b 127
)

rem Expanded on a top-level line, not inside a block: the parentheses in the
rem bootstrap are safe here, and cmd's block parser is not asked to skip them.
%GRS_PY% -E -c "%GRS_BOOT%" "%DIR%" %*
