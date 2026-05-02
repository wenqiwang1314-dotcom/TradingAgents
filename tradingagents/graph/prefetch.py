from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any, Callable, Dict, Iterable, List, Tuple

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_global_news,
    get_income_statement,
    get_insider_transactions,
    get_news,
)
from tradingagents.dataflows.config import get_config


Task = Tuple[str, Callable[[], Any]]


def _round_seconds(value: float) -> float:
    return round(value, 3)


def _invoke_tool(tool, args: Dict[str, Any]) -> str:
    return str(tool.invoke(args))


def _date_days_before(curr_date: str, days: int) -> str:
    return (datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )


def _max_workers(task_count: int) -> int:
    config = get_config()
    configured = int(config.get("max_data_fetch_concurrency", 4) or 1)
    return max(1, min(configured, task_count))


def run_parallel_tasks(agent_name: str, tasks: Iterable[Task]) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Run independent data fetches concurrently and return results plus timing metrics."""
    task_list = list(tasks)
    if not task_list:
        return {}, {
            "agent": agent_name,
            "mode": "parallel_prefetch",
            "task_count": 0,
            "max_workers": 0,
            "wall_time_sec": 0.0,
            "serial_tool_time_sec": 0.0,
            "estimated_time_saved_sec": 0.0,
            "speedup": 0.0,
            "tasks": {},
        }

    workers = _max_workers(len(task_list))
    started = perf_counter()
    results: Dict[str, str] = {}
    task_metrics: Dict[str, Dict[str, Any]] = {}

    def timed_call(name: str, fn: Callable[[], Any]) -> Tuple[str, str, Dict[str, Any]]:
        task_started = perf_counter()
        try:
            result = str(fn())
            duration = perf_counter() - task_started
            return name, result, {
                "status": "ok",
                "duration_sec": _round_seconds(duration),
                "chars": len(result),
            }
        except Exception as exc:
            duration = perf_counter() - task_started
            message = f"Error fetching {name}: {exc}"
            return name, message, {
                "status": "error",
                "duration_sec": _round_seconds(duration),
                "chars": len(message),
                "error": str(exc),
            }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(timed_call, name, fn): name for name, fn in task_list
        }
        for future in as_completed(futures):
            name, result, metrics = future.result()
            results[name] = result
            task_metrics[name] = metrics

    wall_time = perf_counter() - started
    serial_time = sum(metric["duration_sec"] for metric in task_metrics.values())
    saved = max(0.0, serial_time - wall_time)
    speedup = serial_time / wall_time if wall_time > 0 else 0.0

    metrics = {
        "agent": agent_name,
        "mode": "parallel_prefetch",
        "task_count": len(task_list),
        "max_workers": workers,
        "wall_time_sec": _round_seconds(wall_time),
        "serial_tool_time_sec": _round_seconds(serial_time),
        "estimated_time_saved_sec": _round_seconds(saved),
        "speedup": round(speedup, 2),
        "tasks": task_metrics,
    }
    return results, metrics


def format_prefetch_context(title: str, results: Dict[str, str], metrics: Dict[str, Any]) -> str:
    if not results:
        return ""

    sections: List[str] = [
        f"## {title}",
        (
            "Data was prefetched concurrently before this analyst ran. "
            f"Wall time: {metrics.get('wall_time_sec', 0)}s; "
            f"estimated serial tool time: {metrics.get('serial_tool_time_sec', 0)}s; "
            f"estimated speedup: {metrics.get('speedup', 0)}x."
        ),
    ]
    for name in sorted(results):
        sections.append(f"### {name}\n{results[name]}")
    return "\n\n".join(sections)


def create_social_prefetch_node():
    def social_prefetch_node(state):
        ticker = state["company_of_interest"]
        curr_date = state["trade_date"]
        start_7d = _date_days_before(curr_date, 7)
        start_3d = _date_days_before(curr_date, 3)

        tasks: List[Task] = [
            (
                "company_news_7d",
                lambda: _invoke_tool(
                    get_news,
                    {"ticker": ticker, "start_date": start_7d, "end_date": curr_date},
                ),
            ),
            (
                "company_news_3d",
                lambda: _invoke_tool(
                    get_news,
                    {"ticker": ticker, "start_date": start_3d, "end_date": curr_date},
                ),
            ),
        ]
        results, metrics = run_parallel_tasks("social", tasks)
        return {
            "social_prefetch": format_prefetch_context(
                "Social/Company News Prefetch", results, metrics
            ),
            "social_prefetch_metrics": metrics,
        }

    return social_prefetch_node


def create_news_prefetch_node():
    def news_prefetch_node(state):
        ticker = state["company_of_interest"]
        curr_date = state["trade_date"]
        start_7d = _date_days_before(curr_date, 7)

        tasks: List[Task] = [
            (
                "company_news_7d",
                lambda: _invoke_tool(
                    get_news,
                    {"ticker": ticker, "start_date": start_7d, "end_date": curr_date},
                ),
            ),
            (
                "global_macro_news_7d",
                lambda: _invoke_tool(
                    get_global_news,
                    {"curr_date": curr_date, "look_back_days": 7, "limit": 10},
                ),
            ),
            (
                "insider_transactions",
                lambda: _invoke_tool(get_insider_transactions, {"ticker": ticker}),
            ),
        ]
        results, metrics = run_parallel_tasks("news", tasks)
        return {
            "news_prefetch": format_prefetch_context(
                "News/Macro/Insider Prefetch", results, metrics
            ),
            "news_prefetch_metrics": metrics,
        }

    return news_prefetch_node


def create_fundamentals_prefetch_node():
    def fundamentals_prefetch_node(state):
        ticker = state["company_of_interest"]
        curr_date = state["trade_date"]

        tasks: List[Task] = [
            (
                "fundamentals",
                lambda: _invoke_tool(
                    get_fundamentals, {"ticker": ticker, "curr_date": curr_date}
                ),
            ),
            (
                "balance_sheet",
                lambda: _invoke_tool(
                    get_balance_sheet,
                    {"ticker": ticker, "freq": "quarterly", "curr_date": curr_date},
                ),
            ),
            (
                "cashflow",
                lambda: _invoke_tool(
                    get_cashflow,
                    {"ticker": ticker, "freq": "quarterly", "curr_date": curr_date},
                ),
            ),
            (
                "income_statement",
                lambda: _invoke_tool(
                    get_income_statement,
                    {"ticker": ticker, "freq": "quarterly", "curr_date": curr_date},
                ),
            ),
        ]
        results, metrics = run_parallel_tasks("fundamentals", tasks)
        return {
            "fundamentals_prefetch": format_prefetch_context(
                "Fundamentals Prefetch", results, metrics
            ),
            "fundamentals_prefetch_metrics": metrics,
        }

    return fundamentals_prefetch_node
