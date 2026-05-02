# TradingAgents 本地 vLLM / Nemotron 使用说明

本文档说明如何在本机使用 `TauricResearch/TradingAgents`，并通过本地 vLLM 服务调用 `nvidia/nemotron-3-super`。内容覆盖环境检查、启动验证、运行测试、参数配置、Claude Code 协作方式、循环问题修复说明与故障排查。

> 注意：TradingAgents 是研究框架，输出不构成金融、投资或交易建议。

## 1. 当前已验证状态

项目目录：

```bash
/home/lucas/ai/TradingAgents
```

Python 虚拟环境：

```bash
/home/lucas/ai/TradingAgents/.venv
```

本地 vLLM OpenAI-compatible endpoint：

```bash
http://localhost:5000/v1
```

模型 ID：

```bash
nvidia/nemotron-3-super
```

已验证项目能力：

- `uv sync` 已完成依赖安装。
- `tradingagents` 包可以正常 import。
- yfinance 数据链路可用。
- vLLM `/v1/models` 和 `/v1/chat/completions` 可用。
- 项目测试已通过：`63 passed, 40 subtests passed`。
- `openai_client.py` 已兼容自定义 OpenAI-compatible `backend_url`。
- `trading_graph.py` 的 `max_recur_limit` 已生效。
- market analyst 的工具循环已做收敛修复。

## 2. 快速开始

进入项目目录：

```bash
cd /home/lucas/ai/TradingAgents
```

检查虚拟环境和包：

```bash
uv run python -c "from tradingagents.graph.trading_graph import TradingAgentsGraph; print('TradingAgents import OK')"
```

检查项目测试：

```bash
uv run python -m pytest -q
```

预期结果：

```text
63 passed, 40 subtests passed
```

检查 yfinance 数据链路：

```bash
uv run python test.py
```

预期能看到类似：

```text
Testing optimized implementation with 30-day lookback:
Execution time: ...
Result length: ...
## macd values from ...
```

检查 vLLM 模型列表：

```bash
curl -s http://localhost:5000/v1/models
```

预期能看到：

```json
{
  "object": "list",
  "data": [
    {
      "id": "nvidia/nemotron-3-super"
    }
  ]
}
```

检查 vLLM 简单对话：

```bash
curl -s http://localhost:5000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dummy' \
  -d '{
    "model": "nvidia/nemotron-3-super",
    "messages": [
      {"role": "user", "content": "Reply with exactly: ok"}
    ],
    "max_tokens": 64
  }'
```

## 3. 推荐配置

在 Python 代码中推荐使用如下配置：

```python
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()

config.update({
    "llm_provider": "openai",
    "backend_url": "http://localhost:5000/v1",
    "deep_think_llm": "nvidia/nemotron-3-super",
    "quick_think_llm": "nvidia/nemotron-3-super",

    # 研究和风险讨论轮数。先从 1 开始。
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,

    # LangGraph 最大递归步数。
    # 如果跑完整图，建议先用 40-80；调试时用 8-15。
    "max_recur_limit": 60,

    # 每个 analyst 最多允许多少轮工具调用。
    # market analyst 修复后默认推荐 4：
    # 1 次 get_stock_data + 3 次 get_indicators。
    "max_analyst_tool_iterations": 4,

    # 输出语言。
    "output_language": "English",
})

config["data_vendors"] = {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}
```

## 4. 最小 market analyst 测试

这个测试只验证 market analyst + 工具调用循环，不跑完整 TradingAgents 图。它适合确认 Nemotron 是否能在工具预算内退出并生成 `market_report`。

