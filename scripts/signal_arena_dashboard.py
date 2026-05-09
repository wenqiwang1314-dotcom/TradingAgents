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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    arena_to_tradingagents_symbol,
    load_dotenv,
)

RESULTS_DIR = PROJECT_ROOT / "results"
SIGNAL_DIR = RESULTS_DIR / "signal_arena"
RUNS_PATH = SIGNAL_DIR / "runs.jsonl"
DAILY_RUNS_PATH = SIGNAL_DIR / "daily_runs.jsonl"
LAST_RUN_PATH = SIGNAL_DIR / "last_run.json"
CRON_LOG_PATH = SIGNAL_DIR / "cron.log"
LOOP_LOG_PATH = SIGNAL_DIR / "loop.log"
PORTFOLIO_HISTORY_PATH = SIGNAL_DIR / "portfolio_history.jsonl"
PORTFOLIO_CACHE_PATH = SIGNAL_DIR / "portfolio_cache.json"
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


def text_section(title: str, content: Any) -> dict[str, str]:
    return {"title": title, "content": str(content or "")}


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


def compact_text(value: Any, limit: int = 520) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def first_number(*values: Any) -> float | None:
    for value in values:
        number = as_float(value)
        if number is not None:
            return number
    return None


def normalized_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip()
    if not text:
        return ""
    return arena_to_tradingagents_symbol(text)


def normalized_action(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"buy", "sell", "hold"}:
        return text
    return action_from_signal(str(value or "")) or "hold"


def unwrap_api_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def read_portfolio_history(limit: int = 500) -> list[dict[str, Any]]:
    return read_jsonl_tail(PORTFOLIO_HISTORY_PATH, limit)


