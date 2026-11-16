$ErrorActionPreference='Stop'
$labRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
$labSavedEnvironment=[Environment]::GetEnvironmentVariables('Process')
$labSavedLocation=Get-Location
try {
& (Join-Path $labRoot 'scripts\rebuild\Enter-LocalEnvironment.ps1')
& (Join-Path $PSScriptRoot 'Start-LocalPostgres.ps1')
& (Join-Path $labRoot '.venv\Scripts\python.exe') -I -B (Join-Path $PSScriptRoot 'database.py') status
if ($LASTEXITCODE -ne 0) { throw 'Lab database is not ready; run Initialize-Lab.ps1' }
$statePath=Join-Path $labRoot 'var\lab\api-process.json'
function Test-OwnedApi {
    $handler=$null
    $client=$null
    $response=$null
    $script:labApiReadinessFailure=$null
    try {
        if (-not (Test-Path -LiteralPath $statePath)) { throw 'API state file is not available yet' }
        $state=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if ($state.root -ne $labRoot) { throw 'API ownership mismatch' }
        if ($state.status -ne 'running') { throw ('API state is '+$state.status) }
        $process=Get-Process -Id $state.pid -ErrorAction Stop
        if ($process.ProcessName -notlike 'python*') { throw 'API PID does not belong to Python' }
        # Invoke-RestMethod -NoProxy is unavailable in Windows PowerShell 5.1.
        # Keep proxy bypass local to this request; never change system settings.
        Add-Type -AssemblyName System.Net.Http
        $handler=[Net.Http.HttpClientHandler]::new()
        $handler.UseProxy=$false
        $handler.AllowAutoRedirect=$false
        $client=[Net.Http.HttpClient]::new($handler,$false)
        $client.Timeout=[TimeSpan]::FromSeconds(2)
        $response=$client.GetAsync('http://127.0.0.1:8765/health').GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200) { throw ('API health returned HTTP '+[int]$response.StatusCode) }
        $health=$response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
        if ($health.status -ne 'ok' -or $health.mode -ne 'local_learning' -or $health.check -ne 'http_liveness_only') {
            throw 'API health response does not match the local lab'
        }
        return $true
    } catch {
        $script:labApiReadinessFailure=$_.Exception.Message
        return $false
    } finally {
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
        if ($null -ne $handler) { $handler.Dispose() }
    }
}
if (-not (Test-OwnedApi)) {
    $busy=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($busy) { throw ('Port 8765 is occupied; no unrelated process will be stopped. Last readiness check: '+$script:labApiReadinessFailure) }
    $apiStartedProcess=Start-Process -FilePath (Join-Path $labRoot '.venv\Scripts\python.exe') -ArgumentList @('-I','-B',('"'+(Join-Path $PSScriptRoot 'run_api.py')+'"')) -WorkingDirectory $labRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $labRoot 'var\logs\lab-api.log') -RedirectStandardError (Join-Path $labRoot 'var\logs\lab-api.err.log') -PassThru
    $null=$apiStartedProcess.Handle
    $deadline=[DateTime]::UtcNow.AddSeconds(30)
    while (-not (Test-OwnedApi)) {
        $apiStartedProcess.Refresh()
        if ($apiStartedProcess.HasExited) { throw ('API process exited with code '+$apiStartedProcess.ExitCode+'; inspect var/logs/lab-api.err.log. Last readiness check: '+$script:labApiReadinessFailure) }
        if ([DateTime]::UtcNow -gt $deadline) { throw ('API readiness was not confirmed within 30 seconds; inspect var/logs/lab-api.err.log. Last readiness check: '+$script:labApiReadinessFailure) }
        Start-Sleep -Milliseconds 300
    }
}
Write-Output 'SQL: 127.0.0.1:55432 / aml_lab. Swagger: http://127.0.0.1:8765/docs. No models started.'
} finally {
    foreach ($key in @([Environment]::GetEnvironmentVariables('Process').Keys)) {
        if (-not $labSavedEnvironment.Contains($key)) { Remove-Item -LiteralPath ('Env:'+$key) -ErrorAction SilentlyContinue }
    }
    foreach ($entry in $labSavedEnvironment.GetEnumerator()) {
        # .NET Framework deletes empty values when written back; leave unchanged
        # variables alone so an inherited empty variable is preserved in PS5.1.
        if ([Environment]::GetEnvironmentVariable($entry.Key,'Process') -cne $entry.Value) {
            [Environment]::SetEnvironmentVariable($entry.Key,$entry.Value,'Process')
        }
    }
    Set-Location -LiteralPath $labSavedLocation.Path
}