```bash
cd /home/lucas/ai/TradingAgents

uv run python - <<'PY'
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

config = DEFAULT_CONFIG.copy()
config.update({
    "llm_provider": "openai",
    "backend_url": "http://localhost:5000/v1",
    "deep_think_llm": "nvidia/nemotron-3-super",
    "quick_think_llm": "nvidia/nemotron-3-super",
    "max_analyst_tool_iterations": 4,
    "max_recur_limit": 20,
})

ta = TradingAgentsGraph(
    selected_analysts=["market"],
    debug=False,
    config=config,
)

node = create_market_analyst(ta.quick_thinking_llm)
state = ta.propagator.create_initial_state("AAPL", "2024-11-01")

for i in range(8):
    out = node(state)
    state["messages"].extend(out["messages"])

    if out.get("market_report"):
        state["market_report"] = out["market_report"]

    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    print("ITER", i + 1, "tool_calls", [(tc.get("name"), tc.get("args")) for tc in tool_calls])

    route = ta.conditional_logic.should_continue_market(state)
    print("ROUTE", route)

    if route != "tools_market":
        break

    tool_out = ta.tool_nodes["market"].invoke({"messages": state["messages"]})
    state["messages"].extend(tool_out["messages"])

print("FINAL_ROUTE", ta.conditional_logic.should_continue_market(state))
print("MARKET_REPORT_LEN", len(state.get("market_report", "")))
print(state.get("market_report", "")[:2000])
PY
```

修复后的典型结果：

```text
ITER 1 tool_calls [('get_stock_data', ...)]
ROUTE tools_market
ITER 2 tool_calls [('get_indicators', close_50_sma)]
ROUTE tools_market
ITER 3 tool_calls [('get_indicators', close_200_sma)]
ROUTE tools_market
ITER 4 tool_calls [('get_indicators', macd)]
ROUTE tools_market
ITER 5 tool_calls []
ROUTE Msg Clear Market
FINAL_ROUTE Msg Clear Market
MARKET_REPORT_LEN 6804
```

说明：

- 前 4 轮是合法工具调用。
- 第 5 轮模型停止工具调用并输出最终 market report。
- `ROUTE Msg Clear Market` 表示 market analyst 已经退出工具循环，进入下一个图节点。

## 5. 完整 TradingAgents 图测试

完整图会经过：

```text
Analyst -> Researcher debate -> Research Manager -> Trader -> Risk Analysts -> Portfolio Manager
```

Nemotron 生成速度约十几 tokens/s，完整图可能耗时较长。建议先只启用 `market` analyst。

```bash
cd /home/lucas/ai/TradingAgents

timeout 1200 uv run python - <<'PY'
from langgraph.errors import GraphRecursionError
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config.update({
    "llm_provider": "openai",
    "backend_url": "http://localhost:5000/v1",
    "deep_think_llm": "nvidia/nemotron-3-super",
    "quick_think_llm": "nvidia/nemotron-3-super",
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 60,
    "max_analyst_tool_iterations": 4,
    "output_language": "English",
})

config["data_vendors"] = {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}

ta = TradingAgentsGraph(
    selected_analysts=["market"],
    debug=False,
    config=config,
)

try:
    state, decision = ta.propagate("AAPL", "2024-11-01")
except GraphRecursionError as exc:
    print("GRAPH_RECURSION_LIMIT_HIT")
    print(str(exc).split("\n")[0])
else:
    print("FINAL_DECISION_START")
    print(decision)
    print("FINAL_DECISION_END")
    print("FINAL_TRADE_DECISION_START")
    print(state.get("final_trade_decision", ""))
    print("FINAL_TRADE_DECISION_END")
PY
```

建议：

- 第一次完整图测试使用 `selected_analysts=["market"]`。
- 稳定后再尝试 `["market", "news"]`。
- 最后再尝试完整 analyst 组合：`["market", "social", "news", "fundamentals"]`。

完整 analyst 组合成本更高、耗时更长，也更容易暴露本地模型在工具调用格式上的不稳定。

## 6. CLI 交互模式

项目自带 CLI：

```bash
cd /home/lucas/ai/TradingAgents
uv run tradingagents
```

或者：

```bash
uv run python -m cli.main
```

CLI 会让你交互式选择：

- ticker
- analysis date
- output language
- analysts
- research depth
- LLM provider
- thinking model

使用本地 vLLM 时，如果 CLI 没有直接显示你的自定义 endpoint，建议先使用 Python 脚本方式，因为脚本能明确设置：

```python
config["llm_provider"] = "openai"
config["backend_url"] = "http://localhost:5000/v1"
config["quick_think_llm"] = "nvidia/nemotron-3-super"
config["deep_think_llm"] = "nvidia/nemotron-3-super"
```

## 7. Claude Code 外层协作方式

Claude Code CLI 适合作为 TradingAgents 的外层工程 Agent，用来：

