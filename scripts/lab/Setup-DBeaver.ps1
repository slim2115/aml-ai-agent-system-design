param([switch]$Download, [switch]$RepairUsers)
$ErrorActionPreference = 'Stop'
$labRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $labRoot
if (Get-Process dbeaver,dbeaverc -ErrorAction SilentlyContinue) { throw 'Close DBeaver before setup; existing workspaces will not be edited while it runs.' }
$utf8 = New-Object System.Text.UTF8Encoding($false)
$version = '26.2.3'
$archive = Join-Path $labRoot "var\downloads\dbeaver-ce-$version-windows-x86_64.zip"
$archiveHash = '0edfc68e1efe2cc364b48466d7d94a7e029bca067c4638282bbf104cab546547'
$install = Join-Path $labRoot "var\tools\dbeaver-$version"
$exe = Join-Path $install 'dbeaver\dbeaver.exe'
$desktopRoot = Join-Path $labRoot 'var\dbeaver'
$workspace = Join-Path $desktopRoot 'workspace'
$project = Join-Path $workspace 'General'
$driver = Join-Path $desktopRoot 'drivers\postgresql-42.7.13.jar'
$driverHash = '6e0e4cc2d8cae902084f8a2b18728b073a6fd9d1f87c9d8bff8f298c18185b93'

