"""Cheap, local health checks for the agents the harness knows (M5.8).

Nothing here touches the network, spawns a process or reads a key's value: a check looks at which environment
variables are set (by name), whether the agent's binary is on ``PATH`` and whether its SDK can be imported.
Results are cached for :data:`TTL_S` seconds per agent in a :class:`HealthCache` (the clock is injectable).
"""

import importlib.util
import shutil
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from harness.config import AgentConfig

TTL_S = 30.0
HealthStatus = Literal["ok", "degraded", "down", "disabled", "unknown"]

JULES_KEY = "JULES_API_KEY"
CLAUDE_KEY = "ANTHROPIC_API_KEY"
CODEX_KEY = "CODEX_API_KEY"
COPILOT_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN")
# The agents that need no ``agents.<name>`` config entry to be on (the server offers them when their key is set).
_ON_WITHOUT_CONFIG = {"jules", "claude", "codex"}


class AgentHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    enabled: bool
    status: HealthStatus
    detail: str
    checked_at: str


def _has(env: Mapping[str, str], *names: str) -> str | None:
    """The first of ``names`` that is set (non-empty) in ``env``: the NAME, never the value."""
    return next((n for n in names if env.get(n)), None)


def _evaluate(name: str, cfg: AgentConfig | None, env: Mapping[str, str]) -> tuple[bool, HealthStatus, str]:
    enabled = cfg.enabled if cfg is not None else name in _ON_WITHOUT_CONFIG
    if name not in _ON_WITHOUT_CONFIG and name != "copilot":
        return enabled, "unknown", "no health check for this agent"
    if not enabled:
        return False, "disabled", "agent is not enabled in the repo config" if cfg else "not configured"
    if name == "jules":
        if _has(env, JULES_KEY):
            return True, "ok", f"{JULES_KEY} is set"
        return True, "down", f"{JULES_KEY} is not set"
    if name == "claude":
        if not _has(env, CLAUDE_KEY):
            return True, "down", f"{CLAUDE_KEY} is not set"
        if importlib.util.find_spec("claude_agent_sdk") is None:
            return True, "degraded", f"{CLAUDE_KEY} is set but claude-agent-sdk is not importable"
        return True, "ok", f"{CLAUDE_KEY} is set and claude-agent-sdk is importable"
    if name == "codex":
        missing = []
        if not _has(env, CODEX_KEY):
            missing.append(f"{CODEX_KEY} is not set")
        if shutil.which("codex") is None:
            missing.append("codex is not on PATH")
        if missing:
            return True, "down", "; ".join(missing)
        return True, "ok", f"{CODEX_KEY} is set and codex is on PATH"
    # copilot: opt-in through agents.copilot, needs a user token that only a network call can verify
    token_name = _has(env, *COPILOT_TOKENS)
    if token_name is None:
        return True, "down", "no GH_TOKEN or GITHUB_TOKEN is set"
    if str(env[token_name]).startswith("ghs_"):
        return True, "down", f"{token_name} is an installation token; copilot needs a user token"
    return True, "unknown", f"{token_name} is set; its user type is only checked when a session starts"


class HealthCache:
    """Per-agent results kept for ``ttl_s`` seconds."""

    def __init__(
        self,
        ttl_s: float = TTL_S,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self.wall = wall
        self._items: dict[str, tuple[float, AgentHealth]] = {}

    def get(self, name: str, cfg: AgentConfig | None, env: Mapping[str, str]) -> AgentHealth:
        now = self.clock()
        hit = self._items.get(name)
        if hit is not None and now - hit[0] < self.ttl_s:
            return hit[1]
        enabled, status, detail = _evaluate(name, cfg, env)
        health = AgentHealth(
            name=name,
            enabled=enabled,
            status=status,
            detail=detail,
            checked_at=self.wall().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._items[name] = (now, health)
        return health


DEFAULT_CACHE = HealthCache()


async def check_health(
    name: str, cfg: AgentConfig | None, env: Mapping[str, str], *, cache: HealthCache | None = None
) -> AgentHealth:
    """The agent's health from local checks only; cached for 30 s. ``cfg`` is its ``agents.<name>`` entry, if any."""
    return (cache or DEFAULT_CACHE).get(name, cfg, env)
