<#
.SYNOPSIS
    Syncs Amazon Kindle, Kindle+ (Kindle Unlimited / Prime), Kindle Bookmarks/Highlights, and Audible Library into MyLibrary.
.DESCRIPTION
    Wrapper script for scanner/sync_amazon.py. Pass any optional arguments (e.g., `kindle`, `audible`, `--headless`, `import-file`).
.EXAMPLE
    .\sync_amazon.ps1
    .\sync_amazon.ps1 kindle
    .\sync_amazon.ps1 --headless
#>
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ScriptDir
try {
    $VenvPython = Join-Path $ScriptDir "venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        & $VenvPython "scanner/sync_amazon.py" @args
    } else {
        python "scanner/sync_amazon.py" @args
    }
} finally {
    Pop-Location
}

