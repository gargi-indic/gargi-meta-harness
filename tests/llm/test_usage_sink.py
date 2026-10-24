"""The reviewer reports what a call cost through the usage sink (``on_usage``); nothing changes without one."""

import json

from harness.adapters.base import Usage
from harness.llm.backend import FakeBackend, emit_usage, usage_sink
from harness.llm.reviewer import run_review

VALID = {"verdict": "approve", "findings": [], "correction": "", "summary": "ok"}


async def test_the_reviewer_reports_usage_for_every_backend_call():
    seen: list[Usage] = []
    first, second = (
        Usage(input_tokens=10, output_tokens=2, cost_usd=0.01),
        Usage(cost_usd=0.02, estimated=True),
    )
    backend = FakeBackend(["not json", json.dumps(VALID)], usage=[first, second])
    with usage_sink(seen.append):
        result = await run_review(backend, system="s", prompt="p")
    assert result.verdict == "approve"
    assert seen == [first, second]  # the retry cost money too


async def test_no_sink_means_no_callback_and_no_change():
    backend = FakeBackend([VALID], usage=[Usage(cost_usd=1)])
    assert (await run_review(backend, system="s", prompt="p")).verdict == "approve"


async def test_the_sink_does_not_leak_out_of_its_block():
    seen: list[Usage] = []
    with usage_sink(seen.append):
        emit_usage(Usage(cost_usd=1))
    emit_usage(Usage(cost_usd=2))
    assert [u.cost_usd for u in seen] == [1]


async def test_unscripted_calls_report_nothing():
    seen: list[Usage] = []
    with usage_sink(seen.append):
        await run_review(FakeBackend([VALID]), system="s", prompt="p")
    assert seen == []


async def test_the_sdk_backend_reports_cost_and_tokens_from_the_result_message(monkeypatch):
    from harness.llm.backend import ClaudeSdkBackend
    from tests.llm.test_backend import SdkResultMessage, install_sdk

    message = SdkResultMessage(structured_output=VALID)
    message.total_cost_usd = 0.4
    message.usage = {"input_tokens": 7, "output_tokens": 3}
    message.model_usage = {"claude-x": {"costUSD": 0.4}}
    install_sdk(monkeypatch, [message])
    seen: list[Usage] = []
    with usage_sink(seen.append):
        assert await ClaudeSdkBackend().review("s", "p", {}, None) == VALID
    assert seen == [Usage(input_tokens=7, output_tokens=3, cost_usd=0.4, model="claude-x")]
