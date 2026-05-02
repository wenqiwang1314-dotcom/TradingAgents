#!/usr/bin/env python3
"""Web dashboard for Signal Arena + TradingAgents.

Uses only the Python standard library plus the existing project dependencies.
Bind host/port/token via .env:
SIGNAL_DASHBOARD_HOST=0.0.0.0
SIGNAL_DASHBOARD_PORT=8787
SIGNAL_DASHBOARD_TOKEN=...
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.signal_arena_agent import (  # noqa: E402
    API_KEY_ENV,
    BASE_URL,
    SignalArenaClient,
    action_from_signal,
    load_dotenv,
)

RESULTS_DIR = PROJECT_ROOT / "results"
SIGNAL_DIR = RESULTS_DIR / "signal_arena"
RUNS_PATH = SIGNAL_DIR / "runs.jsonl"
LAST_RUN_PATH = SIGNAL_DIR / "last_run.json"
CRON_LOG_PATH = SIGNAL_DIR / "cron.log"
LOOP_LOG_PATH = SIGNAL_DIR / "loop.log"
PORTFOLIO_HISTORY_PATH = SIGNAL_DIR / "portfolio_history.jsonl"
CONVERSATION_DIR = SIGNAL_DIR / "conversation_traces"
CURRENT_CONVERSATION_PATH = SIGNAL_DIR / "current_conversation.json"
LAST_SELECTION_PATH = SIGNAL_DIR / "stock_selection.json"
JOBS_DIR = SIGNAL_DIR / "dashboard_jobs"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tail_text(path: Path, max_lines: int = 120) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Unable to read {path}: {exc}"
    return "\n".join(lines[-max_lines:])


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_jsonl_tail(path: Path, limit: int = 30) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit * 3 :]:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def unwrap_api_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def read_portfolio_history(limit: int = 500) -> list[dict[str, Any]]:
    return read_jsonl_tail(PORTFOLIO_HISTORY_PATH, limit)


def record_portfolio_history(home_result: dict[str, Any], portfolio_result: dict[str, Any]) -> list[dict[str, Any]]:
    home = unwrap_api_data(home_result)
    portfolio_payload = unwrap_api_data(portfolio_result)
    portfolio = portfolio_payload.get("portfolio") or home.get("portfolio") or {}
    if not isinstance(portfolio, dict) or portfolio.get("total_value") is None:
        return read_portfolio_history()

    agent = portfolio_payload.get("agent") or home.get("agent") or {}
    row = {
        "timestamp": now_iso(),
        "agent_id": agent.get("id"),
        "agent_username": agent.get("username"),
        "cash": portfolio.get("cash"),
        "holdings_value": portfolio.get("holdings_value"),
        "total_value": portfolio.get("total_value"),
        "total_invested": portfolio.get("total_invested"),
        "return_rate": portfolio.get("return_rate"),
        "total_fees": portfolio.get("total_fees"),
        "rank": home.get("rank"),
        "market_status": home.get("market_status"),
    }

    history = read_portfolio_history()
    should_append = True
    if history:
        last = history[-1]
        try:
            last_ts = datetime.fromisoformat(str(last.get("timestamp")).replace("Z", "+00:00"))
            should_append = (datetime.now(timezone.utc) - last_ts).total_seconds() >= 60
        except Exception:
            should_append = True
        watched = ("cash", "holdings_value", "total_value", "return_rate", "rank")
        if any(last.get(key) != row.get(key) for key in watched):
            should_append = True

    if should_append:
        SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
        with PORTFOLIO_HISTORY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        history.append(row)
    return history[-500:]


def snapshot_history(snapshot_result: dict[str, Any]) -> list[dict[str, Any]]:
    payload = unwrap_api_data(snapshot_result)
    snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
    if not isinstance(snapshots, list):
        return []
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        rows.append(
            {
                "timestamp": snapshot.get("snapshot_time") or snapshot.get("timestamp"),
                "snapshot_date": snapshot.get("snapshot_date"),
                "cash": snapshot.get("cash"),
                "holdings_value": snapshot.get("holdings_value"),
                "total_value": snapshot.get("total_value"),
                "total_invested": snapshot.get("total_invested"),
                "return_rate": snapshot.get("return_rate"),
            }
        )
    return rows[-500:]


def current_portfolio_snapshot(home_result: dict[str, Any], portfolio_result: dict[str, Any]) -> dict[str, Any] | None:
    home_payload = unwrap_api_data(home_result)
    portfolio_payload = unwrap_api_data(portfolio_result)
    home = home_payload if isinstance(home_payload, dict) else {}
    portfolio = {}
    if isinstance(portfolio_payload, dict):
        portfolio = portfolio_payload.get("portfolio") or {}
    if not portfolio and isinstance(home.get("portfolio"), dict):
        portfolio = home["portfolio"]
    if not isinstance(portfolio, dict) or portfolio.get("total_value") is None:
        return None
    return {
        "timestamp": now_iso(),
        "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
        "cash": portfolio.get("cash"),
        "holdings_value": portfolio.get("holdings_value"),
        "total_value": portfolio.get("total_value"),
        "total_invested": portfolio.get("total_invested"),
        "return_rate": portfolio.get("return_rate"),
        "source": "live",
    }


def merge_live_portfolio_snapshot(history: list[dict[str, Any]], live: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not live:
        return history
    if not history:
        return [live]
    last = history[-1]
    watched = ("cash", "holdings_value", "total_value", "return_rate")
    if any(last.get(key) != live.get(key) for key in watched):
        return [*history, live][-500:]
    return history


def safe_env() -> dict[str, Any]:
    keys = [
        "SIGNAL_ARENA_MODE",
        "SIGNAL_ARENA_MARKET",
        "SIGNAL_ARENA_EXECUTE_TRADE",
        "TRADINGAGENTS_ANALYSTS",
        "TRADINGAGENTS_AGENT_TIMEOUT_SECONDS",
        "TRADINGAGENTS_BACKEND_URL",
        "TRADINGAGENTS_DEEP_MODEL",
        "TRADINGAGENTS_QUICK_MODEL",
        "SIGNAL_DASHBOARD_HOST",
        "SIGNAL_DASHBOARD_PORT",
        "SIGNAL_DASHBOARD_ALLOW_TRADES",
    ]
    return {
        "api_key_configured": bool(os.getenv(API_KEY_ENV)),
        "base_url": BASE_URL,
        **{key: os.getenv(key) for key in keys if os.getenv(key) is not None},
    }


def client() -> SignalArenaClient:
    return SignalArenaClient(BASE_URL, os.getenv(API_KEY_ENV), timeout=10)


def try_call(name: str, func: Any) -> dict[str, Any]:
    try:
        return {"ok": True, "data": func()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    stock = run.get("stock") or {}
    final_decision = run.get("final_trade_decision") or ""
    signal = run.get("signal") or ""
    trade = run.get("trade")
    if isinstance(trade, dict):
        if trade.get("skipped"):
            trade_status = "skipped"
            trade_reason = trade.get("reason") or ""
        elif trade.get("ok") is False:
            trade_status = "failed"
            trade_reason = trade.get("error") or trade.get("message") or ""
        else:
            trade_status = "submitted"
            trade_reason = trade.get("message") or trade.get("status") or ""
    elif run.get("execute_trade"):
        trade_status = "pending"
        trade_reason = ""
    else:
        trade_status = "dry-run"
        trade_reason = ""
    return {
        "mode": run.get("mode"),
        "timestamp": run.get("timestamp"),
        "symbol": stock.get("symbol") or run.get("tradingagents_symbol"),
        "name": stock.get("name"),
        "trade_date": run.get("trade_date") or stock.get("trade_date"),
        "action": run.get("action") or action_from_signal(f"{signal}\n{final_decision}"),
        "shares": run.get("shares"),
        "execute_trade": run.get("execute_trade"),
        "auth_configured": run.get("auth_configured"),
        "trade": trade,
        "trade_status": trade_status,
        "trade_reason": trade_reason,
        "signal_preview": (signal or final_decision)[:900],
    }


def latest_agent_run() -> dict[str, Any] | None:
    latest = read_json(LAST_RUN_PATH)
    if isinstance(latest, dict) and latest.get("mode") == "agent":
        return latest

    for row in reversed(read_jsonl_tail(RUNS_PATH, 100)):
        if isinstance(row, dict) and row.get("mode") == "agent":
            return row
    return None


def list_analysis_logs(limit: int = 80) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    for path in RESULTS_DIR.glob("*/TradingAgentsStrategy_logs/full_states_log_*.json"):
        if "signal_arena" in path.parts:
            continue
        data = read_json(path) or {}
        final_decision = data.get("final_trade_decision") or ""
        symbol = data.get("company_of_interest") or path.parts[-3]
        logs.append(
            {
                "symbol": symbol,
                "trade_date": data.get("trade_date") or path.stem.replace("full_states_log_", ""),
                "path": str(path.relative_to(PROJECT_ROOT)),
                "mtime": path.stat().st_mtime,
                "mtime_iso": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "action": action_from_signal(final_decision),
                "market_report_chars": len(data.get("market_report") or ""),
                "final_preview": final_decision[:1200],
            }
        )
    logs.sort(key=lambda item: item["mtime"], reverse=True)
    return logs[:limit]


def load_analysis(relative_path: str) -> dict[str, Any]:
    path = (PROJECT_ROOT / relative_path).resolve()
    if not path.is_file() or PROJECT_ROOT.resolve() not in path.parents:
        raise ValueError("invalid analysis path")
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError("analysis file is not JSON")
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "company": data.get("company_of_interest"),
        "trade_date": data.get("trade_date"),
        "market_report": data.get("market_report"),
        "sentiment_report": data.get("sentiment_report"),
        "news_report": data.get("news_report"),
        "fundamentals_report": data.get("fundamentals_report"),
        "investment_plan": data.get("investment_plan"),
        "trader_investment_decision": data.get("trader_investment_decision"),
        "final_trade_decision": data.get("final_trade_decision"),
    }


def conversation_summary(trace: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    sections = trace.get("sections") if isinstance(trace.get("sections"), list) else []
    preview = ""
    for section in sections:
        if isinstance(section, dict) and section.get("content"):
            preview = str(section.get("content"))[:420]
            break
    return {
        "id": trace.get("id") or (path.stem if path else ""),
        "status": trace.get("status"),
        "started_at": trace.get("started_at"),
        "updated_at": trace.get("updated_at"),
        "finished_at": trace.get("finished_at"),
        "symbol": trace.get("tradingagents_symbol") or trace.get("arena_symbol"),
        "stock_name": trace.get("stock_name"),
        "market": trace.get("market"),
        "action": trace.get("action"),
        "analysts": trace.get("analysts") or [],
        "section_count": len(sections),
        "preview": preview,
    }


def fallback_conversation_from_last_run() -> dict[str, Any] | None:
    run = read_json(LAST_RUN_PATH)
    if not isinstance(run, dict):
        return None
    symbol = run.get("tradingagents_symbol") or (run.get("stock") or {}).get("symbol")
    return {
        "id": f"last_run_{symbol or 'unknown'}",
        "status": "completed",
        "started_at": run.get("timestamp"),
        "updated_at": run.get("timestamp"),
        "finished_at": run.get("timestamp"),
        "market": run.get("market"),
        "arena_symbol": (run.get("stock") or {}).get("symbol"),
        "tradingagents_symbol": symbol,
        "stock_name": (run.get("stock") or {}).get("name"),
        "trade_date": run.get("trade_date"),
        "analysts": run.get("analysts") or [],
        "action": run.get("action"),
        "shares": run.get("shares"),
        "signal": run.get("signal"),
        "final_trade_decision": run.get("final_trade_decision"),
        "sections": [
            {"title": "Signal", "content": run.get("signal") or ""},
            {"title": "Final Trade Decision", "content": run.get("final_trade_decision") or ""},
        ],
    }


def runtime_conversation_from_process() -> dict[str, Any] | None:
    processes = active_processes()
    agent_lines = [line for line in processes if "signal_arena_agent.py" in line]
    if not agent_lines:
        return None
    latest = read_json(LAST_RUN_PATH) if LAST_RUN_PATH.exists() else {}
    stock = latest.get("stock") if isinstance(latest, dict) else {}
    return {
        "id": "current_running_agent",
        "status": "running",
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "market": os.getenv("SIGNAL_ARENA_MARKET", "AUTO"),
        "arena_symbol": stock.get("symbol") if isinstance(stock, dict) else None,
        "tradingagents_symbol": latest.get("tradingagents_symbol") if isinstance(latest, dict) else None,
        "stock_name": stock.get("name") if isinstance(stock, dict) else None,
        "analysts": (os.getenv("TRADINGAGENTS_ANALYSTS", "market") or "market").split(","),
        "sections": [
            {
                "title": "Current Agent Runtime",
                "content": "当前 Signal Arena Agent 正在推理。完整模型对话会在本轮结束后写入历史；下一轮开始会实时写入 running trace。",
            },
            {"title": "Processes", "content": "\n".join(agent_lines)},
            {"title": "Loop Log Tail", "content": tail_text(LOOP_LOG_PATH, 80)},
        ],
    }


def load_current_conversation() -> dict[str, Any] | None:
    trace = read_json(CURRENT_CONVERSATION_PATH)
    if isinstance(trace, dict) and trace.get("status") == "running":
        return trace
    runtime = runtime_conversation_from_process()
    if runtime:
        return runtime
    if isinstance(trace, dict):
        return trace
    return fallback_conversation_from_last_run()


def list_conversations(limit: int = 80) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if CONVERSATION_DIR.exists():
        for path in CONVERSATION_DIR.glob("*.json"):
            trace = read_json(path)
            if isinstance(trace, dict):
                item = conversation_summary(trace, path)
                item["path"] = str(path.relative_to(PROJECT_ROOT))
                item["mtime"] = path.stat().st_mtime
                rows.append(item)
    fallback = fallback_conversation_from_last_run()
    if fallback and not any(row.get("id") == fallback.get("id") for row in rows):
        item = conversation_summary(fallback)
        item["path"] = ""
        item["mtime"] = LAST_RUN_PATH.stat().st_mtime if LAST_RUN_PATH.exists() else 0
        rows.append(item)
    rows.sort(key=lambda row: row.get("mtime") or 0, reverse=True)
    for row in rows:
        row.pop("mtime", None)
    return rows[:limit]


def load_conversation(trace_id: str) -> dict[str, Any]:
    safe_id = "".join(ch for ch in trace_id if ch.isalnum() or ch in "._-")
    if safe_id == "current_running_agent":
        runtime = runtime_conversation_from_process()
        if runtime:
            return runtime
    if not safe_id:
        current = load_current_conversation()
        if current:
            return current
        raise ValueError("conversation not found")
    if safe_id.startswith("last_run_"):
        fallback = fallback_conversation_from_last_run()
        if fallback:
            return fallback
    path = (CONVERSATION_DIR / f"{safe_id}.json").resolve()
    if not path.is_file() or CONVERSATION_DIR.resolve() not in path.parents:
        raise ValueError("conversation not found")
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError("conversation file is not JSON")
    return data


def command_output(args: list[str], timeout: int = 8) -> str:
    try:
        proc = subprocess.run(
            args,
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.stdout.strip()
    except Exception as exc:
        return str(exc)


def active_processes() -> list[str]:
    output = command_output(
        ["bash", "-lc", "ps -eo pid,ppid,etime,pcpu,pmem,cmd | grep -E 'signal_arena_loop|signal_arena_agent|smoke_test_tradingagents|vllm serve' | grep -v 'grep -E' || true"],
        timeout=5,
    )
    return [line for line in output.splitlines() if line.strip()]


def cron_status() -> dict[str, Any]:
    crontab = command_output(["bash", "-lc", "crontab -l 2>/dev/null | rg 'signal_arena|TradingAgents' || true"])
    return {"installed": bool(crontab.strip()), "lines": crontab.splitlines()}


def loop_status() -> dict[str, Any]:
    status = command_output(
        ["bash", "-lc", "systemctl --user is-active signal-arena-loop.service 2>/dev/null || true"],
        timeout=4,
    ).strip()
    enabled = command_output(
        ["bash", "-lc", "systemctl --user is-enabled signal-arena-loop.service 2>/dev/null || true"],
        timeout=4,
    ).strip()
    return {"active": status == "active", "status": status or "unknown", "enabled": enabled or "unknown"}


def dashboard_summary() -> dict[str, Any]:
    arena = client()
    market = os.getenv("SIGNAL_ARENA_MARKET", "US")
    history = [summarize_run(row) for row in read_jsonl_tail(RUNS_PATH, 25)]
    latest = latest_agent_run()
    summary: dict[str, Any] = {
        "generated_at": now_iso(),
        "config": safe_env(),
        "cron": cron_status(),
        "loop": loop_status(),
        "processes": active_processes(),
        "latest_run": summarize_run(latest) if isinstance(latest, dict) else None,
        "history": history,
        "analysis_logs": list_analysis_logs(),
        "stock_selection": read_json(LAST_SELECTION_PATH),
        "current_conversation": conversation_summary(load_current_conversation() or {}),
        "conversation_history": list_conversations(30),
        "cron_log_tail": tail_text(CRON_LOG_PATH, 100),
        "loop_log_tail": tail_text(LOOP_LOG_PATH, 120),
        "jobs": list_jobs(),
    }
    summary["arena_home"] = try_call("home", arena.home) if arena.api_key else {"ok": False, "error": "No API key"}
    summary["arena_portfolio"] = try_call("portfolio", arena.portfolio) if arena.api_key else {"ok": False, "error": "No API key"}
    summary["arena_snapshots"] = try_call("snapshots", arena.snapshots) if arena.api_key else {"ok": False, "error": "No API key"}
    summary["portfolio_history"] = merge_live_portfolio_snapshot(
        snapshot_history(summary["arena_snapshots"]),
        current_portfolio_snapshot(summary["arena_home"], summary["arena_portfolio"]),
    )
    if not summary["portfolio_history"] and arena.api_key:
        summary["portfolio_history"] = record_portfolio_history(summary["arena_home"], summary["arena_portfolio"])
    summary["top_movers"] = try_call("top_movers", arena.top_movers)
    summary["stocks"] = try_call("stocks", lambda: arena.stocks(market, 8))
    summary["leaderboard"] = try_call("leaderboard", arena.leaderboard)
    return summary


def list_jobs() -> list[dict[str, Any]]:
    with JOBS_LOCK:
        items = list(JOBS.values())
    for item in items:
        proc = item.get("process")
        if proc and item["status"] == "running" and proc.poll() is not None:
            item["status"] = "completed" if proc.returncode == 0 else "failed"
            item["returncode"] = proc.returncode
            item["finished_at"] = item.get("finished_at") or now_iso()
        item.pop("process", None)
    return sorted(items, key=lambda row: row["started_at"], reverse=True)


def start_job(payload: dict[str, Any]) -> dict[str, Any]:
    mode = payload.get("mode", "health")
    if mode not in {"health", "agent", "join"}:
        raise ValueError("mode must be health, agent, or join")
    execute_trade = bool(payload.get("execute_trade"))
    if execute_trade and os.getenv("SIGNAL_DASHBOARD_ALLOW_TRADES") != "1":
        raise PermissionError("live trading is disabled by SIGNAL_DASHBOARD_ALLOW_TRADES")

    market = payload.get("market") or os.getenv("SIGNAL_ARENA_MARKET", "US")
    analysts = payload.get("analysts") or os.getenv("TRADINGAGENTS_ANALYSTS", "market")
    timeout_seconds = str(payload.get("timeout_seconds") or os.getenv("TRADINGAGENTS_AGENT_TIMEOUT_SECONDS", "1800"))
    symbol = (payload.get("symbol") or "").strip()

    job_id = uuid.uuid4().hex[:12]
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = JOBS_DIR / f"{job_id}.log"
    args = [
        "uv",
        "run",
        "python",
        "scripts/signal_arena_agent.py",
        "--mode",
        mode,
        "--market",
        market,
        "--analysts",
        analysts,
        "--agent-timeout-seconds",
        timeout_seconds,
    ]
    if symbol:
        args.extend(["--symbol", symbol])
    if execute_trade:
        args.append("--execute-trade")

    env = os.environ.copy()
    env["PATH"] = f"/home/lucas/.local/bin:{env.get('PATH', '')}"
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        args,
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    job = {
        "id": job_id,
        "status": "running",
        "mode": mode,
        "market": market,
        "symbol": symbol,
        "analysts": analysts,
        "execute_trade": execute_trade,
        "timeout_seconds": timeout_seconds,
        "pid": process.pid,
        "log_path": str(log_path.relative_to(PROJECT_ROOT)),
        "started_at": now_iso(),
        "process": process,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    return {k: v for k, v in job.items() if k != "process"}


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Signal Arena · Live Monitor</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    :root {
      color-scheme: dark;
      --bg: #060a10;
      --panel: #0d1520;
      --panel2: #111c2b;
      --line: #1e2f45;
      --line2: #243548;
      --text: #dde8f5;
      --muted: #7a8ea6;
      --good: #34d399;
      --bad: #f87171;
      --warn: #fbbf24;
      --accent: #60a5fa;
      --accent2: #2dd4bf;
      --accent-glow: rgba(96,165,250,0.12);
      --good-glow: rgba(52,211,153,0.12);
      --shadow: rgba(0,0,0,0.5);
      --radius: 10px;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      letter-spacing: -0.01em;
      -webkit-font-smoothing: antialiased;
    }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--line2); border-radius: 10px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--muted); }

    /* ── Header ─────────────────────────────────── */
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(6,10,16,0.92);
      backdrop-filter: blur(20px) saturate(180%);
      border-bottom: 1px solid var(--line);
      padding: 14px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    header::after {
      content: '';
      position: absolute;
      bottom: -1px;
      left: 0; right: 0;
      height: 1px;
      background: linear-gradient(90deg, transparent, var(--accent2), transparent);
      opacity: 0.3;
    }
    .header-brand { display: flex; align-items: center; gap: 12px; }
    .brand-icon {
      width: 36px; height: 36px; border-radius: 10px;
      background: linear-gradient(135deg, var(--accent2), var(--accent));
      display: flex; align-items: center; justify-content: center;
      font-size: 18px; font-weight: 800; color: #040d18;
      box-shadow: 0 0 16px rgba(45,212,191,0.3);
      flex-shrink: 0;
    }
    h1 { margin: 0; font-size: 18px; font-weight: 750; letter-spacing: -0.02em; }
    h2 { margin: 0 0 14px; font-size: 14px; font-weight: 700; letter-spacing: -0.01em; text-transform: uppercase; color: var(--muted); }
    h3 { margin: 14px 0 8px; font-size: 12px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
    .section-title { color: var(--text); font-size: 15px; font-weight: 700; text-transform: none; letter-spacing: -0.01em; }
    button, input, select {
      font: inherit;
      border: 1px solid var(--line2);
      background: rgba(255,255,255,0.04);
      color: var(--text);
      border-radius: 8px;
      padding: 8px 12px;
      min-height: 36px;
      transition: all 0.15s ease;
    }
    input::placeholder { color: #3d5268; }
    button {
      cursor: pointer;
      background: var(--accent);
      border-color: var(--accent);
      color: #04111f;
      font-weight: 650;
      letter-spacing: -0.01em;
    }
    button:hover { filter: brightness(1.1); }
    button.secondary {
      background: rgba(255,255,255,0.05);
      color: var(--text);
      border-color: var(--line2);
    }
    button.secondary:hover { background: rgba(255,255,255,0.09); border-color: var(--accent2); }
    .live-indicator { display: flex; align-items: center; gap: 8px; }
    .live-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--good);
      box-shadow: 0 0 0 3px rgba(52,211,153,0.2);
      animation: pulse-dot 2s ease-in-out infinite;
      flex-shrink: 0;
    }
    @keyframes pulse-dot {
      0%, 100% { box-shadow: 0 0 0 3px rgba(52,211,153,0.2); }
      50% { box-shadow: 0 0 0 5px rgba(52,211,153,0.05); }
    }

    /* ── Layout ─────────────────────────────────── */
    main {
      width: min(1540px, calc(100vw - 32px));
      margin: 18px auto 48px;
      display: grid;
      grid-template-columns: 1.35fr 1fr;
      gap: 16px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 18px;
      min-width: 0;
      box-shadow: 0 4px 24px var(--shadow);
      transition: border-color 0.2s;
    }
    section:hover { border-color: var(--line2); }
    .span { grid-column: 1 / -1; }

    /* ── Metric Cards ────────────────────────────── */
    .grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
    .metric {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 14px 16px;
      min-height: 88px;
      background: var(--panel2);
      transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s;
      position: relative;
      overflow: hidden;
    }
    .metric::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 2px;
      background: linear-gradient(90deg, var(--accent2), var(--accent));
      opacity: 0;
      transition: opacity 0.2s;
    }
    .metric:hover { border-color: var(--accent2); transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.4); }
    .metric:hover::before { opacity: 1; }
    .label { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }
    .value { font-size: 24px; font-weight: 800; overflow-wrap: anywhere; letter-spacing: -0.02em; }
    .sub { color: var(--muted); font-size: 11px; margin-top: 6px; font-weight: 500; }

    /* ── Shared Utilities ───────────────────────── */
    .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .between { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
    table { width: 100%; border-collapse: collapse; }
    th { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid var(--line); padding: 8px 8px; }
    td { border-bottom: 1px solid rgba(30,47,69,0.5); padding: 10px 8px; font-size: 13px; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255,255,255,0.02); }
    .pill {
      display: inline-flex; align-items: center;
      height: 22px; border-radius: 999px;
      padding: 0 10px; font-size: 11px; font-weight: 700;
      letter-spacing: 0.02em; text-transform: uppercase;
      border: 1px solid var(--line); background: #0c121c;
    }
    .buy { color: var(--good); border-color: rgba(52,211,153,0.3); background: rgba(52,211,153,0.08); }
    .sell { color: var(--bad); border-color: rgba(248,113,113,0.3); background: rgba(248,113,113,0.08); }
    .hold { color: var(--warn); border-color: rgba(251,191,36,0.3); background: rgba(251,191,36,0.08); }
    pre {
      margin: 0; white-space: pre-wrap; overflow-wrap: anywhere;
      background: #03080f; color: #c7deff;
      border: 1px solid var(--line); border-radius: 8px;
      padding: 14px; max-height: 360px; overflow: auto;
      font-size: 12px; line-height: 1.5;
    }
    .muted { color: var(--muted); }
    .tabs { display: flex; gap: 6px; margin-bottom: 14px; }
    .tab {
      background: rgba(255,255,255,0.04); color: var(--muted);
      border-color: var(--line); font-size: 12px; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.04em;
      padding: 6px 14px; min-height: 32px;
    }
    .tab.active { background: var(--accent2); color: #04111f; border-color: var(--accent2); }
    .hidden { display: none; }

    /* ── Charts ─────────────────────────────────── */
    .chart-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .chart-card {
      border: 1px solid var(--line); border-radius: var(--radius);
      padding: 14px; background: var(--panel2); min-height: 220px;
      transition: border-color 0.2s;
    }
    .chart-card:hover { border-color: var(--line2); }
    .chart-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 10px; }
    .chart-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
    .chart-value { font-size: 22px; font-weight: 800; letter-spacing: -0.02em; }
    .chart-wrap { width: 100%; height: 148px; }
    .chart-wrap svg { width: 100%; height: 100%; display: block; overflow: visible; }
    .axis { stroke: #1e2f45; stroke-width: 1; }
    .grid-line { stroke: #1a2b3e; stroke-width: 1; stroke-dasharray: 3 3; }
    .cash-line { color: var(--accent2); stroke: var(--accent2); }
    .return-line { color: var(--good); stroke: var(--good); }
    .value-line { color: var(--accent); stroke: var(--accent); }
    .chart-line { fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    .chart-area { fill: currentColor; stroke: none; opacity: 0.1; }
    .empty-chart { height: 148px; display: grid; place-items: center; color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; font-size: 12px; }

    /* ── Conversations ───────────────────────────── */
    .conversation-grid { display: grid; grid-template-columns: 1.2fr .8fr; gap: 12px; }
    .conversation-panel { border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel2); padding: 14px; min-width: 0; }
    .conversation-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 12px; }
    .conversation-meta { color: var(--muted); font-size: 11px; margin-top: 4px; overflow-wrap: anywhere; }
    .conversation-stream { display: grid; gap: 10px; max-height: 700px; overflow: auto; padding-right: 4px; }
    .message-block { border: 1px solid #1e2f45; border-radius: 8px; background: #070e1a; overflow: hidden; }
    .message-block.final { border-color: rgba(96,165,250,.45); box-shadow: 0 0 0 1px rgba(96,165,250,.1) inset; }
    .message-top { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 9px 12px; border-bottom: 1px solid #1a2a3c; background: #0a1424; }
    .message-title { color: var(--accent2); font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
    .role-dot { width: 7px; height: 7px; border-radius: 999px; background: var(--accent2); box-shadow: 0 0 0 3px rgba(45,212,191,.15); flex: 0 0 auto; }
    .message-title-row { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
    .message-tools { color: var(--muted); font-size: 10px; }
    .message-body { padding: 12px; color: #c8daf0; font-size: 13px; line-height: 1.6; }
    .message-body p { margin: 0 0 10px; }
    .message-body p:last-child { margin-bottom: 0; }
    .message-body .section-heading { color: #e8f0fc; font-weight: 750; margin-top: 14px; }
    .message-body .bullet { position: relative; padding-left: 16px; }
    .message-body .bullet::before { content: ""; position: absolute; left: 3px; top: .7em; width: 5px; height: 5px; border-radius: 999px; background: var(--accent2); }
    .message-body .final-line { border: 1px solid rgba(52,211,153,.3); background: rgba(52,211,153,.07); border-radius: 6px; padding: 9px 12px; color: #a7f3d0; font-weight: 760; }
    .thinking { border-top: 1px solid #1a2a3c; background: #04090f; }
    .thinking summary { cursor: pointer; color: var(--muted); padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
    .thinking-content { max-height: 240px; overflow: auto; padding: 0 12px 12px; color: #8aa0bc; font-size: 12px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
    .history-list { display: grid; gap: 6px; max-height: 660px; overflow: auto; padding-right: 4px; }
    .history-item { width: 100%; text-align: left; background: rgba(255,255,255,0.02); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; cursor: pointer; transition: all 0.15s; }
    .history-item:hover { background: rgba(255,255,255,0.05); border-color: var(--line2); }
    .history-item.active { border-color: var(--accent2); box-shadow: 0 0 0 1px rgba(45,212,191,.2) inset; background: rgba(45,212,191,.04); }
    .card-list { display: grid; gap: 10px; }
    .mini-card { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: var(--panel2); }

    /* ── Responsive ─────────────────────────────── */
    @media (max-width: 1100px) {
      .chart-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .chart-grid { grid-template-columns: 1fr; }
      .conversation-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      header { align-items: flex-start; flex-direction: column; }
      main { width: calc(100vw - 20px); margin-top: 12px; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      table { display: block; overflow-x: auto; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-brand">
      <div class="brand-icon">S</div>
      <div>
        <h1>Signal Arena</h1>
        <div class="sub" id="subtitle" style="margin-top:3px">TradingAgents · Live Monitor</div>
      </div>
    </div>
    <div class="row">
      <div class="live-indicator">
        <div class="live-dot"></div>
        <span class="muted" style="font-size:12px;font-weight:600">LIVE</span>
      </div>
      <button class="secondary" onclick="refreshAll()">↻ Refresh</button>
    </div>
  </header>

  <main id="appMain">
    <section class="span">
      <div class="grid" id="metrics"></div>
    </section>

    <section class="span">
      <div class="between" style="margin-bottom:14px">
        <span class="section-title">Asset Curve</span>
        <span class="sub" id="chartMeta">Waiting for history data</span>
      </div>
      <div class="chart-grid" id="portfolioCharts"></div>
    </section>

    <section class="span">
      <div class="between" style="margin-bottom:14px">
        <span class="section-title">Model Conversations</span>
        <span class="sub" id="conversationMeta">Waiting for conversation data</span>
      </div>
      <div class="conversation-grid">
        <div class="conversation-panel">
          <div class="conversation-head">
            <div>
              <div class="section-title" style="font-size:13px">Current Conversation</div>
              <div class="conversation-meta" id="currentConversationTitle">No active conversation</div>
            </div>
            <button class="secondary" onclick="loadCurrentConversation()">↻ Refresh</button>
          </div>
          <div class="conversation-stream" id="currentConversation"></div>
        </div>
        <div class="conversation-panel">
          <div class="conversation-head">
            <div>
              <div class="section-title" style="font-size:13px">Conversation History</div>
              <div class="conversation-meta">Click any entry to view full content</div>
            </div>
          </div>
          <div class="history-list" id="conversationHistory"></div>
        </div>
      </div>
    </section>

    <section>
      <div class="between" style="margin-bottom:14px">
        <span class="section-title">Account & Holdings</span>
        <span class="pill" id="authPill">loading</span>
      </div>
      <div id="portfolio"></div>
    </section>

    <section>
      <div class="between" style="margin-bottom:14px">
        <span class="section-title">Stock Selection</span>
      </div>
      <div id="stockSelection"></div>
    </section>

    <section>
      <div class="between" style="margin-bottom:14px">
        <span class="section-title">Top Movers</span>
      </div>
      <div id="movers"></div>
    </section>

    <section>
      <div class="between" style="margin-bottom:14px">
        <span class="section-title">Leaderboard</span>
      </div>
      <div id="leaderboard"></div>
    </section>

    <section class="span">
      <div class="tabs">
        <button class="tab active" onclick="showTab('history', event)">Run History</button>
        <button class="tab" onclick="showTab('analyses', event)">Analysis Results</button>
        <button class="tab" onclick="showTab('logs', event)">Loop Logs</button>
      </div>
      <div id="tab-history"></div>
      <div id="tab-analyses" class="hidden"></div>
      <div id="tab-logs" class="hidden"><pre id="cronLog"></pre></div>
    </section>
  </main>

  <script>
    let DATA = null;
    let activeTab = 'history';
    let selectedConversationId = '';

    function authHeaders() { return {'Content-Type': 'application/json'}; }
    function fmt(n) {
      if (n === null || n === undefined || Number.isNaN(Number(n))) return 'N/A';
      return Number(n).toLocaleString(undefined, {maximumFractionDigits: 2});
    }
    function num(n) {
      const value = Number(n);
      return Number.isFinite(value) ? value : null;
    }
    function pct(n) {
      const value = num(n);
      return value === null ? 'N/A' : (value * 100).toFixed(2) + '%';
    }
    function compact(n) {
      const value = num(n);
      if (value === null) return 'N/A';
      return value.toLocaleString(undefined, {notation: 'compact', maximumFractionDigits: 2});
    }
    function timeLabel(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString(undefined, {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
    }
    function esc(s) {
      return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function pill(action) {
      const a = (action || 'hold').toLowerCase();
      return `<span class="pill ${a}">${esc(a)}</span>`;
    }
    async function api(path, opts={}) {
      const res = await fetch(path, {...opts, credentials: 'same-origin', headers: {...authHeaders(), ...(opts.headers || {})}});
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }
    async function refreshAll() {
      try {
        DATA = await api('/api/summary');
        render();
      } catch (err) {
        document.getElementById('subtitle').textContent = 'Load failed: ' + String(err.message || err);
      }
    }
    function metric(label, value, sub='') {
      return `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
    }
    function chartCard({title, rows, valueOf, displayValue, lineClass, valueText, subText}) {
      const points = rows.map((row, index) => ({index, timestamp: row.timestamp, value: valueOf(row)}))
        .filter(point => point.value !== null && Number.isFinite(point.value));
      if (points.length < 2) {
        return `<div class="chart-card"><div class="chart-head"><div><div class="chart-title">${esc(title)}</div><div class="sub">Need at least 2 data points</div></div><div class="chart-value">${esc(valueText || 'N/A')}</div></div><div class="empty-chart">Curve accumulates with each refresh</div></div>`;
      }
      const width = 640, height = 158, padX = 12, padY = 16;
      let min = Math.min(...points.map(p => p.value));
      let max = Math.max(...points.map(p => p.value));
      if (min === max) {
        min -= Math.max(1, Math.abs(min) * 0.01);
        max += Math.max(1, Math.abs(max) * 0.01);
      }
      const x = i => padX + (points.length === 1 ? 0 : i * (width - padX * 2) / (points.length - 1));
      const y = v => height - padY - ((v - min) * (height - padY * 2) / (max - min));
      const d = points.map((p, i) => `${i ? 'L' : 'M'} ${x(i).toFixed(1)} ${y(p.value).toFixed(1)}`).join(' ');
      const area = `${d} L ${x(points.length - 1).toFixed(1)} ${height - padY} L ${x(0).toFixed(1)} ${height - padY} Z`;
      const first = points[0];
      const last = points[points.length - 1];
      const delta = last.value - first.value;
      const deltaText = displayValue(delta);
      return `<div class="chart-card">
        <div class="chart-head">
          <div>
            <div class="chart-title">${esc(title)}</div>
            <div class="sub">${esc(timeLabel(first.timestamp))} – ${esc(timeLabel(last.timestamp))} · ${points.length} pts</div>
          </div>
          <div style="text-align:right">
            <div class="chart-value">${esc(valueText || displayValue(last.value))}</div>
            <div class="sub">Δ ${esc(deltaText)}</div>
          </div>
        </div>
        <div class="chart-wrap">
          <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}">
            <line class="grid-line" x1="${padX}" y1="${padY}" x2="${width - padX}" y2="${padY}"></line>
            <line class="grid-line" x1="${padX}" y1="${height / 2}" x2="${width - padX}" y2="${height / 2}"></line>
            <line class="axis" x1="${padX}" y1="${height - padY}" x2="${width - padX}" y2="${height - padY}"></line>
            <path class="chart-area ${lineClass}" d="${area}"></path>
            <path class="chart-line ${lineClass}" d="${d}"></path>
            <circle cx="${x(points.length - 1).toFixed(1)}" cy="${y(last.value).toFixed(1)}" r="3.5" fill="currentColor"></circle>
          </svg>
        </div>
        <div class="between sub" style="margin-top:4px"><span>${esc(displayValue(min))}</span><span>${esc(displayValue(max))}</span></div>
      </div>`;
    }
    function renderCharts(portfolio) {
      const rows = DATA.portfolio_history || [];
      const latest = rows[rows.length - 1] || {};
      document.getElementById('chartMeta').textContent = rows.length ? `${rows.length} local snapshots · updated each minute` : 'Waiting for history data';
      document.getElementById('portfolioCharts').innerHTML = [
        chartCard({
          title: 'Total Value',
          rows,
          valueOf: row => num(row.total_value),
          displayValue: compact,
          lineClass: 'value-line',
          valueText: fmt(latest.total_value ?? portfolio.total_value),
        }),
        chartCard({
          title: 'Cash Balance',
          rows,
          valueOf: row => num(row.cash),
          displayValue: compact,
          lineClass: 'cash-line',
          valueText: fmt(latest.cash ?? portfolio.cash),
        }),
        chartCard({
          title: 'Return Rate',
          rows,
          valueOf: row => {
            const value = num(row.return_rate);
            return value === null ? null : value * 100;
          },
          displayValue: value => {
            const n = num(value);
            return n === null ? 'N/A' : n.toFixed(3) + '%';
          },
          lineClass: 'return-line',
          valueText: pct(latest.return_rate ?? portfolio.return_rate),
        }),
      ].join('');
    }
    function render() {
      const home = DATA.arena_home?.data?.data || {};
      const portfolio = home.portfolio || DATA.arena_portfolio?.data?.data?.portfolio || {};
      const loopText = DATA.loop?.active ? 'Loop running' : `Loop ${DATA.loop?.status || 'unknown'}`;
      document.getElementById('subtitle').textContent = `Updated ${DATA.generated_at} · ${loopText} · cron ${DATA.cron?.installed ? 'installed' : 'not installed'}`;
      document.getElementById('authPill').textContent = DATA.config.api_key_configured ? 'API Key OK' : 'No API Key';
      document.getElementById('authPill').className = 'pill ' + (DATA.config.api_key_configured ? 'buy' : 'sell');
      document.getElementById('metrics').innerHTML = [
        metric('Total Value', fmt(portfolio.total_value), 'Signal Arena'),
        metric('Cash', fmt(portfolio.cash), home.market_status || ''),
        metric('Return Rate', portfolio.return_rate !== undefined ? (Number(portfolio.return_rate) * 100).toFixed(2) + '%' : 'N/A', `Rank ${home.rank ?? 'N/A'} / ${home.total_participants ?? 'N/A'}`),
        metric('Last Action', DATA.latest_run?.action || 'N/A', DATA.latest_run?.symbol || DATA.latest_run?.mode || ''),
        metric('Last Trade', DATA.latest_run?.trade_status || 'N/A', DATA.latest_run?.trade_reason || (DATA.latest_run?.shares !== undefined ? `shares ${DATA.latest_run.shares}` : '')),
      ].join('');
      renderCharts(portfolio);
      renderConversationSummary();
      renderPortfolio();
      renderSelection();
      renderMovers();
      renderLeaderboard();
      renderTabs();
    }
    function statusPill(status, action='') {
      const normalized = (action || status || 'hold').toLowerCase();
      const cls = normalized.includes('fail') ? 'sell' : normalized.includes('running') ? 'hold' : normalized.includes('sell') ? 'sell' : normalized.includes('buy') ? 'buy' : 'hold';
      return `<span class="pill ${cls}">${esc(status || action || 'unknown')}</span>`;
    }
    function renderConversationSummary() {
      const current = DATA.current_conversation || {};
      const history = DATA.conversation_history || [];
      document.getElementById('conversationMeta').textContent = history.length ? `${history.length} entries · current ${current.status || 'N/A'}` : 'Full conversation saved after each round';
      renderConversationHistory(history);
      if (!selectedConversationId && current.id) {
        selectedConversationId = current.id;
        loadConversation(current.id);
      } else if (!selectedConversationId && history[0]?.id) {
        selectedConversationId = history[0].id;
        loadConversation(history[0].id);
      }
      if (!current.id && !history.length) {
        document.getElementById('currentConversationTitle').textContent = 'No conversation records';
        document.getElementById('currentConversation').innerHTML = '<div class="muted">Waiting for agent trace to be written.</div>';
      }
    }
    function renderConversationHistory(rows) {
      document.getElementById('conversationHistory').innerHTML = rows.length ? rows.map(row => {
        const active = row.id === selectedConversationId ? ' active' : '';
        const title = `${row.symbol || 'unknown'} ${row.stock_name || ''}`.trim();
        const sub = `${row.status || ''} · ${row.started_at || row.updated_at || ''}`;
        return `<button class="history-item${active}" onclick="loadConversation('${esc(row.id)}')">
          <div class="between"><strong>${esc(title)}</strong>${statusPill(row.status, row.action)}</div>
          <div class="conversation-meta">${esc(sub)}</div>
          <div class="sub">${esc(row.preview || '').slice(0,160)}</div>
        </button>`;
      }).join('') : '<div class="muted">No conversation history.</div>';
    }
    function splitModelText(content) {
      const text = String(content || '').trim();
      const marker = '</think>';
      if (!text.includes(marker)) return {draft: '', answer: text};
      const parts = text.split(marker);
      return {draft: parts.slice(0, -1).join(marker).trim(), answer: parts.at(-1).trim()};
    }
    function paragraphClass(line) {
      const trimmed = line.trim();
      if (/^FINAL TRANSACTION PROPOSAL\s*:/i.test(trimmed)) return 'final-line';
      if (/^(Rating|Ticker|Executive Summary|Investment Thesis|Action for|Key watch|Market Analyst|Portfolio Manager)\s*:?/i.test(trimmed)) return 'section-heading';
      if (/^[-*]\s+/.test(trimmed) || /^\d+\.\s+/.test(trimmed)) return 'bullet';
      return '';
    }
    function prettyText(text) {
      const normalized = String(text || '')
        .replace(/\*\*(.*?)\*\*/g, '$1')
        .replace(/[ \t]+\n/g, '\n')
        .trim();
      if (!normalized) return '<p class="muted">No content.</p>';
      const blocks = normalized.split(/\n{2,}/).map(block => block.trim()).filter(Boolean);
      return blocks.map(block => {
        const lines = block.split('\n').map(line => line.trim()).filter(Boolean);
        if (lines.length > 1 && lines.every(line => /^[-*]\s+/.test(line) || /^\d+\.\s+/.test(line))) {
          return lines.map(line => `<p class="bullet">${esc(line.replace(/^[-*]\s+/, '').replace(/^\d+\.\s+/, ''))}</p>`).join('');
        }
        const firstClass = paragraphClass(lines[0] || '');
        const clean = lines.join(' ').replace(/^[-*]\s+/, '').replace(/^\d+\.\s+/, '');
        return `<p class="${firstClass}">${esc(clean)}</p>`;
      }).join('');
    }
    function roleLabel(title) {
      const t = String(title || 'Message');
      if (t.includes('Runtime')) return 'Runtime';
      if (t.includes('Market')) return 'Market Analysis';
      if (t.includes('Social')) return 'Sentiment';
      if (t.includes('News')) return 'News Analysis';
      if (t.includes('Fundamentals')) return 'Fundamentals';
      if (t.includes('Bull')) return 'Bull Case';
      if (t.includes('Bear')) return 'Bear Case';
      if (t.includes('Risk')) return 'Risk Control';
      if (t.includes('Portfolio')) return 'Portfolio Manager';
      if (t.includes('Signal')) return 'Trade Signal';
      return t;
    }
    function renderMessage(section, index) {
      const title = section.title || 'Message';
      const split = splitModelText(section.content || '');
      const hasDraft = Boolean(split.draft);
      const isFinal = /Portfolio|Final|Signal|Processed/i.test(title);
      return `<div class="message-block ${isFinal ? 'final' : ''}">
        <div class="message-top">
          <div class="message-title-row">
            <span class="role-dot"></span>
            <span class="message-title">${esc(roleLabel(title))}</span>
          </div>
          <div class="message-tools">${esc(title)} · #${index + 1}</div>
        </div>
        <div class="message-body">${prettyText(split.answer)}</div>
        ${hasDraft ? `<details class="thinking"><summary>View reasoning draft</summary><div class="thinking-content">${esc(split.draft)}</div></details>` : ''}
      </div>`;
    }
    function renderConversation(trace) {
      selectedConversationId = trace?.id || selectedConversationId;
      const title = `${trace?.tradingagents_symbol || trace?.arena_symbol || 'unknown'} ${trace?.stock_name || ''}`.trim();
      const meta = `${trace?.status || 'unknown'} · ${trace?.started_at || ''} · ${trace?.analysts?.join?.(', ') || ''}`;
      document.getElementById('currentConversationTitle').innerHTML = `${esc(title)} ${statusPill(trace?.status, trace?.action)}<div class="conversation-meta">${esc(meta)}</div>`;
      const sections = Array.isArray(trace?.sections) ? trace.sections : [];
      document.getElementById('currentConversation').innerHTML = sections.length ? sections.map(renderMessage).join('') : '<div class="muted">Round in progress — full content shown after model returns.</div>';
      renderConversationHistory(DATA?.conversation_history || []);
    }
    async function loadConversation(id) {
      if (!id) return;
      try {
        const trace = await api('/api/conversation?id=' + encodeURIComponent(id));
        renderConversation(trace);
      } catch (err) {
        document.getElementById('currentConversation').innerHTML = `<div class="sell">Load failed: ${esc(err.message || err)}</div>`;
      }
    }
    async function loadCurrentConversation() {
      try {
        const payload = await api('/api/conversations');
        DATA.current_conversation = payload.current ? {
          id: payload.current.id,
          status: payload.current.status,
          started_at: payload.current.started_at,
          updated_at: payload.current.updated_at,
          symbol: payload.current.tradingagents_symbol || payload.current.arena_symbol,
          action: payload.current.action,
          preview: (payload.current.sections || []).map(s => s.content || '').find(Boolean) || ''
        } : {};
        DATA.conversation_history = payload.history || [];
        renderConversation(payload.current || {});
      } catch (err) {
        document.getElementById('currentConversation').innerHTML = `<div class="sell">Load failed: ${esc(err.message || err)}</div>`;
      }
    }
    function renderPortfolio() {
      const p = DATA.arena_portfolio?.data?.data || {};
      const holdings = p.holdings || [];
      let html = `<div class="sub" style="margin-bottom:10px">Agent: <strong>${esc(p.agent?.username || DATA.arena_home?.data?.data?.agent?.username || 'N/A')}</strong></div>`;
      if (!holdings.length) html += '<p class="muted">No positions.</p>';
      else html += `<table><thead><tr><th>Symbol</th><th>Name</th><th>Shares</th><th>Value</th><th>P&amp;L</th></tr></thead><tbody>${holdings.map(h => {
        const pnl = h.pnl ?? h.profit_loss;
        const rate = h.profit_rate !== undefined ? ` (${pct(h.profit_rate)})` : '';
        const cls = Number(pnl) >= 0 ? 'buy' : 'sell';
        return `<tr><td style="font-weight:600">${esc(h.symbol)}</td><td>${esc(h.name)}</td><td>${fmt(h.shares)}</td><td>${fmt(h.market_value)}</td><td class="${cls}">${fmt(pnl)}${esc(rate)}</td></tr>`;
      }).join('')}</tbody></table>`;
      document.getElementById('portfolio').innerHTML = html;
    }
    function renderMovers() {
      const movers = DATA.top_movers?.data?.data?.movers || {};
      document.getElementById('movers').innerHTML = Object.entries(movers).map(([market, rows]) => {
        return `<h3>${esc(market)}</h3><table><tbody>${rows.slice(0,5).map(r => `<tr><td style="font-weight:600">${esc(r.symbol)}</td><td>${esc(r.name)}</td><td>${fmt(r.price)}</td><td class="${r.change_rate >= 0 ? 'buy' : 'sell'}" style="font-weight:700">${(r.change_rate*100).toFixed(2)}%</td></tr>`).join('')}</tbody></table>`;
      }).join('');
    }
    function renderLeaderboard() {
      const rows = DATA.leaderboard?.data?.data?.leaderboard || [];
      document.getElementById('leaderboard').innerHTML = `<table><thead><tr><th>Rank</th><th>Agent</th><th>Total Value</th><th>Return</th></tr></thead><tbody>${rows.slice(0,8).map(r => `<tr><td style="font-weight:700;color:var(--accent)">#${r.rank}</td><td>${esc(r.agent?.username || r.agent?.nickname)}</td><td>${fmt(r.total_value)}</td><td style="font-weight:700" class="${r.return_rate>=0?'buy':'sell'}">${(r.return_rate*100).toFixed(2)}%</td></tr>`).join('')}</tbody></table>`;
    }
    function renderSelection() {
      const selection = DATA.stock_selection || DATA.latest_run?.stock_selection;
      if (!selection) {
        document.getElementById('stockSelection').innerHTML = '<span class="muted">Candidates will appear after next auto-selection round.</span>';
        return;
      }
      const selected = selection.selected || {};
      const rows = selection.top_candidates || [];
      document.getElementById('stockSelection').innerHTML = `<div class="mini-card" style="margin-bottom:10px">
        <div class="between"><strong>${esc(selected.symbol || 'N/A')} ${esc(selected.name || '')}</strong><span class="pill buy">${esc(String(selected.score ?? 'N/A'))}</span></div>
        <div class="sub" style="margin-top:4px">${esc(selection.strategy || selection.mode || '')} · ${esc(selection.timestamp || '')}</div>
        <div class="sub" style="margin-top:3px">${esc([...(selected.reasons || []), ...(selected.penalties || [])].join(' · '))}</div>
      </div>
      <table><thead><tr><th>Candidate</th><th>Score</th><th>Change</th><th>Reasons</th></tr></thead><tbody>${rows.slice(0,6).map(row => `<tr><td style="font-weight:600">${esc(row.symbol)} <span class="muted">${esc(row.name || '')}</span></td><td>${fmt(row.score)}</td><td class="${row.change_rate >= 0 ? 'buy' : 'sell'}" style="font-weight:700">${(Number(row.change_rate || 0) * 100).toFixed(2)}%</td><td class="muted">${esc([...(row.reasons || []), ...(row.penalties || [])].join(' · ')).slice(0,180)}</td></tr>`).join('')}</tbody></table>`;
    }
    function renderTabs() {
      renderHistory();
      renderAnalyses();
      document.getElementById('cronLog').textContent = DATA.loop_log_tail || DATA.cron_log_tail || 'No loop log.';
    }
    function renderHistory() {
      const rows = (DATA.history || []).slice().reverse();
      document.getElementById('tab-history').innerHTML = `<table><thead><tr><th>Timestamp</th><th>Mode</th><th>Symbol</th><th>Action</th><th>Trade</th><th>Reason</th><th>Signal Preview</th></tr></thead><tbody>${rows.map(r => `<tr><td style="white-space:nowrap;font-size:12px">${esc(r.timestamp || '')}</td><td>${esc(r.mode)}</td><td style="font-weight:600">${esc(r.symbol || '')}</td><td>${pill(r.action)}</td><td>${esc(r.execute_trade ? r.trade_status || 'live' : 'dry-run')}</td><td class="muted">${esc(r.trade_reason || '')}</td><td class="muted" style="font-size:12px">${esc(r.signal_preview || '').slice(0,220)}</td></tr>`).join('')}</tbody></table>`;
    }
    function renderAnalyses() {
      const rows = DATA.analysis_logs || [];
      document.getElementById('tab-analyses').innerHTML = `<div class="card-list">${rows.map(r => `<div class="mini-card"><div class="between"><div><strong>${esc(r.symbol)}</strong> <span class="muted">${esc(r.trade_date)}</span> ${pill(r.action)}</div><button class="secondary" onclick="loadAnalysis('${esc(r.path)}')">View</button></div><div class="sub" style="margin-top:4px">${esc(r.path)} · ${esc(r.mtime_iso)}</div><p class="muted" style="margin:8px 0 0;font-size:12px">${esc(r.final_preview).slice(0,380)}</p><div id="analysis-${btoa(r.path).replaceAll('=','')}"></div></div>`).join('')}</div>`;
    }
    async function loadAnalysis(path) {
      const data = await api('/api/analysis?path=' + encodeURIComponent(path));
      const id = 'analysis-' + btoa(path).replaceAll('=','');
      document.getElementById(id).innerHTML = `<div style="margin-top:12px"><h3>Final Decision</h3><pre>${esc(data.final_trade_decision || '')}</pre><h3>Market Report</h3><pre>${esc(data.market_report || '')}</pre></div>`;
    }
    function showTab(name, event) {
      activeTab = name;
      for (const tab of ['history','analyses','logs']) {
        document.getElementById('tab-' + tab).classList.toggle('hidden', tab !== name);
      }
      for (const btn of document.querySelectorAll('.tab')) btn.classList.remove('active');
      if (event?.target) event.target.classList.add('active');
    }
    refreshAll();
    setInterval(refreshAll, 15000);
  </script>
</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SignalArenaDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def token_ok(self) -> bool:
        return True  # Token verification disabled — running on private Tailscale network

    def login_ok(self, supplied_token: str) -> bool:
        return True  # Token verification disabled

    def send_json(self, payload: Any, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(
        self,
        text: str,
        status: int = 200,
        content_type: str = "text/plain; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def require_token(self) -> bool:
        if self.token_ok():
            return True
        self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            headers = {}
            query_token = parse_qs(parsed.query).get("token", [""])[0]
            if query_token and self.login_ok(query_token):
                headers["Set-Cookie"] = f"signal_dashboard_token={query_token}; Path=/; SameSite=Lax"
            self.send_text(HTML_PAGE, content_type="text/html; charset=utf-8", extra_headers=headers)
            return
        if not self.require_token():
            return
        try:
            if parsed.path == "/api/summary":
                self.send_json(dashboard_summary())
            elif parsed.path == "/api/jobs":
                self.send_json({"jobs": list_jobs()})
            elif parsed.path == "/api/analysis":
                path = parse_qs(parsed.query).get("path", [""])[0]
                self.send_json(load_analysis(path))
            elif parsed.path == "/api/conversations":
                self.send_json({"current": load_current_conversation(), "history": list_conversations()})
            elif parsed.path == "/api/conversation":
                trace_id = parse_qs(parsed.query).get("id", [""])[0]
                self.send_json(load_conversation(trace_id))
            elif parsed.path == "/api/log":
                job_id = parse_qs(parsed.query).get("job", [""])[0]
                if job_id:
                    self.send_text(tail_text(JOBS_DIR / f"{job_id}.log", 250))
                else:
                    self.send_text(tail_text(CRON_LOG_PATH, 250))
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_HEAD(self) -> None:
        if urlparse(self.path).path == "/" or self.token_ok():
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
        else:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            supplied_token = str(payload.get("token", ""))
            if self.login_ok(supplied_token):
                self.send_json(
                    {"ok": True},
                    extra_headers={"Set-Cookie": f"signal_dashboard_token={supplied_token}; Path=/; SameSite=Lax"},
                )
            else:
                self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        if not self.require_token():
            return
        try:
            if parsed.path == "/api/run":
                self.send_json(start_job(payload), HTTPStatus.ACCEPTED)
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    host = os.getenv("SIGNAL_DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("SIGNAL_DASHBOARD_PORT", "8787"))
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Dashboard listening on http://{host}:{port}")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
