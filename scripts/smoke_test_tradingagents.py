#!/usr/bin/env python3
"""
Smoke test for TradingAgents - Market Analyst Only (Level 1)
Tests that the market analyst exits tool loop correctly.
"""

import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

def test_market_analyst_only():
    """Test market analyst with tool loop control."""
    print("Running Level 1 smoke test: Market Analyst Only")
    print("=" * 50)

    # Configuration for Level 1 testing based on LOCAL_VLLM_NEMOTRON_USAGE.md
    config = DEFAULT_CONFIG.copy()
    config.update({
        "llm_provider": "openai",
        "backend_url": "http://localhost:5000/v1",
        "deep_think_llm": "nvidia/nemotron-3-super",
        "quick_think_llm": "nvidia/nemotron-3-super",
        "max_analyst_tool_iterations": 4,
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "output_language": "English",
        "data_cache_dir": "./data_cache",
        "results_dir": "./results",
    })

    # Create TradingAgents instance with only market analyst
    ta = TradingAgentsGraph(
        selected_analysts=["market"],  # Level 1: market only
        debug=False,  # Set to False to reduce output noise
        config=config
    )

    # Test with a recent date and well-known ticker
    company_name = "AAPL"
    trade_date = "2024-10-03"  # A date from our test.py output

    print(f"Testing {company_name} on {trade_date}")
    print(f"Max analyst tool iterations: {config['max_analyst_tool_iterations']}")
    print(f"Max recursion limit: {config.get('max_recur_limit', 100)}")
    print()

    try:
        # Run the analysis
        state, signal = ta.propagate(company_name, trade_date)

        print()
        print("Analysis completed successfully!")
        print(f"Final decision: {state.get('final_trade_decision', 'N/A')}")

        # Check if we got reports
        market_report = state.get('market_report', '')
        if market_report:
            print(f"Market report length: {len(market_report)} characters")
            # Check for final marker
            if "[FINAL_ANALYST_REPORT]" in market_report:
                print("✓ Found [FINAL_ANALYST_REPORT] marker")
            else:
                print("⚠ No [FINAL_ANALYST_REPORT] marker found")

            # Show beginning of report
            print("\nMarket report preview:")
            print("-" * 40)
            print(market_report[:500] + ("..." if len(market_report) > 500 else ""))
            print("-" * 40)

        return True

    except Exception as e:
        print(f"Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_market_analyst_only()
    sys.exit(0 if success else 1)