param([switch]$RequireBoth)
# Read-only server evidence: this checks real DBeaver sessions, not a JDBC double.
$ErrorActionPreference='Stop'
$labRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
$settings=Get-Content -LiteralPath (Join-Path $labRoot 'var\postgres\lab-connection.json') -Raw | ConvertFrom-Json
if($settings.host -ne '127.0.0.1' -or $settings.port -ne 55432 -or $settings.database -ne 'aml_lab' -or $settings.roles.reader.user -ne 'aml_reader') { throw 'Unexpected lab configuration' }
$previousPassword=$env:PGPASSWORD
$previousEncoding=$env:PGCLIENTENCODING
$previousOptions=$env:PGOPTIONS
try {
    $env:PGPASSWORD=$settings.roles.reader.password
    $env:PGCLIENTENCODING='UTF8'
    $env:PGOPTIONS='-c statement_timeout=5000'
    $query=@'
SELECT coalesce(json_agg(s ORDER BY application_name, usename), '[]'::json)
FROM (
 SELECT application_name, usename, datname, count(*) AS connection_count
 FROM pg_stat_activity
 WHERE datname='aml_lab'
   AND (application_name LIKE 'DBeaver %'
        OR application_name IN ('AML DBeaver student','AML DBeaver reader'))
 GROUP BY application_name, usename, datname
) s;
'@
    $raw=& (Join-Path $labRoot 'var\tools\postgresql-17.11\pgsql\bin\psql.exe') -X -h 127.0.0.1 -p 55432 -U aml_reader -d aml_lab -v ON_ERROR_STOP=1 -t -A -c $query
    if($LASTEXITCODE -ne 0) { throw 'Could not read database sessions' }
    $parsed=ConvertFrom-Json -InputObject ([string]$raw)
    $sessions=@()
    foreach($row in $parsed) { $sessions += $row }
    $passed=@()
    $unexpected=@($sessions | Where-Object {
        $_.usename -notin @('aml_reader','aml_student') -or
        ($_.application_name -eq 'AML DBeaver reader' -and $_.usename -ne 'aml_reader') -or
        ($_.application_name -eq 'AML DBeaver student' -and $_.usename -ne 'aml_student')
    })
    foreach($role in @('reader','student')) {
        # DBeaver replaces JDBC ApplicationName with its own name per context.
        $matching=@($sessions | Where-Object {$_.usename -eq ('aml_'+$role)})
        if($matching.Count -gt 0) { $passed+=$role }
    }
    $result=[ordered]@{
        observed_at_utc=[DateTimeOffset]::UtcNow.ToString('o')
        status=$(if($unexpected.Count){'FAIL'}elseif($passed.Count -eq 2){'PASS'}else{'INCOMPLETE'})
        source='PostgreSQL pg_stat_activity; real DBeaver application names and authenticated roles'
        confirmed_roles=$passed
        unexpected_sessions=$unexpected
        sessions=$sessions
    }
    $result | ConvertTo-Json -Depth 6
    if($RequireBoth -and ($passed.Count -ne 2 -or $unexpected.Count)) { throw 'Expected DBeaver role check failed' }
} finally {
    $env:PGPASSWORD=$previousPassword
    $env:PGCLIENTENCODING=$previousEncoding
    $env:PGOPTIONS=$previousOptions
}
