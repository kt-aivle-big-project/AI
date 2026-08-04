param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$WarehouseId = "WH-001",
    [string]$SimulationId = "SIM-V18-MIXED",
    [string]$UserCommand = "ORD-001을 출고하고 IN-001도 입고해. 전체 완료시간을 최소화해."
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$scriptPath = Join-Path $PSScriptRoot "call_native_plan.ps1"
& $scriptPath `
  -BaseUrl $BaseUrl `
  -WarehouseId $WarehouseId `
  -SimulationId $SimulationId `
  -Backend cuopt `
  -InputMode natural `
  -UserCommand $UserCommand
