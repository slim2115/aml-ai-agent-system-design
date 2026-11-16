#Requires -Version 7.0
[CmdletBinding(DefaultParameterSetName='Existing')]
param([Parameter(Mandatory, ParameterSetName='Network')][switch]$Download,
      [Parameter(Mandatory, ParameterSetName='Local')][string]$SourceDirectory)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot 'Assert-StandRoot.ps1') $root
$recipe = Join-Path $root 'config\embedding-files.json'
$manifest = Get-Content -LiteralPath $recipe -Raw | ConvertFrom-Json
$revision = 'e8f8c211226b894fcb81acc59f3b34ba3efd5f42'
$model = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
if ($manifest.model_id -ne $model -or $manifest.files.Count -ne 10) { throw 'Unexpected embedding recipe' }
$dest = Join-Path $root 'var\models\paraphrase-multilingual-MiniLM-L12-v2'
$liveRecipe = "$dest.manifest.json"
function Test-ByteInventory([string]$Folder) {
    $all = @(Get-ChildItem -LiteralPath $Folder -Recurse -File -Force)
    if ($all.Count -ne $manifest.files.Count) { throw 'Incomplete or unexpected files in model directory' }
    foreach ($entry in $manifest.files) {
        $file = Join-Path $Folder $entry.path
        if ((Get-FileHash -LiteralPath $file).Hash -ne $entry.sha256) { throw "Model file mismatch: $($entry.path)" }
    }
}
foreach ($entry in $manifest.files) {
    if ($entry.path -notmatch '^(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+$' -or $entry.path.Split('/') -contains '..' -or
        $entry.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid model file descriptor' }
}
if (Test-Path -LiteralPath $liveRecipe) {
    if ((Get-FileHash -LiteralPath $liveRecipe).Hash -ne (Get-FileHash -LiteralPath $recipe).Hash) {
        throw 'Existing model manifest differs; inspect the local copy.'
    }
}
if (Test-Path -LiteralPath $dest) {
    Test-ByteInventory $dest
} else {
    if (-not $Download -and -not $SourceDirectory) { throw 'Use -Download or -SourceDirectory to install the pinned model.' }
    [void][IO.Directory]::CreateDirectory((Split-Path $dest))
    $stage = Join-Path $root ('var\tmp\embedding-' + [guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($stage)
    foreach ($entry in $manifest.files) {
        $file = Join-Path $stage $entry.path
        [void][IO.Directory]::CreateDirectory((Split-Path $file))
        if ($Download) {
            Invoke-WebRequest -Uri "https://huggingface.co/$model/resolve/$revision/$($entry.path)" -OutFile $file -TimeoutSec 600
        } else {
            [IO.File]::Copy((Join-Path $SourceDirectory $entry.path), $file)
        }
    }
    Test-ByteInventory $stage
    [IO.Directory]::Move($stage, $dest)
}
if (-not (Test-Path -LiteralPath $liveRecipe)) { [IO.File]::Copy($recipe, $liveRecipe) }
Write-Output "Verified $($manifest.files.Count) model files: $model ($revision)"
