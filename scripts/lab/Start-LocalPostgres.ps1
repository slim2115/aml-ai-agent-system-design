# Start only the owned cluster and wait for pg_ctl, never for its server children.
$ErrorActionPreference = 'Stop'
$labRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
$cluster = Join-Path $labRoot 'var\postgres\cluster'
$marker = Get-Content -LiteralPath (Join-Path $labRoot 'var\postgres\cluster-owner.json') -Raw | ConvertFrom-Json
if ($marker.root -ne $labRoot -or $marker.cluster -ne $cluster -or $marker.port -ne 55432) { throw 'Cluster ownership mismatch' }
$pgCtl = Join-Path $labRoot 'var\tools\postgresql-17.11\pgsql\bin\pg_ctl.exe'
& $pgCtl -D $cluster status
if ($LASTEXITCODE -eq 0) { return }
$stdout = Join-Path $labRoot 'var\logs\pgctl-start.log'
$stderr = Join-Path $labRoot 'var\logs\pgctl-start.err.log'
$serverLog = Join-Path $labRoot 'var\logs\postgres-lab.log'
$arguments = @('-D', ('"' + $cluster + '"'), '-l', ('"' + $serverLog + '"'), '-w', '-t', '30', 'start')
$process = Start-Process -FilePath $pgCtl -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
try {
    $null = $process.Handle
    if (-not $process.WaitForExit(35000)) { throw "PostgreSQL startup timed out. Inspect $stderr and $serverLog" }
    if ($process.ExitCode -ne 0) { throw "PostgreSQL startup failed ($($process.ExitCode)). Inspect $stderr and $serverLog" }
    Get-Content -LiteralPath $stdout
} finally {
    $process.Dispose()
}