- 阅读项目代码。
- 自动运行测试。
- 分析 vLLM 日志。
- 修改 prompt 或路由逻辑。
- 生成实验报告。
- 做批量实验编排。

不建议把 Claude Code CLI 直接包成 TradingAgents 内部 LLM backend。原因是 TradingAgents 需要 LangChain 模型对象支持：

```python
llm.invoke(...)
llm.bind_tools(...)
```

而 Claude Code CLI 是进程级代码 Agent，有自己的工具系统和权限系统。把它塞进 TradingAgents 内部会形成：

```text
TradingAgents tool loop -> Claude Code agent loop -> local bridge -> vLLM tool loop
```

这样会产生双重 agent/tool-call，状态复杂、延迟高、容易循环。

推荐 Claude Code 外层使用命令：

```bash
cd /home/lucas/ai/TradingAgents

CLAUDE_LAUNCH_CWD=/home/lucas/ai/TradingAgents \
VLLM_BASE_URL=http://127.0.0.1:5000/v1 \
VLLM_MODEL=nvidia/nemotron-3-super \
/home/lucas/_push_dgx_spark_vllm/ai/claude-code-local-bridge/run.sh \
  -p --max-turns 3 --output-format json \
  "阅读这个项目，运行最小测试，并分析 TradingAgents 与本地 vLLM 的工具调用问题"
```

## 8. 本次循环问题的直接原因

原始循环不是 Parser 错误，而是逻辑循环。

抓到的 vLLM 模式如下：

```text
SEQ 1 finish_reason=tool_calls -> get_stock_data(...)
SEQ 2 finish_reason=tool_calls -> get_indicators(close_50_sma)
SEQ 3 finish_reason=tool_calls -> get_indicators(close_200_sma)
SEQ 4 finish_reason=tool_calls -> get_indicators(macd)
```

这些都是合法工具调用，说明：

- vLLM 没有返回 parser error。
- LangChain 没有因为格式错误重试。
- 模型是在正常、连续地请求工具。

直接触发点在条件边逻辑：

```python
if last_message.tool_calls:
    return "tools_market"
```

这意味着只要最后一条 AIMessage 里有工具调用，LangGraph 就继续进入 ToolNode。

市场分析 prompt 又要求 “up to 8 indicators”，Nemotron 倾向于一个指标一个工具调用，因此低 `max_recur_limit` 下会撞递归上限。

## 9. 已加入的收敛机制

### 9.1 Prompt 层

market analyst prompt 增加了强约束：

```text
For local tool-calling models, keep the workflow short:
after get_stock_data, use no more than three high-value indicator calls.
Prefer close_50_sma, close_200_sma, and macd.

When you have enough information, you MUST stop using tools
and output your final market analyst report starting with [FINAL_ANALYST_REPORT].
Never include tool calls in the same response as [FINAL_ANALYST_REPORT].
```

### 9.2 路由层

条件边现在按以下顺序判断：

```text
1. 如果最后消息以 [FINAL_ANALYST_REPORT] 开头，结束 analyst。
2. 如果没有 tool_calls，结束 analyst。
3. 如果工具调用参数重复，结束 analyst。
4. 如果 analyst 工具调用超过预算，结束 analyst。
5. 否则进入 ToolNode。
```

### 9.3 动态工具预算

配置项：

```python
config["max_analyst_tool_iterations"] = 4
```

默认含义：

```text
1 次 get_stock_data
3 次 get_indicators
```

当工具调用接近预算时，prompt 会动态追加：

```text
You are running out of tool budget...
```

当工具预算耗尽时，prompt 会动态追加：

```text
You have exhausted the analyst tool budget. You MUST NOT call any more tools.
Use the current context and output your final analyst report now...
```

## 10. 推荐实验矩阵

先跑小矩阵，不要一开始跑完整组合。

### 10.1 market-only

```python
selected_analysts = ["market"]
max_analyst_tool_iterations = 4
max_recur_limit = 60
```

目标：

- 确认工具循环退出。
- 确认 `market_report` 非空。
- 确认后续 debate/risk 能进入。

### 10.2 market + news

```python
selected_analysts = ["market", "news"]
max_analyst_tool_iterations = 4
max_recur_limit = 80
```

目标：

- 检查 news tools 是否也存在工具循环。
- 如果 news 也循环，再把同样的预算/marker 策略移植到 news analyst prompt。

