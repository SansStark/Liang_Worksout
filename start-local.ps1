param([int] $Port = 5173)

$ErrorActionPreference = 'Stop'
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if ($nodeCommand) {
    $nodeDirectory = Split-Path -Parent $nodeCommand.Source
} else {
    $nodeRuntime = Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot '.cache\tools') -Directory -Filter 'node-*-win-x64' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $nodeRuntime) {
        throw 'Install Node.js 22 LTS or newer, then retry.'
    }
    $nodeDirectory = $nodeRuntime.FullName
}
$env:PATH = $nodeDirectory + ';' + $env:PATH
Push-Location $PSScriptRoot
try {
    & (Join-Path $nodeDirectory 'npm.cmd') run dev -- --host 127.0.0.1 --port $Port
} finally {
    Pop-Location
}
