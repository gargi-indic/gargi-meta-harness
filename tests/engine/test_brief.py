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


def test_build_brief_structure():
    issue = make_issue(1, "Test issue", "This is a test issue.")
    parent = make_issue(2, "Parent epic", "This is the parent epic.")
    repo_config = RepoConfig(control_issue=0, protected_paths=[".harness/", "docs/"])

    text = build_brief(
        issue=issue,
        parent_epic=parent,
        repo_config=repo_config,
        context_files_text={"docs/README.md": "Context content."},
        playbook_prompt="Do the thing.",
    )

    assert "# Task: #1 Test issue" in text
    assert "This is a test issue." in text
    assert "## Parent epic: #2 Parent epic" in text
    assert "This is the parent epic." in text
    assert "## Context files\n\n### docs/README.md\n\nContext content." in text
    assert "## Playbook instructions\n\nDo the thing." in text
    assert "## Rules" in text
    assert "- Stay strictly within the issue's scope" in text
    assert "- Do not modify these protected paths: `.harness/`, `docs/`." in text
    assert "- Title the PR `#1: Test issue`." in text
    assert "- The PR body must start with `Closes #1`." in text


def test_build_brief_redaction(monkeypatch):
    monkeypatch.setenv("JULES_API_KEY", "super-secret-jules-key")
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-github-token")

    # Reload secrets to pick up the env vars
    from importlib import reload

    import harness.secrets

    reload(harness.secrets)

    from harness.engine.brief import build_brief

    issue = make_issue(
        1, "Test with super-secret-jules-key inside", "Body with super-secret-github-token inside"
    )
    repo_config = RepoConfig(control_issue=0, protected_paths=[])

    text = build_brief(
        issue=issue,
        parent_epic=None,
        repo_config=repo_config,
        context_files_text={"file.txt": "File with super-secret-jules-key inside"},
        playbook_prompt=None,
    )

    assert "super-secret" not in text
    assert "[redacted]" in text


def test_build_brief_truncation():
    issue = make_issue(1, "Test", "A" * 70000)
    repo_config = RepoConfig(control_issue=0, protected_paths=[])
    text = build_brief(issue, None, repo_config, {}, None)
    assert len(text) == 60000 + len("\n\n[truncated]")
    assert text.endswith("[truncated]")

    issue.body = "Small body"
    text = build_brief(issue, None, repo_config, {"large.txt": "A" * 30000}, None)
    assert len(text) < 60000
    assert "A" * 20000 + "\n\n[truncated]" in text
