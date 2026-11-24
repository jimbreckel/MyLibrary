@echo off
setlocal
pushd "%~dp0"
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" scanner\sync_owned.py %*
) else (
    python scanner\sync_owned.py %*
)
popd
