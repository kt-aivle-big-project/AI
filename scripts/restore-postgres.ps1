<#
.SYNOPSIS
Restores the shared LARO PostgreSQL database from a canonical dump.

.DESCRIPTION
This is a one-time/bootstrap operation for a developer machine. The script:

1. Starts only the PostgreSQL Compose service if necessary.
2. Stops the Compose-managed FastAPI service so it cannot use the database.
3. Recreates POSTGRES_DB and restores a pg_dump custom archive or plain SQL.
4. Checks that the core Spring BE tables exist and prints basic row counts.

The script does not synchronize Neo4j. After Spring BE is running, execute the
existing GraphSync endpoint from Spring Swagger.

The restore replaces the target database. -Force is deliberately required.
Stop the separately running Spring BE before using this script.

.PARAMETER DumpPath
Path to a full PostgreSQL database dump. The default location is
db/postgres/snapshots/laro-current.dump. Custom-format pg_dump archives and
plain .sql dumps are supported.

.PARAMETER Force
Confirms that the current POSTGRES_DB may be dropped and recreated.

.PARAMETER TimeoutSeconds
Maximum time to wait for the PostgreSQL container to become ready.

.EXAMPLE
.\scripts\restore-postgres.ps1 `
    -DumpPath .\db\postgres\snapshots\laro-current.dump `
    -Force

.NOTES
After a successful restore:

1. Run .\scripts\start_local_stack.ps1 -StopLegacy
2. Start Spring BE with SQL_INIT_MODE=never
3. In Spring Swagger, run POST /api/graph-sync/warehouses/1
4. In AI Swagger, verify that plan preflight reports READY

Do not use start_local_stack.ps1 -ResetData after restoring. -ResetData
deletes the named PostgreSQL volume and therefore deletes the restored data.
#>

[CmdletBinding()]
param(
    [string]$DumpPath,
    [switch]$Force,
    [ValidateRange(10, 900)]
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if ([string]::IsNullOrWhiteSpace($DumpPath)) {
    $DumpPath = Join-Path $Root "db\postgres\snapshots\laro-current.dump"
} elseif (-not [System.IO.Path]::IsPathRooted($DumpPath)) {
    $DumpPath = Join-Path $Root $DumpPath
}

if (-not (Test-Path -LiteralPath $DumpPath -PathType Leaf)) {
    throw @"
PostgreSQL dump was not found:
  $DumpPath

Provide a canonical full-database dump with -DumpPath. Example:
  .\scripts\restore-postgres.ps1 -DumpPath C:\backup\laro-current.dump -Force
"@
}

$DumpFile = Get-Item -LiteralPath $DumpPath
if ($DumpFile.Length -le 0) {
    throw "PostgreSQL dump is empty: $($DumpFile.FullName)"
}

if (-not $Force) {
    throw @"
Restore was not started because -Force was not supplied.

This operation drops and recreates POSTGRES_DB in the current AI Docker stack.
Back up data that must be retained, stop Spring BE, and re-run with -Force.
"@
}

if (-not (Test-Path -LiteralPath ".env.docker" -PathType Leaf)) {
    throw ".env.docker was not found in $Root. Create it from .env.docker.example first."
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    & docker compose --env-file .env.docker @Arguments
    $exitCode = $LASTEXITCODE
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "docker compose command failed with exit code ${exitCode}: $($Arguments -join ' ')"
    }
    return $exitCode
}

function Test-CustomPgDump {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $buffer = New-Object byte[] 5
        $read = $stream.Read($buffer, 0, $buffer.Length)
        if ($read -ne 5) {
            return $false
        }
        return [System.Text.Encoding]::ASCII.GetString($buffer) -eq "PGDMP"
    } finally {
        $stream.Dispose()
    }
}

Write-Host "Starting the PostgreSQL service..." -ForegroundColor Cyan
Invoke-Compose -Arguments @("up", "-d", "postgres") | Out-Null

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$postgresReady = $false
while ((Get-Date) -lt $deadline) {
    & docker compose --env-file .env.docker exec -T postgres `
        sh -lc 'pg_isready -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-warehouse}"' `
        *> $null
    if ($LASTEXITCODE -eq 0) {
        $postgresReady = $true
        break
    }
    Start-Sleep -Seconds 2
}

if (-not $postgresReady) {
    Invoke-Compose -Arguments @("logs", "--tail", "100", "postgres") -AllowFailure | Out-Null
    throw "PostgreSQL did not become ready within $TimeoutSeconds seconds."
}

$ContainerId = "$(& docker compose --env-file .env.docker ps -q postgres)".Trim()
if ([string]::IsNullOrWhiteSpace($ContainerId)) {
    throw "Could not resolve the PostgreSQL container ID."
}

