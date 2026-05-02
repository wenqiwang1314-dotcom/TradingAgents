# CLAUDE.md — TradingAgents on DGX Spark (vLLM + Nemotron)

## 🎯 System Goal

This workspace is used to:

* Run **TradingAgents (LangGraph multi-agent system)**
* Use **local vLLM backend (Nemotron 3 Super)**
* Benchmark **agent stability + tool-calling behavior + concurrency**
* Debug **infinite tool loops / recursion issues**

---

## 🖥️ Environment

Project root:

```
/home/lucas/ai/TradingAgents
```

Python env:

```
.venv (managed via uv)
```

LLM backend:

```
http://localhost:5000/v1
model: nvidia/nemotron-3-super
```

---

## ⚠️ Hard Constraints (MUST FOLLOW)

* DO NOT stop vLLM service
* DO NOT modify CUDA / PyTorch / Triton
* DO NOT kill running processes
* DO NOT install heavy new dependencies
* DO NOT attempt real trading execution
* ONLY modify project code minimally and safely

---

## 🧠 Architecture Understanding

TradingAgents = LangGraph-based system:

```
Analyst → Tool → Analyst loop
→ Research Debate → Risk → Portfolio Decision
```

Key problem:

```
Nemotron may not stop tool-calling → infinite loop
```

---

## 🔧 Key Files

Focus areas:

```
tradingagents/graph/trading_graph.py
tradingagents/graph/conditional_logic.py
tradingagents/agents/analysts/
tradingagents/llm_clients/openai_client.py
```

---

## ✅ Baseline Checks (ALWAYS RUN FIRST)

Run in order:

```bash
uv run python -m pytest -q
uv run python test.py
curl -s http://localhost:5000/v1/models
```

---

## 🧪 Testing Levels

### Level 1 — Market Analyst Only

Goal: ensure tool loop exits correctly

* selected_analysts = ["market"]
* max_analyst_tool_iterations = 4

Expected:

```
tool_calls → tool_calls → tool_calls → FINAL_REPORT
```

---

### Level 2 — Partial Graph

```
selected_analysts = ["market", "news"]
```

---

### Level 3 — Full Graph

```
["market", "social", "news", "fundamentals"]
```

---

## 🔁 Loop Problem Definition

Loop occurs when:

```
last_message.tool_calls != empty
→ route = tools_market
→ back to analyst
→ repeats
```

---

## 🛑 Loop Control Mechanisms (MUST PRESERVE)

### 1. Tool Budget

```
config["max_analyst_tool_iterations"] = 4
```

### 2. Final Marker

```
[FINAL_ANALYST_REPORT]
```

### 3. Repeated Tool Detection

Same tool + same args → terminate

---

## 🧪 Smoke Test Command

Run minimal test:

```bash
uv run python scripts/smoke_test_tradingagents.py
```

If not exists → create it.

---

## ⚡ Concurrency Benchmark

Target:

```
1 / 2 / 4 concurrent TradingAgents runs
```

Measure:

* success rate
* latency
* recursion failures
* tool call counts

Output:

```
results/concurrency_report.md
```

---

## 🔍 Debugging (VERY IMPORTANT)

If loop happens:

### Step 1: Inspect logs

Add TRACE:

```
[TRACE] step=3 | node=market_analyst → tools_market
```

### Step 2: Check tool pattern

Bad pattern:

```
get_indicators(macd)
get_indicators(macd)
get_indicators(macd)
```

### Step 3: Check finish_reason

Expected:

```
finish_reason = stop
```

Bad:

```
finish_reason = tool_calls (forever)
```

---

## 📊 vLLM Payload Debug

Optional:

```
/tmp/tradingagents_vllm_payloads.jsonl
```

Inspect:

* tool_calls
* content
* repetition

---

## 🤖 Claude Code CLI Role

Claude Code is **NOT the LLM backend**.

Claude Code is used for:

* reading code
* writing test scripts
* modifying prompts
* analyzing logs
* generating reports

---

## 🚀 Recommended Claude Commands

### Analyze system

```
Run tests, check vLLM connectivity, summarize system health.
```

### Build benchmark

```
Create concurrency benchmark script and generate report.
```

### Fix loop

```
Detect repeated tool calls and enforce early exit.
```

---

## ❌ DO NOT DO

* Do NOT embed Claude inside TradingAgents LLM
* Do NOT create nested agent loops
* Do NOT increase recursion limit blindly
* Do NOT run full graph before market-only is stable

---

## 🎯 Success Criteria

System is considered stable when:

* market analyst exits tool loop
* no infinite recursion
* 4 concurrent runs succeed
* final decision is produced

---

## 🧠 Core Insight

```
Problem is NOT TradingAgents
Problem is NOT vLLM

Problem = model does not know when to STOP calling tools
```

---

## 📌 Workflow

```
1. baseline checks
2. market-only test
3. loop fix
4. partial graph
5. concurrency test
6. report generation
```

---

## 🧭 Final Goal

Build:

```
DGX Spark Agent Benchmark System
```

Evaluate:

* model tool-calling stability
* multi-agent convergence
* local LLM scalability

```
```