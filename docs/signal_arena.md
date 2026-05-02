# Signal Arena local integration

Source skill: https://signal.coze.site/skill.md

## Local checks

Read-only API health check:

```bash
uv run python scripts/signal_arena_agent.py --mode health --market US
```

Run TradingAgents locally against the strongest US mover, without placing an order:

```bash
uv run python scripts/signal_arena_agent.py --mode agent --market US
```

When no `--symbol` is supplied, the runner uses autonomous stock selection:

1. resolve the market (`AUTO` picks CN/HK/US by China trading hours),
2. collect Signal Arena `top-movers` plus the market stock list,
3. filter by price, optional whitelist/blacklist, and extreme moves,
4. score candidates by momentum, top-mover rank, volume, intraday strength,
   current holdings, and recent-analysis penalty,
5. send only the highest-scoring stock to TradingAgents for deeper analysis.

Selection details are saved to:

```text
results/signal_arena/stock_selection.json
```

Useful `.env` knobs:

```bash
SIGNAL_ARENA_SELECTION_MODE=autonomous
SIGNAL_ARENA_SELECTION_STRATEGY=momentum
SIGNAL_ARENA_ALLOWED_MARKETS=CN
SIGNAL_ARENA_CANDIDATE_LIMIT=320
SIGNAL_ARENA_MIN_PRICE=1
SIGNAL_ARENA_MIN_VOLUME=1000000
SIGNAL_ARENA_MAX_ABS_CHANGE_RATE=0.18
SIGNAL_ARENA_RECENT_SYMBOL_PENALTY_HOURS=8
SIGNAL_ARENA_SYMBOL_BLACKLIST=
SIGNAL_ARENA_SYMBOL_WHITELIST=
SIGNAL_ARENA_SYMBOL_WHITELIST_CN=sh600519,sh601318,sh600036,sh600276,sh600900,sh600309,sh600150,sh601100,sz000858,sz000333,sz000651,sz300750,sz002415
SIGNAL_ARENA_SYMBOL_WHITELIST_US=gb_aapl,gb_msft,gb_nvda,gb_amzn,gb_goog,gb_meta,gb_tsla,gb_avgo,gb_mu,gb_jpm,gb_xom,gb_abt,gb_adbe
```

The recommended mode is a fixed liquid universe plus autonomous scoring inside
that universe. This avoids spending slow local inference on thin or noisy names,
while still letting the agent choose among the verified China candidates each round.
`SIGNAL_ARENA_ALLOWED_MARKETS=CN` keeps the runner on the verified A-share pool
and prevents the `AUTO` scheduler from falling into HK when no HK fixed pool is
configured.

To force one stock and bypass autonomous selection:

```bash
SIGNAL_ARENA_SYMBOL=gb_nvda
```

For a quick timeout check:

```bash
uv run python scripts/signal_arena_agent.py --mode agent --market US --agent-timeout-seconds 5
```

Join or query private account state requires:

```bash
export SIGNAL_ARENA_API_KEY="your_api_key"
uv run python scripts/signal_arena_agent.py --mode join
```

Real order submission is opt-in:

```bash
SIGNAL_ARENA_API_KEY="your_api_key" \
uv run python scripts/signal_arena_agent.py --mode agent --market US --execute-trade
```

Results are written to:

```text
results/signal_arena/last_run.json
results/signal_arena/runs.jsonl
```

## 30 minute schedule

The wrapper script defaults to dry-run mode. Put runtime settings in `.env`:

```bash
SIGNAL_ARENA_API_KEY=your_api_key
SIGNAL_ARENA_MODE=agent
SIGNAL_ARENA_MARKET=CN
SIGNAL_ARENA_EXECUTE_TRADE=0
TRADINGAGENTS_ANALYSTS=market
TRADINGAGENTS_AGENT_TIMEOUT_SECONDS=1200
```

Install a 30 minute cron task:

```bash
bash scripts/install_signal_arena_cron.sh
```

To enable live arena trading after local validation:

```bash
SIGNAL_ARENA_EXECUTE_TRADE=1
```

## Dashboard

Start the monitor panel:

```bash
bash scripts/run_signal_arena_dashboard.sh
```

Default bind:

```text
http://100.123.254.50:8787
```

The dashboard is bound to the machine's Tailscale IP only. Use the dashboard
token stored in `.env` as `SIGNAL_DASHBOARD_TOKEN`.

Service management:

```bash
systemctl --user status signal-arena-dashboard.service
systemctl --user restart signal-arena-dashboard.service
```

The dashboard shows:

- Signal Arena account, rank, cash, holdings, market status
- top movers, leaderboard, recent runs, cron log
- TradingAgents analysis history under `results/*/TradingAgentsStrategy_logs`
- manual health / dry-run job launch

Live trading controls are disabled unless `SIGNAL_DASHBOARD_ALLOW_TRADES=1`.
