from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from harness.cli import app
from harness.config import GlobalConfig, RepoConfig
from harness.store.db import Store
from harness.store.repo import IssueRow, upsert_issue, upsert_repo

runner = CliRunner()


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GARGI_HARNESS_CONFIG", str(home / ".config" / "gargi-harness"))
    return home


@pytest.fixture
def mock_global_config(temp_home):
    cfg = GlobalConfig(
        port=7777, data_dir=temp_home / ".gargi-harness", env_files=[temp_home / "env"], repos=[]
    )
    from harness.config import save_global

    save_global(cfg)
    return cfg


@patch("harness.cli._make_github")
@patch("harness.cli.ensure_harness_labels")
def test_init(mock_ensure, mock_make_gh, mock_global_config):
    mock_gh = MagicMock()

    async def mock_ensure_coro(*args, **kwargs):
        pass

    mock_ensure.side_effect = mock_ensure_coro
    mock_make_gh.return_value = mock_gh

    result = runner.invoke(app, ["init", "owner/repo"])
    assert result.exit_code == 0
    assert "default_playbook: plan-build-review" in result.stdout
    assert "token" not in result.stdout.lower()

    # Check config was saved
    from harness.config import load_global

    cfg = load_global()
    assert len(cfg.repos) == 1
    assert cfg.repos[0].name == "owner/repo"


def test_status_no_db(mock_global_config):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "not synced: run harness sync" in result.stdout


def test_status_seeded_db(mock_global_config):
    db_path = mock_global_config.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    store.migrate()

    upsert_repo(store, "owner/repo", "main", RepoConfig().model_dump())

    issue1 = IssueRow(
        repo="owner/repo",
        number=1,
        id=101,
        title="Epic",
        body="",
        state="open",
        labels=[],
        parent=None,
        blocked_by=[],
        updated_at="2023-01-01",
    )
    issue2 = IssueRow(
        repo="owner/repo",
        number=2,
        id=102,
        title="Sub",
        body="",
        state="open",
        labels=[],
        parent=1,
        blocked_by=[],
        updated_at="2023-01-01",
    )
    issue3 = IssueRow(
        repo="owner/repo",
        number=3,
        id=103,
        title="Blocked Sub",
        body="",
        state="open",
        labels=[],
        parent=1,
        blocked_by=[2],
        updated_at="2023-01-01",
    )

    upsert_issue(store, issue1)
    upsert_issue(store, issue2)
    upsert_issue(store, issue3)
    store.close()

    # Needs a repo in config
    mock_global_config.repos.append(type("obj", (object,), {"name": "owner/repo"}))
    from harness.config import save_global

    save_global(mock_global_config)

    result = runner.invoke(app, ["status", "owner/repo"])
    assert result.exit_code == 0
    assert "Epic" in result.stdout
    assert "EPIC" in result.stdout
    assert "READY" in result.stdout
    assert "BLOCKED" in result.stdout
    assert "ready: #2" in result.stdout
