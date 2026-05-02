from argparse import Namespace
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.daily_auto_tradingagents import (
    close_positions,
    market_from_arena_symbol,
    normalize_selector_picks,
    parse_markets,
    parse_selector_json,
    resolve_selected_candidates,
)


def _candidate(symbol: str, score: float) -> dict:
    return {
        "stock": {"symbol": symbol, "name": symbol, "price": 10.0},
        "scorecard": {
            "symbol": symbol,
            "name": symbol,
            "market": "CN" if symbol.startswith(("sh", "sz")) else "US",
            "score": score,
            "eligible": True,
        },
    }


def test_parse_selector_json_accepts_fenced_json():
    payload = parse_selector_json(
        """```json
        {"selections":{"CN":[{"symbol":"sh600519"}],"US":["gb_aapl"]}}
        ```"""
    )
    assert payload["selections"]["CN"][0]["symbol"] == "sh600519"
    assert normalize_selector_picks(payload)["US"][0]["symbol"] == "gb_aapl"


def test_resolve_selected_candidates_falls_back_to_local_score():
    candidates = {
        "CN": [_candidate("sh600519", 91), _candidate("sz000001", 80), _candidate("sh601318", 70)],
        "US": [_candidate("gb_nvda", 95), _candidate("gb_aapl", 88), _candidate("gb_msft", 77)],
    }
    picks = {
        "CN": [{"symbol": "not_in_pool"}, {"symbol": "sh600519"}],
        "US": [{"symbol": "gb_nvda"}, {"symbol": "gb_nvda"}],
    }

    resolved, warnings = resolve_selected_candidates(candidates, picks, picks_per_market=2)

    assert [item["scorecard"]["symbol"] for item in resolved["CN"]] == ["sh600519", "sz000001"]
    assert [item["scorecard"]["symbol"] for item in resolved["US"]] == ["gb_nvda", "gb_aapl"]
    assert any("not in the top candidate pool" in warning for warning in warnings)
    assert any("duplicate" in warning for warning in warnings)


def test_market_helpers():
    assert parse_markets("CN, US") == ["CN", "US"]
    assert parse_markets("") == ["CN", "US"]
    assert market_from_arena_symbol("gb_aapl") == "US"
    assert market_from_arena_symbol("sh600519") == "CN"
    assert market_from_arena_symbol("sz000001") == "CN"
    assert market_from_arena_symbol("hk00700") == "HK"


def test_close_positions_dry_run_filters_markets():
    class FakeClient:
        api_key = "key"

        def portfolio(self):
            return {
                "data": {
                    "holdings": [
                        {"symbol": "gb_aapl", "shares": 3},
                        {"symbol": "sh600519", "quantity": 200},
                        {"symbol": "hk00700", "shares": 10},
                        {"symbol": "gb_msft", "shares": 0},
                    ]
                }
            }

        def trade(self, symbol, action, shares, reason):  # pragma: no cover
            raise AssertionError("dry-run should not submit trades")

    args = Namespace(markets="CN,US", execute_trade=False)

    result = close_positions(args, FakeClient())

    assert [(row["symbol"], row["shares"]) for row in result["closed"]] == [
        ("gb_aapl", 3),
        ("sh600519", 200),
    ]
    assert all(row["trade"]["skipped"] for row in result["closed"])
    assert any(row.get("symbol") == "hk00700" for row in result["skipped"])
    assert any(row.get("symbol") == "gb_msft" for row in result["skipped"])
