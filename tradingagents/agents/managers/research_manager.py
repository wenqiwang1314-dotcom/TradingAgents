
from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.rating import RATING_SCALE_PROMPT


def create_research_manager(llm):
    def research_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        prompt = f"""As the portfolio manager and debate facilitator, your role is to critically evaluate this round of debate and make a definitive decision: align with the bear analyst, align with the bull analyst, or choose a neutral stance only if it is strongly justified based on the arguments presented.

{RATING_SCALE_PROMPT}

Summarize the key points from both sides concisely, focusing on the most compelling evidence or reasoning. Your recommendation must use exactly one five-point rating: Buy, Overweight, Hold, Underweight, or Sell. Avoid defaulting to Hold simply because both sides have valid points; commit to a stance grounded in the debate's strongest arguments.

Additionally, develop a detailed investment plan for the trader. This should include:

Your Recommendation: A decisive five-point rating supported by the most convincing arguments.
Trade Action: BUY, HOLD, or SELL using the mapping above.
Rationale: An explanation of why these arguments lead to your conclusion.
Strategic Actions: Concrete steps for implementing the recommendation.
Present your analysis conversationally, as if speaking naturally, without special formatting.

{instrument_context}

Here is the debate:
Debate History:
{history}"""
        response = llm.invoke(prompt)

        new_investment_debate_state = {
            "judge_decision": response.content,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": response.content,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": response.content,
        }

    return research_manager_node
