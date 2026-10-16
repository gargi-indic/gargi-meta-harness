"""Builds the redacted Markdown brief handed to a coding agent."""

from harness.config import RepoConfig
from harness.secrets import redact
from harness.store.repo import IssueRow

FILE_CAP = 20_000
TOTAL_CAP = 60_000
TRUNCATED = "\n\n[truncated]"


def build_brief(
    issue: IssueRow,
    parent_epic: IssueRow | None,
    repo_config: RepoConfig,
    context_files_text: dict[str, str],
    playbook_prompt: str | None,
) -> str:
    parts = [f"# Task: #{issue.number} {issue.title}"]
    if issue.body:
        parts.append(issue.body)

    if parent_epic is not None:
        parts.append(f"## Parent epic: #{parent_epic.number} {parent_epic.title}")
        if parent_epic.body:
            parts.append(parent_epic.body)

    if context_files_text:
        parts.append("## Context files")
        for path, text in context_files_text.items():
            parts.append(f"### {path}")
            text = redact(text)
            parts.append(text[:FILE_CAP] + TRUNCATED if len(text) > FILE_CAP else text)

    if playbook_prompt:
        parts.append("## Playbook instructions")
        parts.append(playbook_prompt)

    rules = [
        "- Stay strictly within the issue's scope and its out-of-scope list.",
        f"- Title the PR `#{issue.number}: {issue.title}`.",
        f"- The PR body must start with `Closes #{issue.number}`.",
        "- Paste the raw output of lint and test commands in the PR body.",
        "- Do not add dependencies beyond what the issue requires.",
        "- Ask instead of guessing when the spec is unclear.",
    ]
    if repo_config.protected_paths:
        paths = ", ".join(f"`{p}`" for p in repo_config.protected_paths)
        rules.insert(1, f"- Do not modify these protected paths: {paths}.")
    rules_text = "## Rules\n\n" + "\n".join(rules)

    head = redact("\n\n".join(parts))
    budget = TOTAL_CAP - len(rules_text) - 2
    if len(head) > budget:
        head = head[: budget - len(TRUNCATED)] + TRUNCATED
    return redact(f"{head}\n\n{rules_text}")
