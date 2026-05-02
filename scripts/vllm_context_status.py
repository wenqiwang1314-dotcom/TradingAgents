#!/usr/bin/env python3
"""Show the active vLLM model context window and runtime pressure."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def http_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_text(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def service_root(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    path = parsed.path
    if path.endswith("/v1"):
        path = path[:-3] or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "", ""))


def metric_value(metrics: str, name: str) -> float | None:
    prefix = f"{name}"
    for line in metrics.splitlines():
        if not line.startswith(prefix):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        try:
            return float(parts[1])
        except ValueError:
            return None
    return None


def vllm_args() -> dict[str, str]:
    try:
        output = subprocess.check_output(["ps", "-eo", "args="], text=True)
    except (OSError, subprocess.CalledProcessError):
        return {}

    for line in output.splitlines():
        if "vllm serve" not in line:
            continue
        parts = line.split()
        found: dict[str, str] = {}
        for index, part in enumerate(parts):
            if not part.startswith("--"):
                continue
            key = part[2:]
            value = "true"
            if index + 1 < len(parts) and not parts[index + 1].startswith("--"):
                value = parts[index + 1]
            found[key] = value
        return found
    return {}


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("TRADINGAGENTS_BACKEND_URL", "http://127.0.0.1:5000/v1"),
        help="OpenAI-compatible vLLM base URL, usually ending with /v1.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("TRADINGAGENTS_DEEP_MODEL") or os.getenv("TRADINGAGENTS_QUICK_MODEL"),
        help="Served model id to inspect. Defaults to TradingAgents model env vars.",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    root_url = service_root(base_url)

    try:
        models_payload = http_json(f"{base_url}/models", args.timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Failed to query {base_url}/models: {exc}", file=sys.stderr)
        return 2

    models = models_payload.get("data") or []
    model = next((item for item in models if item.get("id") == args.model), None)
    if model is None and models:
        model = models[0]

    metrics_text = ""
    try:
        metrics_text = http_text(f"{root_url}/metrics", args.timeout)
    except (urllib.error.URLError, TimeoutError):
        pass

    launch_args = vllm_args()
    max_model_len = model.get("max_model_len") if model else None
    output = {
        "base_url": base_url,
        "model_id": model.get("id") if model else None,
        "model_root": model.get("root") if model else None,
        "context_window_tokens": max_model_len,
        "launch_max_model_len": launch_args.get("max-model-len"),
        "max_num_batched_tokens": launch_args.get("max-num-batched-tokens"),
        "max_num_seqs": launch_args.get("max-num-seqs"),
        "gpu_memory_utilization": launch_args.get("gpu-memory-utilization"),
        "runtime": {
            "requests_running": metric_value(metrics_text, "vllm:num_requests_running"),
            "requests_waiting": metric_value(metrics_text, "vllm:num_requests_waiting"),
            "kv_cache_usage_percent": (
                value * 100
                if (value := metric_value(metrics_text, "vllm:kv_cache_usage_perc")) is not None
                else None
            ),
            "prompt_tokens_total": metric_value(metrics_text, "vllm:prompt_tokens_total"),
            "generation_tokens_total": metric_value(metrics_text, "vllm:generation_tokens_total"),
        },
        "recommended_agent_budget_tokens": int(max_model_len * 0.70) if isinstance(max_model_len, int) else None,
        "recommended_output_reserve_tokens": int(max_model_len * 0.10) if isinstance(max_model_len, int) else None,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    print(f"Base URL: {output['base_url']}")
    print(f"Model: {output['model_id']}")
    print(f"Context window: {output['context_window_tokens']} tokens")
    print(f"Launch --max-model-len: {output['launch_max_model_len']}")
    print(f"Launch --max-num-batched-tokens: {output['max_num_batched_tokens']}")
    print(f"Launch --max-num-seqs: {output['max_num_seqs']}")
    print(f"GPU memory utilization target: {output['gpu_memory_utilization']}")
    print(f"Requests running/waiting: {output['runtime']['requests_running']} / {output['runtime']['requests_waiting']}")
    print(f"KV cache usage: {output['runtime']['kv_cache_usage_percent']}%")
    print(f"Prompt tokens total: {output['runtime']['prompt_tokens_total']}")
    print(f"Recommended Agent input budget: {output['recommended_agent_budget_tokens']} tokens")
    print(f"Recommended output reserve: {output['recommended_output_reserve_tokens']} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
