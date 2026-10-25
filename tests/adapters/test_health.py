"""Agent health: local checks only (names of keys, PATH, importable SDK), cached for 30 s with a fake clock."""

import importlib.util
import shutil

import pytest

from harness.adapters.copilot import CopilotAgentConfig
from harness.adapters.health import HealthCache, check_health
from harness.config import AgentConfig

SECRET = "sk-super-secret-value"


@pytest.fixture(autouse=True)
def no_process_or_network(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)


def fresh() -> HealthCache:
    return HealthCache()


async def test_jules_ok_and_down() -> None:
    ok = await check_health("jules", AgentConfig(), {"JULES_API_KEY": SECRET}, cache=fresh())
    down = await check_health("jules", AgentConfig(), {}, cache=fresh())
    assert (ok.status, ok.enabled) == ("ok", True)
    assert down.status == "down"


async def test_claude_needs_key_and_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"ANTHROPIC_API_KEY": SECRET}
    assert (await check_health("claude", AgentConfig(), {}, cache=fresh())).status == "down"
    assert (await check_health("claude", AgentConfig(), env, cache=fresh())).status == "degraded"
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    assert (await check_health("claude", AgentConfig(), env, cache=fresh())).status == "ok"


async def test_codex_needs_key_and_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"CODEX_API_KEY": SECRET}
    assert (await check_health("codex", AgentConfig(), env, cache=fresh())).status == "down"
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")
    assert (await check_health("codex", AgentConfig(), env, cache=fresh())).status == "ok"
    assert (await check_health("codex", AgentConfig(), {}, cache=fresh())).status == "down"


async def test_copilot_states() -> None:
    cfg = CopilotAgentConfig()
    assert (await check_health("copilot", None, {"GH_TOKEN": "gho_x"}, cache=fresh())).status == "disabled"
    assert (await check_health("copilot", cfg, {}, cache=fresh())).status == "down"
    assert (await check_health("copilot", cfg, {"GH_TOKEN": "ghs_x"}, cache=fresh())).status == "down"
    assert (await check_health("copilot", cfg, {"GH_TOKEN": "gho_x"}, cache=fresh())).status == "unknown"


async def test_disabled_by_config_and_unknown_agent() -> None:
    off = await check_health("jules", AgentConfig(enabled=False), {"JULES_API_KEY": SECRET}, cache=fresh())
    assert (off.status, off.enabled) == ("disabled", False)
    assert (await check_health("agy", None, {}, cache=fresh())).status == "unknown"


async def test_details_never_hold_secret_values() -> None:
    env = {
        "JULES_API_KEY": SECRET,
        "ANTHROPIC_API_KEY": SECRET,
        "CODEX_API_KEY": SECRET,
        "GH_TOKEN": SECRET,
    }
    for name in ("jules", "claude", "codex", "copilot"):
        health = await check_health(
            name, CopilotAgentConfig() if name == "copilot" else AgentConfig(), env, cache=fresh()
        )
        assert SECRET not in health.detail and SECRET not in health.model_dump_json()


async def test_cache_ttl_with_a_fake_clock() -> None:
    now = [100.0]
    cache = HealthCache(clock=lambda: now[0])
    env: dict[str, str] = {}
    first = await check_health("jules", AgentConfig(), env, cache=cache)
    assert first.status == "down"

    env["JULES_API_KEY"] = SECRET
    now[0] += 29.9
    assert (await check_health("jules", AgentConfig(), env, cache=cache)).status == "down"  # cached
    now[0] += 0.2
    assert (await check_health("jules", AgentConfig(), env, cache=cache)).status == "ok"  # expired
