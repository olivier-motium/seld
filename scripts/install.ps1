$ErrorActionPreference = "Stop"

$Version = if ($env:GSV_VERSION) { $env:GSV_VERSION } else { "0.1.0" }
$ReleaseBase = if ($env:GSV_RELEASE_BASE_URL) {
    $env:GSV_RELEASE_BASE_URL
} else {
    "https://github.com/olivier-motium/gsv/releases/download/v$Version"
}
$InstallDir = if ($env:GSV_BIN_DIR) {
    $env:GSV_BIN_DIR
} else {
    Join-Path $env:LOCALAPPDATA "GSV\bin"
}
$Target = Join-Path $InstallDir "gsv.exe"

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw "Codex CLI was not found on PATH. Install Codex Desktop or the Codex CLI first."
}

$Architecture = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
    "X64" { "x86_64" }
    # Windows ARM64 uses the x64 asset through emulation; hosted E2E must validate it.
    "Arm64" { "x86_64" }
    default { throw "Unsupported CPU architecture: $_" }
}
$Asset = "gsv-windows-$Architecture.exe"
$Temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("gsv-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Temporary | Out-Null

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
    Copy-Item -LiteralPath $Download -Destination $Staged
    if (Test-Path -LiteralPath $Target) {
        if ((Get-Item -LiteralPath $Target).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "Refusing to replace a reparse-point GSV target: $Target"
        }
        $Backup = Join-Path $InstallDir (".gsv.previous." + [guid]::NewGuid() + ".exe")
    }
    try {
        if ($Backup) {
            [System.IO.File]::Replace($Staged, $Target, $Backup, $true)
        } else {
            [System.IO.File]::Move($Staged, $Target)
        }
        $InstalledNew = $true
        & $Target setup @args
        if ($LASTEXITCODE -ne 0) {
            throw "GSV setup failed with exit code $LASTEXITCODE."
        }
        if ($Backup -and (Test-Path -LiteralPath $Backup)) {
            Remove-Item -LiteralPath $Backup -Force
        }
    } catch {
        if ($InstalledNew) {
            if ($Backup -and (Test-Path -LiteralPath $Backup)) {
                if (Test-Path -LiteralPath $Target) {
                    $FailedCandidate = Join-Path $InstallDir (".gsv.failed." + [guid]::NewGuid() + ".exe")
                    [System.IO.File]::Replace($Backup, $Target, $FailedCandidate, $true)
                    Remove-Item -LiteralPath $FailedCandidate -Force -ErrorAction SilentlyContinue
                } else {
                    [System.IO.File]::Move($Backup, $Target)
                }
            } else {
                Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
            }
        }
        throw
    } finally {
        Remove-Item -LiteralPath $Staged -Force -ErrorAction SilentlyContinue
    }
} finally {
    Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Installed GSV at $Target"
Write-Host "Restart Codex, open a fresh task, and ask: What do you remember?"