$apiContainerId = "$(& docker compose --env-file .env.docker ps -q laro-api)".Trim()
if (-not [string]::IsNullOrWhiteSpace($apiContainerId)) {
    Write-Host "Stopping the Compose-managed LARO API during restore..." -ForegroundColor Yellow
    Invoke-Compose -Arguments @("stop", "laro-api") | Out-Null
}

Write-Warning "The external Spring BE must also be stopped before the database is replaced."

$containerDumpPath = "/tmp/laro-restore-$([guid]::NewGuid().ToString('N')).dump"
$containerTarget = "${ContainerId}:$containerDumpPath"
$isCustomDump = Test-CustomPgDump -Path $DumpFile.FullName
$DatabaseUser = "$(& docker compose --env-file .env.docker exec -T postgres printenv POSTGRES_USER)".Trim()
$DatabaseName = "$(& docker compose --env-file .env.docker exec -T postgres printenv POSTGRES_DB)".Trim()
if ([string]::IsNullOrWhiteSpace($DatabaseUser)) { $DatabaseUser = "postgres" }
if ([string]::IsNullOrWhiteSpace($DatabaseName)) { $DatabaseName = "warehouse" }

if ($DatabaseName -in @("postgres", "template0", "template1")) {
    throw "Refusing to replace protected database: $DatabaseName"
}

try {
    Write-Host "Copying the canonical dump into the PostgreSQL container..." -ForegroundColor Cyan
    & docker cp $DumpFile.FullName $containerTarget
    if ($LASTEXITCODE -ne 0) {
        throw "docker cp failed for $($DumpFile.FullName)."
    }

    Write-Host "Recreating POSTGRES_DB..." -ForegroundColor Yellow
    & docker compose --env-file .env.docker exec -T postgres `
        dropdb -U $DatabaseUser --if-exists --force $DatabaseName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to drop POSTGRES_DB. Confirm that Spring BE is stopped."
    }

    & docker compose --env-file .env.docker exec -T postgres `
        createdb -U $DatabaseUser $DatabaseName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create POSTGRES_DB after it was dropped."
    }

    if ($isCustomDump) {
        Write-Host "Restoring the pg_dump custom archive..." -ForegroundColor Cyan
        & docker compose --env-file .env.docker exec -T postgres `
            pg_restore `
            --exit-on-error `
            --no-owner `
            --no-privileges `
            -U $DatabaseUser `
            -d $DatabaseName `
            $containerDumpPath
    } else {
        Write-Host "Restoring the plain SQL dump..." -ForegroundColor Cyan
        & docker compose --env-file .env.docker exec -T postgres `
            psql `
            -v ON_ERROR_STOP=1 `
            -U $DatabaseUser `
            -d $DatabaseName `
            -f $containerDumpPath
    }

    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL restore failed. The recreated target database may be empty."
    }

    Write-Host "Checking the restored LARO schema..." -ForegroundColor Cyan
    $VerifySql = @"
SELECT CASE
         WHEN to_regclass('public.warehouse_layout') IS NOT NULL
          AND to_regclass('public.warehouse_node') IS NOT NULL
          AND to_regclass('public.warehouse_edge') IS NOT NULL
          AND to_regclass('public.product') IS NOT NULL
          AND to_regclass('public.warehouse_items') IS NOT NULL
         THEN 'CORE_SCHEMA_OK'
         ELSE 'CORE_SCHEMA_MISSING'
       END;
SELECT 'warehouses=' || count(*) FROM public.warehouse_layout;
SELECT 'warehouse_nodes=' || count(*) FROM public.warehouse_node;
SELECT 'warehouse_edges=' || count(*) FROM public.warehouse_edge;
SELECT 'products=' || count(*) FROM public.product;
SELECT 'warehouse_items=' || count(*) FROM public.warehouse_items;
"@

    $verification = & docker compose --env-file .env.docker exec -T postgres `
        psql `
        -v ON_ERROR_STOP=1 `
        -U $DatabaseUser `
        -d $DatabaseName `
        -At `
        -c $VerifySql
    if ($LASTEXITCODE -ne 0 -or $verification -notcontains "CORE_SCHEMA_OK") {
        throw "Restore completed, but the expected LARO core schema was not found."
    }

    $verification | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    Write-Host "PostgreSQL restore completed successfully." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "1. Run .\scripts\start_local_stack.ps1 -StopLegacy"
    Write-Host "2. Start Spring BE with SQL_INIT_MODE=never"
    Write-Host "3. In Spring Swagger, run POST /api/graph-sync/warehouses/1"
    Write-Host "4. In AI Swagger, verify plan preflight reports READY"
    Write-Host ""
    Write-Host "Do not use -ResetData unless you intentionally want to delete this restored database." -ForegroundColor Yellow
} finally {
    & docker exec $ContainerId rm -f $containerDumpPath *> $null
}
