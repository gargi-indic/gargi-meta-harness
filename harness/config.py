import json
import os
import re
import tomllib
from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SerializeAsAny, field_validator

from harness.ops.pricing import ModelPrice

CONFIG_HOME = Path(os.environ.get("GARGI_HARNESS_CONFIG", Path.home() / ".config" / "gargi-harness"))


class RepoEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        import re

        if "/" not in v or v.count("/") != 1:
            raise ValueError("name must be in the format owner/name")
        parts = v.split("/")
        pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
        if not all(pattern.match(p) for p in parts):
            raise ValueError("name parts must match ^[A-Za-z0-9_.-]+$")
        return v


# Notification kinds a user can enable (``[notifications] kinds``); ``notify`` (the notify node) is always allowed.
NOTIFICATION_KINDS = (
    "plan_approval",
    "merge_approval",
    "gate",
    "question",
    "escalation",
    "handoff",
    "budget",
    "failure",
)
DEFAULT_NOTIFICATION_KINDS = (
    "plan_approval",
    "merge_approval",
    "gate",
    "question",
    "escalation",
    "handoff",
    "budget",
)
_QUIET_HOURS = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def parse_quiet_hours(text: str) -> tuple[time, time]:
    """Parse ``"22:00-08:00"`` (the range may cross midnight) into (start, end); ValueError when invalid."""
    match = _QUIET_HOURS.match(text.strip())
    if match is None:
        raise ValueError(f"quiet_hours must look like HH:MM-HH:MM, got {text!r}")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError(f"quiet_hours has an invalid time: {text!r}")
    return time(h1, m1), time(h2, m2)


class NotificationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    kinds: list[str] = Field(default_factory=lambda: list(DEFAULT_NOTIFICATION_KINDS))
    quiet_hours: str | None = None
    backend: Literal["auto", "macos", "null"] = "auto"
    dedupe_window_s: int = Field(default=60, ge=0)

    @field_validator("kinds")
    @classmethod
    def validate_kinds(cls, v: list[str]) -> list[str]:
        unknown = [k for k in v if k not in NOTIFICATION_KINDS]
        if unknown:
            raise ValueError(f"unknown notification kinds: {', '.join(unknown)}")
        return v

    @field_validator("quiet_hours")
    @classmethod
    def validate_quiet_hours(cls, v: str | None) -> str | None:
        if v is not None:
            parse_quiet_hours(v)
        return v


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = 7777
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".gargi-harness")
    env_files: list[Path] = Field(
        default_factory=lambda: [Path.home() / ".config" / "jules" / "env", CONFIG_HOME / "env"]
    )
    repos: list[RepoEntry] = Field(default_factory=list)
    # Model price overrides ([pricing."model"] tables); the key "unknown" replaces the fallback price.
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    @property
    def data_path(self) -> Path:
        return self.data_dir.expanduser()

    @property
    def worktrees_dir(self) -> Path:
        """Where :class:`harness.workspaces.WorktreeManager` keeps its caches and worktrees."""
        return self.data_path / "worktrees"

    @property
    def db_path(self) -> Path:
        return self.data_path / "harness.db"


def load_global(path: Path | None = None) -> GlobalConfig:
    p = path if path is not None else CONFIG_HOME / "config.toml"
    p = p.expanduser()
    if not p.is_file():
        return GlobalConfig()
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
            return GlobalConfig(**data)
    except Exception as e:
        raise ValueError(f"failed to load global config: {e}") from e


def save_global(cfg: GlobalConfig, path: Path | None = None) -> Path:
    p = path if path is not None else CONFIG_HOME / "config.toml"
    p = p.expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append(f"port = {cfg.port}")
    lines.append(f"data_dir = {json.dumps(str(cfg.data_dir))}")
    env_strs = [json.dumps(str(p)) for p in cfg.env_files]
    lines.append(f"env_files = [{', '.join(env_strs)}]")

    notes = cfg.notifications
    lines.append("")
    lines.append("[notifications]")
    lines.append(f"enabled = {json.dumps(notes.enabled)}")
    lines.append(f"kinds = [{', '.join(json.dumps(k) for k in notes.kinds)}]")
    if notes.quiet_hours is not None:
        lines.append(f"quiet_hours = {json.dumps(notes.quiet_hours)}")
    lines.append(f"backend = {json.dumps(notes.backend)}")
    lines.append(f"dedupe_window_s = {notes.dedupe_window_s}")

    for repo in cfg.repos:
        lines.append("")
        lines.append("[[repos]]")
        lines.append(f"name = {json.dumps(repo.name)}")
    for model, price in cfg.pricing.items():
        lines.append("")
        lines.append(f"[pricing.{json.dumps(model)}]")
        lines.append(f"input_per_mtok = {price.input_per_mtok!r}")
        lines.append(f"output_per_mtok = {price.output_per_mtok!r}")
    lines.append("")

    # write securely 0o600
    p.touch(mode=0o600)
    # Set permissions in case the file already existed with wider permissions
    p.chmod(0o600)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_concurrency: int = Field(default=1, ge=1)
    model: str | None = None


def _typed_agent_entries(value: Any) -> Any:
    """Parse the ``copilot`` entry with its own config class, which has fields that ``AgentConfig`` forbids."""
    if isinstance(value, dict) and isinstance(value.get("copilot"), dict):
        from harness.adapters.copilot import CopilotAgentConfig  # local import: copilot imports this module

        return {**value, "copilot": CopilotAgentConfig(**value["copilot"])}
    return value


class EscalateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labels: list[str] = Field(default_factory=lambda: ["needs-discussion"])
    failures: int = Field(default=2, ge=1)


class MergeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["squash", "merge", "rebase"] = "squash"
    delete_branch: bool = True


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    control_issue: int | None = None
    default_playbook: str = "plan-build-review"
    default_agent: str = "jules"
    base_branch: str | None = None
    context_files: list[str] = Field(default_factory=list)
    agents: Annotated[dict[str, SerializeAsAny[AgentConfig]], BeforeValidator(_typed_agent_entries)] = Field(
        default_factory=lambda: {"jules": AgentConfig()}
    )
    protected_paths: list[str] = Field(default_factory=lambda: [".harness/", ".claude/", ".orchestrator/"])
    escalate: EscalateConfig = Field(default_factory=EscalateConfig)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    command_users: list[str] = Field(default_factory=list)


def parse_repo_config(text: str | None) -> RepoConfig:
    if not text or not text.strip():
        return RepoConfig()
    try:
        data = yaml.safe_load(text)
        if data is None:
            return RepoConfig()
        if not isinstance(data, dict):
            raise ValueError("repo config must be a mapping")
        return RepoConfig(**data)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
