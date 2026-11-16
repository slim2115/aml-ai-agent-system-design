#Requires -Version 7.0
$ErrorActionPreference='Stop'
$integrationRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $integrationRoot
Write-Output 'AML Kafka worker in this terminal. Ctrl-C stops intake/projection; accepted API jobs continue.'
Write-Output 'Prepare API search in the integrated UI. Pending requests remain in PostgreSQL while search is unavailable.'
& (Join-Path $integrationRoot '.venv\Scripts\python.exe') -I -B (Join-Path $PSScriptRoot 'lab.py') aml-worker
if ($LASTEXITCODE -ne 0) { throw 'AML worker stopped with an error. Restore dependencies and run it again; queued requests are retained.' }
