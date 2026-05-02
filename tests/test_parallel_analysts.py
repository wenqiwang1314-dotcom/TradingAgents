import threading
import time

from langchain_core.messages import AIMessage

import tradingagents.graph.setup as graph_setup_module
from tradingagents.agents.utils.agent_states import InvestDebateState, RiskDebateState
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.prefetch import run_parallel_tasks
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup


ANALYSTS = ["market", "social", "news", "fundamentals"]
REPORT_KEYS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def _debate_state(count=0, current_response=""):
    return InvestDebateState(
        {
            "bull_history": "",
            "bear_history": "",
            "history": current_response,
            "current_response": current_response,
            "judge_decision": "",
            "count": count,
        }
    )


def _risk_state(count=0, latest_speaker=""):
    return RiskDebateState(
        {
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "history": "",
            "latest_speaker": latest_speaker,
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": count,
        }
    )


def test_selected_analysts_run_in_parallel_and_join_before_research(monkeypatch):
    monkeypatch.setattr(
        graph_setup_module,
        "get_config",
        lambda: {"parallel_data_prefetch_enabled": False},
    )

    entered = set()
    lock = threading.Lock()
    all_entered = threading.Event()

    def make_analyst_factory(analyst_type):
        def factory(llm):
            def node(state):
                assert [message.content for message in state["messages"]] == ["TEST"]

                with lock:
                    entered.add(analyst_type)
                    if len(entered) == len(ANALYSTS):
                        all_entered.set()

                assert all_entered.wait(timeout=1.0)

                return {
                    "messages": [AIMessage(content=f"{analyst_type} complete")],
                    REPORT_KEYS[analyst_type]: f"{analyst_type} report",
                }

            return node

        return factory

    for analyst_type in ANALYSTS:
        monkeypatch.setattr(
            graph_setup_module,
            f"create_{analyst_type}_analyst"
            if analyst_type != "social"
            else "create_social_media_analyst",
            make_analyst_factory(analyst_type),
        )

    def bull_node(state):
        assert state["market_report"] == "market report"
        assert state["sentiment_report"] == "social report"
        assert state["news_report"] == "news report"
        assert state["fundamentals_report"] == "fundamentals report"
        return {
            "investment_debate_state": _debate_state(
                count=1, current_response="Bull"
            )
        }

    monkeypatch.setattr(
        graph_setup_module, "create_bull_researcher", lambda llm: bull_node
    )
    monkeypatch.setattr(
        graph_setup_module,
        "create_bear_researcher",
        lambda llm: lambda state: {"investment_debate_state": _debate_state()},
    )
    monkeypatch.setattr(
        graph_setup_module,
        "create_research_manager",
        lambda llm: lambda state: {"investment_plan": "plan"},
    )
    monkeypatch.setattr(
        graph_setup_module,
        "create_trader",
        lambda llm: lambda state: {"trader_investment_plan": "trader plan"},
    )
    monkeypatch.setattr(
        graph_setup_module,
        "create_aggressive_debator",
        lambda llm: lambda state: {
            "risk_debate_state": _risk_state(count=1, latest_speaker="Aggressive")
        },
    )
    monkeypatch.setattr(
        graph_setup_module,
        "create_neutral_debator",
        lambda llm: lambda state: {"risk_debate_state": _risk_state()},
    )
    monkeypatch.setattr(
        graph_setup_module,
        "create_conservative_debator",
        lambda llm: lambda state: {"risk_debate_state": _risk_state()},
    )
    monkeypatch.setattr(
        graph_setup_module,
        "create_portfolio_manager",
        lambda llm: lambda state: {"final_trade_decision": "Rating: Hold"},
    )

    tool_nodes = {
        analyst_type: lambda state: {"messages": []} for analyst_type in ANALYSTS
    }
    graph_setup = GraphSetup(
        quick_thinking_llm=object(),
        deep_thinking_llm=object(),
        tool_nodes=tool_nodes,
        conditional_logic=ConditionalLogic(
            max_debate_rounds=0, max_risk_discuss_rounds=0
        ),
    )
    graph = graph_setup.setup_graph(ANALYSTS).compile()
    initial_state = Propagator(max_concurrency=4).create_initial_state(
        "TEST", "2026-05-02"
    )

    result = graph.invoke(
        initial_state,
        config={"recursion_limit": 20, "max_concurrency": 4},
    )

    assert entered == set(ANALYSTS)
    assert result["final_trade_decision"] == "Rating: Hold"


def test_parallel_prefetch_metrics_capture_wall_time_and_speedup(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.graph.prefetch.get_config",
        lambda: {"max_data_fetch_concurrency": 3},
    )

    def delayed_result(name):
        time.sleep(0.05)
        return f"{name} data"

    results, metrics = run_parallel_tasks(
        "test",
        [
            ("one", lambda: delayed_result("one")),
            ("two", lambda: delayed_result("two")),
            ("three", lambda: delayed_result("three")),
        ],
    )

    assert results == {
        "one": "one data",
        "two": "two data",
        "three": "three data",
    }
    assert metrics["task_count"] == 3
    assert metrics["max_workers"] == 3
    assert metrics["wall_time_sec"] < metrics["serial_tool_time_sec"]
    assert metrics["estimated_time_saved_sec"] > 0
    assert metrics["speedup"] > 1
    assert all(task["status"] == "ok" for task in metrics["tasks"].values())
