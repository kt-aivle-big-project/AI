param(
    [switch]$DeleteData
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($DeleteData) {
    docker compose --env-file .env.docker down -v --remove-orphans
} else {
    docker compose --env-file .env.docker down --remove-orphans
}
