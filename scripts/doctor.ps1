$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
& uv run python "$Root/scripts/delivery.py" doctor @args
exit $LASTEXITCODE
