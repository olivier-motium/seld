$ErrorActionPreference = "Stop"

$InstallDir = if ($env:GSV_BIN_DIR) {
    $env:GSV_BIN_DIR
} else {
    Join-Path $env:LOCALAPPDATA "GSV\bin"
}
$Target = Join-Path $InstallDir "gsv.exe"

if (-not (Test-Path -LiteralPath $Target -PathType Leaf)) {
    throw "Seld executable not found at $Target"
}

& $Target bridge stop | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Seld Bridge stop failed with exit code $LASTEXITCODE."
}
$CleanupOutput = (& $Target --json codex uninstall @args | Out-String).Trim()
$CleanupStatus = $LASTEXITCODE
Write-Output $CleanupOutput
try {
    $CleanupPayload = $CleanupOutput | ConvertFrom-Json
} catch {
    throw "Seld cleanup output was not valid JSON. The executable was kept."
}
if (
    $CleanupPayload.result.integration_removed -eq $true -and
    $CleanupPayload.result.recovery_retained -eq $true
) {
    Write-Host "Active Seld integration was removed, but receipt-bound recovery evidence remains. The executable was kept. Inspect and delete only the exact retained_cleanup_paths printed above, then re-run this uninstaller to retire the catalog safely."
    exit 3
}
if ($CleanupStatus -ne 0) {
    throw "Seld cleanup is incomplete (exit $CleanupStatus). The executable was kept so you can run the printed retry command."
}
if (
    $CleanupPayload.ok -ne $true -or
    $CleanupPayload.result.cleanup_complete -ne $true
) {
    throw "Seld cleanup output did not verify ok:true and result.cleanup_complete:true. The executable was kept."
}
Remove-Item -LiteralPath $Target -Force
Write-Host "Removed the Seld executable and verified Seld-owned integration. Vault and config were preserved."
