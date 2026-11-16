$ErrorActionPreference='Stop'
$labRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
& (Join-Path $labRoot 'scripts\rebuild\Enter-LocalEnvironment.ps1')
$pgBin=Join-Path $labRoot 'var\tools\postgresql-17.11\pgsql\bin'
$pgCluster=Join-Path $labRoot 'var\postgres\cluster'
$pgConfig=Join-Path $labRoot 'var\postgres\lab-connection.json'
$pgMarker=Join-Path $labRoot 'var\postgres\cluster-owner.json'
if (-not (Test-Path -LiteralPath (Join-Path $pgBin 'initdb.exe'))) { throw 'Run Install-LabDependencies.ps1 first' }
New-Item -ItemType Directory -Force -Path (Join-Path $labRoot 'var\postgres'),(Join-Path $labRoot 'var\logs'),(Join-Path $labRoot 'var\lab') | Out-Null
function New-LabSecret { [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant() }
if (-not (Test-Path -LiteralPath $pgConfig)) {
    $labRoles=[ordered]@{}
    foreach ($role in @('admin','reader','student','api')) {
        $userName=@{admin='aml_lab_admin';reader='aml_reader';student='aml_student';api='aml_api'}[$role]
        $labRoles[$role]=[ordered]@{user=$userName;password=(New-LabSecret)}
    }
    [ordered]@{host='127.0.0.1';port=55432;database='aml_lab';api_port=8765;roles=$labRoles;api_token=(New-LabSecret)} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $pgConfig -Encoding utf8
}
$settings=Get-Content -LiteralPath $pgConfig -Raw | ConvertFrom-Json
if ($settings.host -ne '127.0.0.1' -or $settings.port -ne 55432 -or $settings.database -ne 'aml_lab') { throw 'Unexpected lab configuration' }
if (Test-Path -LiteralPath (Join-Path $pgCluster 'PG_VERSION')) {
    if (-not (Test-Path -LiteralPath $pgMarker)) { throw 'Unmarked existing PostgreSQL cluster; refusing changes' }
    $owner=Get-Content -LiteralPath $pgMarker -Raw | ConvertFrom-Json
    if ($owner.cluster -ne $pgCluster -or $owner.root -ne $labRoot) { throw 'Cluster ownership marker mismatch' }
} else {
    if ((Test-Path -LiteralPath $pgCluster) -and @(Get-ChildItem -LiteralPath $pgCluster -Force).Count -gt 0) { throw 'Nonempty unfinished cluster; inspect before retry' }
    $passwordFile=Join-Path $labRoot ('var\tmp\initdb-'+[Guid]::NewGuid().ToString('N')+'.txt')
    [IO.File]::WriteAllText($passwordFile,$settings.roles.admin.password,[Text.UTF8Encoding]::new($false))
    try {
        & (Join-Path $pgBin 'initdb.exe') -D $pgCluster -U aml_lab_admin --pwfile $passwordFile --auth=scram-sha-256 --encoding=UTF8 --locale=C
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL initialization failed' }
    } finally {
        if (Test-Path -LiteralPath $passwordFile) { Remove-Item -LiteralPath $passwordFile }
    }
    @'

# Independent local teaching cluster; no system-wide service.
listen_addresses = '127.0.0.1'
port = 55432
max_connections = 20
shared_buffers = '64MB'
work_mem = '4MB'
maintenance_work_mem = '32MB'
timezone = 'UTC'
log_min_messages = warning
log_min_error_statement = panic
'@ | Add-Content -LiteralPath (Join-Path $pgCluster 'postgresql.conf') -Encoding utf8
    @'
# Only the separate loopback teaching identities may connect.
host all aml_lab_admin 127.0.0.1/32 scram-sha-256
host aml_lab aml_reader,aml_student,aml_api 127.0.0.1/32 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::0/0 reject
'@ | Set-Content -LiteralPath (Join-Path $pgCluster 'pg_hba.conf') -Encoding utf8
    [ordered]@{root=$labRoot;cluster=$pgCluster;port=55432;version='17.11';created_at_utc=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pgMarker -Encoding utf8
}
& (Join-Path $PSScriptRoot 'Start-LocalPostgres.ps1')
& (Join-Path $labRoot '.venv\Scripts\python.exe') -I -B (Join-Path $PSScriptRoot 'database.py') bootstrap
if ($LASTEXITCODE -ne 0) { throw 'Lab schema bootstrap failed' }
Write-Output 'Local PostgreSQL lab initialized at 127.0.0.1:55432. Credentials remain in var/postgres/lab-connection.json.'
