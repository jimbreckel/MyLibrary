<#
.SYNOPSIS
    Syncs Packt Owned Titles (https://subscription.packtpub.com/owned) and Springer Owned Titles (https://library.springer.com/) into MyLibrary.
.DESCRIPTION
    Wrapper script for scanner/sync_owned.py. Pass any optional arguments (e.g., `packt`, `springer`, `--headless`, `import-file`).
.EXAMPLE
    .\sync_owned.ps1
    .\sync_owned.ps1 packt
    .\sync_owned.ps1 springer
#>
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ScriptDir
try {
    $VenvPython = Join-Path $ScriptDir "venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        & $VenvPython "scanner/sync_owned.py" @args
    } else {
        python "scanner/sync_owned.py" @args
    }
} finally {
    Pop-Location
}
