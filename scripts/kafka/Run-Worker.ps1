#Requires -Version 7.0
$ErrorActionPreference='Stop'
$kafkaRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $kafkaRoot
Write-Output 'Kafka worker in this terminal. Ctrl-C stops it. Start PostgreSQL and the Kafka broker first.'
& (Join-Path $kafkaRoot '.venv\Scripts\python.exe') -I -B (Join-Path $PSScriptRoot 'lab.py') worker
if ($LASTEXITCODE -ne 0) { throw 'Worker stopped with an error. Restore the dependency, then run this command again.' }
