#!/usr/bin/env python3
"""Daily Signal Arena automation for TradingAgents.

Pre-open mode:
1. Build top-N CN/US candidate pools with the existing Signal Arena scorer.
2. Ask an OpenAI API-key backed selector model to choose 2 CN + 2 US stocks.
3. Run local TradingAgents for each selected stock.
4. Optionally submit Signal Arena trades when --execute-trade is provided.

Close mode:
1. Read the current Signal Arena portfolio.
2. Sell all matching holdings when --execute-trade is provided.

The script is read-only by default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from signal_arena_agent import (  # noqa: E402
    API_KEY_ENV,
    BASE_URL,
    SIGNAL_DIR,
    SignalArenaClient,
    TimeoutExpired,
    arena_to_tradingagents_symbol,
    collect_stock_candidates,
    conversation_sections_from_state,
    env_list,
    extract_cash,
    extract_holding_shares,
    load_dotenv,
    market_whitelist,
    portfolio_symbols,
    recent_symbols,
    run_with_timeout,
    score_stock,
    shares_for_buy,
    text_section,
    write_conversation_trace,
)
from tradingagents.llm_clients import create_llm_client  # noqa: E402
from tradingagents.rating import action_from_signal  # noqa: E402


DAILY_RUNS_PATH = SIGNAL_DIR / "daily_runs.jsonl"
DEFAULT_MARKETS = ("CN", "US")
DEFAULT_PICKS_PER_MARKET = 2


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_markets(value: str | None) -> list[str]:
    markets = [item.strip().upper() for item in (value or "").split(",") if item.strip()]
    return markets or list(DEFAULT_MARKETS)


def exchange_trade_date(market: str) -> str:
    zone = ZoneInfo("America/New_York") if market.upper() == "US" else ZoneInfo("Asia/Shanghai")
    return datetime.now(zone).date().isoformat()


def market_from_arena_symbol(symbol: str, fallback: str | None = None) -> str:
    lower = symbol.lower()
    if lower.startswith("gb_"):
        return "US"
    if lower.startswith(("sh", "sz")):
        return "CN"
    if lower.startswith("hk"):
        return "HK"
    return (fallback or "").upper()


def compact_scorecard(scorecard: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": scorecard.get("symbol"),
        "name": scorecard.get("name"),
        "market": scorecard.get("market"),
        "price": scorecard.get("price"),
        "change_rate": scorecard.get("change_rate"),
        "volume": scorecard.get("volume"),
        "score": scorecard.get("score"),
        "sources": scorecard.get("sources") or [],
        "reasons": scorecard.get("reasons") or [],
        "penalties": scorecard.get("penalties") or [],
    }


def top_candidates_for_market(
    client: SignalArenaClient,
    market: str,
    *,
    portfolio_payload: dict[str, Any] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates, meta = collect_stock_candidates(client, market)
    whitelist = market_whitelist(market)
    blacklist = env_list("SIGNAL_ARENA_SYMBOL_BLACKLIST")
    recent = recent_symbols(float(os.getenv("SIGNAL_ARENA_RECENT_SYMBOL_PENALTY_HOURS", "8")))
    held = portfolio_symbols(portfolio_payload or {})

    scored = [
        {
            "stock": stock,
            "scorecard": score_stock(
                stock,
                recent=recent,
                held=held,
                whitelist=whitelist,
                blacklist=blacklist,
            ),
        }
        for stock in candidates
    ]
    eligible = [item for item in scored if item["scorecard"]["eligible"]]
    if not eligible:
        eligible = scored
    eligible.sort(key=lambda item: item["scorecard"]["score"], reverse=True)
    return eligible[:limit], {
        **meta,
        "top_candidate_limit": limit,
        "eligible_count": len(eligible),
        "whitelist_size": len(whitelist),
    }


def candidate_payload(candidates_by_market: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {
        market: [compact_scorecard(item["scorecard"]) for item in entries]
        for market, entries in candidates_by_market.items()
    }


def parse_selector_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("selector response did not contain a JSON object")
    return json.loads(stripped[start : end + 1])


def normalize_selector_picks(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw = payload.get("selections") if isinstance(payload.get("selections"), dict) else payload
    normalized: dict[str, list[dict[str, Any]]] = {}
    for market, picks in raw.items():
        market_key = str(market).upper()
        if not isinstance(picks, list):
            continue
        normalized[market_key] = []
        for pick in picks:
            if isinstance(pick, str):
                normalized[market_key].append({"symbol": pick})
            elif isinstance(pick, dict):
                normalized[market_key].append(dict(pick))
    return normalized


def resolve_selected_candidates(
    candidates_by_market: dict[str, list[dict[str, Any]]],
    selector_picks: dict[str, list[dict[str, Any]]],
    *,
    picks_per_market: int,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    resolved: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for market, candidates in candidates_by_market.items():
        by_symbol = {
            str(item["scorecard"].get("symbol") or "").lower(): item
            for item in candidates
            if item["scorecard"].get("symbol")
        }
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        for pick in selector_picks.get(market, []):
            symbol = str(pick.get("symbol") or pick.get("ticker") or "").lower()
            if not symbol:
                warnings.append(f"{market}: selector pick missing symbol")
                continue
            if symbol in selected_symbols:
                warnings.append(f"{market}: duplicate selector pick {symbol}")
                continue
            candidate = by_symbol.get(symbol)
            if not candidate:
                warnings.append(f"{market}: selector pick {symbol} is not in the top candidate pool")
                continue
            selected.append({**candidate, "selector_pick": pick})
            selected_symbols.add(symbol)
            if len(selected) >= picks_per_market:
                break
        if len(selected) < picks_per_market:
            for candidate in candidates:
                symbol = str(candidate["scorecard"].get("symbol") or "").lower()
                if not symbol or symbol in selected_symbols:
                    continue
                selected.append({**candidate, "selector_pick": {"reason": "local score fallback"}})
                selected_symbols.add(symbol)
                if len(selected) >= picks_per_market:
                    break
        if len(selected) < picks_per_market:
            warnings.append(f"{market}: only resolved {len(selected)} pick(s)")
        resolved[market] = selected[:picks_per_market]
    return resolved, warnings


def select_with_gpt(
    candidates: dict[str, list[dict[str, Any]]],
    *,
    picks_per_market: int,
    model: str,
    provider: str,
    base_url: str | None,
) -> tuple[dict[str, list[dict[str, Any]]], str]:
    llm_client = create_llm_client(
        provider=provider,
        model=model,
        base_url=base_url,
        timeout=int(os.getenv("DAILY_SELECTOR_TIMEOUT_SECONDS", "90")),
        max_retries=int(os.getenv("DAILY_SELECTOR_MAX_RETRIES", "2")),
    )
    llm = llm_client.get_llm()
    system_prompt = (
        "You are a disciplined equity selector. Choose stocks only from the provided candidate pools. "
        "Return JSON only. Do not include hidden reasoning, chain-of-thought, markdown, or prose."
    )
    target_markets = list(candidates.keys())
    target_description = ", ".join(f"exactly {picks_per_market} {market} stock(s)" for market in target_markets)
    schema = {
        "selections": {
            market: [{"symbol": "...", "reason": "..."} for _ in range(picks_per_market)]
            for market in target_markets
        }
    }
    user_prompt = (
        f"Choose {target_description}. "
        "Prefer liquid, explainable opportunities and avoid candidates with severe penalties. "
        f"Return this schema: {json.dumps(schema, ensure_ascii=False)}.\n\n"
        f"Candidates:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}"
    )
    response = llm.invoke([("system", system_prompt), ("user", user_prompt)])
    text = str(getattr(response, "content", response) or "")
    return normalize_selector_picks(parse_selector_json(text)), text


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_daily_result(result: dict[str, Any]) -> Path:
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    markets = "_".join(str(item).lower() for item in result.get("markets", [])) or "all"
    path = SIGNAL_DIR / f"daily_{result['mode']}_{markets}_{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    append_jsonl(DAILY_RUNS_PATH, result)
    return path


def analyze_selected_stock(
    *,
    market: str,
    selected: dict[str, Any],
    trade_date: str | None,
    analysts: list[str],
    timeout_seconds: int,
    execute_trade: bool,
    client: SignalArenaClient,
    home_payload: dict[str, Any],
    portfolio_payload: dict[str, Any],
    budget_fraction: float,
) -> dict[str, Any]:
    stock = selected["stock"]
    arena_symbol = str(stock["symbol"])
    ta_symbol = arena_to_tradingagents_symbol(arena_symbol)
    resolved_trade_date = trade_date or stock.get("trade_date") or exchange_trade_date(market)
    trace_id = f"daily_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{ta_symbol.replace('/', '_')}"
    trace_base = {
        "id": trace_id,
        "status": "running",
        "started_at": now_utc(),
        "mode": "daily_preopen",
        "market": market,
        "arena_symbol": arena_symbol,
        "tradingagents_symbol": ta_symbol,
        "stock_name": stock.get("name"),
        "trade_date": resolved_trade_date,
        "analysts": analysts,
        "execute_trade": execute_trade,
        "selector_pick": selected.get("selector_pick") or {},
        "scorecard": selected.get("scorecard") or {},
        "sections": [
            text_section(
                "Run",
                f"Daily TradingAgents analysis for {ta_symbol} ({stock.get('name') or arena_symbol}).",
            )
        ],
    }
    write_conversation_trace(trace_base)

    try:
        state, signal = run_with_timeout(ta_symbol, resolved_trade_date, analysts, timeout_seconds)
    except Exception as exc:
        write_conversation_trace(
            {
                **trace_base,
                "status": "failed",
                "finished_at": now_utc(),
                "error": str(exc),
                "sections": trace_base["sections"] + [text_section("Error", str(exc))],
            }
        )
        raise

    final_decision = state.get("final_trade_decision", "")
    final_rating = state.get("final_trade_rating") or signal
    action = state.get("final_trade_action") or action_from_signal(f"{final_decision}\n{final_rating}")
    cash = extract_cash(home_payload) if home_payload else None
    if action == "buy":
        shares = shares_for_buy(stock, cash, budget_fraction)
    elif action == "sell":
        shares = extract_holding_shares(portfolio_payload, arena_symbol)
    else:
        shares = 0

    result: dict[str, Any] = {
        "market": market,
        "stock": stock,
        "scorecard": selected.get("scorecard") or {},
        "selector_pick": selected.get("selector_pick") or {},
        "tradingagents_symbol": ta_symbol,
        "trade_date": resolved_trade_date,
        "conversation_trace_id": trace_id,
        "signal": signal,
        "final_trade_rating": final_rating,
        "final_trade_action": action,
        "shares": shares,
        "execute_trade": execute_trade,
        "final_trade_decision": final_decision,
    }

    if execute_trade:
        if not client.api_key:
            raise RuntimeError(f"--execute-trade requires {API_KEY_ENV}")
        if action == "hold":
            result["trade"] = {"skipped": True, "reason": "signal is HOLD"}
        elif shares <= 0:
            result["trade"] = {"skipped": True, "reason": "computed shares <= 0"}
        else:
            reason = f"Daily TradingAgents {final_rating} ({action}) for {ta_symbol}. {final_decision}"
            result["trade"] = client.trade(arena_symbol, action, shares, reason)

    write_conversation_trace(
        {
            **trace_base,
            "status": "completed",
            "finished_at": now_utc(),
            "action": action,
            "shares": shares,
            "trade": result.get("trade"),
            "signal": signal,
            "final_trade_rating": final_rating,
            "final_trade_decision": final_decision,
            "sections": conversation_sections_from_state(state, signal),
        }
    )
    return result


def run_preopen(args: argparse.Namespace, client: SignalArenaClient) -> dict[str, Any]:
    markets = parse_markets(args.markets)
    if args.execute_trade and not client.api_key:
        raise RuntimeError(f"--execute-trade requires {API_KEY_ENV}")
    home = client.home() if client.api_key else {}
    portfolio = client.portfolio() if client.api_key else {}

    candidates_by_market: dict[str, list[dict[str, Any]]] = {}
    metas: dict[str, dict[str, Any]] = {}
    for market in markets:
        entries, meta = top_candidates_for_market(
            client,
            market,
            portfolio_payload=portfolio,
            limit=args.candidate_limit,
        )
        candidates_by_market[market] = entries
        metas[market] = meta

    candidates_for_llm = candidate_payload(candidates_by_market)
    selector_response = ""
    if args.selector_json:
        selector_picks = normalize_selector_picks(parse_selector_json(args.selector_json))
        selector_response = args.selector_json
    else:
        selector_picks, selector_response = select_with_gpt(
            candidates_for_llm,
            picks_per_market=args.picks_per_market,
            model=args.selector_model,
            provider=args.selector_provider,
            base_url=args.selector_base_url,
        )
    selected_by_market, warnings = resolve_selected_candidates(
        candidates_by_market,
        selector_picks,
        picks_per_market=args.picks_per_market,
    )

    analysts = [item.strip() for item in args.analysts.split(",") if item.strip()]
    analyses: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for market, selections in selected_by_market.items():
        for selected in selections:
            try:
                analyses.append(
                    analyze_selected_stock(
                        market=market,
                        selected=selected,
                        trade_date=args.trade_date,
                        analysts=analysts,
                        timeout_seconds=args.agent_timeout_seconds,
                        execute_trade=args.execute_trade,
                        client=client,
                        home_payload=home,
                        portfolio_payload=portfolio,
                        budget_fraction=args.budget_fraction,
                    )
                )
            except TimeoutExpired as exc:
                errors.append({"symbol": str(selected["scorecard"].get("symbol")), "error": str(exc)})
                if args.stop_on_error:
                    raise
            except Exception as exc:
                errors.append({"symbol": str(selected["scorecard"].get("symbol")), "error": str(exc)})
                if args.stop_on_error:
                    raise

    return {
        "mode": "preopen",
        "timestamp": now_utc(),
        "markets": markets,
        "selector": {
            "provider": args.selector_provider,
            "model": args.selector_model,
            "raw_response": selector_response,
        },
        "candidate_limit": args.candidate_limit,
        "picks_per_market": args.picks_per_market,
        "top_candidates": candidates_for_llm,
        "candidate_meta": metas,
        "selected": {
            market: [
                {
                    "scorecard": item.get("scorecard") or {},
                    "selector_pick": item.get("selector_pick") or {},
                }
                for item in selections
            ]
            for market, selections in selected_by_market.items()
        },
        "warnings": warnings,
        "analysts": analysts,
        "analyses": analyses,
        "errors": errors,
        "execute_trade": args.execute_trade,
        "auth_configured": bool(client.api_key),
    }


def holding_shares(holding: dict[str, Any]) -> int:
    for key in ("shares", "quantity", "available_shares"):
        value = holding.get(key)
        if isinstance(value, (int, float)):
            return max(int(value), 0)
    return 0


def portfolio_holdings(portfolio_payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = portfolio_payload.get("data", {}) if isinstance(portfolio_payload, dict) else {}
    holdings = data.get("holdings") or []
    return holdings if isinstance(holdings, list) else []


def close_positions(args: argparse.Namespace, client: SignalArenaClient) -> dict[str, Any]:
    if args.execute_trade and not client.api_key:
        raise RuntimeError(f"--execute-trade requires {API_KEY_ENV}")
    markets = parse_markets(args.markets)
    portfolio = client.portfolio()
    closed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for holding in portfolio_holdings(portfolio):
        symbol = str(holding.get("symbol") or "")
        if not symbol:
            skipped.append({"holding": holding, "reason": "missing symbol"})
            continue
        market = market_from_arena_symbol(symbol, holding.get("market"))
        if markets and market not in markets:
            skipped.append({"symbol": symbol, "market": market, "reason": "outside selected markets"})
            continue
        shares = holding_shares(holding)
        if shares <= 0:
            skipped.append({"symbol": symbol, "market": market, "reason": "shares <= 0"})
            continue
        row: dict[str, Any] = {
            "symbol": symbol,
            "market": market,
            "shares": shares,
            "execute_trade": args.execute_trade,
        }
        if args.execute_trade:
            row["trade"] = client.trade(symbol, "sell", shares, "Daily end-of-session close-out")
        else:
            row["trade"] = {"skipped": True, "reason": "dry-run"}
        closed.append(row)

    return {
        "mode": "close",
        "timestamp": now_utc(),
        "markets": markets,
        "execute_trade": args.execute_trade,
        "auth_configured": bool(client.api_key),
        "closed": closed,
        "skipped": skipped,
        "portfolio": portfolio,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily GPT selector + TradingAgents automation")
    parser.add_argument("--mode", choices=["preopen", "close"], default="preopen")
    parser.add_argument("--markets", default=os.getenv("DAILY_TRADING_MARKETS", "CN,US"))
    parser.add_argument("--candidate-limit", type=int, default=int(os.getenv("DAILY_CANDIDATE_LIMIT", "10")))
    parser.add_argument("--picks-per-market", type=int, default=int(os.getenv("DAILY_PICKS_PER_MARKET", "2")))
    parser.add_argument("--selector-provider", default=os.getenv("DAILY_SELECTOR_PROVIDER", "openai"))
    parser.add_argument("--selector-model", default=os.getenv("DAILY_SELECTOR_MODEL", "gpt-5.4"))
    parser.add_argument("--selector-base-url", default=os.getenv("DAILY_SELECTOR_BASE_URL") or None)
    parser.add_argument("--selector-json", help="Testing/debug override: raw selector JSON response")
    parser.add_argument("--trade-date", help="Override TradingAgents trade date")
    parser.add_argument("--analysts", default=os.getenv("TRADINGAGENTS_ANALYSTS", "market,social,news,fundamentals"))
    parser.add_argument(
        "--agent-timeout-seconds",
        type=int,
        default=int(os.getenv("TRADINGAGENTS_AGENT_TIMEOUT_SECONDS", "1200")),
    )
    parser.add_argument("--budget-fraction", type=float, default=float(os.getenv("SIGNAL_ARENA_BUDGET_FRACTION", "0.10")))
    parser.add_argument("--execute-trade", action="store_true", help="Submit Signal Arena orders")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop after the first TradingAgents failure")
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    client = SignalArenaClient(BASE_URL, os.getenv(API_KEY_ENV))
    result = close_positions(args, client) if args.mode == "close" else run_preopen(args, client)
    out_path = write_daily_result(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
