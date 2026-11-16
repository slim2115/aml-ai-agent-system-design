$ErrorActionPreference='Stop'
$labRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
$labSavedEnvironment=[Environment]::GetEnvironmentVariables('Process')
$labSavedLocation=Get-Location
try {
& (Join-Path $labRoot 'scripts\rebuild\Enter-LocalEnvironment.ps1')
$out=Join-Path $labRoot 'var\checks\lab'
New-Item -ItemType Directory -Force -Path $out | Out-Null
$python=Join-Path $labRoot '.venv\Scripts\python.exe'
& $python -I -B -m pytest -q -p no:cacheprovider tests/lab --junitxml=var/checks/lab/api-unit.xml
if ($LASTEXITCODE -ne 0) { throw 'Lab unit tests failed' }
& $python -I -B (Join-Path $PSScriptRoot 'qualify_database.py')
if ($LASTEXITCODE -ne 0) { throw 'SQL qualification failed; check var/checks/lab/sql-qualification.json' }
& $python -I -B (Join-Path $PSScriptRoot 'qualify_api.py')
if ($LASTEXITCODE -ne 0) { throw 'HTTP qualification failed; check var/checks/lab/api-qualification.json' }
Write-Output 'Lab checks passed. Evidence: var/checks/lab. No practice reset performed.'
} finally {
    foreach ($key in @([Environment]::GetEnvironmentVariables('Process').Keys)) {
        if (-not $labSavedEnvironment.Contains($key)) { Remove-Item -LiteralPath ('Env:'+$key) -ErrorAction SilentlyContinue }
    }
    foreach ($entry in $labSavedEnvironment.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key,$entry.Value,'Process') }
    Set-Location -LiteralPath $labSavedLocation.Path
}
