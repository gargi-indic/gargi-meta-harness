import json
import os
import tomllib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = 7777
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".gargi-harness")
    env_files: list[Path] = Field(
        default_factory=lambda: [Path.home() / ".config" / "jules" / "env", CONFIG_HOME / "env"]
    )
    repos: list[RepoEntry] = Field(default_factory=list)

    @property
    def data_path(self) -> Path:
        return self.data_dir.expanduser()

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

    for repo in cfg.repos:
        lines.append("")
        lines.append("[[repos]]")
        lines.append(f"name = {json.dumps(repo.name)}")
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
    agents: dict[str, AgentConfig] = Field(default_factory=lambda: {"jules": AgentConfig()})
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
