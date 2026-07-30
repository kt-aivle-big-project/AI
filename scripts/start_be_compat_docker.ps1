param(
    [switch]$ResetData,
    [switch]$SkipNativePlanBootstrap,
    [switch]$StopLegacy,
    [int]$TimeoutSeconds = 240,
    [string]$WarehouseId = "WH-001",
    [string]$SimulationId = "SIM-V18-MIXED",
    [string]$NativeFixture = "scenarios/fixtures/V18_mixed_inbound_outbound"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Test-Path ".env.docker")) {
    Copy-Item ".env.docker.example" ".env.docker"
    Write-Host "Created .env.docker from .env.docker.example." -ForegroundColor Yellow
}

$ConflictingContainers = @(
    "laro-be-compat-v2-postgres",
    "laro-be-compat-v2-redis",
    "laro-be-compat-v2-neo4j",
    "laro-be-compat-v2-api",
    "laro-v13-21-postgres",
    "laro-v13-21-redis",
    "laro-v13-21-neo4j",
    "laro-v13-20-postgres",
    "laro-v13-20-redis",
    "laro-v13-20-neo4j",
    "laro-v13-16-postgres",
    "laro-v13-16-redis",
    "laro-v13-16-neo4j"
)
$RunningConflicts = @(
    docker ps --format "{{.Names}}" |
      Where-Object { $ConflictingContainers -contains $_ }
)
if ($RunningConflicts.Count -gt 0) {
    if (-not $StopLegacy) {
        throw (
            "Containers already use the standard DB/API ports: " +
            ($RunningConflicts -join ", ") +
            ". Re-run with -StopLegacy or stop them manually."
        )
    }
    Write-Host "Stopping conflicting containers: $($RunningConflicts -join ', ')" -ForegroundColor Yellow
    docker stop $RunningConflicts | Out-Host
}

if ($ResetData) {
    docker compose --env-file .env.docker down -v --remove-orphans
}

docker compose --env-file .env.docker up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed."
}
docker compose --env-file .env.docker ps

$portLine = docker compose --env-file .env.docker port laro-api 8000
$apiPort = 8000
if ($portLine -match ':(\d+)\s*$') {
    $apiPort = [int]$Matches[1]
}
$baseUrl = "http://localhost:$apiPort"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri "$baseUrl/health" -TimeoutSec 5
        $contract = Invoke-RestMethod -Uri "$baseUrl/compat/v2/contract" -TimeoutSec 5
        if ($health.status -eq "ok" -and $contract.ready) {
            $ready = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}

if (-not $ready) {
    docker compose --env-file .env.docker logs --tail 150 laro-api
    throw "LARO API/DB contract did not become ready within $TimeoutSeconds seconds."
}

if (-not $SkipNativePlanBootstrap) {
    Write-Host "Seeding the native plan demo into PostgreSQL, Redis, and Neo4j..." -ForegroundColor Cyan
    docker compose --env-file .env.docker exec -T laro-api `
      python -m scripts.bootstrap_native_plan_demo `
      --warehouse-id $WarehouseId `
      --data-dir $NativeFixture
    if ($LASTEXITCODE -ne 0) {
        throw "Native plan demo bootstrap failed."
    }

    $encodedSimulation = [System.Uri]::EscapeDataString($SimulationId)
    $encodedWarehouse = [System.Uri]::EscapeDataString($WarehouseId)
    $preflight = Invoke-RestMethod `
      -Uri "$baseUrl/api/v1/warehouses/$encodedWarehouse/missions/plan/preflight?simulation_id=$encodedSimulation" `
      -TimeoutSec 30
    if (-not $preflight.ready) {
        $preflight | ConvertTo-Json -Depth 20 | Out-Host
        throw "Native plan preflight is not ready."
    }
}

Write-Host ""
Write-Host "LARO plan bridge is ready." -ForegroundColor Green
Write-Host "LARO API           : $baseUrl"
Write-Host "Swagger            : $baseUrl/docs"
Write-Host "PostgreSQL         : localhost:5432"
Write-Host "Redis              : localhost:6379"
Write-Host "Neo4j Browser      : http://localhost:7474"
Write-Host "BE compatibility   : POST $baseUrl/optimize (unchanged)"
Write-Host "Native plan API    : POST $baseUrl/api/v1/warehouses/$WarehouseId/missions/plan"
Write-Host "Plan preflight     : GET  $baseUrl/api/v1/warehouses/$WarehouseId/missions/plan/preflight?simulation_id=$SimulationId"
Write-Host ""
Write-Host "Run the native plan check:" -ForegroundColor Cyan
Write-Host ".\scripts\run_native_plan_api_check.ps1 -Backend ortools -Repeat 1"
Write-Host ""
Write-Host "BE-main source remains unchanged. Start it separately only when testing the old /optimize path."
