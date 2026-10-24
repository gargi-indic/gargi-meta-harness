"""Every built-in playbook validates against the real adapter capability tables."""

from pathlib import Path

import pytest

from harness.adapters.base import AdapterRegistry
from harness.adapters.claude import ClaudeAdapter, ClaudeAgentConfig
from harness.adapters.codex import CodexAdapter, CodexAgentConfig
from harness.adapters.fake import FakeAdapter
from harness.playbook.load import builtin_playbooks, load_file
from harness.playbook.validate import validate

PLAYBOOK_DIR = Path(__file__).resolve().parents[2] / "harness" / "playbooks"
FILES = sorted(PLAYBOOK_DIR.glob("*.yaml"))


def _caps() -> dict[str, set[str]]:
    registry = AdapterRegistry()
    registry.register(FakeAdapter())
    # Only the capability tables are read, so the workspace manager and GitHub client are not needed.
    registry.register(ClaudeAdapter(workspaces=None, gh=None, config=ClaudeAgentConfig()))  # type: ignore[arg-type]
    registry.register(CodexAdapter(workspaces=None, gh=None, config=CodexAgentConfig()))  # type: ignore[arg-type]
    return registry.caps_by_name()


def test_the_builtin_playbooks_are_all_found():
    assert {p.stem for p in FILES} >= {"plan-build-review", "tournament"}
    assert set(builtin_playbooks()) == {p.stem for p in FILES}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_playbook_validates_with_the_real_capability_tables(path):
    assert validate(load_file(path), agent_caps=_caps()) == []
