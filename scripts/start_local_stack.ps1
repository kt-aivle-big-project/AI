param(
    [switch]$ResetData,
    [switch]$StopLegacy,
    [int]$TimeoutSeconds = 240
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
    "laro-be-plan-bridge-v4-postgres", "laro-be-plan-bridge-v4-redis",
    "laro-be-plan-bridge-v4-neo4j", "laro-be-plan-bridge-v4-api",
    "laro-be-plan-bridge-v3-postgres", "laro-be-plan-bridge-v3-redis",
    "laro-be-plan-bridge-v3-neo4j", "laro-be-plan-bridge-v3-api",
    "laro-be-compat-v2-postgres", "laro-be-compat-v2-redis",
    "laro-be-compat-v2-neo4j", "laro-be-compat-v2-api",
    "laro-v13-21-postgres", "laro-v13-21-redis", "laro-v13-21-neo4j",
    "laro-v13-20-postgres", "laro-v13-20-redis", "laro-v13-20-neo4j",
    "laro-v13-16-postgres", "laro-v13-16-redis", "laro-v13-16-neo4j"
)
$RunningConflicts = @(docker ps --format "{{.Names}}" | Where-Object { $ConflictingContainers -contains $_ })
if ($RunningConflicts.Count -gt 0) {
    if (-not $StopLegacy) {
        throw "Containers already use the standard DB/API ports: $($RunningConflicts -join ', '). Re-run with -StopLegacy."
    }
    docker stop $RunningConflicts | Out-Host
}

if ($ResetData) {
    docker compose --env-file .env.docker down -v --remove-orphans
}

docker compose --env-file .env.docker up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed." }
docker compose --env-file .env.docker ps

$portLine = docker compose --env-file .env.docker port laro-api 8000
$apiPort = 8000
if ($portLine -match ':(\d+)\s*$') { $apiPort = [int]$Matches[1] }
$baseUrl = "http://localhost:$apiPort"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri "$baseUrl/health" -TimeoutSec 5
        if ($health.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $ready) {
    docker compose --env-file .env.docker logs --tail 150 laro-api
    throw "LARO API stack did not become ready within $TimeoutSeconds seconds."
}

Write-Host ""
Write-Host "LARO API is ready." -ForegroundColor Green
Write-Host "LARO API      : $baseUrl"
Write-Host "Swagger       : $baseUrl/docs"
Write-Host "PostgreSQL    : localhost:5432"
Write-Host "Redis         : localhost:6379"
Write-Host "Neo4j Browser : http://localhost:7474"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Start the existing Spring BE with the local profile so Hibernate creates public.* tables."
Write-Host "2. Run .\scripts\prepare_be_centered_stack.ps1 -WarehouseId <numeric BE warehouse id> -SimulationRunId <numeric run id>."
Write-Host "3. Call Spring POST /api/laro/simulation-runs/{simulationRunId}/plan or FastAPI POST /api/v1/simulation-runs/{id}/missions/plan."
Write-Host ""
Write-Host "Native mission planning is available under $baseUrl/api/v1/simulation-runs/{id}/missions/."
