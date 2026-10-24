from dataclasses import dataclass
from typing import Any

import pytest

from harness.ops.sdk_usage import usage_from_result


@dataclass
class ResultMessage:
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    model_usage: dict[str, Any] | None = None


def test_reported_cost_is_not_estimated():
    usage = usage_from_result(
        ResultMessage(
            1.25,
            {"input_tokens": 10, "output_tokens": 5},
            {"m-small": {"costUSD": 0.1}, "m-big": {"costUSD": 1.15}},
        )
    )
    assert usage is not None
    assert (usage.cost_usd, usage.estimated, usage.model) == (1.25, False, "m-big")
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)


def test_tokens_without_cost_are_estimated_from_the_table():
    usage = usage_from_result(ResultMessage(usage={"input_tokens": 1_000_000}), default_model="claude-sonnet")
    assert usage is not None and usage.estimated is True and usage.cost_usd > 0


def test_nothing_reported_is_unknown():
    assert usage_from_result(ResultMessage()) is None
    assert usage_from_result(object()) is None


@pytest.mark.parametrize("bad", [True, -1, "5", None])
def test_junk_token_values_are_ignored(bad):
    assert usage_from_result(ResultMessage(usage={"input_tokens": bad, "output_tokens": bad})) is None
