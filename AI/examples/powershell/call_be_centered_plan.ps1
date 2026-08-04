param(
    [long]$SimulationRunId = 1,
    [string]$SpringBaseUrl = "http://localhost:8080",
    [string]$RequestFile = ".\examples\be_centered\spring_plan_request.json",
    [string]$AccessToken = ""
)

$ErrorActionPreference = "Stop"
$headers = @{}
if (-not [string]::IsNullOrWhiteSpace($AccessToken)) {
    $headers.Authorization = "Bearer $AccessToken"
}

Write-Host "[1/2] Preflight" -ForegroundColor Cyan
$preflight = Invoke-RestMethod `
  -Method Get `
  -Uri "$SpringBaseUrl/api/laro/simulation-runs/$SimulationRunId/plan/preflight" `
  -Headers $headers `
  -TimeoutSec 120
$preflight | ConvertTo-Json -Depth 30 | Out-Host
if (-not $preflight.ready) { throw "LARO preflight is not ready." }

Write-Host "[2/2] Plan" -ForegroundColor Cyan
$body = Get-Content $RequestFile -Raw -Encoding UTF8
$response = Invoke-RestMethod `
  -Method Post `
  -Uri "$SpringBaseUrl/api/laro/simulation-runs/$SimulationRunId/plan" `
  -Headers $headers `
  -ContentType "application/json; charset=utf-8" `
  -Body $body `
  -TimeoutSec 1200
$response | ConvertTo-Json -Depth 100 | Out-Host

[PSCustomObject]@{
    status = $response.result.status
    request_id = $response.requestId
    plan_id = $response.result.plan.planId
    plan_version = $response.result.plan.planVersion
    makespan_ms = $response.result.plan.makespanMs
    robot_count = @($response.result.plan.robots).Count
    trace_url = $response.traceUrl
} | Format-List