function Save-NewFile([string]$Path, [string]$Content) {
    if (-not (Test-Path -LiteralPath $Path)) {
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
        [IO.File]::WriteAllText($Path, $Content, $utf8)
    }
}
function Get-VerifiedFile([string]$Path, [string]$Url, [string]$Hash) {
    if (-not (Test-Path -LiteralPath $Path)) {
        if (-not $Download) { throw "Missing $Path. Run Setup-DBeaver.ps1 -Download with network access." }
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
        & curl.exe -fL --retry 2 --connect-timeout 15 --max-time 300 -o $Path $Url
        if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
    }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Hash) { throw "SHA256 mismatch: $Path. Existing file retained for inspection." }
}
Get-VerifiedFile $archive "https://github.com/dbeaver/dbeaver/releases/download/$version/dbeaver-ce-$version-windows-x86_64.zip" $archiveHash
Get-VerifiedFile $driver 'https://repo.maven.apache.org/maven2/org/postgresql/postgresql/42.7.13/postgresql-42.7.13.jar' $driverHash
if (-not (Test-Path -LiteralPath $exe)) {
    if ((Test-Path -LiteralPath $install) -and @(Get-ChildItem -LiteralPath $install -Force).Count -gt 0) { throw 'Incomplete installation exists; inspect it before retrying. No files overwritten.' }
    [void][IO.Directory]::CreateDirectory($install)
    & tar.exe -xf $archive -C $install
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $exe)) { throw 'DBeaver extraction failed' }
}
$markerPath = Join-Path $desktopRoot 'installation.json'
if (Test-Path -LiteralPath $markerPath) {
    $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($marker.root -ne $labRoot -or $marker.workspace -ne $workspace -or $marker.version -ne $version) { throw 'DBeaver workspace ownership mismatch' }
} elseif (Test-Path -LiteralPath $workspace) {
    throw 'Unowned workspace exists. Setup will not overwrite it.'
}
$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$credentialScript = Join-Path $PSScriptRoot 'Get-DBeaverPassword.ps1'
$connections = [ordered]@{}
$types = [ordered]@{}
foreach ($role in @('student','reader')) {
    $readOnly = $role -eq 'reader'
    $id = 'aml-' + $role
    # Build Cyrillic names without depending on PowerShell 5.1's script encoding.
    $label = if ($readOnly) { -join ([char[]]@(0x43F,0x440,0x43E,0x441,0x43C,0x43E,0x442,0x440)) } else { -join ([char[]]@(0x43F,0x440,0x430,0x43A,0x442,0x438,0x43A,0x430)) }
    $types[$id] = [ordered]@{ name=('AML - '+$label); color=$(if($readOnly){'220,240,255'}else{'255,240,190'}); 'auto-commit'=$readOnly; 'smart-commit'=$false }
    $connections[$id] = [ordered]@{
        provider='postgresql'; driver='aml-postgres-jdbc'; name=('AML '+[char]0x2014+' '+$label)
        'save-password'=$true; 'read-only'=$readOnly
        configuration=[ordered]@{
            host='127.0.0.1'; port='55432'; database='aml_lab'
            # Native/shell authentication reads configuration.user, not auth-properties.userName.
            user=('aml_'+$role)
            url='jdbc:postgresql://127.0.0.1:55432/aml_lab'; configurationType='MANUAL'; type=$id
            'auth-model'='shell_command'
            'auth-properties'=[ordered]@{
                passwordCommand=('"'+$powerShellExe+'" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$credentialScript+'" -Role '+$role)
                passwordCommandWorkingDir=$labRoot; passwordCommandTimeoutMs='10000'
            }
            properties=[ordered]@{connectTimeout='10'; socketTimeout='60'; ApplicationName=('AML DBeaver '+$role); options='-c timezone=UTC -c search_path=practice,reference,pg_catalog'}
            bootstrap=[ordered]@{autocommit=$readOnly; txnIsolation=2; defaultCatalog='aml_lab'; defaultSchema='practice'; ignoreErrors=$false}
        }
    }
}
Save-NewFile (Join-Path $project '.project') '<?xml version="1.0" encoding="UTF-8"?><projectDescription><name>General</name><comment>AML SQL practice</comment><projects/><buildSpec/><natures><nature>org.jkiss.dbeaver.DBeaverNature</nature></natures></projectDescription>'
Save-NewFile (Join-Path $project '.dbeaver\data-sources.json') ([ordered]@{folders=@{};connections=$connections;'connection-types'=$types} | ConvertTo-Json -Depth 12)
if ($RepairUsers) {
    $connectionFile = Join-Path $project '.dbeaver\data-sources.json'
    $original = [IO.File]::ReadAllText($connectionFile)
    $existing = $original | ConvertFrom-Json
    foreach ($role in @('student','reader')) {
        $id = 'aml-' + $role
        $profile = $existing.connections.$id
        $cfg = $profile.configuration
        if ($null -eq $profile -or $profile.driver -ne 'aml-postgres-jdbc' -or $cfg.host -ne '127.0.0.1' -or $cfg.port -ne '55432' -or $cfg.database -ne 'aml_lab' -or $cfg.url -ne 'jdbc:postgresql://127.0.0.1:55432/aml_lab' -or $cfg.'auth-model' -ne 'shell_command') {
            throw 'Unexpected AML connection profile. No connection file changed.'
        }
        $cfg | Add-Member -MemberType NoteProperty -Name user -Value ('aml_'+$role) -Force
        if ($null -ne $cfg.'auth-properties') { $cfg.'auth-properties'.PSObject.Properties.Remove('userName') }
    }
    $backup = Join-Path $desktopRoot ('backups\data-sources-before-user-fix-' + [Guid]::NewGuid().ToString('N') + '.json')
    Save-NewFile $backup $original
    [IO.File]::WriteAllText($connectionFile, ($existing | ConvertTo-Json -Depth 40), $utf8)
    Write-Output 'AML profile usernames repaired; unrelated connections and encrypted credential storage preserved.'
}
$prefs = "eclipse.preferences.version=1`nconnection.shellAuthModelEnabled=true`n"
Save-NewFile (Join-Path $workspace '.metadata\.plugins\org.eclipse.core.runtime\.settings\org.jkiss.dbeaver.core.prefs') $prefs
$jarXml = [Security.SecurityElement]::Escape($driver)
$driversXml = '<?xml version="1.0" encoding="UTF-8"?><drivers><provider id="postgresql"><driver id="aml-postgres-jdbc" name="AML PostgreSQL (local)" class="org.postgresql.Driver" url="jdbc:postgresql://{host}[:{port}]/[{database}]" port="55432" custom="true" categories="sql"><library type="jar" path="'+$jarXml+'" custom="true"/></driver></provider></drivers>'
Save-NewFile (Join-Path $desktopRoot 'drivers.xml') $driversXml
# Keep these settings in the portable installation too: reopening the executable
# must use the same driver and timezone as the desktop shortcut.
$iniPath = Join-Path $install 'dbeaver\dbeaver.ini'
$iniOriginal = [IO.File]::ReadAllText($iniPath)
$iniLines = @($iniOriginal -split '\r?\n')
if (@($iniLines | Where-Object { $_ -eq '-vmargs' }).Count -ne 1) {
    throw 'Unexpected dbeaver.ini: expected one -vmargs section'
}
$iniLines = @($iniLines | Where-Object {
    $_ -notmatch '^-Duser\.timezone=' -and
    $_ -notmatch '^-Ddbeaver\.drivers\.configuration-file='
})
$iniContent = ($iniLines -join "`r`n").TrimEnd("`r", "`n") +
    "`r`n-Duser.timezone=UTC`r`n-Ddbeaver.drivers.configuration-file=" +
    (Join-Path $desktopRoot 'drivers.xml') + "`r`n"
if ($iniContent -ne $iniOriginal) {
    Save-NewFile (Join-Path $desktopRoot ('backups\dbeaver-before-runtime-fix-' + [Guid]::NewGuid().ToString('N') + '.ini')) $iniOriginal
    [IO.File]::WriteAllText($iniPath, $iniContent, $utf8)
}
Save-NewFile $markerPath ([ordered]@{root=$labRoot;workspace=$workspace;version=$version;archive_sha256=$archiveHash;jdbc_version='42.7.13';jdbc_sha256=$driverHash;created_at_utc=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json)

$shortcutDir = Join-Path $labRoot 'lab\desktop'
[void][IO.Directory]::CreateDirectory($shortcutDir)
$wsh = New-Object -ComObject WScript.Shell
try {
    foreach ($action in @('Start','Stop')) {
        $shortcut = $wsh.CreateShortcut((Join-Path $shortcutDir ("AML SQL API - $action.lnk")))
        if ($shortcut.TargetPath -and $shortcut.TargetPath -ne $powerShellExe) { throw 'Existing unrelated shortcut will not be replaced' }
        $shortcut.TargetPath=$powerShellExe
        $shortcut.Arguments='-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot ($action+'-SqlDesktop.ps1'))+'"'
        $shortcut.WorkingDirectory=$labRoot
        $shortcut.IconLocation=$exe+',0'
        $shortcut.Description='AML local SQL/API lab - '+$action
        $shortcut.Save()
    }
} finally { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($wsh) }
Write-Output 'DBeaver 26.2.3 prepared. Open lab/desktop/AML SQL API - Start.lnk. Existing settings and SQL files preserved.'
