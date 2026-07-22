$ErrorActionPreference = "Stop"

$InstallDir = if ($env:GSV_BIN_DIR) {
    $env:GSV_BIN_DIR
} else {
    Join-Path $env:LOCALAPPDATA "GSV\bin"
}
$Target = Join-Path $InstallDir "gsv.exe"

if (-not (Test-Path -LiteralPath $Target -PathType Leaf)) {
    throw "GSV executable not found at $Target"
}

& $Target codex uninstall @args
if ($LASTEXITCODE -ne 0) {
    throw "GSV integration uninstall failed with exit code $LASTEXITCODE."
}
Remove-Item -LiteralPath $Target -Force
Write-Host "Removed the GSV executable and Codex integration. Vault and config were preserved."
