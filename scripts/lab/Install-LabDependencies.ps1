param([switch]$Offline)
$ErrorActionPreference='Stop'
$labRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
& (Join-Path $labRoot 'scripts\rebuild\Enter-LocalEnvironment.ps1')
$labEvidence=Join-Path $labRoot 'var\checks\lab'
$labDownloads=Join-Path $labRoot 'var\lab-downloads'
New-Item -ItemType Directory -Force -Path $labEvidence,$labDownloads,(Join-Path $labRoot 'lab\api\static') | Out-Null
$downloadRecords=[Collections.Generic.List[object]]::new()
$lockPath=Join-Path $labRoot 'requirements\lab-extra-windows-py312.lock'
$labPython=Join-Path $labRoot '.venv\Scripts\python.exe'
$wheelArgs=@('--only-binary=:all:','--no-deps','--require-hashes','--find-links',$labDownloads,'-r',$lockPath)
if ($Offline) { $wheelArgs+=@('--no-index') }
& $labPython -I -B -m pip install --disable-pip-version-check @wheelArgs
if ($LASTEXITCODE -ne 0) { throw 'Lab Python installation failed' }
& $labPython -I -B -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed' }
# The published EDB URL was resolved from its official Windows 17.11 link.
$pgUrl='https://get.enterprisedb.com/postgresql/postgresql-17.11-4-windows-x64-binaries.zip'
$pgZip=Join-Path $labDownloads 'postgresql-17.11-4-windows-x64-binaries.zip'
if (-not (Test-Path -LiteralPath $pgZip)) {
    if ($Offline) { throw 'Offline mode requires the pinned PostgreSQL ZIP in var/lab-downloads' }
    Invoke-WebRequest -Uri $pgUrl -OutFile ($pgZip+'.partial')
    if ((Get-Item -LiteralPath ($pgZip+'.partial')).Length -ne 379726839) { throw 'Unexpected PostgreSQL archive length' }
    Move-Item -LiteralPath ($pgZip+'.partial') -Destination $pgZip
}
$pgHash=(Get-FileHash -LiteralPath $pgZip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($pgHash -ne 'b9424ee7bc60b52450ff910a3630225df32e633f3cb29c1d126d9299d59aea28') { throw 'PostgreSQL archive hash differs from the tested release' }
$downloadRecords.Add([pscustomobject]@{name='postgresql';version='17.11-4';file=[IO.Path]::GetFileName($pgZip);source=$pgUrl;sha256=$pgHash;hash_source='Locally computed downloaded archive hash; no vendor checksum claim'})
$pgTarget=Join-Path $labRoot 'var\tools\postgresql-17.11'
if (-not (Test-Path -LiteralPath (Join-Path $pgTarget 'pgsql\bin\postgres.exe'))) {
    New-Item -ItemType Directory -Force -Path $pgTarget | Out-Null
    Expand-Archive -LiteralPath $pgZip -DestinationPath $pgTarget
}
& (Join-Path $pgTarget 'pgsql\bin\postgres.exe') --version
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL executable verification failed' }
# Swagger assets are staged by the finite Python helper with publisher integrity.
foreach ($asset in @('swagger-ui-bundle.js','swagger-ui.css','favicon.svg','LICENSE')) {
    if (-not (Test-Path -LiteralPath (Join-Path $labRoot ('lab\api\static\'+$asset)) -PathType Leaf)) { throw "Missing published Swagger asset: $asset" }
}
[ordered]@{checked_at_utc=[DateTimeOffset]::UtcNow.ToString('o');status='PASS';scope='Additional Python wheels, standalone PostgreSQL binaries and local Swagger assets; no cluster or server started by this script';downloads=$downloadRecords} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $labEvidence 'dependencies.json') -Encoding utf8
