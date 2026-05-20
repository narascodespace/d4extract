# Build the D4.Export Windows .exe via PyInstaller.
#
# Usage (from the d4extract repo root, in an activated venv):
#     .\packaging\build.ps1
#
# rustydemon-cli.exe is auto-detected at
# ``rustydemon\target\release\rustydemon-cli.exe`` and bundled
# alongside the output. Pass ``-RustydemonExe <path>`` to override.
# Pass ``-Clean`` to wipe build/ and dist/ before building.

param(
    [switch]$Clean,
    [string]$RustydemonExe,
    [switch]$SkipRustydemon
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $RepoRoot
try {
    if ($Clean) {
        Write-Host "Cleaning build/ and dist/..." -ForegroundColor Cyan
        Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
    }

    Write-Host "Checking PyInstaller is installed..." -ForegroundColor Cyan
    python -m pip show pyinstaller > $null 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing pyinstaller..." -ForegroundColor Yellow
        python -m pip install pyinstaller
    }

    Write-Host "Running PyInstaller..." -ForegroundColor Cyan
    python -m PyInstaller packaging\d4extract.spec --noconfirm
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed (exit $LASTEXITCODE)"
    }

    # Bundle rustydemon-cli.exe. The runtime path-resolver in
    # src/d4extract/casc/rustydemon.py already checks ./rustydemon/, so
    # we drop the binary plus its LICENSE there next to the .exe.
    if (-not $SkipRustydemon) {
        # If no explicit path given, auto-detect the local Cargo build.
        if (-not $RustydemonExe) {
            $AutoPath = Join-Path $RepoRoot "rustydemon\target\release\rustydemon-cli.exe"
            if (Test-Path $AutoPath) {
                $RustydemonExe = $AutoPath
                Write-Host "Auto-detected rustydemon at $AutoPath" -ForegroundColor Cyan
            }
        }

        if ($RustydemonExe) {
            if (-not (Test-Path $RustydemonExe)) {
                throw "rustydemon binary not found at $RustydemonExe"
            }
            $TargetDir = Join-Path $RepoRoot "dist\rustydemon"
            New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
            Copy-Item -Force $RustydemonExe (Join-Path $TargetDir "rustydemon-cli.exe")

            # AGPL redistribution requires we include the upstream LICENSE.
            $RdLicense = Join-Path $RepoRoot "rustydemon\LICENSE"
            if (Test-Path $RdLicense) {
                Copy-Item -Force $RdLicense (Join-Path $TargetDir "LICENSE.rustydemon.txt")
            }
            Write-Host "Bundled rustydemon-cli.exe at dist\rustydemon\" -ForegroundColor Green
        } else {
            Write-Host "No rustydemon binary found — skipping bundle. Pass -RustydemonExe or build it under rustydemon\target\release\." -ForegroundColor Yellow
        }
    }

    $ExePath = Join-Path $RepoRoot "dist\d4extract.exe"
    if (Test-Path $ExePath) {
        $Size = [math]::Round(((Get-Item $ExePath).Length / 1MB), 1)
        Write-Host "`nBuild complete: $ExePath ($Size MB)" -ForegroundColor Green
    } else {
        throw "Expected $ExePath was not produced."
    }
}
finally {
    Pop-Location
}
