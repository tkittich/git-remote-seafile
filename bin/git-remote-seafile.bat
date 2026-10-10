@echo off
setlocal
set "DIR=%~dp0.."
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
rem `where python` succeeds on machines where `python` is the Microsoft Store
rem alias -- which then fails with the Store's own message.  --version is the
rem probe that actually runs the interpreter.
python --version >nul 2>nul
if errorlevel 1 (
    py -3 --version >nul 2>nul
    if errorlevel 1 (
        echo git-remote-seafile: neither python nor py -3 could run on PATH >&2
        exit /b 127
    )
    py -3 -m git_remote_seafile.cli %*
    exit /b %errorlevel%
)
python -m git_remote_seafile.cli %*
