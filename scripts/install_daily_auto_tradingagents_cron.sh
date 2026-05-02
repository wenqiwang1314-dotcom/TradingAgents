#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${TRADINGAGENTS_PROJECT_DIR:-/home/lucas/ai/TradingAgents}"
RUNNER="${PROJECT_DIR}/scripts/run_daily_auto_tradingagents.sh"
LOG_DIR="${PROJECT_DIR}/results/signal_arena"
MARKER="# TradingAgents daily Signal Arena automation"

mkdir -p "${LOG_DIR}"
chmod +x "${RUNNER}"

tmp_file="$(mktemp)"
new_file="$(mktemp)"
trap 'rm -f "${tmp_file}" "${new_file}"' EXIT

crontab -l 2>/dev/null | awk -v marker="${MARKER}" '
  $0 == marker {skip=1; next}
  skip && $0 == "" {skip=0; next}
  !skip {print}
' > "${tmp_file}" || true

cat > "${new_file}" <<EOF
${MARKER}
CRON_TZ=Asia/Shanghai
5 9 * * 1-5 DAILY_TRADING_MODE=preopen DAILY_TRADING_MARKETS=CN ${RUNNER} >> ${LOG_DIR}/daily_cn_preopen.log 2>&1
5 15 * * 1-5 DAILY_TRADING_MODE=close DAILY_TRADING_MARKETS=CN ${RUNNER} >> ${LOG_DIR}/daily_cn_close.log 2>&1
CRON_TZ=America/New_York
5 9 * * 1-5 DAILY_TRADING_MODE=preopen DAILY_TRADING_MARKETS=US ${RUNNER} >> ${LOG_DIR}/daily_us_preopen.log 2>&1
5 16 * * 1-5 DAILY_TRADING_MODE=close DAILY_TRADING_MARKETS=US ${RUNNER} >> ${LOG_DIR}/daily_us_close.log 2>&1

EOF

cat "${tmp_file}" "${new_file}" | crontab -

echo "Installed daily TradingAgents Signal Arena cron jobs:"
cat "${new_file}"
