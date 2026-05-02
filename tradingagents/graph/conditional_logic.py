# TradingAgents/graph/conditional_logic.py

from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.agents.utils.agent_utils import (
    analyst_tool_call_count,
    has_analyst_final_marker,
    has_repeated_tool_call,
)


class ConditionalLogic:
    """Handles conditional logic for determining graph flow."""

    def __init__(
        self,
        max_debate_rounds=1,
        max_risk_discuss_rounds=1,
        max_analyst_tool_iterations=4,
    ):
        """Initialize with configuration parameters."""
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_discuss_rounds = max_risk_discuss_rounds
        self.max_analyst_tool_iterations = max_analyst_tool_iterations

    def _should_continue_analyst(self, state: AgentState, tool_node: str, clear_node: str):
        messages = state["messages"]
        last_message = messages[-1]

        if has_analyst_final_marker(last_message):
            return clear_node

        if not getattr(last_message, "tool_calls", None):
            return clear_node

        if has_repeated_tool_call(messages):
            return clear_node

        if analyst_tool_call_count(messages) > self.max_analyst_tool_iterations:
            return clear_node

        return tool_node

    def should_continue_market(self, state: AgentState):
        """Determine if market analysis should continue."""
        return self._should_continue_analyst(
            state, "tools_market", "Msg Clear Market"
        )

    def should_continue_social(self, state: AgentState):
        """Determine if social media analysis should continue."""
        return self._should_continue_analyst(
            state, "tools_social", "Msg Clear Social"
        )

    def should_continue_news(self, state: AgentState):
        """Determine if news analysis should continue."""
        return self._should_continue_analyst(
            state, "tools_news", "Msg Clear News"
        )

    def should_continue_fundamentals(self, state: AgentState):
        """Determine if fundamentals analysis should continue."""
        return self._should_continue_analyst(
            state, "tools_fundamentals", "Msg Clear Fundamentals"
        )

    def should_continue_debate(self, state: AgentState) -> str:
        """Determine if debate should continue."""

        if (
            state["investment_debate_state"]["count"] >= 2 * self.max_debate_rounds
        ):  # 3 rounds of back-and-forth between 2 agents
            return "Research Manager"
        if state["investment_debate_state"]["current_response"].startswith("Bull"):
            return "Bear Researcher"
        return "Bull Researcher"

    def should_continue_risk_analysis(self, state: AgentState) -> str:
        """Determine if risk analysis should continue."""
        if (
            state["risk_debate_state"]["count"] >= 3 * self.max_risk_discuss_rounds
        ):  # 3 rounds of back-and-forth between 3 agents
            return "Portfolio Manager"
        if state["risk_debate_state"]["latest_speaker"].startswith("Aggressive"):
            return "Conservative Analyst"
        if state["risk_debate_state"]["latest_speaker"].startswith("Conservative"):
            return "Neutral Analyst"
        return "Aggressive Analyst"
