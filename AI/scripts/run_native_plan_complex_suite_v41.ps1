param(
    [string]$BaseUrl = "http://localhost:8000",
    [ValidateSet("ortools", "cuopt", "cuopt_payload_only")]
    [string]$Backend = "ortools",
    [string[]]$Include = @(),
    [string[]]$Category = @(),
    [string[]]$Tag = @(),
    [int]$MinDifficulty = 1,
    [int]$Repeat = 1,
    [int]$MaxWorkers = 1,
    [int]$TimeoutSeconds = 900,
    [string]$OutputDir = "",
    [switch]$SkipOpenAI,
    [switch]$NoDebug,
    [switch]$Archive,
    [switch]$Strict
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Root
try {
    $Arguments = @(
        "-m", "scripts.run_native_plan_complex_suite_v41",
        "--base-url", $BaseUrl,
        "--backend", $Backend,
        "--min-difficulty", $MinDifficulty,
        "--repeat", $Repeat,
        "--max-workers", $MaxWorkers,
        "--timeout-seconds", $TimeoutSeconds
    )
    foreach ($Value in $Include) { $Arguments += @("--include", $Value) }
    foreach ($Value in $Category) { $Arguments += @("--category", $Value) }
    foreach ($Value in $Tag) { $Arguments += @("--tag", $Value) }
    if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
        $Arguments += @("--output-dir", $OutputDir)
    }
    if ($SkipOpenAI) { $Arguments += "--skip-openai" }
    if ($NoDebug) { $Arguments += "--no-debug" }
    if ($Archive) { $Arguments += "--archive" }
    if ($Strict) { $Arguments += "--strict" }

    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Native Plan complex suite failed."
    }
}
finally {
    Pop-Location
}
