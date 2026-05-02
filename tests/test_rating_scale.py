from unittest.mock import MagicMock

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.trader.trader import create_trader
from tradingagents.rating import action_from_rating, action_from_signal, extract_rating
from tradingagents.graph.signal_processing import SignalProcessor


def _pm_state():
    return {
        "company_of_interest": "NVDA",
        "market_report": "Market report.",
        "sentiment_report": "Sentiment report.",
        "news_report": "News report.",
        "fundamentals_report": "Fundamentals report.",
        "investment_plan": "Rating: Overweight\nTrade Action: BUY",
        "trader_investment_plan": "Rating: Overweight\nTrade Action: BUY",
        "past_context": "",
        "risk_debate_state": {
            "history": "Risk debate.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 0,
        },
    }


def test_rating_to_action_mapping():
    assert action_from_rating("Buy") == "buy"
    assert action_from_rating("Overweight") == "buy"
    assert action_from_rating("Hold") == "hold"
    assert action_from_rating("Underweight") == "sell"
    assert action_from_rating("Sell") == "sell"
    assert action_from_rating("not-a-rating") == "hold"


def test_action_from_signal_extracts_five_point_rating():
    assert action_from_signal("Rating: Overweight\nBuild position gradually.") == "buy"
    assert action_from_signal("**Rating**: **Underweight**\nReduce exposure.") == "sell"
    assert extract_rating("1. Rating: Buy\nAction: BUY") == "Buy"


def test_signal_processor_prefers_deterministic_rating_extraction():
    llm = MagicMock()
    processor = SignalProcessor(llm)

    assert processor.process_signal("Rating: Underweight\nTrade Action: SELL") == "Underweight"
    llm.invoke.assert_not_called()


def test_trader_prompt_uses_five_point_rating_and_action_mapping():
    captured = {}
    llm = MagicMock()
    llm.invoke.side_effect = lambda messages: (
        captured.__setitem__("messages", messages)
        or MagicMock(content="Rating: Hold\nTrade Action: HOLD")
    )
    node = create_trader(llm)

    node({"company_of_interest": "NVDA", "investment_plan": "Plan."})

    system_prompt = captured["messages"][0]["content"]
    assert "Buy / Overweight -> Trade Action: BUY" in system_prompt
    assert "Underweight / Sell -> Trade Action: SELL" in system_prompt
    assert "Buy / Overweight / Hold / Underweight / Sell" in system_prompt
    assert "FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**" not in system_prompt


def test_portfolio_manager_prompt_uses_same_rating_and_action_mapping():
    captured = {}
    llm = MagicMock()
    llm.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt)
        or MagicMock(content="Rating: Hold\nTrade Action: HOLD")
    )
    node = create_portfolio_manager(llm)

    node(_pm_state())

    prompt = captured["prompt"]
    assert "Buy / Overweight -> Trade Action: BUY" in prompt
    assert "Underweight / Sell -> Trade Action: SELL" in prompt
    assert "1. **Rating**: State one of Buy / Overweight / Hold / Underweight / Sell." in prompt
    assert "2. **Trade Action**: State one of BUY / HOLD / SELL" in prompt
