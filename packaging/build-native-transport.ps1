[CmdletBinding()]
param(
    [string]$OutputPath = (Join-Path $env:LOCALAPPDATA "UsageMaxDisplay\trofeo-pump.exe")
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Manifest = Join-Path $RepoRoot "native\trofeo-pump\Cargo.toml"
$TempRoot = Join-Path $env:TEMP ("usagemax-display-build-" + [guid]::NewGuid().ToString("N"))

try {
    New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
    & cargo build --release --manifest-path $Manifest --target-dir (Join-Path $TempRoot "target")
    if ($LASTEXITCODE -ne 0) {
        throw "cargo build failed with exit code $LASTEXITCODE"
    }

    $BuiltBinary = Join-Path $TempRoot "target\release\trofeo-pump.exe"
    if (-not (Test-Path -LiteralPath $BuiltBinary -PathType Leaf)) {
        throw "cargo did not produce the expected binary: $BuiltBinary"
    }

    $ResolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
    $OutputDirectory = Split-Path -Parent $ResolvedOutput
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    Copy-Item -LiteralPath $BuiltBinary -Destination $ResolvedOutput -Force
    Write-Output "Built native transport at: $ResolvedOutput"
}
finally {
    if (Test-Path -LiteralPath $TempRoot) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force
    }
}
