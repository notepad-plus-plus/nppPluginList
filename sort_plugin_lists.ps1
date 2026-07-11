[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$validator = Join-Path -Path $PSScriptRoot -ChildPath 'validator.py'

& python $validator sort
if ($LASTEXITCODE -ne 0) {
    throw "Plugin list sorting failed with exit code $LASTEXITCODE."
}
