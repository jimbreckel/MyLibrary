$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $ScriptDir "venv\Scripts\python.exe"
$TargetPy = Join-Path $ScriptDir "scanner\empty_kindle_pc.py"
if (Test-Path $VenvPy) {
    & $VenvPy $TargetPy @args
} else {
    python $TargetPy @args
}
