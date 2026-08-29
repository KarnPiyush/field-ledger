#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec uv run python "$ROOT/scripts/delivery.py" package "$@"
