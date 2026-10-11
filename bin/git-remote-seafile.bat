@echo off
setlocal
set "DIR=%~dp0.."
rem `-I` and an explicit bootstrap, rather than `-m`.  `python -m package`
rem prepends the *current directory* to sys.path, and a remote helper runs with
rem the cwd set to the repository git is operating on -- which may be a tree the
rem user has just cloned and has not read.  A `requests.py` sitting there
rem shadowed the real module, so `git clone seafile://host/lib/path` in such a
rem tree ran whatever that file contained, as the user.  `-I` is isolated: no
rem cwd on sys.path, no user site-packages, PYTHONPATH ignored.  That is why this
rem file no longer sets PYTHONPATH; the bootstrap adds this checkout instead.
set "GRS_BOOT=import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['git-remote-seafile'] + sys.argv[2:]; from git_remote_seafile.cli import main; sys.exit(main())"

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
%GRS_PY% -I -c "%GRS_BOOT%" "%DIR%" %*
