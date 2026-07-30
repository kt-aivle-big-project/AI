param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$WarehouseId = "WH-001",
    [string]$SimulationId = "SIM-V18-MIXED",
    [ValidateSet("ortools", "cuopt", "cuopt_payload_only")]
    [string]$Backend = "ortools"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$encodedWarehouse = [System.Uri]::EscapeDataString($WarehouseId)
$encodedSimulation = [System.Uri]::EscapeDataString($SimulationId)

Write-Host "[1/3] Native plan preflight" -ForegroundColor Cyan
$preflight = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/warehouses/$encodedWarehouse/missions/plan/preflight?simulation_id=$encodedSimulation"
$preflight | ConvertTo-Json -Depth 20 | Out-Host
if (-not $preflight.ready) {
    throw "Native plan preflight is NOT_READY."
}

Write-Host "[2/3] POST native plan" -ForegroundColor Cyan
$body = @{
  warehouse_id = $WarehouseId
  simulation_id = $SimulationId
  optimization_backend = $Backend
  events = @(
    @{ type = "new_order"; order_id = "ORD-001" },
    @{ type = "inbound_item_arrived"; inbound_id = "IN-001" }
  )
} | ConvertTo-Json -Depth 20

$response = Invoke-RestMethod `
  -Method Post `
  -Uri "$BaseUrl/api/v1/warehouses/$encodedWarehouse/missions/plan" `
  -ContentType "application/json; charset=utf-8" `
  -Body $body

$response | ConvertTo-Json -Depth 40 | Out-Host
if ($Backend -eq "cuopt_payload_only") {
    if ($response.status -ne "ready_for_cuopt") {
        throw "Expected ready_for_cuopt, got $($response.status)."
    }
    return
}
if ($response.status -ne "plan_validated" -or $null -eq $response.plan) {
    throw "Expected plan_validated with plan, got $($response.status)."
}

Write-Host "[3/3] Compact node/validation trace" -ForegroundColor Cyan
$planId = [string]$response.plan.plan_id
$trace = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/warehouses/$encodedWarehouse/missions/plans/$planId/trace"

$trace.checks | ConvertTo-Json -Depth 10 | Out-Host
$trace.nodes | Format-Table node_name, status, duration_ms, llm_used, error_code

[PSCustomObject]@{
  status = $response.status
  plan_id = $planId
  plan_version = $response.plan.plan_version
  robot_count = @($response.plan.robots).Count
  step_count = (@($response.plan.robots) | ForEach-Object { @($_.steps).Count } | Measure-Object -Sum).Sum
  makespan_ms = $response.plan.makespan_ms
  all_trace_checks_true = (@($trace.checks.PSObject.Properties.Value | Where-Object { $_ -ne $true }).Count -eq 0)
} | Format-List
