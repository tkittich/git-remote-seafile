@echo off
setlocal
set "DIR=%~dp0.."
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
python -m git_remote_seafile.cli %*