### 10.3 full analysts

```python
selected_analysts = ["market", "social", "news", "fundamentals"]
max_analyst_tool_iterations = 4
max_recur_limit = 120
```

目标：

- 验证完整 multi-agent 路径。
- 观察总耗时和 vLLM KV cache 使用。

## 11. 抓取 vLLM 请求/响应 Payload

如果需要诊断模型是否重复工具调用，可以给 TradingAgents 注入 `httpx.Client` event hooks。

示例：

```python
import json
import time
from pathlib import Path

import httpx

log_path = Path("/tmp/tradingagents_vllm_payloads.jsonl")
seq = {"n": 0}

def log(kind, data):
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "kind": kind, **data}, ensure_ascii=False) + "\n")

def on_request(request):
    seq["n"] += 1
    body = request.content.decode("utf-8", errors="replace") if request.content else ""
    try:
        payload = json.loads(body) if body else None
    except Exception:
        payload = body[:4000]
    log("request", {
        "seq": seq["n"],
        "method": request.method,
        "url": str(request.url),
        "payload": payload,
    })

def on_response(response):
    response.read()
    try:
        payload = response.json()
    except Exception:
        payload = response.text[:4000]
    log("response", {
        "seq": seq["n"],
        "status_code": response.status_code,
        "payload": payload,
    })

client = httpx.Client(
    event_hooks={
        "request": [on_request],
        "response": [on_response],
    },
    timeout=180.0,
)

config["http_client"] = client
config["max_retries"] = 0
```

运行后检查：

```bash
wc -l /tmp/tradingagents_vllm_payloads.jsonl
tail -n 8 /tmp/tradingagents_vllm_payloads.jsonl
```

提取响应摘要：

```bash
uv run python - <<'PY'
import json
from pathlib import Path

p = Path("/tmp/tradingagents_vllm_payloads.jsonl")
rows = [json.loads(l) for l in p.read_text().splitlines()]

for row in rows:
    if row["kind"] != "response":
        continue

    choice = row["payload"]["choices"][0]
    msg = choice["message"]
    tool_calls = msg.get("tool_calls") or []

    print("SEQ", row["seq"], "finish=", choice.get("finish_reason"))
    print("TOOLS", [
        (tc["function"]["name"], tc["function"]["arguments"])
        for tc in tool_calls
    ])
    print("CONTENT", (msg.get("content") or "").replace("\n", " ")[:300])
    print()
PY
```

## 12. 查看 vLLM 日志

当前 vLLM 日志文件：

```bash
/home/lucas/LLM/dgxspark-vllm-nemotron/logs/nemotron-20260409-003258.log
```

查看最近请求：

```bash
tail -n 80 /home/lucas/LLM/dgxspark-vllm-nemotron/logs/nemotron-20260409-003258.log
```

重点观察：

```text
POST /v1/chat/completions HTTP/1.1" 200 OK
Avg prompt throughput
Avg generation throughput
Running: 1 reqs
Waiting: 0 reqs
GPU KV cache usage
```

如果 `Running: 1 reqs` 长时间不变，通常表示当前生成还没结束。

## 13. 常见问题排查

### 13.1 Recursion limit reached

现象：

```text
Recursion limit of N reached without hitting a stop condition.
```

原因可能是：

- analyst 工具循环未退出。
- debate/risk 讨论轮数过高。
- 模型持续生成 tool_calls。

处理：

```python
config["max_recur_limit"] = 80
config["max_analyst_tool_iterations"] = 4
config["max_debate_rounds"] = 1
config["max_risk_discuss_rounds"] = 1
```

如果仍然循环，先只跑：

```python
selected_analysts=["market"]
```

### 13.2 模型输出 tool_calls 但内容里已有结论

现象：

```text
message.content 有分析结论
message.tool_calls 仍然非空
```

当前策略：

- 如果结论以 `[FINAL_ANALYST_REPORT]` 行首开头，路由优先结束。
- 如果只是思考文字里提到 marker，不会误判。

如果其它 analyst 也出现类似情况，可把 market analyst 的策略复制到对应 analyst。

### 13.3 重复请求同一个工具参数

现象：

```text
get_indicators(AAPL, macd, 2024-11-01, 100)
get_indicators(AAPL, macd, 2024-11-01, 100)
```

