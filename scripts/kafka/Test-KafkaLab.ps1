#Requires -Version 7.0
$ErrorActionPreference='Stop'
$kafkaRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $kafkaRoot
Write-Output 'Requires running PostgreSQL, Swagger, Kafka and a worker in another terminal. Adds one retained synthetic result.'
& (Join-Path $kafkaRoot '.venv\Scripts\python.exe') -I -B (Join-Path $PSScriptRoot 'qualify_cycle.py')
if ($LASTEXITCODE -ne 0) { throw 'Kafka live cycle not confirmed. Inspect the result file printed above.' }
