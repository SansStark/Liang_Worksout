param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $SyncArgs
)

$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
}

Push-Location $PSScriptRoot
try {
    # Python reads .env itself. Keep credentials out of process arguments and errors.
    & $pythonPath (Join-Path $PSScriptRoot 'run_page\strava_sync.py') @SyncArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'Strava sync stopped. See the message above; saved recent activities are retained.'
    }
    Write-Host 'Sync complete. Refresh the dev site, or rebuild to publish the new data.'
} finally {
    Pop-Location
}
