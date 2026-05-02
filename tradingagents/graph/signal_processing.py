# TradingAgents/graph/signal_processing.py

from typing import Any

from tradingagents.llm_clients.base_client import strip_reasoning_traces
from tradingagents.rating import extract_rating, normalize_rating


class SignalProcessor:
    """Processes trading signals to extract actionable decisions."""

    def __init__(self, quick_thinking_llm: Any):
        """Initialize with an LLM for processing."""
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """
        Process a full trading signal to extract the core decision.

        Args:
            full_signal: Complete trading signal text

        Returns:
            Extracted rating (BUY, OVERWEIGHT, HOLD, UNDERWEIGHT, or SELL)
        """
        cleaned_signal = strip_reasoning_traces(full_signal)
        extracted = extract_rating(cleaned_signal)
        if extracted:
            return extracted

        messages = [
            (
                "system",
                "You are an efficient assistant that extracts the trading decision from analyst reports. "
                "Extract the rating as exactly one of: Buy, Overweight, Hold, Underweight, Sell. "
                "Output only the single rating word, nothing else.",
            ),
            ("human", cleaned_signal),
        ]

        model_rating = strip_reasoning_traces(self.quick_thinking_llm.invoke(messages).content)
        return normalize_rating(model_rating) or "Hold"
