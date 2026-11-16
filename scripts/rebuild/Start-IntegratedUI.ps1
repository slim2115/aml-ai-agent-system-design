#requires -Version 7.0
# The API owns workflow execution; this UI shares its saved reports and reviews.
$ErrorActionPreference = 'Stop'
$integratedRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$integratedSavedEnvironment = [Environment]::GetEnvironmentVariables('Process')
$integratedSavedLocation = Get-Location
try {
    & (Join-Path $integratedRoot 'scripts\rebuild\Enter-LocalEnvironment.ps1')
    $env:AML_WORKFLOW_BACKEND = 'api'
    Write-Output 'Integrated operator UI: http://127.0.0.1:8501. Start scripts/lab/Start-Lab.ps1 separately; search preparation remains explicit.'
    & (Join-Path $integratedRoot '.venv\Scripts\python.exe') -I -B -m streamlit run (Join-Path $integratedRoot 'src\ui\app.py') --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
    if ($LASTEXITCODE -ne 0) { throw 'Integrated UI stopped with an error; inspect the terminal output.' }
}
finally {
    $integratedCurrentEnvironment = [Environment]::GetEnvironmentVariables('Process')
    foreach ($integratedName in $integratedCurrentEnvironment.Keys) {
        if (-not $integratedSavedEnvironment.Contains($integratedName)) {
            [Environment]::SetEnvironmentVariable($integratedName, $null, 'Process')
        }
    }
    foreach ($integratedName in $integratedSavedEnvironment.Keys) {
        if ($integratedCurrentEnvironment[$integratedName] -ne $integratedSavedEnvironment[$integratedName]) {
            [Environment]::SetEnvironmentVariable($integratedName, $integratedSavedEnvironment[$integratedName], 'Process')
        }
    }
    Set-Location -LiteralPath $integratedSavedLocation
}
