#!/usr/bin/env bash
set -euo pipefail

cd /home/lucas/ai/TradingAgents
mkdir -p results/signal_arena

export PATH="/home/lucas/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
UV_BIN="${UV_BIN:-$(command -v uv)}"
LOCK_FILE="results/signal_arena/run.lock"
COOLDOWN_SECONDS="${SIGNAL_ARENA_LOOP_COOLDOWN_SECONDS:-60}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "$(date -Is) Signal Arena run already active; loop exiting."
  exit 0
fi

while true; do
  if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
  fi

  MODE="${SIGNAL_ARENA_MODE:-agent}"
  MARKET="${SIGNAL_ARENA_MARKET:-AUTO}"
  ANALYSTS="${TRADINGAGENTS_ANALYSTS:-market}"
  TIMEOUT_SECONDS="${TRADINGAGENTS_AGENT_TIMEOUT_SECONDS:-1800}"
  COOLDOWN_SECONDS="${SIGNAL_ARENA_LOOP_COOLDOWN_SECONDS:-60}"

  echo "$(date -Is) Signal Arena loop starting: mode=${MODE} market=${MARKET} analysts=${ANALYSTS} execute=${SIGNAL_ARENA_EXECUTE_TRADE:-0}"

  EXTRA_ARGS=()
  if [[ "${SIGNAL_ARENA_EXECUTE_TRADE:-0}" == "1" ]]; then
    EXTRA_ARGS+=(--execute-trade)
  fi
  if [[ -n "${SIGNAL_ARENA_SYMBOL:-}" ]]; then
    EXTRA_ARGS+=(--symbol "${SIGNAL_ARENA_SYMBOL}")
  fi

  set +e
  timeout --kill-after=30s "$((TIMEOUT_SECONDS + 60))s" \
    "${UV_BIN}" run python scripts/signal_arena_agent.py \
    --mode "${MODE}" \
    --market "${MARKET}" \
    --analysts "${ANALYSTS}" \
    --agent-timeout-seconds "${TIMEOUT_SECONDS}" \
    "${EXTRA_ARGS[@]}"
  status=$?
  set -e

  echo "$(date -Is) Signal Arena loop finished with status=${status}; sleeping ${COOLDOWN_SECONDS}s"
  sleep "${COOLDOWN_SECONDS}"
done
