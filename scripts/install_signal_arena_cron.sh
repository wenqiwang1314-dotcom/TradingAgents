#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/lucas/ai/TradingAgents"
RUNNER="${PROJECT_DIR}/scripts/run_signal_arena_30m.sh"
LOG_DIR="${PROJECT_DIR}/results/signal_arena"
CRON_LINE="*/30 * * * * ${RUNNER} >> ${LOG_DIR}/cron.log 2>&1"

mkdir -p "${LOG_DIR}"
chmod +x "${RUNNER}"

tmp_file="$(mktemp)"
trap 'rm -f "${tmp_file}"' EXIT

crontab -l 2>/dev/null | grep -vF "${RUNNER}" > "${tmp_file}" || true
printf '%s\n' "${CRON_LINE}" >> "${tmp_file}"
crontab "${tmp_file}"

echo "Installed Signal Arena cron:"
echo "${CRON_LINE}"
