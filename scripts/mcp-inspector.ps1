$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
& uv run python "$Root/scripts/delivery.py" mcp-inspector @args
exit $LASTEXITCODE
