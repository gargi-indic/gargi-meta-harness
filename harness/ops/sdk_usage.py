"""Read cost and token usage from a ``claude_agent_sdk`` ``ResultMessage``.

Checked against the installed SDK: ``ResultMessage`` has ``total_cost_usd: float | None``,
``usage: dict | None`` (``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens``,
``cache_read_input_tokens``) and ``model_usage: dict[model, {inputTokens, outputTokens, costUSD, ...}] | None``.
Everything is read tolerantly, so an SDK without those fields yields ``None`` (cost unknown, shown as ``n/a``).
"""

from typing import Any

from harness.adapters.base import Usage
from harness.ops.pricing import PricingTable, estimate_cost

_INPUT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_from_result(
    message: Any, *, default_model: str | None = None, table: PricingTable | None = None
) -> Usage | None:
    raw = getattr(message, "usage", None)
    raw = raw if isinstance(raw, dict) else {}
    parts = [_int(raw.get(key)) for key in _INPUT_KEYS]
    input_tokens = sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None
    output_tokens = _int(raw.get("output_tokens"))

    model = default_model
    per_model = getattr(message, "model_usage", None)
    if isinstance(per_model, dict) and per_model:
        costs = {
            name: float(v.get("costUSD") or 0.0) if isinstance(v, dict) else 0.0
            for name, v in per_model.items()
        }
        model = max(costs, key=lambda name: costs[name])

    cost = getattr(message, "total_cost_usd", None)
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return Usage(
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=float(cost), model=model
        )
    if input_tokens is None and output_tokens is None:
        return None
    guess = estimate_cost(model, input_tokens, output_tokens, table)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=guess.usd,
        estimated=True,
        model=model,
    )
