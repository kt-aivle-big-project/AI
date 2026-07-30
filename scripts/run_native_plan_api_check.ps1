param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$WarehouseId = "WH-001",
    [string]$SimulationId = "SIM-V18-MIXED",
    [ValidateSet("ortools", "cuopt", "cuopt_payload_only")]
    [string]$Backend = "ortools",
    [int]$Repeat = 1
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
    python -m scripts.check_native_plan_api `
      --base-url $BaseUrl `
      --warehouse-id $WarehouseId `
      --simulation-id $SimulationId `
      --backend $Backend `
      --repeat $Repeat
    if ($LASTEXITCODE -ne 0) {
        throw "Native plan API check failed."
    }
}
finally {
    Pop-Location
}
