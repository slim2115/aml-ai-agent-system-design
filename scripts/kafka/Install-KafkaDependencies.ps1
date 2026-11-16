#Requires -Version 7.0
param([switch]$UsePackageIndex)
$ErrorActionPreference='Stop'
$kafkaRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $kafkaRoot
$kafkaPython=Join-Path $kafkaRoot '.venv\Scripts\python.exe'
$kafkaLock=Join-Path $kafkaRoot 'requirements\kafka-extra-windows-py312.lock'
$kafkaWheelhouse=Join-Path $kafkaRoot 'var\wheelhouse\kafka-windows-py312'
$kafkaArgs=@('-I','-B','-m','pip','install','--require-hashes','--only-binary=:all:','--no-deps','-r',$kafkaLock)
if (-not $UsePackageIndex) { $kafkaArgs+=@('--no-index','--find-links',$kafkaWheelhouse) }
& $kafkaPython @kafkaArgs
if ($LASTEXITCODE -ne 0) { throw 'Kafka client install failed. For an empty wheel cache use -UsePackageIndex.' }
& $kafkaPython -I -B -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
Write-Output 'Kafka client installed. No broker, database or worker was started.'
