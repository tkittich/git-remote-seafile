@echo off
setlocal
set "DIR=%~dp0.."
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
where python >nul 2>nul
if errorlevel 1 (
    echo git-remote-seafile: python was not found on PATH >&2
    exit /b 127
)
python -m git_remote_seafile.cli %*
