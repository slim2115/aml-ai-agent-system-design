param([Parameter(Mandatory=$true)][ValidateSet('student','reader')][string]$Role)
# Called by DBeaver's password provider. Never run in a transcript or log stdout.
$ErrorActionPreference = 'Stop'
try {
    $labRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    & (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
    $settings = Get-Content -LiteralPath (Join-Path $labRoot 'var\postgres\lab-connection.json') -Raw | ConvertFrom-Json
    if ($settings.host -ne '127.0.0.1' -or $settings.port -ne 55432 -or $settings.database -ne 'aml_lab') { throw 'Invalid configuration' }
    $account = $settings.roles.$Role
    if ($account.user -ne ('aml_' + $Role) -or [string]::IsNullOrWhiteSpace($account.password) -or $account.password -match '[\r\n]') { throw 'Invalid account' }
    [Console]::Out.WriteLine([string]$account.password)
} catch {
    [Console]::Error.WriteLine('Local lab credential could not be loaded. Check the project configuration.')
    exit 1
}
