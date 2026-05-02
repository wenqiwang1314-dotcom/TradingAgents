#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${TRADINGAGENTS_PROJECT_DIR:-/home/lucas/ai/TradingAgents}"
cd "${PROJECT_DIR}"
mkdir -p results/signal_arena

export PATH="/home/lucas/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
UV_BIN="${UV_BIN:-$(command -v uv)}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

MODE="${DAILY_TRADING_MODE:-preopen}"
MARKETS="${DAILY_TRADING_MARKETS:-CN,US}"
ANALYSTS="${TRADINGAGENTS_ANALYSTS:-market,social,news,fundamentals}"
TIMEOUT_SECONDS="${TRADINGAGENTS_AGENT_TIMEOUT_SECONDS:-1200}"
RUN_TIMEOUT_SECONDS="${DAILY_RUN_TIMEOUT_SECONDS:-7200}"
LOCK_NAME="$(printf '%s_%s' "${MODE}" "${MARKETS}" | tr ',/' '__')"
LOCK_FILE="results/signal_arena/daily_${LOCK_NAME}.lock"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "$(date -Is) Daily TradingAgents ${MODE}/${MARKETS} already active; skipping."
  exit 0
fi

EXTRA_ARGS=()
if [[ "${DAILY_EXECUTE_TRADE:-0}" == "1" ]]; then
  EXTRA_ARGS+=(--execute-trade)
fi
if [[ -n "${DAILY_SELECTOR_MODEL:-}" ]]; then
  EXTRA_ARGS+=(--selector-model "${DAILY_SELECTOR_MODEL}")
fi
if [[ -n "${DAILY_SELECTOR_BASE_URL:-}" ]]; then
  EXTRA_ARGS+=(--selector-base-url "${DAILY_SELECTOR_BASE_URL}")
fi
if [[ -n "${DAILY_CANDIDATE_LIMIT:-}" ]]; then
  EXTRA_ARGS+=(--candidate-limit "${DAILY_CANDIDATE_LIMIT}")
fi
if [[ -n "${DAILY_PICKS_PER_MARKET:-}" ]]; then
  EXTRA_ARGS+=(--picks-per-market "${DAILY_PICKS_PER_MARKET}")
fi

exec timeout --kill-after=60s "${RUN_TIMEOUT_SECONDS}s" \
  "${UV_BIN}" run python scripts/daily_auto_tradingagents.py \
  --mode "${MODE}" \
  --markets "${MARKETS}" \
  --analysts "${ANALYSTS}" \
  --agent-timeout-seconds "${TIMEOUT_SECONDS}" \
  "${EXTRA_ARGS[@]}"
