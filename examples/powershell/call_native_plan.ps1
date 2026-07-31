param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$WarehouseId = "WH-001",
    [string]$SimulationId = "SIM-V18-MIXED",
    [ValidateSet("ortools", "cuopt", "cuopt_payload_only")]
    [string]$Backend = "ortools",
    [ValidateSet("structured", "natural")]
    [string]$InputMode = "structured",
    [string]$UserCommand = "ORD-001을 출고하고 IN-001도 입고해. 전체 완료시간을 최소화해."
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
$bodyObject = @{
  warehouse_id = $WarehouseId
  simulation_id = $SimulationId
  optimization_backend = $Backend
}
if ($InputMode -eq "natural") {
  $bodyObject.events = @()
  $bodyObject.user_command = $UserCommand
}
else {
  $bodyObject.events = @(
    @{ type = "new_order"; order_id = "ORD-001" },
    @{ type = "inbound_item_arrived"; inbound_id = "IN-001" }
  )
}
$body = $bodyObject | ConvertTo-Json -Depth 20

$response = Invoke-RestMethod `
  -Method Post `
  -Uri "$BaseUrl/api/v1/warehouses/$encodedWarehouse/missions/plan" `
  -ContentType "application/json; charset=utf-8" `
  -Body $body `
  -TimeoutSec 900

$response | ConvertTo-Json -Depth 60 | Out-Host
if ($Backend -eq "cuopt_payload_only") {
    if ($response.status -ne "ready_for_cuopt") {
        throw "Expected ready_for_cuopt, got $($response.status)."
    }
    return
}
if ($response.status -ne "plan_validated" -or $null -eq $response.plan) {
    Write-Host "[Input rejection]" -ForegroundColor Yellow
    $response.input_rejection | ConvertTo-Json -Depth 30 | Out-Host
    Write-Host "[Errors]" -ForegroundColor Yellow
    $response.errors | ConvertTo-Json -Depth 30 | Out-Host
    throw "Expected plan_validated with plan, got $($response.status)."
}

Write-Host "[3/3] Compact node/validation trace" -ForegroundColor Cyan
$planId = [string]$response.plan.plan_id
$trace = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/warehouses/$encodedWarehouse/missions/plans/$planId/trace"

$trace.checks | ConvertTo-Json -Depth 10 | Out-Host
$trace.repository | ConvertTo-Json -Depth 10 | Out-Host
$trace.nodes | Format-Table node_name, status, duration_ms, llm_used, error_code

$logicalById = @{}
foreach ($operation in @($response.plan.logical_operations)) {
    $logicalById[[string]$operation.operation_id] = $operation
}
foreach ($operationId in @("ORD-001", "IN-001")) {
    if (-not $logicalById.ContainsKey($operationId)) {
        throw "Missing logical operation: $operationId"
    }
    if (@($logicalById[$operationId].task_ids).Count -eq 0) {
        throw "$operationId has no task_ids."
    }
    if ([string]::IsNullOrWhiteSpace([string]$logicalById[$operationId].assigned_robot_id)) {
        throw "$operationId has no assigned_robot_id."
    }
}

$failedChecks = @(
    $trace.checks.PSObject.Properties |
      Where-Object { $_.Value -ne $true }
)
if ($failedChecks.Count -gt 0) {
    throw "Trace contains failed checks: $($failedChecks.Name -join ', ')"
}

[PSCustomObject]@{
  status = $response.status
  final_route = $response.final_route
  router_llm_executed = $response.router_llm_executed
  plan_id = $planId
  plan_version = $response.plan.plan_version
  robot_count = @($response.plan.robots).Count
  step_count = (@($response.plan.robots) | ForEach-Object { @($_.steps).Count } | Measure-Object -Sum).Sum
  logical_operation_ids = (@($response.plan.logical_operations) | ForEach-Object { $_.operation_id }) -join ","
  makespan_ms = $response.plan.makespan_ms
  repository_type = $trace.repository.repository_type
  all_trace_checks_true = $true
} | Format-List
