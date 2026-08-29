$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
& uv run python "$Root/scripts/delivery.py" reset @args
exit $LASTEXITCODE
