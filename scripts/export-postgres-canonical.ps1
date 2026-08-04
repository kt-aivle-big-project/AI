<#
.SYNOPSIS
Exports a shareable canonical PostgreSQL dump for the LARO local stack.

.DESCRIPTION
Creates a PostgreSQL custom archive containing the complete database schema and
the current warehouse, map, product, inventory, facility, and robot master data.

Runtime-only data is intentionally omitted: previous simulation runs and tasks,
optimization results, AI request logs, generated plans, and inventory
reservations. The tables and sequences themselves remain in the archive.

The two local demo users are retained because warehouse_layout.user_id refers to
them and a restored developer stack must still be able to log in. Do not use this
script against a production database containing real user accounts.

.PARAMETER OutputPath
Destination of the custom pg_dump archive. The default is
db/postgres/snapshots/laro-current.dump.

.PARAMETER Force
Allows an existing output file to be replaced. The source database is never
modified.

.EXAMPLE
.\scripts\export-postgres-canonical.ps1 `
    -OutputPath .\laro-current.dump `
    -Force

.NOTES
The resulting file can be restored with:

.\scripts\restore-postgres.ps1 -DumpPath .\laro-current.dump -Force
#>

[CmdletBinding()]
param(
    [string]$OutputPath,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $Root "db\postgres\snapshots\laro-current.dump"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $Root $OutputPath
}

$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$OutputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}

if ((Test-Path -LiteralPath $OutputPath -PathType Leaf) -and -not $Force) {
    throw "Output dump already exists. Re-run with -Force to replace it: $OutputPath"
}

if (-not (Test-Path -LiteralPath ".env.docker" -PathType Leaf)) {
    throw ".env.docker was not found in $Root."
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & docker compose --env-file .env.docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose command failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
    }
}

Write-Host "Starting the PostgreSQL service if necessary..." -ForegroundColor Cyan
Invoke-Compose -Arguments @("up", "-d", "postgres")

$ContainerId = (& docker compose --env-file .env.docker ps -q postgres).Trim()
if ([string]::IsNullOrWhiteSpace($ContainerId)) {
    throw "Could not resolve the PostgreSQL container ID."
}

$DatabaseUser = (& docker compose --env-file .env.docker exec -T postgres printenv POSTGRES_USER).Trim()
$DatabaseName = (& docker compose --env-file .env.docker exec -T postgres printenv POSTGRES_DB).Trim()
if ([string]::IsNullOrWhiteSpace($DatabaseUser)) { $DatabaseUser = "postgres" }
if ([string]::IsNullOrWhiteSpace($DatabaseName)) { $DatabaseName = "warehouse" }

$RuntimeTables = @(
    "laro_contract.request_log",
    "laro_ext.inventory_reservation",
    "laro_ext.request_log",
    "laro_ext.simulation_plan",
    "public.event",
    "public.optimization_results",
    "public.robot_route_results",
    "public.simulation",
    "public.simulation_run_robots",
    "public.simulation_runs",
    "public.task",
    "public.task_assignment_results",
    "public.user_consents"
)

$ContainerDumpPath = "/tmp/laro-canonical-$([guid]::NewGuid().ToString('N')).dump"

try {
    $PgDumpArguments = @(
        "compose", "--env-file", ".env.docker", "exec", "-T", "postgres",
        "pg_dump",
        "-U", $DatabaseUser,
        "-d", $DatabaseName,
        "--format=custom",
        "--compress=6",
        "--no-owner",
        "--no-privileges",
        "--file=$ContainerDumpPath"
    )

    foreach ($Table in $RuntimeTables) {
        $PgDumpArguments += "--exclude-table-data=$Table"
    }

    Write-Host "Exporting canonical PostgreSQL data..." -ForegroundColor Cyan
    & docker @PgDumpArguments
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump failed with exit code $LASTEXITCODE."
    }

    $ContainerTarget = "${ContainerId}:$ContainerDumpPath"
    & docker cp $ContainerTarget $OutputPath
    if ($LASTEXITCODE -ne 0) {
        throw "docker cp failed while writing $OutputPath."
    }

    $DumpFile = Get-Item -LiteralPath $OutputPath
    if ($DumpFile.Length -le 5) {
        throw "The generated dump is unexpectedly empty: $OutputPath"
    }

    $Stream = [System.IO.File]::OpenRead($OutputPath)
    try {
        $Magic = New-Object byte[] 5
        $Read = $Stream.Read($Magic, 0, $Magic.Length)
        if ($Read -ne 5 -or [System.Text.Encoding]::ASCII.GetString($Magic) -ne "PGDMP") {
            throw "The generated file is not a PostgreSQL custom archive: $OutputPath"
        }
    } finally {
        $Stream.Dispose()
    }

    Write-Host ""
    Write-Host "Canonical PostgreSQL dump created." -ForegroundColor Green
    Write-Host "  path: $OutputPath"
    Write-Host "  size: $([math]::Round($DumpFile.Length / 1KB, 1)) KiB"
    Write-Host "  included: schema, warehouse/map, products, inventory, facilities, robot masters"
    Write-Host "  excluded data: simulation history, tasks, reservations, generated plans, request logs"
    Write-Host ""
    Write-Host "Restore with:" -ForegroundColor Cyan
    Write-Host ".\scripts\restore-postgres.ps1 -DumpPath `"$OutputPath`" -Force"
} finally {
    & docker exec $ContainerId rm -f $ContainerDumpPath *> $null
}
