#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --with pytest --with pyyaml pytest -q tests/
