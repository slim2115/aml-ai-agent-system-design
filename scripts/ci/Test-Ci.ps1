param(
    [string]$PythonPath,
    [string]$Wheelhouse,
    [switch]$UsePackageIndex
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not $PythonPath) { $PythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Provide -PythonPath to an existing CPython 3.12 executable.' }
if ($Wheelhouse -and $UsePackageIndex) { throw 'Choose a wheelhouse or -UsePackageIndex, not both.' }
if (-not $Wheelhouse -and -not $UsePackageIndex) { $Wheelhouse = Join-Path $projectRoot 'var\wheelhouse\ci-windows-py312' }
if ($Wheelhouse) {
    $Wheelhouse = [IO.Path]::GetFullPath($Wheelhouse)
    if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container)) { throw 'CI wheelhouse missing. Supply -Wheelhouse or explicitly use -UsePackageIndex.' }
}
& (Join-Path $projectRoot 'scripts\rebuild\Enter-LocalEnvironment.ps1')
$env:PYTHONUTF8 = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$environmentDir = Join-Path $projectRoot "var\verification\ci\$stamp"
$outputRelative = "var/checks/ci-local/$stamp"
$outputDir = Join-Path $projectRoot $outputRelative
if ((Test-Path -LiteralPath $environmentDir) -or (Test-Path -LiteralPath $outputDir)) { throw 'Run directory already exists.' }
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$results = [Collections.Generic.List[object]]::new()
$ciPython = Join-Path $environmentDir 'Scripts\python.exe'
$passed = $false
try {
    & $PythonPath -I -B -c "import platform,sys; assert sys.version_info[:2] == (3,12) and sys.platform == 'win32' and platform.machine().lower() in ('amd64','x86_64'), 'Windows x64 CPython 3.12 required'"
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported interpreter profile.' }
    & $PythonPath -I -B -m venv $environmentDir *> (Join-Path $outputDir 'venv.log')
    if ($LASTEXITCODE -ne 0) { throw 'Clean environment creation failed.' }
    $installArgs = @('-I','-B','-m','pip','--isolated','--disable-pip-version-check','install','--require-hashes','--only-binary=:all:','--report',(Join-Path $outputDir 'install-report.json'),'-r',(Join-Path $projectRoot 'requirements\ci-windows-py312.lock'))
    if ($UsePackageIndex) { $installArgs += @('--index-url','https://pypi.org/simple') }
    else { $installArgs += @('--no-index','--find-links',$Wheelhouse) }
    & $ciPython @installArgs *> (Join-Path $outputDir 'install.log')
    if ($LASTEXITCODE -ne 0) { throw 'Locked installation failed; inspect install.log.' }
    $results.Add(@{name='clean_locked_install';passed=$true})
    & $ciPython -I -B -m pip --isolated --disable-pip-version-check check *> (Join-Path $outputDir 'pip-check.log')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
    $results.Add(@{name='pip_check';passed=$true})
    & $ciPython -I -B scripts/rebuild/check_ci_inputs.py --output-dir $outputRelative *> (Join-Path $outputDir 'ci-inputs.log')
    if ($LASTEXITCODE -ne 0) { throw 'CI input checks failed; inspect ci-inputs.log.' }
    $results.Add(@{name='ci_inputs';passed=$true})
    & $ciPython -I -B scripts/rebuild/run_checks.py --output-dir $outputRelative *> (Join-Path $outputDir 'tests.log')
    if ($LASTEXITCODE -ne 0) { throw 'Test suite failed; inspect tests.log.' }
    $results.Add(@{name='test_suite';passed=$true})
    & $ciPython -I -B scripts/diagnostics/qualify_ci_ui.py --output-dir "$outputRelative/ui" *> (Join-Path $outputDir 'ui.log')
    if ($LASTEXITCODE -ne 0) { throw 'UI qualification failed; inspect ui.log.' }
    $results.Add(@{name='ui_qualification';passed=$true})
    $passed = $true
}
finally {
    $files = @('.github/workflows/ci.yml','pyproject.toml','requirements/ci.in','requirements/ci-windows-py312.lock','scripts/rebuild/check_ci_inputs.py','scripts/rebuild/run_checks.py','scripts/diagnostics/qualify_ci_ui.py','scripts/ci/Test-Ci.ps1')
    $hashes = @($files | ForEach-Object { @{path=$_;sha256=(Get-FileHash -LiteralPath (Join-Path $projectRoot $_) -Algorithm SHA256).Hash.ToLower()} })
    @{completed_at_utc=[DateTimeOffset]::UtcNow.ToString('o');status=$(if ($passed) {'PASS'} else {'FAIL'});scope='Fresh local Windows CPython 3.12 CI environment; no hosted Actions run, model inference or live PostgreSQL';environment=$environmentDir;output=$outputDir;checks=$results.ToArray();files=$hashes} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $outputDir 'summary.json') -Encoding utf8
    Write-Output "CI output: $outputDir"
}
Write-Output 'PASS: clean installation, CI inputs, tests and UI qualification.'
