#!/usr/bin/env bash
set -euo pipefail

cd /home/lucas/ai/TradingAgents

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

LOG_FILE="${SIGNAL_WATCHDOG_LOG:-results/signal_arena/watchdog.log}"
MIN_AVAILABLE_MB="${SIGNAL_WATCHDOG_MIN_AVAILABLE_MB:-6144}"
MAX_SWAP_USED_MB="${SIGNAL_WATCHDOG_MAX_SWAP_USED_MB:-8192}"
MAX_AGENT_SECONDS="${SIGNAL_WATCHDOG_MAX_AGENT_SECONDS:-1200}"
VERBOSE="${SIGNAL_WATCHDOG_VERBOSE:-0}"

mkdir -p "$(dirname "${LOG_FILE}")"

log() {
  echo "$(date -Is) $*" >> "${LOG_FILE}"
}

mem_available_mb="$(
  awk '/^MemAvailable:/ { printf "%d", $2 / 1024 }' /proc/meminfo
)"

swap_used_mb="$(
  awk '
    /^SwapTotal:/ { total=$2 }
    /^SwapFree:/ { free=$2 }
    END { printf "%d", (total - free) / 1024 }
  ' /proc/meminfo
)"

agent_info="$(
  ps -eo pid=,etimes=,args= \
    | awk '
      /scripts\/signal_arena_agent.py/ && !/awk/ {
        if ($2 > max_seconds) {
          max_seconds=$2
          max_pid=$1
        }
      }
      END {
        if (max_pid != "") {
          print max_pid, max_seconds
        }
      }
    '
)"

agent_pid=""
agent_seconds=0
if [[ -n "${agent_info}" ]]; then
  read -r agent_pid agent_seconds <<< "${agent_info}"
fi

loop_active="0"
if systemctl --user is-active --quiet signal-arena-loop.service; then
  loop_active="1"
fi

reason=""
if (( mem_available_mb < MIN_AVAILABLE_MB )); then
  reason="available_memory_mb=${mem_available_mb} below threshold=${MIN_AVAILABLE_MB}"
elif (( swap_used_mb > MAX_SWAP_USED_MB )); then
  reason="swap_used_mb=${swap_used_mb} above threshold=${MAX_SWAP_USED_MB}"
elif [[ -n "${agent_pid}" ]] && (( agent_seconds > MAX_AGENT_SECONDS )); then
  reason="agent_pid=${agent_pid} runtime_seconds=${agent_seconds} above threshold=${MAX_AGENT_SECONDS}"
fi

if [[ -z "${reason}" ]]; then
  if [[ "${VERBOSE}" == "1" ]]; then
    log "ok available_memory_mb=${mem_available_mb} swap_used_mb=${swap_used_mb} loop_active=${loop_active} agent_pid=${agent_pid:-none} agent_seconds=${agent_seconds}"
  fi
  exit 0
fi

if [[ "${loop_active}" != "1" && -z "${agent_pid}" ]]; then
  if [[ "${VERBOSE}" == "1" ]]; then
    log "threshold breached but no active Signal Arena agent to interrupt: ${reason}; available_memory_mb=${mem_available_mb} swap_used_mb=${swap_used_mb}"
  fi
  exit 0
fi

log "interrupting Signal Arena agent: ${reason}; available_memory_mb=${mem_available_mb} swap_used_mb=${swap_used_mb} loop_active=${loop_active} agent_pid=${agent_pid:-none} agent_seconds=${agent_seconds}"

systemctl --user stop signal-arena-loop.service || true
pkill -TERM -f 'scripts/signal_arena_agent.py' || true
sleep 5
pkill -KILL -f 'scripts/signal_arena_agent.py' || true

log "interrupt complete; vLLM service intentionally untouched"
