param([switch]$ReadOnly, [string]$File)
$ErrorActionPreference='Stop'
$labRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
$settings=Get-Content -LiteralPath (Join-Path $labRoot 'var\postgres\lab-connection.json') -Raw | ConvertFrom-Json
$role=if ($ReadOnly) { $settings.roles.reader } else { $settings.roles.student }
$previousPassword=$env:PGPASSWORD
$previousOptions=$env:PGOPTIONS
$previousEncoding=$env:PGCLIENTENCODING
try {
    $env:PGPASSWORD=$role.password
    $env:PGOPTIONS='-c search_path=practice,reference,pg_catalog -c timezone=UTC'
    $env:PGCLIENTENCODING='UTF8'
    $arguments=@('-X','-h','127.0.0.1','-p','55432','-U',$role.user,'-d','aml_lab','-v','ON_ERROR_STOP=1')
    if ($File) {
        $sqlFile=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($File)
        if (-not (Test-Path -LiteralPath $sqlFile -PathType Leaf)) { throw 'SQL file does not exist' }
        $arguments+=@('-f',$sqlFile)
    }
    & (Join-Path $labRoot 'var\tools\postgresql-17.11\pgsql\bin\psql.exe') @arguments
    if ($LASTEXITCODE -ne 0) { throw 'SQL session returned an error' }
} finally {
    $env:PGPASSWORD=$previousPassword
    $env:PGOPTIONS=$previousOptions
    $env:PGCLIENTENCODING=$previousEncoding
}