def cache_portfolio_result(result: dict[str, Any]) -> None:
    payload = unwrap_api_data(result)
    holdings = payload.get("holdings") if isinstance(payload, dict) else None
    portfolio = payload.get("portfolio") if isinstance(payload, dict) else None
    if not isinstance(portfolio, dict) or not isinstance(holdings, list):
        return
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_CACHE_PATH.write_text(
        json.dumps(
            {
                "cached_at": now_iso(),
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def read_cached_portfolio_result(max_age_hours: int = 36) -> dict[str, Any] | None:
    cached = read_json(PORTFOLIO_CACHE_PATH)
    if not isinstance(cached, dict):
        return None
    cached_at = cached.get("cached_at")
    result = cached.get("result")
    if not isinstance(result, dict):
        return None
    try:
        cached_dt = datetime.fromisoformat(str(cached_at).replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - cached_dt.astimezone(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None
    if age_hours > max_age_hours:
        return None
    return {
        **result,
        "from_cache": True,
        "cached_at": cached_at,
        "cache_age_hours": round(age_hours, 2),
    }


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
        "SIGNAL_ARENA_USE_GPT_PREOPEN",
        "SIGNAL_ARENA_GPT_PREOPEN_MAX_AGE_HOURS",
        "SIGNAL_ARENA_GPT_PREOPEN_RECENT_HOURS",
        "TRADINGAGENTS_ANALYSTS",
        "TRADINGAGENTS_AGENT_TIMEOUT_SECONDS",
        "TRADINGAGENTS_BACKEND_URL",
        "TRADINGAGENTS_DEEP_MODEL",
        "TRADINGAGENTS_QUICK_MODEL",
        "SIGNAL_DASHBOARD_HOST",
        "SIGNAL_DASHBOARD_PORT",
        "SIGNAL_DASHBOARD_ALLOW_TRADES",
        "SIGNAL_DASHBOARD_ARENA_TIMEOUT",
        "SIGNAL_DASHBOARD_HOME_TIMEOUT",
        "SIGNAL_DASHBOARD_PORTFOLIO_TIMEOUT",
        "SIGNAL_DASHBOARD_PORTFOLIO_CACHE_MAX_AGE_HOURS",
        "SIGNAL_DASHBOARD_SNAPSHOTS_TIMEOUT",
        "SIGNAL_DASHBOARD_TRADES_TIMEOUT",
        "SIGNAL_DASHBOARD_LEADERBOARD_TIMEOUT",
    ]
    return {
        "api_key_configured": bool(os.getenv(API_KEY_ENV)),
        "base_url": BASE_URL,
        **{key: os.getenv(key) for key in keys if os.getenv(key) is not None},
    }


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def client(timeout: int | None = None) -> SignalArenaClient:
    default_timeout = env_int("SIGNAL_DASHBOARD_ARENA_TIMEOUT", 10)
    return SignalArenaClient(BASE_URL, os.getenv(API_KEY_ENV), timeout=timeout or default_timeout)


def try_call(name: str, func: Any) -> dict[str, Any]:
    started = time.monotonic()
    try:
        return {"ok": True, "data": func(), "elapsed_sec": round(time.monotonic() - started, 3)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed_sec": round(time.monotonic() - started, 3)}


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


def summarize_preopen_pick(pick: dict[str, Any]) -> dict[str, Any]:
    scorecard = pick.get("scorecard") if isinstance(pick.get("scorecard"), dict) else {}
    selector_pick = pick.get("selector_pick") if isinstance(pick.get("selector_pick"), dict) else {}
    evidence = selector_pick.get("evidence") if isinstance(selector_pick.get("evidence"), list) else []
    return {
        "symbol": scorecard.get("symbol") or selector_pick.get("symbol") or pick.get("symbol"),
        "name": scorecard.get("name"),
        "score": scorecard.get("score"),
        "change_rate": scorecard.get("change_rate"),
        "volume": scorecard.get("volume"),
        "reason": selector_pick.get("reason"),
        "confidence": selector_pick.get("confidence"),
        "risk": selector_pick.get("risk"),
        "evidence": evidence[:3],
        "sources": (selector_pick.get("sources") or [])[:4],
    }


def latest_preopen_selections() -> dict[str, Any]:
    markets: dict[str, Any] = {}
    latest_timestamp = ""
    for row in reversed(read_jsonl_tail(DAILY_RUNS_PATH, 200)):
        if row.get("mode") != "preopen":
            continue
        selected = row.get("selected") if isinstance(row.get("selected"), dict) else {}
        selector = row.get("selector") if isinstance(row.get("selector"), dict) else {}
        selector_summary = {
            key: selector.get(key)
            for key in ("provider", "model", "web_search", "search_context_size", "max_search_calls")
            if key in selector
        }
        for market, picks in selected.items():
            if market in markets or not isinstance(picks, list):
                continue
            markets[market] = {
                "timestamp": row.get("timestamp"),
                "selector": selector_summary,
                "picks": [summarize_preopen_pick(pick) for pick in picks if isinstance(pick, dict)],
                "analyses": [
                    {
                        "symbol": item.get("tradingagents_symbol"),
                        "rating": item.get("final_trade_rating"),
                        "action": item.get("final_trade_action"),
                        "shares": item.get("shares"),
                        "execute_trade": item.get("execute_trade"),
                    }
                    for item in (row.get("analyses") or [])
                    if isinstance(item, dict) and item.get("market") == market
                ],
            }
            if str(row.get("timestamp") or "") > latest_timestamp:
                latest_timestamp = str(row.get("timestamp") or "")
        if {"CN", "US", "HK"}.issubset(set(markets)):
            break
    return {"timestamp": latest_timestamp, "markets": markets}


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


def trade_data_from_payload(trade: Any) -> dict[str, Any]:
    if not isinstance(trade, dict):
        return {}
    data = unwrap_api_data(trade)
    if data:
        return data
    return trade


def trade_status_from_payload(trade: Any, execute_trade: Any = None) -> tuple[str, str]:
    if isinstance(trade, dict):
        if trade.get("skipped"):
            return "skipped", str(trade.get("reason") or "")
        data = trade_data_from_payload(trade)
        status = first_value(data.get("status"), trade.get("status"))
        message = first_value(data.get("message"), trade.get("message"), data.get("reason"), trade.get("reason"))
        if status:
            return str(status), str(message or "")
        if trade.get("ok") is False or trade.get("success") is False:
            return "failed", str(first_value(trade.get("error"), trade.get("message"), data.get("message")) or "")
        if data:
            return "submitted", str(message or "")
    if execute_trade:
        return "pending", ""
    return "dry-run", ""


def source_record_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return uuid.uuid5(uuid.NAMESPACE_URL, raw).hex[:16]


def decision_record(
    *,
    source: str,
    source_label: str,
    symbol: Any,
    arena_symbol: Any = None,
    name: Any = None,
    market: Any = None,
    action: Any = None,
    selected_at: Any = None,
    decided_at: Any = None,
    trade_date: Any = None,
    shares: Any = None,
    price: Any = None,
    price_kind: str = "参考价",
    currency: Any = None,
    status: str = "",
    status_detail: str = "",
    score: Any = None,
    change_rate: Any = None,
    volume: Any = None,
    confidence: Any = None,
    reason: Any = None,
    risk: Any = None,
    evidence: Any = None,
    signal_preview: Any = None,
    trace_id: Any = None,
    trade_id: Any = None,
    source_path: Any = None,
    execute_trade: Any = None,
) -> dict[str, Any]:
    display_symbol = normalized_symbol(symbol or arena_symbol)
    raw_symbol = str(arena_symbol or symbol or "").strip()
    normalized = normalized_action(action)
    numeric_price = as_float(price)
    numeric_shares = as_int(shares)
    timestamp = first_value(selected_at, decided_at, trade_date)
    return {
        "id": source_record_id(source, display_symbol, raw_symbol, normalized, timestamp, numeric_shares, numeric_price, trade_id, trace_id),
        "source": source,
        "source_label": source_label,
        "symbol": display_symbol,
        "arena_symbol": raw_symbol,
        "name": str(name or ""),
        "market": str(market or ""),
        "action": normalized,
        "selected_at": str(selected_at or ""),
        "decided_at": str(decided_at or ""),
        "trade_date": str(trade_date or ""),
        "shares": numeric_shares,
        "price": numeric_price,
        "price_kind": price_kind,
        "currency": str(currency or ""),
        "status": status,
        "status_detail": compact_text(status_detail, 240),
        "score": as_float(score),
        "change_rate": as_float(change_rate),
        "volume": as_float(volume),
        "confidence": as_float(confidence),
        "reason": compact_text(reason, 620),
        "risk": compact_text(risk, 420),
        "evidence": [compact_text(item, 220) for item in evidence[:4]] if isinstance(evidence, list) else [],
        "signal_preview": compact_text(signal_preview, 900),
        "trace_id": str(trace_id or ""),
        "trade_id": str(trade_id or ""),
        "source_path": str(source_path or ""),
        "execute_trade": bool(execute_trade),
    }


def joined_reasons(*items: Any) -> str:
    values: list[str] = []
    for item in items:
        if isinstance(item, list):
            values.extend(str(value) for value in item if value)
        elif item:
            values.append(str(item))
    return " · ".join(dict.fromkeys(values))


def decision_records_from_runs(limit: int = 180) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for run in read_jsonl_tail(RUNS_PATH, limit):
        if run.get("mode") != "agent":
            continue
        stock = run.get("stock") if isinstance(run.get("stock"), dict) else {}
        selection = run.get("stock_selection") if isinstance(run.get("stock_selection"), dict) else {}
        selected = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
        trade = run.get("trade") if isinstance(run.get("trade"), dict) else {}
        trade_data = trade_data_from_payload(trade)
        status, status_detail = trade_status_from_payload(trade, run.get("execute_trade"))
        price = first_number(
            trade_data.get("executed_price"),
            trade_data.get("filled_price"),
            trade_data.get("avg_price"),
            trade_data.get("price"),
            trade_data.get("estimated_price"),
            selected.get("price"),
            stock.get("price"),
        )
        price_kind = "成交价" if first_number(trade_data.get("executed_price"), trade_data.get("filled_price"), trade_data.get("avg_price")) is not None else "决策参考价"
        records.append(
            decision_record(
                source="agent_run",
                source_label="TradingAgents 深度分析",
                symbol=run.get("tradingagents_symbol") or stock.get("symbol") or selected.get("symbol"),
                arena_symbol=stock.get("symbol") or selected.get("symbol"),
                name=stock.get("name") or selected.get("name"),
                market=run.get("market") or selected.get("market"),
                action=run.get("action") or run.get("final_trade_action") or f"{run.get('signal', '')}\n{run.get('final_trade_decision', '')}",
                selected_at=selection.get("timestamp") or run.get("timestamp"),
                decided_at=run.get("timestamp"),
                trade_date=run.get("trade_date") or stock.get("trade_date"),
                shares=first_value(run.get("shares"), trade_data.get("shares")),
                price=price,
                price_kind=price_kind,
                currency=trade_data.get("currency"),
                status=status,
                status_detail=status_detail,
                score=selected.get("score"),
                change_rate=first_value(selected.get("change_rate"), stock.get("change_rate")),
                volume=first_value(selected.get("volume"), stock.get("volume")),
                reason=joined_reasons(selected.get("reasons"), selected.get("penalties")),
                signal_preview=run.get("final_trade_decision") or run.get("signal"),
                trace_id=run.get("conversation_trace_id"),
                trade_id=trade_data.get("trade_id"),
                source_path=str(RUNS_PATH.relative_to(PROJECT_ROOT)),
                execute_trade=run.get("execute_trade"),
            )
        )
    return records


def decision_records_from_daily_runs(limit: int = 180) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in read_jsonl_tail(DAILY_RUNS_PATH, limit):
        mode = row.get("mode")
        row_timestamp = row.get("timestamp")
        for order in row.get("entry_orders") or []:
            if not isinstance(order, dict):
                continue
            scorecard = order.get("scorecard") if isinstance(order.get("scorecard"), dict) else {}
            selector_pick = order.get("selector_pick") if isinstance(order.get("selector_pick"), dict) else {}
            trade = order.get("trade") if isinstance(order.get("trade"), dict) else {}
            trade_data = trade_data_from_payload(trade)
            status, status_detail = trade_status_from_payload(trade, order.get("execute_trade"))
            price = first_number(
                trade_data.get("executed_price"),
                trade_data.get("filled_price"),
                trade_data.get("avg_price"),
                trade_data.get("price"),
                trade_data.get("estimated_price"),
                scorecard.get("price"),
            )
            records.append(
                decision_record(
                    source="preopen_order",
                    source_label="GPT 盘前选股/下单",
                    symbol=order.get("symbol") or scorecard.get("symbol"),
                    arena_symbol=order.get("symbol") or scorecard.get("symbol"),
                    name=order.get("name") or scorecard.get("name"),
                    market=order.get("market") or scorecard.get("market") or (row.get("markets") or [""])[0],
                    action=order.get("action"),
                    selected_at=row_timestamp,
                    decided_at=first_value(trade_data.get("created_at"), trade_data.get("submitted_at"), row_timestamp),
                    trade_date=scorecard.get("trade_date"),
                    shares=first_value(order.get("shares"), trade_data.get("shares")),
                    price=price,
                    price_kind="成交价" if first_number(trade_data.get("executed_price"), trade_data.get("filled_price"), trade_data.get("avg_price")) is not None else "下单参考价",
                    currency=trade_data.get("currency"),
                    status=status,
                    status_detail=status_detail,
                    score=scorecard.get("score"),
                    change_rate=scorecard.get("change_rate"),
                    volume=scorecard.get("volume"),
                    confidence=selector_pick.get("confidence"),
                    reason=selector_pick.get("reason") or joined_reasons(scorecard.get("reasons"), scorecard.get("penalties")),
                    risk=selector_pick.get("risk"),
                    evidence=selector_pick.get("evidence") if isinstance(selector_pick.get("evidence"), list) else [],
                    signal_preview=selector_pick.get("reason"),
                    trade_id=trade_data.get("trade_id"),
                    source_path=str(DAILY_RUNS_PATH.relative_to(PROJECT_ROOT)),
                    execute_trade=order.get("execute_trade"),
                )
            )
        for analysis in row.get("analyses") or []:
            if not isinstance(analysis, dict):
                continue
            stock = analysis.get("stock") if isinstance(analysis.get("stock"), dict) else {}
            scorecard = analysis.get("scorecard") if isinstance(analysis.get("scorecard"), dict) else {}
            selector_pick = analysis.get("selector_pick") if isinstance(analysis.get("selector_pick"), dict) else {}
            trade = analysis.get("trade") if isinstance(analysis.get("trade"), dict) else {}
            trade_data = trade_data_from_payload(trade)
            status, status_detail = trade_status_from_payload(trade, analysis.get("execute_trade"))
            records.append(
                decision_record(
                    source="daily_agent_analysis",
                    source_label="Daily TradingAgents 分析",
                    symbol=analysis.get("tradingagents_symbol") or stock.get("symbol") or scorecard.get("symbol"),
                    arena_symbol=stock.get("symbol") or scorecard.get("symbol"),
                    name=stock.get("name") or scorecard.get("name"),
                    market=analysis.get("market") or row.get("markets"),
                    action=analysis.get("final_trade_action") or f"{analysis.get('final_trade_rating', '')}\n{analysis.get('final_trade_decision', '')}",
                    selected_at=row_timestamp,
                    decided_at=first_value(analysis.get("timestamp"), row_timestamp),
                    trade_date=analysis.get("trade_date") or stock.get("trade_date"),
                    shares=first_value(analysis.get("shares"), trade_data.get("shares")),
                    price=first_number(trade_data.get("executed_price"), trade_data.get("estimated_price"), stock.get("price"), scorecard.get("price")),
                    price_kind="决策参考价",
                    currency=trade_data.get("currency"),
                    status=status,
                    status_detail=status_detail,
                    score=scorecard.get("score"),
                    change_rate=first_value(stock.get("change_rate"), scorecard.get("change_rate")),
                    volume=first_value(stock.get("volume"), scorecard.get("volume")),
                    confidence=selector_pick.get("confidence"),
                    reason=selector_pick.get("reason") or joined_reasons(scorecard.get("reasons"), scorecard.get("penalties")),
                    risk=selector_pick.get("risk"),
                    evidence=selector_pick.get("evidence") if isinstance(selector_pick.get("evidence"), list) else [],
                    signal_preview=analysis.get("final_trade_decision") or analysis.get("signal"),
                    trace_id=analysis.get("conversation_trace_id"),
                    trade_id=trade_data.get("trade_id"),
                    source_path=str(DAILY_RUNS_PATH.relative_to(PROJECT_ROOT)),
                    execute_trade=analysis.get("execute_trade"),
                )
            )
        for close_row in row.get("closed") or []:
            if not isinstance(close_row, dict):
                continue
            trade = close_row.get("trade") if isinstance(close_row.get("trade"), dict) else {}
            trade_data = trade_data_from_payload(trade)
            status, status_detail = trade_status_from_payload(trade, close_row.get("execute_trade"))
            records.append(
                decision_record(
                    source="close_out",
                    source_label="收盘平仓计划",
                    symbol=close_row.get("symbol"),
                    arena_symbol=close_row.get("symbol"),
                    market=close_row.get("market") or (row.get("markets") or [""])[0],
                    action="sell",
                    selected_at=row_timestamp,
                    decided_at=first_value(trade_data.get("created_at"), row_timestamp),
                    shares=first_value(close_row.get("shares"), trade_data.get("shares")),
                    price=first_number(trade_data.get("executed_price"), trade_data.get("estimated_price"), trade_data.get("price")),
                    price_kind="成交价" if first_number(trade_data.get("executed_price"), trade_data.get("filled_price"), trade_data.get("avg_price")) is not None else "平仓参考价",
                    currency=trade_data.get("currency"),
                    status=status,
                    status_detail=status_detail,
                    reason="Daily end-of-session close-out",
                    trade_id=trade_data.get("trade_id"),
                    source_path=str(DAILY_RUNS_PATH.relative_to(PROJECT_ROOT)),
                    execute_trade=close_row.get("execute_trade"),
                )
            )
    return records


def extract_trade_rows_from_api(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    payload = unwrap_api_data(result)
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("trades", "orders", "records", "rows", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
    return [row for row in candidates if isinstance(row, dict)]


def decision_records_from_arena_trades(result: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for trade in extract_trade_rows_from_api(result):
        status, status_detail = trade_status_from_payload(trade, True)
        price = first_number(
            trade.get("executed_price"),
            trade.get("filled_price"),
            trade.get("avg_price"),
            trade.get("price"),
            trade.get("estimated_price"),
            trade.get("reference_price"),
        )
        records.append(
            decision_record(
                source="arena_trade",
                source_label="Signal Arena 成交/订单",
                symbol=trade.get("symbol"),
                arena_symbol=trade.get("symbol"),
                name=trade.get("name"),
                market=trade.get("market"),
                action=trade.get("action"),
                selected_at=first_value(trade.get("created_at"), trade.get("submitted_at"), trade.get("timestamp")),
                decided_at=first_value(trade.get("executed_at"), trade.get("settled_at"), trade.get("updated_at"), trade.get("created_at"), trade.get("timestamp")),
                trade_date=trade.get("trade_date"),
                shares=first_value(trade.get("shares"), trade.get("quantity")),
                price=price,
                price_kind="成交价" if first_number(trade.get("executed_price"), trade.get("filled_price"), trade.get("avg_price")) is not None else "订单参考价",
                currency=trade.get("currency"),
                status=status,
                status_detail=status_detail,
                reason=first_value(trade.get("reason"), trade.get("message")),
                trade_id=trade.get("trade_id") or trade.get("id"),
                source_path="GET /api/v1/arena/trades",
                execute_trade=True,
            )
        )
    return records


def decision_records_from_analysis_logs(existing_keys: set[tuple[str, str, str]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for log in list_analysis_logs(160):
        action = normalized_action(log.get("action"))
        key = (normalized_symbol(log.get("symbol")), str(log.get("trade_date") or ""), action)
        if key in existing_keys and action != "hold":
            continue
        records.append(
            decision_record(
                source="analysis_log",
                source_label="Full State Log",
                symbol=log.get("symbol"),
                action=action,
                selected_at=log.get("trade_date") or log.get("mtime_iso"),
                decided_at=log.get("mtime_iso"),
                trade_date=log.get("trade_date"),
                shares=0 if action == "hold" else None,
                status="analysis-only",
                reason="历史 full_states_log 决策记录",
                signal_preview=log.get("final_preview"),
                source_path=log.get("path"),
            )
        )
    return records


def build_decision_timeline(arena_trades_result: Any = None) -> dict[str, Any]:
    records = decision_records_from_runs()
    records.extend(decision_records_from_daily_runs())
    records.extend(decision_records_from_arena_trades(arena_trades_result))
    existing_keys = {
        (record.get("symbol") or "", record.get("trade_date") or "", record.get("action") or "")
        for record in records
    }
    records.extend(decision_records_from_analysis_logs(existing_keys))

    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        if not record.get("symbol"):
            continue
        trade_id = record.get("trade_id")
        if trade_id:
            key = f"trade:{trade_id}"
        else:
            key = "|".join(
                str(record.get(name) or "")
                for name in ("source", "symbol", "action", "selected_at", "decided_at", "shares", "price", "trace_id")
            )
        if key not in deduped:
            deduped[key] = record

    sorted_records = sorted(
        deduped.values(),
        key=lambda row: first_value(row.get("selected_at"), row.get("decided_at"), row.get("trade_date")) or "",
    )[-600:]
    symbols: dict[str, dict[str, Any]] = {}
    for record in sorted_records:
        symbol = record.get("symbol") or ""
        item = symbols.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": record.get("name") or "",
                "market": record.get("market") or "",
                "count": 0,
                "priced_count": 0,
                "latest_at": "",
                "latest_price": None,
                "actions": {"buy": 0, "sell": 0, "hold": 0},
            },
        )
        item["count"] += 1
        action = record.get("action")
        if action in item["actions"]:
            item["actions"][action] += 1
        if record.get("price") is not None:
            item["priced_count"] += 1
            item["latest_price"] = record.get("price")
        latest_at = first_value(record.get("selected_at"), record.get("decided_at"), record.get("trade_date")) or ""
        if latest_at and str(latest_at) >= str(item["latest_at"]):
            item["latest_at"] = str(latest_at)
            if record.get("name"):
                item["name"] = record.get("name")
            if record.get("market"):
                item["market"] = record.get("market")
    symbol_rows = sorted(symbols.values(), key=lambda item: item.get("latest_at") or "", reverse=True)
    return {
        "records": sorted_records,
        "symbols": symbol_rows,
        "generated_at": now_iso(),
    }


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


def enrich_running_conversation(trace: dict[str, Any]) -> dict[str, Any]:
    sections = trace.get("sections") if isinstance(trace.get("sections"), list) else []
    if len(sections) > 1:
        return trace
    runtime = runtime_conversation_from_process()
    if not runtime:
        return trace
    runtime_sections = runtime.get("sections") if isinstance(runtime.get("sections"), list) else []
    note = text_section(
        "Model Output Status",
        "当前轮仍在运行中，TradingAgents 只先写入启动信息。各分析师报告、投资辩论和组合经理结论会在模型返回后一次性写入。\n\n"
        "原始 <think>/<reasoning> 隐藏推理会被清理；看板展示的是可审阅的分析报告和最终投资论证，而不是模型草稿。",
    )
    return {
        **trace,
        "sections": sections + [note] + runtime_sections,
    }


def load_current_conversation() -> dict[str, Any] | None:
    trace = read_json(CURRENT_CONVERSATION_PATH)
    if isinstance(trace, dict) and trace.get("status") == "running":
        return enrich_running_conversation(trace)
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
    if data.get("status") == "running":
        return enrich_running_conversation(data)
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
        "preopen_selection": latest_preopen_selections(),
        "current_conversation": conversation_summary(load_current_conversation() or {}),
        "conversation_history": list_conversations(30),
        "cron_log_tail": tail_text(CRON_LOG_PATH, 100),
        "loop_log_tail": tail_text(LOOP_LOG_PATH, 120),
        "jobs": list_jobs(),
    }
    has_api_key = bool(os.getenv(API_KEY_ENV))
    remote_calls: dict[str, Any] = {
        "top_movers": client(timeout=10).top_movers,
        "stocks": lambda: client(timeout=12).stocks(market, 8),
        "leaderboard": client(timeout=env_int("SIGNAL_DASHBOARD_LEADERBOARD_TIMEOUT", 20)).leaderboard,
    }
    if has_api_key:
        remote_calls.update(
            {
                "arena_home": client(timeout=env_int("SIGNAL_DASHBOARD_HOME_TIMEOUT", 8)).home,
                "arena_portfolio": client(timeout=env_int("SIGNAL_DASHBOARD_PORTFOLIO_TIMEOUT", 20)).portfolio,
                "arena_snapshots": client(timeout=env_int("SIGNAL_DASHBOARD_SNAPSHOTS_TIMEOUT", 30)).snapshots,
                "arena_trades": client(timeout=env_int("SIGNAL_DASHBOARD_TRADES_TIMEOUT", 20)).trades,
            }
        )
    else:
        summary["arena_home"] = {"ok": False, "error": "No API key"}
        summary["arena_portfolio"] = {"ok": False, "error": "No API key"}
        summary["arena_snapshots"] = {"ok": False, "error": "No API key"}
        summary["arena_trades"] = {"ok": False, "error": "No API key"}

    if remote_calls:
        with ThreadPoolExecutor(max_workers=min(6, len(remote_calls))) as executor:
            futures = {
                executor.submit(try_call, name, func): name
                for name, func in remote_calls.items()
            }
            for future in as_completed(futures):
                summary[futures[future]] = future.result()

    if summary.get("arena_portfolio", {}).get("ok"):
        cache_portfolio_result(summary["arena_portfolio"])
    else:
        cached_portfolio = read_cached_portfolio_result(
            env_int("SIGNAL_DASHBOARD_PORTFOLIO_CACHE_MAX_AGE_HOURS", 36)
        )
        if cached_portfolio:
            live_error = summary.get("arena_portfolio", {}).get("error")
            summary["arena_portfolio"] = {
                **cached_portfolio,
                "ok": False,
                "live_error": live_error,
                "error": live_error or "Using cached portfolio because live portfolio API is unavailable.",
            }

    summary["portfolio_history"] = merge_live_portfolio_snapshot(
        snapshot_history(summary["arena_snapshots"]),
        current_portfolio_snapshot(summary["arena_home"], summary["arena_portfolio"]),
    )
    if not summary["portfolio_history"] and has_api_key:
        summary["portfolio_history"] = record_portfolio_history(summary["arena_home"], summary["arena_portfolio"])
    summary["decision_timeline"] = build_decision_timeline(summary.get("arena_trades"))
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

    /* ── Decision Provenance ────────────────────── */
    .decision-shell { display: grid; gap: 14px; }
    .decision-toolbar {
      display: flex; justify-content: space-between; align-items: center;
      gap: 12px; flex-wrap: wrap;
    }
    .decision-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .decision-controls select { min-width: 210px; background: #08111d; }
    .decision-grid { display: grid; grid-template-columns: 1.55fr .85fr; gap: 12px; }
    .decision-panel {
      border: 1px solid var(--line); border-radius: var(--radius);
      background: var(--panel2); padding: 14px; min-width: 0;
    }
    .decision-stat-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
    .decision-stat {
      border: 1px solid rgba(36,53,72,.9); border-radius: 8px;
      background: #091422; padding: 10px 12px; min-height: 68px;
    }
    .decision-stat .stat-value { font-size: 19px; font-weight: 800; margin-top: 5px; overflow-wrap: anywhere; }
    .decision-chart { position: relative; height: 334px; border: 1px solid #1a2b3e; border-radius: 8px; background: #050b14; overflow: hidden; }
    .decision-chart svg { width: 100%; height: 100%; display: block; }
    .decision-price-line { fill: none; stroke: var(--accent); stroke-width: 2.3; stroke-linecap: round; stroke-linejoin: round; }
    .decision-price-area { fill: rgba(96,165,250,.08); stroke: none; }
    .decision-marker { cursor: pointer; stroke: #07101b; stroke-width: 2.5; filter: drop-shadow(0 4px 8px rgba(0,0,0,.42)); }
    .decision-marker.selected { stroke: #fff; stroke-width: 3.2; }
    .decision-buy { fill: var(--good); }
    .decision-sell { fill: var(--bad); }
    .decision-hold { fill: var(--warn); }
    .decision-axis-label { fill: var(--muted); font-size: 11px; font-weight: 650; }
    .decision-tooltip {
      position: fixed; z-index: 40; max-width: 300px;
      background: #07101b; border: 1px solid var(--line2);
      border-radius: 8px; padding: 10px 12px;
      box-shadow: 0 16px 42px rgba(0,0,0,.55);
      color: var(--text); pointer-events: none; font-size: 12px; line-height: 1.45;
    }
    .decision-legend { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 9px; }
    .legend-item { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
    .legend-dot { width: 9px; height: 9px; border-radius: 999px; display: inline-block; }
    .decision-detail { display: grid; gap: 10px; }
    .detail-row { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 10px; font-size: 13px; }
    .detail-label { color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
    .detail-value { overflow-wrap: anywhere; }
    .source-chip {
      display: inline-flex; align-items: center; border: 1px solid var(--line2);
      background: rgba(255,255,255,.04); border-radius: 999px;
      padding: 3px 9px; color: #b9c9dc; font-size: 11px; font-weight: 700;
    }
    .decision-table-wrap { max-height: 430px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
    .decision-table-wrap table { min-width: 980px; }
    .decision-table-wrap tr { cursor: pointer; }
    .decision-table-wrap tr.selected td { background: rgba(45,212,191,.07); }
    .timing-good { color: var(--good); font-weight: 750; }
    .timing-bad { color: var(--bad); font-weight: 750; }
    .timing-neutral { color: var(--warn); font-weight: 750; }

    /* ── Responsive ─────────────────────────────── */
    @media (max-width: 1100px) {
      .chart-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .decision-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .chart-grid { grid-template-columns: 1fr; }
      .conversation-grid { grid-template-columns: 1fr; }
      .decision-stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
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
      <div class="decision-shell">
        <div class="decision-toolbar">
          <div>
            <span class="section-title">Agent Decision Provenance</span>
            <div class="sub" id="decisionMeta">Waiting for decision records</div>
          </div>
          <div class="decision-controls">
            <select id="decisionSymbolSelect" onchange="selectDecisionSymbol(this.value)"></select>
            <button class="tab active" data-decision-action="all" onclick="setDecisionAction('all', event)">All</button>
            <button class="tab" data-decision-action="buy" onclick="setDecisionAction('buy', event)">Buy</button>
            <button class="tab" data-decision-action="hold" onclick="setDecisionAction('hold', event)">Hold</button>
            <button class="tab" data-decision-action="sell" onclick="setDecisionAction('sell', event)">Sell</button>
          </div>
        </div>
        <div class="decision-grid">
          <div class="decision-panel">
            <div class="decision-stat-grid" id="decisionStats"></div>
            <div class="decision-chart" id="decisionChart"></div>
            <div class="decision-legend">
              <span class="legend-item"><span class="legend-dot decision-buy"></span>Buy</span>
              <span class="legend-item"><span class="legend-dot decision-hold"></span>Hold</span>
              <span class="legend-item"><span class="legend-dot decision-sell"></span>Sell</span>
              <span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>Observed price</span>
            </div>
          </div>
          <div class="decision-panel">
            <div class="between" style="margin-bottom:10px">
              <span class="section-title" style="font-size:13px">Selected Record</span>
              <span class="source-chip" id="decisionSource">N/A</span>
            </div>
            <div class="decision-detail" id="decisionDetail"></div>
          </div>
        </div>
        <div class="decision-table-wrap" id="decisionTable"></div>
      </div>
      <div id="decisionTooltip" class="decision-tooltip hidden"></div>
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
    let selectedDecisionSymbol = '';
    let selectedDecisionId = '';
    let decisionActionFilter = 'all';

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
    function priceFmt(n, currency='') {
      const value = num(n);
      if (value === null) return 'N/A';
      const prefix = currency === 'USD' ? '$' : currency === 'CNY' ? '¥' : currency === 'HKD' ? 'HK$' : '';
      return prefix + value.toLocaleString(undefined, {maximumFractionDigits: value >= 100 ? 2 : 4});
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
    function decisionTime(row) {
      return row?.selected_at || row?.decided_at || row?.trade_date || '';
    }
    function decisionRecords() {
      return DATA?.decision_timeline?.records || [];
    }
    function sortedDecisionRows(rows) {
      return rows.slice().sort((a, b) => String(decisionTime(a)).localeCompare(String(decisionTime(b))));
    }
    function decisionById(id) {
      return decisionRecords().find(row => row.id === id);
    }
    function timingInfo(row, latestPrice) {
      const price = num(row?.price);
      const latest = num(latestPrice);
      if (price === null || latest === null || price === 0) return {text: 'N/A', cls: 'muted', edge: null};
      const delta = (latest - price) / price;
      if (row.action === 'buy') {
        return {text: `买后 ${delta >= 0 ? '+' : ''}${(delta * 100).toFixed(2)}%`, cls: delta >= 0 ? 'timing-good' : 'timing-bad', edge: delta};
      }
      if (row.action === 'sell') {
        const edge = -delta;
        return {text: edge >= 0 ? `卖后回落 ${(edge * 100).toFixed(2)}%` : `卖后上涨 ${(-edge * 100).toFixed(2)}%`, cls: edge >= 0 ? 'timing-good' : 'timing-bad', edge};
      }
      return {text: `观察 ${delta >= 0 ? '+' : ''}${(delta * 100).toFixed(2)}%`, cls: Math.abs(delta) < 0.01 ? 'timing-neutral' : delta > 0 ? 'timing-good' : 'timing-bad', edge: delta};
    }
    function decisionStat(label, value, sub='') {
      return `<div class="decision-stat"><div class="label">${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
    }
    function sourceLabel(row) {
      return row?.source_label || row?.source || 'N/A';
    }
    function visibleDecisionRows() {
      const rows = decisionRecords().filter(row => !selectedDecisionSymbol || row.symbol === selectedDecisionSymbol);
      return decisionActionFilter === 'all' ? rows : rows.filter(row => row.action === decisionActionFilter);
    }
    function selectDecisionSymbol(symbol) {
      selectedDecisionSymbol = symbol;
      selectedDecisionId = '';
      renderDecisionExplorer();
    }
    function setDecisionAction(action, event) {
      decisionActionFilter = action;
      selectedDecisionId = '';
      for (const btn of document.querySelectorAll('[data-decision-action]')) {
        btn.classList.toggle('active', btn.dataset.decisionAction === action);
      }
      if (event?.target) event.target.classList.add('active');
      renderDecisionExplorer();
    }
    function selectDecisionRecord(id) {
      selectedDecisionId = id;
      renderDecisionExplorer();
    }
    function showDecisionTooltip(event, id) {
      const row = decisionById(id);
      if (!row) return;
      const tip = document.getElementById('decisionTooltip');
      tip.innerHTML = `<strong>${esc(row.symbol)} ${pill(row.action)}</strong>
        <div class="sub">${esc(timeLabel(decisionTime(row)))} · ${esc(sourceLabel(row))}</div>
        <div style="margin-top:6px">Price: <strong>${esc(priceFmt(row.price, row.currency))}</strong> · Shares: <strong>${esc(fmt(row.shares))}</strong></div>
        <div class="muted" style="margin-top:5px">${esc(row.reason || row.status_detail || '').slice(0,180)}</div>`;
      tip.classList.remove('hidden');
      const left = Math.min(event.clientX + 14, window.innerWidth - tip.offsetWidth - 16);
      const top = Math.min(event.clientY + 14, window.innerHeight - tip.offsetHeight - 16);
      tip.style.left = Math.max(12, left) + 'px';
      tip.style.top = Math.max(12, top) + 'px';
    }
    function hideDecisionTooltip() {
      document.getElementById('decisionTooltip')?.classList.add('hidden');
    }
    function renderDecisionChart(symbolRows, latestPrice) {
      const chart = document.getElementById('decisionChart');
      const priced = sortedDecisionRows(symbolRows)
        .map((row, index) => ({row, index, price: num(row.price), ms: Date.parse(decisionTime(row))}))
        .filter(point => point.price !== null);
      if (!priced.length) {
        chart.innerHTML = '<div class="empty-chart" style="height:100%;border:none">No price observations for this symbol yet</div>';
        return;
      }
      const width = 980, height = 334, left = 56, right = 26, top = 24, bottom = 46;
      let minPrice = Math.min(...priced.map(p => p.price));
      let maxPrice = Math.max(...priced.map(p => p.price));
      if (minPrice === maxPrice) {
        minPrice -= Math.max(1, Math.abs(minPrice) * 0.02);
        maxPrice += Math.max(1, Math.abs(maxPrice) * 0.02);
      }
      const validTimes = priced.map(p => p.ms).filter(ms => Number.isFinite(ms));
      const minTime = validTimes.length ? Math.min(...validTimes) : 0;
      const maxTime = validTimes.length ? Math.max(...validTimes) : priced.length - 1;
      const sameTime = minTime === maxTime;
      const x = (point, i) => sameTime
        ? left + (priced.length === 1 ? (width - left - right) / 2 : i * (width - left - right) / (priced.length - 1))
        : left + ((Number.isFinite(point.ms) ? point.ms : minTime) - minTime) * (width - left - right) / (maxTime - minTime);
      const y = price => top + (maxPrice - price) * (height - top - bottom) / (maxPrice - minPrice);
      const line = priced.map((p, i) => `${i ? 'L' : 'M'} ${x(p, i).toFixed(1)} ${y(p.price).toFixed(1)}`).join(' ');
      const area = `${line} L ${x(priced[priced.length - 1], priced.length - 1).toFixed(1)} ${height - bottom} L ${x(priced[0], 0).toFixed(1)} ${height - bottom} Z`;
      const markerRows = priced.filter(p => decisionActionFilter === 'all' || p.row.action === decisionActionFilter);
      const markers = markerRows.map(point => {
        const i = priced.indexOf(point);
        const cls = `decision-marker decision-${point.row.action || 'hold'} ${point.row.id === selectedDecisionId ? 'selected' : ''}`;
        return `<circle class="${cls}" data-decision-id="${esc(point.row.id)}" cx="${x(point, i).toFixed(1)}" cy="${y(point.price).toFixed(1)}" r="${point.row.id === selectedDecisionId ? 8 : 6.5}"></circle>`;
      }).join('');
      chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Agent decision timeline">
        <line class="grid-line" x1="${left}" y1="${top}" x2="${width - right}" y2="${top}"></line>
        <line class="grid-line" x1="${left}" y1="${height / 2}" x2="${width - right}" y2="${height / 2}"></line>
        <line class="axis" x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}"></line>
        <line class="axis" x1="${left}" y1="${top}" x2="${left}" y2="${height - bottom}"></line>
        <text class="decision-axis-label" x="10" y="${top + 4}">${esc(priceFmt(maxPrice))}</text>
        <text class="decision-axis-label" x="10" y="${height - bottom + 4}">${esc(priceFmt(minPrice))}</text>
        <text class="decision-axis-label" x="${left}" y="${height - 16}">${esc(timeLabel(decisionTime(priced[0].row)))}</text>
        <text class="decision-axis-label" text-anchor="end" x="${width - right}" y="${height - 16}">${esc(timeLabel(decisionTime(priced[priced.length - 1].row)))}</text>
        ${priced.length > 1 ? `<path class="decision-price-area" d="${area}"></path><path class="decision-price-line" d="${line}"></path>` : ''}
        ${markers}
      </svg>`;
      for (const marker of chart.querySelectorAll('[data-decision-id]')) {
        marker.addEventListener('mouseenter', event => showDecisionTooltip(event, marker.dataset.decisionId));
        marker.addEventListener('mousemove', event => showDecisionTooltip(event, marker.dataset.decisionId));
        marker.addEventListener('mouseleave', hideDecisionTooltip);
        marker.addEventListener('click', () => selectDecisionRecord(marker.dataset.decisionId));
      }
    }
    function renderDecisionDetail(row, latestPrice) {
      const detail = document.getElementById('decisionDetail');
      const source = document.getElementById('decisionSource');
      if (!row) {
        source.textContent = 'N/A';
        detail.innerHTML = '<div class="muted">No record selected.</div>';
        return;
      }
      const timing = timingInfo(row, latestPrice);
      source.textContent = sourceLabel(row);
      detail.innerHTML = `
        <div class="between"><div><strong>${esc(row.symbol)}</strong> <span class="muted">${esc(row.name || row.arena_symbol || '')}</span></div>${pill(row.action)}</div>
        <div class="detail-row"><div class="detail-label">Selected</div><div class="detail-value">${esc(row.selected_at || row.decided_at || row.trade_date || 'N/A')}</div></div>
        <div class="detail-row"><div class="detail-label">Shares</div><div class="detail-value">${esc(fmt(row.shares))}</div></div>
        <div class="detail-row"><div class="detail-label">Price</div><div class="detail-value">${esc(priceFmt(row.price, row.currency))} <span class="muted">${esc(row.price_kind || '')}</span></div></div>
        <div class="detail-row"><div class="detail-label">Status</div><div class="detail-value">${esc(row.status || 'N/A')} ${row.execute_trade ? '<span class="source-chip">live</span>' : '<span class="source-chip">dry-run/log</span>'}</div></div>
        <div class="detail-row"><div class="detail-label">Timing</div><div class="detail-value ${timing.cls}">${esc(timing.text)}</div></div>
        <div class="detail-row"><div class="detail-label">Score</div><div class="detail-value">${esc(row.score !== null && row.score !== undefined ? fmt(row.score) : 'N/A')} ${row.confidence !== null && row.confidence !== undefined ? `· confidence ${esc(fmt(row.confidence))}` : ''}</div></div>
        <div class="detail-row"><div class="detail-label">Reason</div><div class="detail-value">${esc(row.reason || row.status_detail || 'N/A')}</div></div>
        ${row.risk ? `<div class="detail-row"><div class="detail-label">Risk</div><div class="detail-value">${esc(row.risk)}</div></div>` : ''}
        ${row.evidence?.length ? `<div class="detail-row"><div class="detail-label">Evidence</div><div class="detail-value">${row.evidence.map(item => `<div>${esc(item)}</div>`).join('')}</div></div>` : ''}
        ${row.signal_preview ? `<div><h3>Decision Note</h3><pre style="max-height:150px">${esc(row.signal_preview)}</pre></div>` : ''}
        ${row.trace_id || row.trade_id || row.source_path ? `<div class="sub">${esc([row.trace_id ? `trace ${row.trace_id}` : '', row.trade_id ? `trade ${row.trade_id}` : '', row.source_path || ''].filter(Boolean).join(' · '))}</div>` : ''}
      `;
    }
    function renderDecisionTable(rows, latestPrice) {
      const bodyRows = rows.slice().reverse().slice(0, 120);
      document.getElementById('decisionTable').innerHTML = bodyRows.length ? `<table><thead><tr><th>Selected At</th><th>Symbol</th><th>Action</th><th>Shares</th><th>Price</th><th>Status</th><th>Timing</th><th>Provenance</th></tr></thead><tbody>${bodyRows.map(row => {
        const timing = timingInfo(row, latestPrice);
        return `<tr class="${row.id === selectedDecisionId ? 'selected' : ''}" onclick="selectDecisionRecord('${esc(row.id)}')">
          <td style="white-space:nowrap;font-size:12px">${esc(row.selected_at || row.decided_at || row.trade_date || '')}</td>
          <td><strong>${esc(row.symbol)}</strong><div class="sub">${esc(row.name || row.arena_symbol || '')}</div></td>
          <td>${pill(row.action)}</td>
          <td>${esc(fmt(row.shares))}</td>
          <td>${esc(priceFmt(row.price, row.currency))}<div class="sub">${esc(row.price_kind || '')}</div></td>
          <td>${esc(row.status || '')}<div class="sub">${esc(row.status_detail || '').slice(0,80)}</div></td>
          <td class="${timing.cls}">${esc(timing.text)}</td>
          <td><span class="source-chip">${esc(sourceLabel(row))}</span><div class="sub">${esc(row.reason || '').slice(0,150)}</div></td>
        </tr>`;
      }).join('')}</tbody></table>` : '<div class="empty-chart">No records for the selected filters</div>';
    }
    function renderDecisionExplorer() {
      const payload = DATA?.decision_timeline || {};
      const records = decisionRecords();
      const symbols = payload.symbols || [];
      const select = document.getElementById('decisionSymbolSelect');
      if (!records.length || !symbols.length) {
        document.getElementById('decisionMeta').textContent = 'No local decision records yet';
        select.innerHTML = '<option value="">No symbols</option>';
        document.getElementById('decisionStats').innerHTML = '';
        document.getElementById('decisionChart').innerHTML = '<div class="empty-chart" style="height:100%;border:none">Waiting for TradingAgents decisions</div>';
        renderDecisionDetail(null, null);
        renderDecisionTable([], null);
        return;
      }
      if (!selectedDecisionSymbol || !symbols.some(item => item.symbol === selectedDecisionSymbol)) {
        selectedDecisionSymbol = (symbols.find(item => item.priced_count)?.symbol || symbols[0].symbol);
      }
      select.innerHTML = symbols.map(item => `<option value="${esc(item.symbol)}" ${item.symbol === selectedDecisionSymbol ? 'selected' : ''}>${esc(item.symbol)}${item.name ? ` · ${esc(item.name)}` : ''} (${item.count})</option>`).join('');
      for (const btn of document.querySelectorAll('[data-decision-action]')) {
        btn.classList.toggle('active', btn.dataset.decisionAction === decisionActionFilter);
      }
      const symbolRows = sortedDecisionRows(records.filter(row => row.symbol === selectedDecisionSymbol));
      const priced = symbolRows.filter(row => num(row.price) !== null);
      const latestPriced = priced[priced.length - 1] || {};
      const latestPrice = latestPriced.price;
      const visible = sortedDecisionRows(visibleDecisionRows());
      if (!selectedDecisionId || !visible.some(row => row.id === selectedDecisionId)) {
        selectedDecisionId = (visible.filter(row => row.price !== null).slice(-1)[0] || visible.slice(-1)[0] || {}).id || '';
      }
      const selected = decisionById(selectedDecisionId);
      const counts = symbolRows.reduce((acc, row) => {
        acc[row.action] = (acc[row.action] || 0) + 1;
        return acc;
      }, {});
      const latestAction = symbolRows[symbolRows.length - 1]?.action || 'N/A';
      document.getElementById('decisionMeta').textContent = `${records.length} records · ${symbols.length} symbols · generated ${payload.generated_at || ''}`;
      document.getElementById('decisionStats').innerHTML = [
        decisionStat('Records', String(symbolRows.length), `${counts.buy || 0} buy · ${counts.hold || 0} hold · ${counts.sell || 0} sell`),
        decisionStat('Latest Action', latestAction.toUpperCase(), symbolRows[symbolRows.length - 1]?.selected_at || ''),
        decisionStat('Latest Price', priceFmt(latestPrice, latestPriced.currency), latestPriced.price_kind || 'observed'),
        decisionStat('Price Points', String(priced.length), selectedDecisionSymbol),
      ].join('');
      renderDecisionChart(symbolRows, latestPrice);
      renderDecisionDetail(selected, latestPrice);
      renderDecisionTable(visible, latestPrice);
    }
    function render() {
      const home = DATA.arena_home?.data?.data || {};
      const generatedAtMs = Date.parse(DATA.generated_at || '');
      const freshPortfolioSnapshots = (DATA.portfolio_history || []).filter(row => {
        const ts = Date.parse(row.timestamp || row.snapshot_time || row.snapshot_date || '');
        if (!Number.isFinite(ts) || !Number.isFinite(generatedAtMs)) return false;
        return Math.abs(generatedAtMs - ts) <= 36 * 60 * 60 * 1000;
      });
      const latestPortfolioSnapshot = freshPortfolioSnapshots.slice(-1)[0] || {};
      const portfolioCandidates = [
        home.portfolio,
        DATA.arena_portfolio?.data?.data?.portfolio,
        latestPortfolioSnapshot,
      ].filter(item => item && item.total_value !== undefined && item.total_value !== null);
      const portfolio = portfolioCandidates[0] || {};
      const portfolioSource = home.portfolio
        ? 'home API'
        : DATA.arena_portfolio?.data?.data?.portfolio
          ? (DATA.arena_portfolio?.from_cache ? `cached portfolio · ${DATA.arena_portfolio.cached_at || ''}` : 'portfolio API')
          : latestPortfolioSnapshot.total_value !== undefined ? 'snapshots API' : 'Signal Arena';
      const rankValue = home.rank ?? portfolio.rank;
      const participantValue = home.total_participants ?? DATA.leaderboard?.data?.data?.total;
      const rankText = rankValue !== undefined && rankValue !== null
        ? `Rank ${rankValue} / ${participantValue ?? 'N/A'}`
        : `Rank unavailable · ${DATA.arena_home?.ok ? 'no home rank' : 'home API timeout'}`;
      const loopText = DATA.loop?.active ? 'Loop running' : `Loop ${DATA.loop?.status || 'unknown'}`;
      document.getElementById('subtitle').textContent = `Updated ${DATA.generated_at} · ${loopText} · cron ${DATA.cron?.installed ? 'installed' : 'not installed'}`;
      document.getElementById('authPill').textContent = DATA.config.api_key_configured ? 'API Key OK' : 'No API Key';
      document.getElementById('authPill').className = 'pill ' + (DATA.config.api_key_configured ? 'buy' : 'sell');
      document.getElementById('metrics').innerHTML = [
        metric('Total Value', fmt(portfolio.total_value), portfolioSource),
        metric('Cash', fmt(portfolio.cash), home.market_status || portfolio.market_status || portfolioSource),
        metric('Return Rate', portfolio.return_rate !== undefined ? (Number(portfolio.return_rate) * 100).toFixed(2) + '%' : 'N/A', rankText),
        metric('Last Action', DATA.latest_run?.action || 'N/A', DATA.latest_run?.symbol || DATA.latest_run?.mode || ''),
        metric('Last Trade', DATA.latest_run?.trade_status || 'N/A', DATA.latest_run?.trade_reason || (DATA.latest_run?.shares !== undefined ? `shares ${DATA.latest_run.shares}` : '')),
      ].join('');
      renderCharts(portfolio);
      renderDecisionExplorer();
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
      const portfolio = p.portfolio || {};
      const source = DATA.arena_portfolio?.from_cache
        ? `cached ${DATA.arena_portfolio.cached_at || ''}`
        : DATA.arena_portfolio?.ok ? 'live portfolio API' : 'portfolio API unavailable';
      let html = `<div class="sub" style="margin-bottom:10px">Agent: <strong>${esc(p.agent?.username || DATA.arena_home?.data?.data?.agent?.username || 'N/A')}</strong> · ${esc(source)}</div>`;
      if (portfolio.total_value !== undefined || portfolio.cash !== undefined) {
        html += `<div class="mini-grid" style="margin-bottom:10px">
          ${metric('Total Value', fmt(portfolio.total_value), 'portfolio')}
          ${metric('Cash', fmt(portfolio.cash), 'available')}
          ${metric('Holdings Value', fmt(portfolio.holdings_value), 'positions')}
          ${metric('Return', pct(portfolio.return_rate), 'portfolio')}
        </div>`;
      }
      if (!holdings.length) {
        const detail = DATA.arena_portfolio?.live_error || DATA.arena_portfolio?.error || '';
        html += `<p class="muted">No positions available${detail ? ` · ${esc(detail)}` : ''}.</p>`;
      }
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
    function reasonText(row) {
      const values = [row.selector_reason, row.reason, ...(row.reasons || []), ...(row.penalties || [])]
        .filter(Boolean)
        .map(v => String(v));
      return [...new Set(values)].join(' · ');
    }
    function symbolKey(value) {
      const text = String(value || '').toLowerCase();
      if (text.startsWith('gb_')) return text.slice(3);
      if (/^sh\d{6}$/.test(text)) return text.slice(2) + '.ss';
      if (/^sz\d{6}$/.test(text)) return text.slice(2) + '.sz';
      if (/^hk\d{5}$/.test(text)) return text.slice(2) + '.hk';
      return text;
    }
    function renderPreopenSelection() {
      const markets = DATA.preopen_selection?.markets || {};
      const entries = Object.entries(markets);
      if (!entries.length) return '';
      return `<div class="mini-card" style="margin-bottom:10px">
        <div class="between"><strong>GPT Pre-open Picks</strong><span class="pill hold">${esc(DATA.preopen_selection?.timestamp || '')}</span></div>
        ${entries.map(([market, payload]) => {
          const selector = payload.selector || {};
          const picks = payload.picks || [];
          const analyses = payload.analyses || [];
          return `<div style="margin-top:10px">
            <div class="sub" style="font-weight:700">${esc(market)} · ${esc(selector.model || '')}${selector.web_search ? ' · web search' : ''}</div>
            <table style="margin-top:6px"><thead><tr><th>Pick</th><th>Confidence</th><th>GPT Reason</th><th>Pre-open TA</th></tr></thead><tbody>${picks.map(pick => {
              const analysis = analyses.find(item => symbolKey(item.symbol) === symbolKey(pick.symbol)) || {};
              return `<tr><td style="font-weight:600">${esc(pick.symbol)} <span class="muted">${esc(pick.name || '')}</span></td><td>${fmt(pick.confidence)}</td><td class="muted">${esc(pick.reason || '').slice(0,220)}</td><td>${esc([analysis.rating, analysis.action].filter(Boolean).join(' / ') || 'pending')}</td></tr>`;
            }).join('')}</tbody></table>
          </div>`;
        }).join('')}
      </div>`;
    }
    function renderSelection() {
      const selection = DATA.stock_selection || DATA.latest_run?.stock_selection;
      const preopenHtml = renderPreopenSelection();
      if (!selection) {
        document.getElementById('stockSelection').innerHTML = preopenHtml || '<span class="muted">Candidates will appear after next auto-selection round.</span>';
        return;
      }
      const selected = selection.selected || {};
      const rows = selection.top_candidates || [];
      const label = selection.mode === 'gpt_preopen' ? 'Live Agent Selection (GPT Pre-open)' : 'Live Agent Selection';
      document.getElementById('stockSelection').innerHTML = `${preopenHtml}<div class="mini-card" style="margin-bottom:10px">
        <div class="sub" style="margin-bottom:6px;font-weight:700">${esc(label)}</div>
        <div class="between"><strong>${esc(selected.symbol || 'N/A')} ${esc(selected.name || '')}</strong><span class="pill buy">${esc(String(selected.score ?? 'N/A'))}</span></div>
        <div class="sub" style="margin-top:4px">${esc(selection.strategy || selection.mode || '')} · ${esc(selection.timestamp || '')}</div>
        <div class="sub" style="margin-top:3px">${esc(reasonText(selected))}</div>
      </div>
      <table><thead><tr><th>Candidate</th><th>Score</th><th>Change</th><th>Reasons</th></tr></thead><tbody>${rows.slice(0,6).map(row => `<tr><td style="font-weight:600">${esc(row.symbol)} <span class="muted">${esc(row.name || '')}</span></td><td>${fmt(row.score)}</td><td class="${row.change_rate >= 0 ? 'buy' : 'sell'}" style="font-weight:700">${(Number(row.change_rate || 0) * 100).toFixed(2)}%</td><td class="muted">${esc(reasonText(row)).slice(0,220)}</td></tr>`).join('')}</tbody></table>`;
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
      for (const btn of document.querySelectorAll('.tabs .tab')) btn.classList.remove('active');
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
