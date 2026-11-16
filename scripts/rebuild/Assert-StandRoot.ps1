param([Parameter(Mandatory)][string]$Root)
$ErrorActionPreference = 'Stop'
$expected = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if ([IO.Path]::GetFullPath($Root) -ne $expected) { throw 'Unexpected stand root' }
foreach ($member in @('.rebuild\origin.json','pyproject.toml','config\demo-policy.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $member) -PathType Leaf)) { throw "Missing project marker: $member" }
}
# Refuse redirected roots and redirected runtime directories before using a local DB or GUI.
$dir = Get-Item -LiteralPath $Root
while ($dir) {
    if ($dir.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Stand root traverses a link' }
    $dir = $dir.Parent
}
foreach ($name in @('var','var\tmp','var\models','var\postgres','var\postgres\cluster','var\lab','var\tools','var\dbeaver','var\workflow')) {
    $member = Get-Item -LiteralPath (Join-Path $Root $name) -ErrorAction SilentlyContinue
    if ($member -and ($member.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Redirected runtime path: $name" }
}
