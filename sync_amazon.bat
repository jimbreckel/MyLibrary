@echo off
setlocal
pushd "%~dp0"
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" scanner\sync_amazon.py %*
) else (
    python scanner\sync_amazon.py %*
)
popd

