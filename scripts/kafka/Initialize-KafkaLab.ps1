#Requires -Version 7.0
$ErrorActionPreference='Stop'
$kafkaRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $kafkaRoot
& (Join-Path $kafkaRoot '.venv\Scripts\python.exe') -I -B (Join-Path $PSScriptRoot 'lab.py') initialize
if ($LASTEXITCODE -ne 0) { throw 'Kafka SQL initialization failed. Start the existing SQL lab first.' }
