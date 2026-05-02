#!/usr/bin/env bash
set -euo pipefail

cd /home/lucas/ai/TradingAgents
mkdir -p results/signal_arena

export PATH="/home/lucas/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
UV_BIN="${UV_BIN:-$(command -v uv)}"

exec "${UV_BIN}" run python scripts/signal_arena_dashboard.py
