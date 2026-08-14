"""
LLM usage tracker for XRDReader pipeline.

Captures tokens, requests, cost, and wall-clock time per phase.
Drop this file next to pipeline.py and import where needed.

Usage:
    from diffai.xrdreader.usage_tracker import UsageTracker, set_tracker, get_tracker

    tracker = UsageTracker()
    set_tracker(tracker)

    # After every LLM call:
    get_tracker().log_call(
        phase="phase0",
        model="gpt-4o",
        resp=resp,
        wall_seconds=elapsed,
    )

    # At the end:
    tracker.print_summary()
    tracker.save_json("outputs/usage_report.json")
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Cost per 1M tokens (update when pricing changes)
# ---------------------------------------------------------------------------
COST_PER_1M = {
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "o3": {"input": 2.00, "output": 8.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 1.10, "output": 4.40},
    "gpt-5.2": {"input": 2.00, "output": 8.00},
    # xAI / Grok
    "grok-3": {"input": 3.00, "output": 15.00},
    "grok-3-mini": {"input": 0.30, "output": 0.50},
    # Gemini (via REST)
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    # Claude (via REST)
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    # DeepSeek
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    # Groq (free tier exists, paid pricing approximate)
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    # Together AI (open-source models)
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": {"input": 0.88, "output": 0.88},
    "meta-llama/Llama-3.2-90B-Vision-Instruct-Turbo": {
        "input": 1.20,
        "output": 1.20,
    },
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8": {
        "input": 0.27,
        "output": 0.85,
    },
    "meta-llama/Llama-4-Scout-17B-16E-Instruct": {
        "input": 0.18,
        "output": 0.59,
    },
    "deepseek-ai/DeepSeek-R1": {"input": 3.00, "output": 7.00},
    "Qwen/Qwen2.5-72B-Instruct-Turbo": {"input": 0.60, "output": 0.60},
}

# Fallback for unknown models
DEFAULT_COST = {"input": 2.00, "output": 8.00}


def _get_cost_rates(model: str) -> Dict[str, float]:
    """Look up cost per 1M tokens. Tries exact match, then longest prefix match."""
    if model in COST_PER_1M:
        return COST_PER_1M[model]
    best_key = ""
    for key in COST_PER_1M:
        if model.startswith(key) and len(key) > len(best_key):
            best_key = key
    if best_key:
        return COST_PER_1M[best_key]
    return DEFAULT_COST


class UsageTracker:
    """
    Accumulates LLM usage across all phases.

    Thread-safe: No. Use one tracker per pipeline run.
    """

    def __init__(self):
        """Start with no calls logged and the wall-clock timer running."""
        self.calls = []
        self.phase_totals = {}
        self.start_time = time.time()

    def log_call(
        self,
        phase: str,
        model: str,
        resp: Any = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        wall_seconds: float = 0.0,
        pdf_name: str = "",
        tool_name: str = "",
    ):
        """
        Log a single LLM API call.

        Pass `resp` (OpenAI response object) and tokens are extracted automatically.
        Or pass `input_tokens`/`output_tokens` manually for non-OpenAI providers.
        """
        if resp is not None:
            usage = getattr(resp, "usage", None)
            if usage is not None:
                input_tokens = getattr(usage, "input_tokens", 0) or 0
                output_tokens = getattr(usage, "output_tokens", 0) or 0

        total_tokens = input_tokens + output_tokens
        rates = _get_cost_rates(model)
        cost = (
            input_tokens / 1_000_000 * rates["input"]
            + output_tokens / 1_000_000 * rates["output"]
        )

        record = {
            "phase": phase,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(cost, 6),
            "wall_seconds": round(wall_seconds, 3),
            "pdf_name": pdf_name,
            "tool_name": tool_name,
            "timestamp": datetime.now().isoformat(),
        }
        self.calls.append(record)

        if phase not in self.phase_totals:
            self.phase_totals[phase] = {
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "wall_seconds": 0.0,
            }
        t = self.phase_totals[phase]
        t["requests"] += 1
        t["input_tokens"] += input_tokens
        t["output_tokens"] += output_tokens
        t["total_tokens"] += total_tokens
        t["cost_usd"] = round(t["cost_usd"] + cost, 6)
        t["wall_seconds"] = round(t["wall_seconds"] + wall_seconds, 3)

    def get_summary(self) -> Dict:
        """Return full summary as a dict."""
        grand = {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "wall_seconds": 0.0,
        }
        for t in self.phase_totals.values():
            for k in grand:
                grand[k] += t[k]
        grand["cost_usd"] = round(grand["cost_usd"], 6)
        grand["wall_seconds"] = round(grand["wall_seconds"], 3)
        grand["pipeline_wall_seconds"] = round(
            time.time() - self.start_time, 3
        )

        return {
            "per_phase": dict(self.phase_totals),
            "grand_total": grand,
            "calls": self.calls,
        }

    def print_summary(self):
        """Print a human-readable summary table."""
        summary = self.get_summary()

        print("\n" + "=" * 80)
        print("LLM USAGE REPORT")
        print("=" * 80)
        print(
            f"{'Phase':<16} {'Requests':>8} {'Input Tok':>11} {'Output Tok':>11} "
            f"{'Total Tok':>11} {'Cost ($)':>10} {'Time (s)':>10}"
        )
        print("-" * 80)

        for phase, t in summary["per_phase"].items():
            print(
                f"{phase:<16} {t['requests']:>8} {t['input_tokens']:>11,} "
                f"{t['output_tokens']:>11,} {t['total_tokens']:>11,} "
                f"${t['cost_usd']:>9.4f} {t['wall_seconds']:>10.1f}"
            )

        g = summary["grand_total"]
        print("-" * 80)
        print(
            f"{'TOTAL':<16} {g['requests']:>8} {g['input_tokens']:>11,} "
            f"{g['output_tokens']:>11,} {g['total_tokens']:>11,} "
            f"${g['cost_usd']:>9.4f} {g['wall_seconds']:>10.1f}"
        )
        print(
            f"\nPipeline wall-clock time: {g['pipeline_wall_seconds']:.1f} sec"
        )
        print("=" * 80 + "\n")

    def snapshot(self) -> Dict:
        """Return a copy of the current summary (non-destructive)."""
        return self.get_summary()

    def reset(self):
        """Clear all accumulated data for a fresh tracking run."""
        self.calls.clear()
        self.phase_totals.clear()
        self.start_time = time.time()

    def save_json(self, path: str):
        """Save full report (per-phase totals + every individual call) to JSON."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.get_summary(), f, indent=2, ensure_ascii=False)
        print(f"Usage report saved to: {path}")


