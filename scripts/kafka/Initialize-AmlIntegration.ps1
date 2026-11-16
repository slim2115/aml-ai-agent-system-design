#Requires -Version 7.0
$ErrorActionPreference='Stop'
$integrationRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $integrationRoot
& (Join-Path $integrationRoot '.venv\Scripts\python.exe') -I -B (Join-Path $PSScriptRoot 'lab.py') initialize-aml
if ($LASTEXITCODE -ne 0) { throw 'AML queue initialization failed. Start the SQL/API lab first.' }
