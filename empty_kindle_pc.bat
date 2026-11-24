@echo off
setlocal
pushd "%~dp0"
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" scanner\empty_kindle_pc.py %*
) else (
    python scanner\empty_kindle_pc.py %*
)
popd
