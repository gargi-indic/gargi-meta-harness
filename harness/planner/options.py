"""What a proposal may name: the enabled agents with their capabilities, and the playbooks that exist.

Shared by the decompose executor and the planner API, so both validate against the same sets.
"""

from collections.abc import Mapping

from harness.adapters.base import Capabilities
from harness.config import AgentConfig
from harness.github.client import GitHub, GitHubError
from harness.playbook.load import PlaybookError, builtin_playbooks, load_playbook
from harness.playbook.models import Playbook

PLAYBOOK_DIR = ".harness/playbooks"
_YAML_SUFFIXES = (".yaml", ".yml")


def enabled_agents(
    caps: Mapping[str, Capabilities], configured: Mapping[str, AgentConfig]
) -> dict[str, Capabilities]:
    """``caps`` (agent name to capabilities) minus the agents that the repo config switches off."""
    return {name: found for name, found in caps.items() if name not in configured or configured[name].enabled}


async def repo_playbooks(gh: GitHub, repo: str) -> dict[str, Playbook]:
    """The built-in playbooks plus the valid ones in the repo's ``.harness/playbooks`` (those win by id)."""
    found = dict(builtin_playbooks())
    try:
        entries = await gh.get(f"repos/{repo}/contents/{PLAYBOOK_DIR}")
    except GitHubError as exc:
        if exc.status == 404:
            return found
        raise
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        if entry.get("type") != "file" or not path.endswith(_YAML_SUFFIXES):
            continue
        text = await gh.file_text(repo, path)
        if text is None:
            continue
        try:
            playbook = load_playbook(text)
        except PlaybookError:
            continue  # a broken repo playbook cannot be chosen
        found[playbook.id] = playbook
    return found
