param([switch]$NoDialogs)
$ErrorActionPreference = 'Stop'
try {
    if (-not $NoDialogs) {
        Add-Type -AssemblyName System.Windows.Forms
        $message = "Остановить учебный SQL/API-стенд?`r`n`r`nСначала выберите Commit или Rollback в DBeaver и отключите оба подключения AML.`r`nНезавершённые изменения будут отменены. Соединения с этой БД/API прервутся. Сохранённые данные останутся."
        $answer = [Windows.Forms.MessageBox]::Show($message, 'AML SQL/API', 'OKCancel', 'Warning')
        if ($answer -ne [Windows.Forms.DialogResult]::OK) { return }
    }
    & (Join-Path $PSScriptRoot 'Stop-Lab.ps1')
    if (-not $NoDialogs) {
        [void][Windows.Forms.MessageBox]::Show('SQL/API-стенд остановлен. Сохранённые данные остались. DBeaver можно закрыть.', 'AML SQL/API', 'OK', 'Information')
    }
} catch {
    if (-not $NoDialogs) {
        Add-Type -AssemblyName System.Windows.Forms
        [void][Windows.Forms.MessageBox]::Show(('Не удалось остановить SQL/API-стенд: ' + $_.Exception.Message), 'AML SQL/API', 'OK', 'Error')
    }
    throw
}
