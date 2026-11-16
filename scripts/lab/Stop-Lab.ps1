$ErrorActionPreference='Stop'
$labRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
$statePath=Join-Path $labRoot 'var\lab\api-process.json'
if (Test-Path -LiteralPath $statePath) {
    $state=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.root -ne $labRoot) { throw 'API ownership mismatch' }
    if ($state.status -in @('starting','running')) {
        [ordered]@{root=$labRoot;instance_id=$state.instance_id} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $labRoot 'var\lab\api-stop.json') -Encoding utf8
        $deadline=[DateTime]::UtcNow.AddSeconds(40)
        do {
            Start-Sleep -Milliseconds 300
            $current=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            $live=Get-Process -Id $state.pid -ErrorAction SilentlyContinue
            if ($current.instance_id -ne $state.instance_id) { throw 'API instance changed; stop aborted' }
            if ([DateTime]::UtcNow -gt $deadline) { throw 'API graceful stop timed out; PostgreSQL left running' }
        } while ($current.status -ne 'stopped' -and $live)
    }
}
$cluster=Join-Path $labRoot 'var\postgres\cluster'
$marker=Get-Content -LiteralPath (Join-Path $labRoot 'var\postgres\cluster-owner.json') -Raw | ConvertFrom-Json
if ($marker.root -ne $labRoot -or $marker.cluster -ne $cluster -or $marker.port -ne 55432) { throw 'Cluster ownership mismatch' }
$pgCtl=Join-Path $labRoot 'var\tools\postgresql-17.11\pgsql\bin\pg_ctl.exe'
& $pgCtl -D $cluster status
if ($LASTEXITCODE -eq 0) {
    & $pgCtl -D $cluster -m fast -w -t 30 stop
    if ($LASTEXITCODE -ne 0) { throw 'Lab PostgreSQL stop failed' }
}
Write-Output 'Local lab stopped. Data retained.'
