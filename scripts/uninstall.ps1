$ErrorActionPreference = "Stop"

$InstallDir = if ($env:CONTINUITY_BIN_DIR) {
    $env:CONTINUITY_BIN_DIR
} else {
    Join-Path $env:LOCALAPPDATA "Continuity\bin"
}
$Target = Join-Path $InstallDir "continuity.exe"

if (-not (Test-Path -LiteralPath $Target -PathType Leaf)) {
    throw "Continuity executable not found at $Target"
}

& $Target codex uninstall @args
if ($LASTEXITCODE -ne 0) {
    throw "Continuity integration uninstall failed with exit code $LASTEXITCODE."
}
Remove-Item -LiteralPath $Target -Force
Write-Host "Removed the Continuity executable and Codex integration. Vault and config were preserved."
