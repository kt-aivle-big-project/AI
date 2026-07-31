param(
    [Parameter(Mandatory=$true)][string]$Scenario,
    [string]$BaseUrl = "http://localhost:8000",
    [ValidateSet("ortools", "cuopt", "cuopt_payload_only")]
    [string]$Backend = "ortools",
    [int]$Repeat = 1,
    [int]$TimeoutSeconds = 900,
    [string]$OutputDir = "",
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
        "-m", "scripts.run_native_plan_complex_scenario_v41",
        "--scenario", $Scenario,
        "--base-url", $BaseUrl,
        "--backend", $Backend,
        "--repeat", $Repeat,
        "--timeout-seconds", $TimeoutSeconds
    )
    if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
        $Arguments += @("--output-dir", $OutputDir)
    }
    if ($NoDebug) { $Arguments += "--no-debug" }
    if ($Archive) { $Arguments += "--archive" }
    if ($Strict) { $Arguments += "--strict" }

    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Native Plan complex scenario failed."
    }
}
finally {
    Pop-Location
}