当前策略：

- `has_repeated_tool_call()` 会检测重复签名。
- 一旦最新工具调用重复历史工具调用，路由结束 analyst。

### 13.4 vLLM 报 model not found

检查模型 ID：

```bash
curl -s http://localhost:5000/v1/models
```

代码中必须使用 `/v1/models` 返回的 `id`：

```python
config["quick_think_llm"] = "nvidia/nemotron-3-super"
config["deep_think_llm"] = "nvidia/nemotron-3-super"
```

不要使用 Ollama 风格名称：

```text
nemotron-3-super:120b
```

### 13.5 Ollama 本地模型加载失败

如果使用 Ollama 版 `nemotron-3-super:120b`，可能出现：

```text
model requires more system memory than is available
```

本项目当前推荐使用已启动的 vLLM：

```bash
http://localhost:5000/v1
```

不要通过 Ollama 加载 120B 模型。

### 13.6 运行太慢

Nemotron 120B 完整图会慢。建议：

```python
selected_analysts=["market"]
max_debate_rounds=1
max_risk_discuss_rounds=1
max_analyst_tool_iterations=4
```

确认稳定后再扩大 analyst 组合。

## 14. 文件修改摘要

本地兼容与循环修复涉及以下文件：

```text
tradingagents/llm_clients/openai_client.py
tradingagents/graph/trading_graph.py
tradingagents/graph/conditional_logic.py
tradingagents/agents/analysts/market_analyst.py
tradingagents/agents/utils/agent_utils.py
```

重点：

- `openai_client.py`：自定义 `backend_url` 时走 Chat Completions，不走 OpenAI Responses API。
- `trading_graph.py`：支持 `max_recur_limit`、`max_analyst_tool_iterations`，并允许注入 `http_client` 抓 payload。
- `conditional_logic.py`：不再只凭 `last_message.tool_calls` 无条件继续。
- `market_analyst.py`：强化本地模型工具预算与最终报告提示。
- `agent_utils.py`：新增 final marker、工具计数、重复工具检测、动态收敛提示。

## 15. 推荐日常工作流

日常最稳流程：

```bash
cd /home/lucas/ai/TradingAgents

# 1. 确认 vLLM 活着
curl -s http://localhost:5000/v1/models | head

# 2. 跑测试
uv run python -m pytest -q

# 3. 跑数据链路
uv run python test.py

# 4. 跑 market-only 子循环
# 使用本文第 4 节脚本

# 5. 跑完整 market-only TradingAgents 图
# 使用本文第 5 节脚本
```

如果要让 Claude Code 协作：

```bash
cd /home/lucas/ai/TradingAgents

CLAUDE_LAUNCH_CWD=/home/lucas/ai/TradingAgents \
VLLM_BASE_URL=http://127.0.0.1:5000/v1 \
VLLM_MODEL=nvidia/nemotron-3-super \
/home/lucas/_push_dgx_spark_vllm/ai/claude-code-local-bridge/run.sh \
  -p --max-turns 3 --output-format json \
  "运行测试，检查 TradingAgents 本地 vLLM 配置，并总结当前风险"
```

## 16. 安全与实验边界

- 不要把模型输出当成真实交易建议。
- 不要在未审查的情况下让 Agent 连接真实券商 API。
- 先使用历史日期，例如 `2024-11-01`。
- 先使用高流动性股票，例如 `AAPL`、`NVDA`、`MSFT`。
- 先只启用 `market` analyst。
- 每次修改 prompt 或路由后，先跑 `pytest`。

## 17. 快速结论

当前推荐运行形态：

```python
TradingAgentsGraph(
    selected_analysts=["market"],
    debug=False,
    config={
        **DEFAULT_CONFIG,
        "llm_provider": "openai",
        "backend_url": "http://localhost:5000/v1",
        "quick_think_llm": "nvidia/nemotron-3-super",
        "deep_think_llm": "nvidia/nemotron-3-super",
        "max_analyst_tool_iterations": 4,
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "max_recur_limit": 60,
    },
)
```

当前问题的直接触发点已经定位为逻辑循环，不是 parser 错误。market analyst 已通过工具预算、final marker、重复工具检测和动态收敛提示完成修复。
