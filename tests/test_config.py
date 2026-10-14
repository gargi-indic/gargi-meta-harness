from pathlib import Path

import pytest

from harness.config import (
    GlobalConfig,
    RepoConfig,
    RepoEntry,
    load_global,
    parse_repo_config,
    save_global,
)


def test_repo_entry_validation():
    with pytest.raises(ValueError):
        RepoEntry(name="invalidname")
    with pytest.raises(ValueError):
        RepoEntry(name="too/many/slashes")
    with pytest.raises(ValueError):
        RepoEntry(name="owner/")
    with pytest.raises(ValueError):
        RepoEntry(name="/repo")
    with pytest.raises(ValueError):
        RepoEntry(name=" / ")

    entry = RepoEntry(name="owner/repo")
    assert entry.name == "owner/repo"

    entry = RepoEntry(name="gargi-indic/gargi-meta-harness")
    assert entry.name == "gargi-indic/gargi-meta-harness"


def test_global_config_defaults():
    cfg = GlobalConfig()
    assert cfg.port == 7777
    assert str(cfg.data_dir) == str(Path.home() / ".gargi-harness")
    assert cfg.env_files == [
        Path.home() / ".config" / "jules" / "env",
        Path.home() / ".config" / "gargi-harness" / "env",
    ]
    assert cfg.repos == []


def test_global_config_properties():
    cfg = GlobalConfig(data_dir=Path("~/.test-gargi-harness"))
    assert cfg.data_path == Path.home() / ".test-gargi-harness"
    assert cfg.db_path == Path.home() / ".test-gargi-harness" / "harness.db"


def test_save_load_global_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = GlobalConfig(
        port=8888,
        data_dir=Path("~/custom-data-dir"),
        env_files=[Path("~/path with spaces/env"), Path('~/path_with_"quote"/env')],
        repos=[RepoEntry(name="o/r1"), RepoEntry(name="o/r2")],
    )

    saved_path = save_global(cfg, path)
    assert saved_path == path

    # Check permissions
    assert (path.stat().st_mode & 0o777) == 0o600

    loaded = load_global(path)
    assert loaded.port == cfg.port
    assert loaded.data_dir == cfg.data_dir
    assert loaded.env_files == cfg.env_files
    assert loaded.repos == cfg.repos


def test_global_config_extra_forbid():
    with pytest.raises(ValueError):
        GlobalConfig(unknown_field="value")


def test_repo_config_defaults():
    cfg = RepoConfig()
    assert cfg.control_issue is None
    assert cfg.default_playbook == "plan-build-review"
    assert cfg.default_agent == "jules"
    assert cfg.base_branch is None
    assert cfg.context_files == []
    assert list(cfg.agents.keys()) == ["jules"]
    assert cfg.agents["jules"].enabled is True
    assert cfg.agents["jules"].max_concurrency == 1
    assert cfg.agents["jules"].model is None
    assert cfg.protected_paths == [".harness/", ".claude/", ".orchestrator/"]
    assert cfg.escalate.labels == ["needs-discussion"]
    assert cfg.escalate.failures == 2
    assert cfg.merge.method == "squash"
    assert cfg.merge.delete_branch is True
    assert cfg.command_users == []


def test_escalate_config_failures():
    from harness.config import EscalateConfig

    with pytest.raises(ValueError):
        EscalateConfig(failures=0)

    cfg = EscalateConfig(failures=1)
    assert cfg.failures == 1


def test_parse_repo_config():
    yaml_text = """
control_issue: 42
default_playbook: custom
default_agent: claude
base_branch: main
context_files: ["docs/SPEC.md"]
agents:
  claude:
    enabled: true
    max_concurrency: 2
    model: claude-3-5-sonnet-20241022
protected_paths: [".github/"]
escalate:
  labels: ["help"]
  failures: 1
merge:
  method: merge
  delete_branch: false
command_users: ["alice"]
"""
    cfg = parse_repo_config(yaml_text)
    assert cfg.control_issue == 42
    assert cfg.default_playbook == "custom"
    assert cfg.default_agent == "claude"
    assert cfg.base_branch == "main"
    assert cfg.context_files == ["docs/SPEC.md"]
    assert "claude" in cfg.agents
    assert cfg.agents["claude"].enabled is True
    assert cfg.agents["claude"].max_concurrency == 2
    assert cfg.agents["claude"].model == "claude-3-5-sonnet-20241022"
    assert cfg.protected_paths == [".github/"]
    assert cfg.escalate.labels == ["help"]
    assert cfg.escalate.failures == 1
    assert cfg.merge.method == "merge"
    assert cfg.merge.delete_branch is False
    assert cfg.command_users == ["alice"]


def test_parse_repo_config_empty_and_invalid():
    # Empty config -> defaults
    assert parse_repo_config(None) == RepoConfig()
    assert parse_repo_config("") == RepoConfig()

    # Invalid YAML format
    with pytest.raises(ValueError):
        parse_repo_config("invalid: yaml: :")

    # Valid YAML but not a mapping
    with pytest.raises(ValueError):
        parse_repo_config("- list item")

    # Unknown key -> ValueError (extra="forbid")
    with pytest.raises(ValueError):
        parse_repo_config("unknown: field")
