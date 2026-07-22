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

& $Target bridge stop | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "GSV Bridge stop failed with exit code $LASTEXITCODE."
}
& $Target codex uninstall @args
if ($LASTEXITCODE -ne 0) {
    throw "GSV cleanup is incomplete (exit $LASTEXITCODE). The executable was kept so you can run the printed retry command."
}
Remove-Item -LiteralPath $Target -Force
Write-Host "Removed the GSV executable and verified GSV-owned integration. Vault and config were preserved."
