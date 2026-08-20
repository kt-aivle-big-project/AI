param(
    [Parameter(Mandatory=$true)][long]$WarehouseId,
    [Parameter(Mandatory=$true)][long]$SimulationRunId,
    [string]$BaseUrl = "http://localhost:8000",
    [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Test-Path ".env.docker")) {
    throw ".env.docker is missing. Run start_local_stack.ps1 first."
}

Write-Host "Applying BE-centered planning profiles and projecting the map..." -ForegroundColor Cyan
docker compose --env-file .env.docker exec -T laro-api `
  python -m scripts.prepare_be_centered_data --warehouse-id $WarehouseId
if ($LASTEXITCODE -ne 0) { throw "BE-centered data preparation failed." }

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$encoded = [System.Uri]::EscapeDataString([string]$SimulationRunId)
$last = $null
while ((Get-Date) -lt $deadline) {
    try {
        $last = Invoke-RestMethod `
          -Uri "$BaseUrl/api/v1/simulation-runs/$encoded/missions/plan/preflight" `
          -TimeoutSec 20
        if ($last.ready) { break }
    } catch {
        $last = $_.Exception.Message
    }
    Start-Sleep -Seconds 2
}

if ($null -eq $last -or -not $last.ready) {
    $last | ConvertTo-Json -Depth 30 | Out-Host
    throw "BE-centered preflight is not ready. Confirm Spring simulation run and Redis robot runtime."
}

Write-Host "BE-centered planning contract is READY." -ForegroundColor Green
$last | ConvertTo-Json -Depth 30 | Out-Host
Write-Host "FastAPI: POST $BaseUrl/api/v1/simulation-runs/$SimulationRunId/missions/plan"
Write-Host "Spring : POST http://localhost:8080/api/laro/simulation-runs/$SimulationRunId/plan"
