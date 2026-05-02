from pathlib import Path
from types import SimpleNamespace

from tradingagents.agents.utils.agent_utils import message_content_text
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.llm_clients.base_client import normalize_content, strip_reasoning_traces


def test_strip_reasoning_traces_removes_complete_blocks():
    text = """
<think>
I should not leak this scratchpad.
</think>
[FINAL_ANALYST_REPORT]
Use the evidence and hold.
"""

    cleaned = strip_reasoning_traces(text)

    assert "<think>" not in cleaned
    assert "scratchpad" not in cleaned
    assert cleaned.startswith("[FINAL_ANALYST_REPORT]")


def test_strip_reasoning_traces_keeps_final_marker_after_unclosed_block():
    text = "<think>draft without close\n[FINAL_ANALYST_REPORT]\nRating: Hold"

    cleaned = strip_reasoning_traces(text)

    assert cleaned == "[FINAL_ANALYST_REPORT]\nRating: Hold"


def test_normalize_content_strips_reasoning_from_string_and_blocks():
    response = SimpleNamespace(
        content=[
            {"type": "reasoning", "text": "hidden"},
            {"type": "text", "text": "<think>hidden</think>Rating: Buy"},
        ]
    )

    normalized = normalize_content(response)

    assert normalized.content == "Rating: Buy"


def test_message_content_text_strips_reasoning():
    message = SimpleNamespace(content="<reasoning>hidden</reasoning>Visible report")

    assert message_content_text(message) == "Visible report"


def test_memory_log_strips_reasoning_before_writing(tmp_path: Path):
    log_path = tmp_path / "memory.md"
    log = TradingMemoryLog({"memory_log_path": str(log_path)})

    log.store_decision(
        "NVDA",
        "2026-05-02",
        "<think>hidden</think>Rating: Buy\nBuy NVDA.",
    )

    stored = log_path.read_text(encoding="utf-8")
    assert "<think>" not in stored
    assert "hidden" not in stored
    assert "Rating: Buy" in stored
