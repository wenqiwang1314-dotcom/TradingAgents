import json
import re

from langchain_core.messages import HumanMessage, RemoveMessage
from tradingagents.llm_clients.base_client import strip_reasoning_traces


ANALYST_FINAL_MARKER = "[FINAL_ANALYST_REPORT]"

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)


def get_language_instruction() -> str:
    """Return prompt instructions for user-facing output formatting.

    Only applied to user-facing agents (analysts, portfolio manager). Internal
    debate agents stay in English for reasoning quality.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    no_reasoning = (
        " Do not include hidden reasoning, scratchpad text, internal "
        "deliberation, or <think>/<reasoning> tags. Output only the final "
        "user-facing report or decision."
    )
    if lang.strip().lower() == "english":
        return no_reasoning
    return f" Write your entire response in {lang}.{no_reasoning}"


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`)."
    )


def message_content_text(message) -> str:
    """Return message content as plain text across provider content shapes."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return strip_reasoning_traces(content)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return strip_reasoning_traces("\n".join(parts))
    return strip_reasoning_traces(str(content or ""))


def has_analyst_final_marker(message) -> bool:
    return bool(
        re.search(
            rf"(?m)^\s*{re.escape(ANALYST_FINAL_MARKER)}\b",
            message_content_text(message),
        )
    )


def analyst_tool_call_count(messages) -> int:
    return sum(1 for message in messages if getattr(message, "tool_calls", None))


def tool_call_signature(tool_call) -> str:
    """Create a stable signature for repeat-call detection."""
    if isinstance(tool_call, dict):
        name = tool_call.get("name") or tool_call.get("function", {}).get("name")
        args = tool_call.get("args") or tool_call.get("function", {}).get("arguments")
    else:
        name = getattr(tool_call, "name", None)
        args = getattr(tool_call, "args", None)

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            pass

    try:
        normalized_args = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    except TypeError:
        normalized_args = str(args)
    return f"{name}:{normalized_args}"


def has_repeated_tool_call(messages) -> bool:
    """Return True when the latest tool call exactly repeats an earlier one."""
    if not messages:
        return False

    last_calls = getattr(messages[-1], "tool_calls", None) or []
    if not last_calls:
        return False

    previous = set()
    for message in messages[:-1]:
        for tool_call in getattr(message, "tool_calls", None) or []:
            previous.add(tool_call_signature(tool_call))

    return any(tool_call_signature(tool_call) in previous for tool_call in last_calls)


def analyst_convergence_instruction(tool_call_count: int, max_tool_calls: int) -> str:
    """Prompt addendum that keeps local models from drifting through tools."""
    remaining = max_tool_calls - tool_call_count
    if remaining <= 0:
        return (
            f"\nYou have exhausted the analyst tool budget. You MUST NOT call any "
            f"more tools. Use the current context and output your final analyst "
            f"report now, starting with {ANALYST_FINAL_MARKER}."
        )
    if remaining <= 1:
        return (
            f"\nYou are running out of tool budget. You may make at most {remaining} "
            f"more tool call. Prefer not to call tools. If you have enough "
            f"information, output your final analyst report now, starting with "
            f"{ANALYST_FINAL_MARKER}."
        )
    return (
        f"\nTool budget for this analyst: you may make at most {max_tool_calls} "
        f"total tool-call turns. When you have enough information, stop using "
        f"tools and output your final analyst report starting with "
        f"{ANALYST_FINAL_MARKER}."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
