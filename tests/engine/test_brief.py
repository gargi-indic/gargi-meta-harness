"""Tests for the agent brief builder."""

import pytest

from harness.config import RepoConfig
from harness.engine.brief import build_brief
from harness.store.repo import IssueRow


def make_issue(number: int, title: str, body: str | None = None) -> IssueRow:
    return IssueRow(
        repo="owner/repo",
        number=number,
        id=number * 100,
        title=title,
        body=body,
        state="open",
        labels=[],
        parent=None,
        blocked_by=[],
        updated_at="2023-01-01T00:00:00Z",
    )


def test_sections_appear_in_order():
    text = build_brief(
        issue=make_issue(1, "Test issue", "This is a test issue."),
        parent_epic=make_issue(2, "Parent epic", "This is the parent epic."),
        repo_config=RepoConfig(),
        context_files_text={"docs/README.md": "Context content."},
        playbook_prompt="Do the thing.",
    )

    markers = [
        "# Task: #1 Test issue\n\nThis is a test issue.",
        "## Parent epic: #2 Parent epic\n\nThis is the parent epic.",
        "## Context files\n\n### docs/README.md\n\nContext content.",
        "## Playbook instructions\n\nDo the thing.",
        "## Rules",
    ]
    positions = [text.index(m) for m in markers]
    assert positions == sorted(positions)


def test_optional_sections_are_omitted():
    text = build_brief(make_issue(1, "T"), None, RepoConfig(), {}, None)

    assert "## Parent epic" not in text
    assert "## Context files" not in text
    assert "## Playbook instructions" not in text
    assert "## Rules" in text


def test_rules_list_every_protected_path_and_pr_conventions():
    paths = [".harness/", ".claude/", ".orchestrator/", "docs/secret.md"]
    text = build_brief(make_issue(7, "Do it"), None, RepoConfig(protected_paths=paths), {}, None)

    rules = text.split("## Rules", 1)[1]
    for path in paths:
        assert f"`{path}`" in rules
    assert "Title the PR `#7: Do it`." in rules
    assert "The PR body must start with `Closes #7`." in rules
    assert "Stay strictly within the issue's scope" in rules
    assert "Paste the raw output of lint and test commands" in rules
    assert "Do not add dependencies" in rules
    assert "Ask instead of guessing" in rules


def test_rules_omit_protected_paths_line_when_none_configured():
    text = build_brief(make_issue(1, "T"), None, RepoConfig(protected_paths=[]), {}, None)

    assert "protected paths" not in text


def test_parent_epic_without_body_has_only_heading():
    text = build_brief(make_issue(1, "T"), make_issue(2, "Epic"), RepoConfig(), {}, None)

    assert "## Parent epic: #2 Epic\n\n## Rules" in text


def test_per_file_cap_truncates_each_file():
    files = {"big.txt": "A" * 25_000, "ok.txt": "B" * 20_000}
    text = build_brief(make_issue(1, "T"), None, RepoConfig(), files, None)

    assert "A" * 20_000 + "\n\n[truncated]" in text
    assert "A" * 20_001 not in text
    assert "B" * 20_000 in text
    assert text.count("[truncated]") == 1


def test_total_cap_truncates_and_keeps_rules():
    text = build_brief(make_issue(1, "T", "A" * 70_000), None, RepoConfig(), {}, None)

    assert len(text) <= 60_000
    assert "[truncated]\n\n## Rules" in text
    assert text.endswith("Ask instead of guessing when the spec is unclear.")


def test_total_cap_applies_to_many_context_files():
    files = {f"f{i}.txt": "C" * 20_000 for i in range(5)}
    text = build_brief(make_issue(1, "T"), None, RepoConfig(), files, None)

    assert len(text) <= 60_000
    assert "[truncated]" in text
    assert "## Rules" in text


def test_brief_under_caps_is_not_marked_truncated():
    text = build_brief(make_issue(1, "T", "short"), None, RepoConfig(), {"a": "b" * 100}, None)

    assert "[truncated]" not in text


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "JULES_API_KEY", "ANTHROPIC_API_KEY"])
def test_secrets_are_redacted_everywhere(monkeypatch, name):
    secret = "s3cr3t-value-0123456789"
    monkeypatch.setenv(name, secret)

    text = build_brief(
        issue=make_issue(1, f"Title {secret}", f"Body with {secret} inside"),
        parent_epic=make_issue(2, "Epic", f"Epic {secret}"),
        repo_config=RepoConfig(),
        context_files_text={"file.txt": f"File with {secret} inside"},
        playbook_prompt=f"Prompt {secret}",
    )

    assert secret not in text
    assert text.count("[redacted]") == 6  # the title also appears in the PR title rule


def test_secret_straddling_the_file_cap_is_still_redacted(monkeypatch):
    secret = "s3cr3t-value-0123456789"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    content = "A" * (20_000 - 5) + secret + "tail"

    text = build_brief(make_issue(1, "T"), None, RepoConfig(), {"f.txt": content}, None)

    assert secret not in text
    assert "s3cr3t" not in text
