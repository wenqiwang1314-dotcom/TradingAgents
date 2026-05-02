#!/usr/bin/env python3
"""Signal Arena integration and local smoke runner.

Default behavior is read-only. Authenticated calls are enabled by setting
SIGNAL_ARENA_API_KEY. Trades are submitted only with --execute-trade.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from multiprocessing import get_context
from queue import Empty
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.rating import action_from_signal
from tradingagents.graph.trading_graph import TradingAgentsGraph


BASE_URL = os.getenv("SIGNAL_ARENA_BASE_URL", "https://signal.coze.site").rstrip("/")
API_KEY_ENV = "SIGNAL_ARENA_API_KEY"
SIGNAL_DIR = PROJECT_ROOT / "results" / "signal_arena"
CONVERSATION_DIR = SIGNAL_DIR / "conversation_traces"
CURRENT_CONVERSATION_PATH = SIGNAL_DIR / "current_conversation.json"
RUNS_PATH = SIGNAL_DIR / "runs.jsonl"
LAST_SELECTION_PATH = SIGNAL_DIR / "stock_selection.json"


class TimeoutExpired(RuntimeError):
    pass


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class SignalArenaClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"agent-auth-api-key": self.api_key}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self.headers if auth else {}
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is False:
            message = payload.get("message") or payload.get("error") or payload
            raise RuntimeError(f"{method} {path} failed: {message}")
        return payload

    def skill(self) -> str:
        response = requests.get(f"{self.base_url}/skill.md", timeout=self.timeout)
        response.raise_for_status()
        return response.text

    def stocks(self, market: str, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        return self.request("GET", "/api/v1/arena/stocks", params={"market": market, "limit": limit, "offset": offset})

    def top_movers(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/arena/top-movers")

    def leaderboard(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/arena/leaderboard")

    def debug_auth(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/arena/debug-auth", auth=True)

    def join(self) -> dict[str, Any]:
        return self.request("POST", "/api/v1/arena/join", auth=True)

    def home(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/arena/home", auth=True)

    def portfolio(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/arena/portfolio", auth=bool(self.api_key))

    def snapshots(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/arena/snapshots", auth=True)

    def trade(self, symbol: str, action: str, shares: int, reason: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/arena/trade",
            auth=True,
            json_body={"symbol": symbol, "action": action, "shares": shares, "reason": reason[:500]},
        )


def arena_to_tradingagents_symbol(symbol: str) -> str:
    lower = symbol.lower()
    if lower.startswith("gb_"):
        return symbol[3:].upper()
    if lower.startswith("sh") and len(symbol) == 8:
        return f"{symbol[2:]}.SS"
    if lower.startswith("sz") and len(symbol) == 8:
        return f"{symbol[2:]}.SZ"
    if lower.startswith("hk") and len(symbol) == 7:
        return f"{symbol[2:]}.HK"
    return symbol.upper()


def env_list(name: str) -> set[str]:
    return {item.strip().lower() for item in os.getenv(name, "").split(",") if item.strip()}


def env_market_list(name: str) -> list[str]:
    return [item.strip().upper() for item in os.getenv(name, "").split(",") if item.strip()]


def allowed_markets() -> list[str]:
    return env_market_list("SIGNAL_ARENA_ALLOWED_MARKETS")


def market_whitelist(market: str) -> set[str]:
    specific = env_list(f"SIGNAL_ARENA_SYMBOL_WHITELIST_{market.upper()}")
    return specific or env_list("SIGNAL_ARENA_SYMBOL_WHITELIST")


def constrain_market(market: str, minutes: int | None = None) -> str:
    resolved = market.upper()
    allowed = allowed_markets()
    if not allowed or resolved in allowed:
        return resolved
    if resolved == "HK":
        if "CN" in allowed and minutes is not None and 9 * 60 + 30 <= minutes < 16 * 60:
            return "CN"
        if "US" in allowed:
            return "US"
    if "US" in allowed:
        return "US"
    return allowed[0]


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def recent_symbols(hours: float) -> set[str]:
    if hours <= 0 or not RUNS_PATH.exists():
        return set()
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    symbols: set[str] = set()
    for line in RUNS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        stock = row.get("stock") or {}
        for symbol in (stock.get("symbol"), row.get("tradingagents_symbol")):
            if symbol:
                symbols.add(str(symbol).lower())
    return symbols


def portfolio_symbols(portfolio_payload: dict[str, Any]) -> set[str]:
    data = portfolio_payload.get("data", {}) if isinstance(portfolio_payload, dict) else {}
    holdings = data.get("holdings") or []
    return {str(item.get("symbol") or "").lower() for item in holdings if item.get("symbol")}


def collect_stock_candidates(client: SignalArenaClient, market: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limit = int(os.getenv("SIGNAL_ARENA_CANDIDATE_LIMIT", "120"))
    movers_payload = client.top_movers()
    mover_rows = movers_payload.get("data", {}).get("movers", {}).get(market, [])
    stocks_payload = client.stocks(market=market, limit=limit)
    stock_rows = stocks_payload.get("data", {}).get("stocks", [])

    by_symbol: dict[str, dict[str, Any]] = {}
    for source, rows in (("stocks", stock_rows), ("top_movers", mover_rows)):
        for index, stock in enumerate(rows):
            symbol = str(stock.get("symbol") or "").lower()
            if not symbol:
                continue
            merged = {**by_symbol.get(symbol, {}), **stock}
            sources = set(merged.get("_sources") or [])
            sources.add(source)
            merged["_sources"] = sorted(sources)
            if source == "top_movers":
                merged["_top_mover_rank"] = index + 1
            by_symbol[symbol] = merged

    meta = {
        "candidate_limit": limit,
        "top_movers_count": len(mover_rows),
        "stocks_count": len(stock_rows),
        "combined_count": len(by_symbol),
        "stocks_total": stocks_payload.get("data", {}).get("total"),
        "latest_trade_date": stocks_payload.get("data", {}).get("latest_trade_date"),
    }
    return list(by_symbol.values()), meta


def score_stock(
    stock: dict[str, Any],
    *,
    recent: set[str],
    held: set[str],
    whitelist: set[str],
    blacklist: set[str],
) -> dict[str, Any]:
    symbol = str(stock.get("symbol") or "").lower()
    price = number(stock.get("price"))
    change_rate = number(stock.get("change_rate"))
    volume = number(stock.get("volume"))
    high = number(stock.get("high"))
    low = number(stock.get("low"))
    mode = os.getenv("SIGNAL_ARENA_SELECTION_STRATEGY", "momentum").lower()
    min_price = float(os.getenv("SIGNAL_ARENA_MIN_PRICE", "1"))
    min_volume = float(os.getenv("SIGNAL_ARENA_MIN_VOLUME", "1000000"))
    max_abs_change_rate = float(os.getenv("SIGNAL_ARENA_MAX_ABS_CHANGE_RATE", "0.18"))

    reasons: list[str] = []
    penalties: list[str] = []
    disqualified: list[str] = []
    score = 0.0

    if whitelist and symbol not in whitelist:
        disqualified.append("not in whitelist")
    if symbol in blacklist:
        disqualified.append("blacklisted")
    if price < min_price:
        disqualified.append(f"price {price:.2f} < min {min_price:.2f}")
    if volume and volume < min_volume:
        penalties.append(f"low volume {volume:.0f}")
        score -= 8
    if abs(change_rate) > max_abs_change_rate:
        penalties.append(f"extreme move {change_rate * 100:.2f}%")
        score -= 12

    if mode == "mean_reversion":
        move_score = clamp(abs(change_rate) / 0.08) * 28
        if change_rate < 0:
            move_score += 10
            reasons.append("down mover for rebound scan")
    else:
        move_score = clamp(max(change_rate, 0) / 0.08) * 38
        if change_rate > 0:
            reasons.append("positive momentum")
    score += move_score

    if "_top_mover_rank" in stock:
        rank_bonus = max(0, 18 - 3 * (int(stock["_top_mover_rank"]) - 1))
        score += rank_bonus
        reasons.append(f"top mover rank {stock['_top_mover_rank']}")

    if volume > 0:
        volume_score = clamp((len(str(int(volume))) - 5) / 4) * 16
        score += volume_score
        reasons.append(f"volume {int(volume):,}")

    if high > low and price > 0:
        close_position = clamp((price - low) / (high - low))
        score += close_position * 10
        if close_position >= 0.7:
            reasons.append("close near intraday high")

    if symbol in held:
        score += 8
        reasons.append("existing holding")
    if symbol in recent:
        score -= 18
        penalties.append("recently analyzed")
    if "top_movers" in (stock.get("_sources") or []):
        score += 8

    return {
        "symbol": stock.get("symbol"),
        "name": stock.get("name"),
        "market": stock.get("market"),
        "price": price,
        "change_rate": change_rate,
        "volume": volume,
        "score": round(score, 2),
        "sources": stock.get("_sources") or [],
        "reasons": reasons,
        "penalties": penalties,
        "disqualified": disqualified,
        "eligible": not disqualified,
    }


def select_stock_autonomously(
    client: SignalArenaClient,
    market: str,
    portfolio_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates, meta = collect_stock_candidates(client, market)
    whitelist = market_whitelist(market)
    blacklist = env_list("SIGNAL_ARENA_SYMBOL_BLACKLIST")
    recent = recent_symbols(float(os.getenv("SIGNAL_ARENA_RECENT_SYMBOL_PENALTY_HOURS", "8")))
    held = portfolio_symbols(portfolio_payload or {})

    scored = [
        {"stock": stock, "scorecard": score_stock(stock, recent=recent, held=held, whitelist=whitelist, blacklist=blacklist)}
        for stock in candidates
    ]
    eligible = [item for item in scored if item["scorecard"]["eligible"]]
    if whitelist and not eligible:
        raise RuntimeError(f"No eligible candidates in whitelist for market={market}; check SIGNAL_ARENA_SYMBOL_WHITELIST_{market}")
    if not eligible:
        eligible = scored
    eligible.sort(key=lambda item: item["scorecard"]["score"], reverse=True)
    if not eligible:
        raise RuntimeError(f"No stock candidates returned for market={market}")

    selected = eligible[0]
    selection = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": os.getenv("SIGNAL_ARENA_SELECTION_MODE", "autonomous"),
        "strategy": os.getenv("SIGNAL_ARENA_SELECTION_STRATEGY", "momentum"),
        "market": market,
        "selected": selected["scorecard"],
        "meta": {**meta, "allowed_markets": allowed_markets(), "whitelist_size": len(whitelist)},
        "top_candidates": [item["scorecard"] for item in eligible[:10]],
    }
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SELECTION_PATH.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    return selected["stock"], selection


def choose_stock(
    client: SignalArenaClient,
    market: str,
    symbol: str | None,
    portfolio_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if symbol:
        payload = client.stocks(market=market, limit=200)
        for stock in payload.get("data", {}).get("stocks", []):
            if stock.get("symbol", "").lower() == symbol.lower():
                return stock, {"mode": "manual", "selected": {"symbol": stock.get("symbol"), "name": stock.get("name"), "reason": "matched explicit symbol"}}
        stock = {"symbol": symbol, "name": symbol, "market": market}
        return stock, {"mode": "manual", "selected": {"symbol": symbol, "reason": "explicit symbol not found in stock list"}}

    if os.getenv("SIGNAL_ARENA_SELECTION_MODE", "autonomous").lower() == "autonomous":
        return select_stock_autonomously(client, market, portfolio_payload)

    movers = client.top_movers().get("data", {}).get("movers", {})
    candidates = movers.get(market, [])
    if candidates:
        return candidates[0], {"mode": "top_mover_first", "selected": {"symbol": candidates[0].get("symbol"), "reason": "first top mover"}}

    stocks = client.stocks(market=market, limit=1).get("data", {}).get("stocks", [])
    if not stocks:
        raise RuntimeError(f"No stocks returned for market={market}")
    return stocks[0], {"mode": "first_stock", "selected": {"symbol": stocks[0].get("symbol"), "reason": "fallback first stock"}}


def resolve_market(market: str) -> str:
    if market.upper() != "AUTO":
        return constrain_market(market)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    minutes = now.hour * 60 + now.minute
    weekday = now.weekday() < 5
    if not weekday:
        return constrain_market("US", minutes)
    if (9 * 60 + 30 <= minutes < 11 * 60 + 30) or (13 * 60 <= minutes < 15 * 60):
        return constrain_market("CN", minutes)
    if (11 * 60 + 30 <= minutes < 12 * 60) or (15 * 60 <= minutes < 16 * 60):
        return constrain_market("HK", minutes)
    if minutes >= 21 * 60 + 30 or minutes < 4 * 60:
        return constrain_market("US", minutes)
    return constrain_market("US", minutes)


def extract_cash(home_payload: dict[str, Any]) -> float | None:
    data = home_payload.get("data", {})
    for key in ("cash", "available_cash", "available_funds"):
        if isinstance(data.get(key), (int, float)):
            return float(data[key])
    account = data.get("account") or data.get("agent") or {}
    for key in ("cash", "available_cash", "available_funds"):
        if isinstance(account.get(key), (int, float)):
            return float(account[key])
    return None


def extract_holding_shares(portfolio_payload: dict[str, Any], symbol: str) -> int:
    data = portfolio_payload.get("data", {}) if isinstance(portfolio_payload, dict) else {}
    holdings = data.get("holdings") or []
    wanted = symbol.lower()
    for holding in holdings:
        holding_symbol = str(holding.get("symbol") or "").lower()
        if holding_symbol == wanted:
            for key in ("shares", "quantity", "available_shares"):
                value = holding.get(key)
                if isinstance(value, (int, float)):
                    return max(int(value), 0)
    return 0


def shares_for_buy(stock: dict[str, Any], cash: float | None, budget_fraction: float) -> int:
    price = float(stock.get("price") or 0)
    if price <= 0:
        return 0
    budget = (cash or 1000000.0) * budget_fraction
    shares = int(budget // price)
    symbol = stock.get("symbol", "").lower()
    if symbol.startswith(("sh", "sz")):
        shares = (shares // 100) * 100
    return max(shares, 0)


def text_section(title: str, content: Any) -> dict[str, str]:
    return {"title": title, "content": str(content or "").strip()}


def conversation_sections_from_state(state: dict[str, Any], signal: str = "") -> list[dict[str, str]]:
    investment_debate = state.get("investment_debate_state") or {}
    risk_debate = state.get("risk_debate_state") or {}
    sections = [
        text_section("Market Analyst", state.get("market_report")),
        text_section("Social Media Analyst", state.get("sentiment_report")),
        text_section("News Analyst", state.get("news_report")),
        text_section("Fundamentals Analyst", state.get("fundamentals_report")),
        text_section("Bull Researcher", investment_debate.get("bull_history")),
        text_section("Bear Researcher", investment_debate.get("bear_history")),
        text_section("Research Manager", investment_debate.get("judge_decision") or state.get("investment_plan")),
        text_section("Trader", state.get("trader_investment_plan")),
        text_section("Aggressive Risk Analyst", risk_debate.get("aggressive_history")),
        text_section("Conservative Risk Analyst", risk_debate.get("conservative_history")),
        text_section("Neutral Risk Analyst", risk_debate.get("neutral_history")),
        text_section("Portfolio Manager", state.get("final_trade_decision")),
        text_section("Processed Signal", signal),
    ]
    return [section for section in sections if section["content"]]


def write_conversation_trace(trace: dict[str, Any]) -> Path:
    CONVERSATION_DIR.mkdir(parents=True, exist_ok=True)
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    trace_id = trace["id"]
    path = CONVERSATION_DIR / f"{trace_id}.json"
    payload = {**trace, "updated_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    CURRENT_CONVERSATION_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_tradingagents(symbol: str, trade_date: str, analysts: list[str]) -> tuple[dict[str, Any], str]:
    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "llm_provider": os.getenv("TRADINGAGENTS_LLM_PROVIDER", "openai"),
            "backend_url": os.getenv("TRADINGAGENTS_BACKEND_URL", "http://localhost:5000/v1"),
            "deep_think_llm": os.getenv("TRADINGAGENTS_DEEP_MODEL", "nvidia/nemotron-3-super"),
            "quick_think_llm": os.getenv("TRADINGAGENTS_QUICK_MODEL", "nvidia/nemotron-3-super"),
            "max_analyst_tool_iterations": int(os.getenv("TRADINGAGENTS_MAX_TOOL_ITERATIONS", "4")),
            "max_debate_rounds": int(os.getenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "1")),
            "max_risk_discuss_rounds": int(os.getenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "1")),
            "output_language": os.getenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "English"),
            "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", str(PROJECT_ROOT / "data_cache")),
            "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", str(PROJECT_ROOT / "results")),
        }
    )
    graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=config)
    return graph.propagate(symbol, trade_date)


def run_with_timeout(
    symbol: str,
    trade_date: str,
    analysts: list[str],
    timeout_seconds: int,
) -> tuple[dict[str, Any], str]:
    ctx = get_context("fork")
    queue = ctx.Queue(maxsize=1)

    def _target() -> None:
        try:
            state, signal_text = run_tradingagents(symbol, trade_date, analysts)
            queue.put(("ok", state, signal_text))
        except Exception as exc:  # pragma: no cover - exercised by integration runs
            queue.put(("error", repr(exc), ""))

    process = ctx.Process(target=_target)
    process.start()
    try:
        status, payload, signal_text = queue.get(timeout=timeout_seconds)
    except Empty as exc:
        process.terminate()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        raise TimeoutExpired(f"TradingAgents timed out after {timeout_seconds}s") from exc
    finally:
        if process.is_alive():
            process.join(timeout=1)

    if status == "error":
        raise RuntimeError(f"TradingAgents child failed: {payload}")
    return payload, signal_text


def write_result(result: dict[str, Any]) -> Path:
    out_dir = PROJECT_ROOT / "results" / "signal_arena"
    out_dir.mkdir(parents=True, exist_ok=True)
    last_run = out_dir / "last_run.json"
    last_run.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    return last_run


def run_health(client: SignalArenaClient, market: str) -> dict[str, Any]:
    resolved_market = resolve_market(market)
    skill_text = client.skill()
    result: dict[str, Any] = {
        "mode": "health",
        "base_url": client.base_url,
        "market": resolved_market,
        "skill_name_seen": "Signal Arena" in skill_text,
        "stocks": client.stocks(market=resolved_market, limit=3).get("data", {}),
        "top_movers": client.top_movers().get("data", {}),
        "leaderboard_top": client.leaderboard().get("data", {}).get("leaderboard", [])[:3],
        "auth_configured": bool(client.api_key),
    }
    if client.api_key:
        result["debug_auth"] = client.debug_auth()
        result["home"] = client.home()
        result["portfolio"] = client.portfolio()
    return result


def run_agent(args: argparse.Namespace, client: SignalArenaClient) -> dict[str, Any]:
    market = resolve_market(args.market)
    home = client.home() if client.api_key else {}
    portfolio = client.portfolio() if client.api_key else {}
    stock, selection = choose_stock(client, market, args.symbol, portfolio)
    arena_symbol = stock["symbol"]
    ta_symbol = arena_to_tradingagents_symbol(arena_symbol)
    trade_date = args.trade_date or stock.get("trade_date") or datetime.now(timezone.utc).date().isoformat()
    analysts = [item.strip() for item in args.analysts.split(",") if item.strip()]
    trace_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{ta_symbol.replace('/', '_')}"
    trace_base = {
        "id": trace_id,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "arena_symbol": arena_symbol,
        "tradingagents_symbol": ta_symbol,
        "stock_name": stock.get("name"),
        "trade_date": trade_date,
        "analysts": analysts,
        "execute_trade": args.execute_trade,
        "stock_selection": selection,
        "sections": [
            text_section(
                "Run",
                f"TradingAgents 正在分析 {ta_symbol} ({stock.get('name') or arena_symbol})，分析师: {', '.join(analysts)}。\n\n选股理由: {', '.join((selection.get('selected') or {}).get('reasons') or [])}",
            )
        ],
    }
    write_conversation_trace(trace_base)

    try:
        state, signal = run_with_timeout(ta_symbol, trade_date, analysts, args.agent_timeout_seconds)
    except Exception as exc:
        write_conversation_trace(
            {
                **trace_base,
                "status": "failed",
                "error": str(exc),
                "sections": trace_base["sections"] + [text_section("Error", str(exc))],
            }
        )
        raise
    final_decision = state.get("final_trade_decision", "")
    final_rating = state.get("final_trade_rating") or signal
    action = state.get("final_trade_action") or action_from_signal(
        f"{final_decision}\n{final_rating}"
    )
    cash = extract_cash(home) if home else None
    if action == "buy":
        shares = shares_for_buy(stock, cash, args.budget_fraction)
    elif action == "sell":
        shares = extract_holding_shares(portfolio, arena_symbol)
    else:
        shares = 0

    result: dict[str, Any] = {
        "mode": "agent",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "stock": stock,
        "tradingagents_symbol": ta_symbol,
        "trade_date": trade_date,
        "analysts": analysts,
        "conversation_trace_id": trace_id,
        "stock_selection": selection,
        "signal": signal,
        "final_trade_rating": final_rating,
        "action": action,
        "shares": shares,
        "execute_trade": args.execute_trade,
        "auth_configured": bool(client.api_key),
        "final_trade_decision": final_decision,
    }

    if args.execute_trade:
        if not client.api_key:
            raise RuntimeError(f"--execute-trade requires {API_KEY_ENV}")
        if action == "hold":
            result["trade"] = {"skipped": True, "reason": "signal is HOLD"}
        elif shares <= 0:
            result["trade"] = {"skipped": True, "reason": "computed shares <= 0"}
        else:
            reason = f"TradingAgents {signal} for {ta_symbol}. {state.get('final_trade_decision', '')}"
            result["trade"] = client.trade(arena_symbol, action, shares, reason)
    write_conversation_trace(
        {
            **trace_base,
            "status": "completed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "shares": shares,
            "stock_selection": selection,
            "trade": result.get("trade"),
            "signal": signal,
            "final_trade_rating": final_rating,
            "final_trade_decision": final_decision,
            "sections": conversation_sections_from_state(state, signal),
        }
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signal Arena local integration runner")
    parser.add_argument("--mode", choices=["health", "agent", "join"], default="health")
    parser.add_argument("--market", choices=["AUTO", "CN", "HK", "US"], default=os.getenv("SIGNAL_ARENA_MARKET", "US"))
    parser.add_argument("--symbol", help="Arena symbol, e.g. gb_aapl, sh600519, hk00700")
    parser.add_argument("--trade-date", help="Override TradingAgents trade date")
    parser.add_argument("--analysts", default=os.getenv("TRADINGAGENTS_ANALYSTS", "market"))
    parser.add_argument(
        "--agent-timeout-seconds",
        type=int,
        default=int(os.getenv("TRADINGAGENTS_AGENT_TIMEOUT_SECONDS", "900")),
    )
    parser.add_argument("--budget-fraction", type=float, default=float(os.getenv("SIGNAL_ARENA_BUDGET_FRACTION", "0.10")))
    parser.add_argument("--execute-trade", action="store_true", help="Submit order to Signal Arena")
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    client = SignalArenaClient(BASE_URL, os.getenv(API_KEY_ENV))

    if args.mode == "health":
        result = run_health(client, args.market)
    elif args.mode == "join":
        if not client.api_key:
            raise RuntimeError(f"join requires {API_KEY_ENV}")
        result = {"mode": "join", "response": client.join()}
    else:
        result = run_agent(args, client)

    out_path = write_result(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
