$ErrorActionPreference = "Stop"

$Version = if ($env:GSV_VERSION) { $env:GSV_VERSION } else { "0.2.0" }
$ReleaseBase = if ($env:GSV_RELEASE_BASE_URL) {
    $env:GSV_RELEASE_BASE_URL
} else {
    "https://github.com/olivier-motium/seld/releases/download/v$Version"
}
$InstallDir = if ($env:GSV_BIN_DIR) {
    $env:GSV_BIN_DIR
} else {
    Join-Path $env:LOCALAPPDATA "GSV\bin"
}
$Target = Join-Path $InstallDir "gsv.exe"

$Architecture = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
    "X64" { "x86_64" }
    # Windows ARM64 uses the x64 asset through emulation; hosted E2E must validate it.
    "Arm64" { "x86_64" }
    default { throw "Unsupported CPU architecture: $_" }
}
$Asset = "gsv-windows-$Architecture.exe"
$Temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("gsv-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Temporary | Out-Null
$RepairRequired = $false
$RetainedBackup = $null

function Invoke-VerifiedBridgeStop {
    param([Parameter(Mandatory = $true)][string]$Executable)

    $Raw = & $Executable --json bridge stop
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "The existing Bridge could not be verified and stopped (exit $ExitCode)."
    }
    try {
        $Payload = ($Raw | Out-String) | ConvertFrom-Json
    } catch {
        throw "The staged GSV executable returned an invalid Bridge stop result."
    }
    if (-not $Payload.ok) {
        throw "The staged GSV executable did not confirm its Bridge stop result."
    }
    return $Payload.result
}

function Invoke-FileReplaceWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Backup
    )

    for ($Attempt = 1; $Attempt -le 50; $Attempt++) {
        try {
            [System.IO.File]::Replace($Source, $Destination, $Backup, $true)
            return
        } catch [System.IO.IOException] {
            if ($Attempt -eq 50) { throw }
            Start-Sleep -Milliseconds 100
        }
    }
}

function Test-PreviousExecutableCompatibility {
    param([Parameter(Mandatory = $true)][string]$Executable)

    try {
        $ProbeRaw = & $Target --json rollback-check $Executable 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $Probe = ($ProbeRaw | Out-String) | ConvertFrom-Json
        return ($Probe.ok -eq $true -and $Probe.result.compatible -eq $true)
    } catch {
        return $false
    }
}

try {
    $Download = Join-Path $Temporary $Asset
    if ($env:GSV_BINARY) {
        Copy-Item -LiteralPath $env:GSV_BINARY -Destination $Download
        if (-not $env:GSV_BINARY_SHA256) {
            throw "GSV_BINARY_SHA256 is required with GSV_BINARY."
        }
        $Expected = $env:GSV_BINARY_SHA256.ToLowerInvariant()
    } else {
        Invoke-WebRequest -Uri "$ReleaseBase/$Asset" -OutFile $Download
        $ChecksumPath = "$Download.sha256"
        Invoke-WebRequest -Uri "$ReleaseBase/$Asset.sha256" -OutFile $ChecksumPath
        $Expected = ((Get-Content -LiteralPath $ChecksumPath -TotalCount 1) -split '\s+')[0].ToLowerInvariant()
    }

    $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Download).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) {
        throw "GSV artifact checksum verification failed."
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $Staged = Join-Path $InstallDir (".gsv.new." + [guid]::NewGuid() + ".exe")
    $Backup = $null
    $InstalledNew = $false
    $PriorBridgeStopped = $false
    try {
        Copy-Item -LiteralPath $Download -Destination $Staged
        & $Staged --version | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "The staged GSV executable did not pass its version preflight."
        }
        if (Test-Path -LiteralPath $Target) {
            if ((Get-Item -LiteralPath $Target).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to replace a reparse-point GSV target: $Target"
            }
            $Backup = Join-Path $InstallDir (".gsv.previous." + [guid]::NewGuid() + ".exe")
            $PriorStop = Invoke-VerifiedBridgeStop -Executable $Staged
            $PriorBridgeStopped = [bool]$PriorStop.stopped
        }
        if ($Backup) {
            Invoke-FileReplaceWithRetry -Source $Staged -Destination $Target -Backup $Backup
        } else {
            [System.IO.File]::Move($Staged, $Target)
        }
        $InstalledNew = $true
        & $Target setup @args
        $SetupStatus = $LASTEXITCODE
        if ($SetupStatus -eq 4) {
            $RepairRequired = $true
            $RetainedBackup = $Backup
        } elseif ($SetupStatus -ne 0) {
            throw "GSV setup failed with exit code $SetupStatus."
        }
        if (-not $RepairRequired -and $Backup -and (Test-Path -LiteralPath $Backup)) {
            Remove-Item -LiteralPath $Backup -Force
        }
    } catch {
        $Failure = $_
        $RollbackSafe = $true
        if ($InstalledNew) {
            try {
                Invoke-VerifiedBridgeStop -Executable $Target | Out-Null
            } catch {
                $RollbackSafe = $false
            }
        }
        if ($InstalledNew -and -not $RollbackSafe) {
            if ($Backup) {
                throw "GSV setup failed and its Bridge stop could not be verified; the previous executable remains staged at $Backup. Original error: $($Failure.Exception.Message)"
            }
            throw "GSV setup failed and its Bridge stop could not be verified; the candidate executable was left in place. Original error: $($Failure.Exception.Message)"
        }
        if ($InstalledNew) {
            if ($Backup -and (Test-Path -LiteralPath $Backup)) {
                if (-not (Test-PreviousExecutableCompatibility -Executable $Backup)) {
                    throw "GSV setup failed and the previous executable could not prove it can read the current vault; no rollback was performed. The candidate remains installed and the previous executable remains staged at $Backup. Original error: $($Failure.Exception.Message)"
                }
                if (Test-Path -LiteralPath $Target) {
                    $FailedCandidate = Join-Path $InstallDir (".gsv.failed." + [guid]::NewGuid() + ".exe")
                    Invoke-FileReplaceWithRetry -Source $Backup -Destination $Target -Backup $FailedCandidate
                    Remove-Item -LiteralPath $FailedCandidate -Force -ErrorAction SilentlyContinue
                } else {
                    [System.IO.File]::Move($Backup, $Target)
                }
            } else {
                Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
            }
        }
        if ($PriorBridgeStopped -and (Test-Path -LiteralPath $Target -PathType Leaf)) {
            & $Target --json bridge open --no-browser | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "The previous executable was restored, but its Bridge could not be restarted."
            }
        }
        if ($SetupStatus -and $SetupStatus -ne 4) {
            [Console]::Error.WriteLine(
                "GSV setup failed; the previous executable was restored when a compatible backup was available."
            )
            exit $SetupStatus
        }
        throw $Failure
    } finally {
        Remove-Item -LiteralPath $Staged -Force -ErrorAction SilentlyContinue
    }
} finally {
    Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Installed GSV at $Target"
if ($RepairRequired) {
    [Console]::Error.WriteLine("GSV and its Codex integration were installed, but the Bridge needs repair. No executable rollback was attempted.")
    if ($RetainedBackup -and (Test-Path -LiteralPath $RetainedBackup)) {
        [Console]::Error.WriteLine("The previous executable remains staged at $RetainedBackup.")
    }
    [Console]::Error.WriteLine("Run gsv --json bridge status, then retry gsv bridge open.")
    exit 4
}
Write-Host "GSV setup completed. Run gsv to open or verify the Bridge."
Write-Host "Restart Codex, open one fresh task, and run `$gsv-onboard."