# ---------------------------------------------------------------------------
# Global singleton — OUTSIDE the class, at module level
# ---------------------------------------------------------------------------
_global_tracker = None


def set_tracker(t):
    """Set the global tracker instance (call once from pipeline.py)."""
    global _global_tracker
    _global_tracker = t


def get_tracker():
    """Get the global tracker. Creates one if none was set."""
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = UsageTracker()
    return _global_tracker


def log_http_call(
    phase: str,
    model: str,
    provider: str,
    resp_dict: dict,
    wall_seconds: float = 0.0,
    pdf_name: str = "",
    tool_name: str = "",
):
    """
    Log an LLM call made via raw HTTP (Gemini/Claude).
    Extracts tokens from the response dict automatically.
    """
    input_tokens = 0
    output_tokens = 0

    if provider == "gemini":
        meta = resp_dict.get("usageMetadata", {})
        input_tokens = meta.get("promptTokenCount", 0) or 0
        output_tokens = meta.get("candidatesTokenCount", 0) or 0
    elif provider in ("claude", "anthropic"):
        usage = resp_dict.get("usage", {})
        input_tokens = usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0

    get_tracker().log_call(
        phase=phase,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        wall_seconds=wall_seconds,
        pdf_name=pdf_name,
        tool_name=tool_name,
    )
