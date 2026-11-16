param([switch]$NoDialogs, [switch]$NoOpen, [ValidateSet('reader','student')][string]$ConnectRole)
$ErrorActionPreference = 'Stop'
$labRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
try {
    & (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
    $desktopExe = Join-Path $labRoot 'var\tools\dbeaver-26.2.3\dbeaver\dbeaver.exe'
    $desktopWorkspace = Join-Path $labRoot 'var\dbeaver\workspace'
    $connections = Join-Path $desktopWorkspace 'General\.dbeaver\data-sources.json'
    if (-not (Test-Path -LiteralPath $desktopExe -PathType Leaf) -or -not (Test-Path -LiteralPath $connections -PathType Leaf)) {
        throw 'DBeaver is not configured. Run scripts/lab/Setup-DBeaver.ps1 first.'
    }
    & (Join-Path $PSScriptRoot 'Start-Lab.ps1')
    if (-not $NoOpen) {
        # Do not start a second copy while the first one is still initializing IPC.
        $existingDesktop = @(Get-Process dbeaver -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $desktopExe })
        if ($existingDesktop.Count -gt 0) {
            Write-Output 'DBeaver is already running. Use its existing window (Alt+Tab) to choose the connection.'
            return
        }
        # This is the interactive SQL editor requested by the operator.
        $driverConfiguration = Join-Path $labRoot 'var\dbeaver\drivers.xml'
        $desktopArgs = @('-nosplash', '-bringToFront', '-data', ('"' + $desktopWorkspace + '"'))
        if ($ConnectRole) {
            $desktopArgs += @('-con', ('"id=aml-' + $ConnectRole + '|create=false|openConsole=true"'))
        }
        $desktopArgs += @('--launcher.appendVmargs', '-vmargs', '-Duser.timezone=UTC', ('"-Ddbeaver.drivers.configuration-file=' + $driverConfiguration + '"'))
        Start-Process -FilePath $desktopExe -WindowStyle Normal -ArgumentList $desktopArgs -WorkingDirectory $labRoot | Out-Null
    }
    Write-Output 'SQL desktop ready. PostgreSQL: 127.0.0.1:55432/aml_lab; Swagger: http://127.0.0.1:8765/docs.'
} catch {
    if (-not $NoDialogs) {
        Add-Type -AssemblyName System.Windows.Forms
        $message = "SQL desktop could not start.`r`n`r`n" + $_.Exception.Message + "`r`n`r`nGuide: docs/operator/dbeaver-quickstart.md`r`nLogs: var/logs/"
        [void][Windows.Forms.MessageBox]::Show($message, 'AML SQL/API', 'OK', 'Error')
    }
    throw
}
