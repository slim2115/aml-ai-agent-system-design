param([string]$OutputDir = '')
$ErrorActionPreference = 'Stop'
$rebuildRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$python = Join-Path $rebuildRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Create the rebuild Python environment first.' }
$arguments = @('-I', '-B', (Join-Path $rebuildRoot 'qa\tools\browser_tooling.py'), 'install')
if ($OutputDir) { $arguments += @('--output-dir', $OutputDir) }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Browser tools installation failed; inspect the installation artifact before retrying.' }
