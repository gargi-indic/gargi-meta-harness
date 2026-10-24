"""CLI: harness plan create (M4.4). GitHub is the respx-backed PlannerWorld; config and database are temporary."""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from harness import cli as cli_module
from harness import config as config_module
from harness.cli import app
from harness.config import GlobalConfig, RepoEntry, save_global
from harness.github.client import GitHub
from harness.github.control_table import parse_table
from harness.planner.materialize import plan_run_key
from harness.planner.proposal import ProposedIssue, ProposedPlan
from harness.store.db import Store
from harness.store.repo import upsert_repo
from tests.planner.gh_fake import CONTROL, REPO, PlannerWorld

runner = CliRunner()


def issue(key: str, *deps: str, **over: Any) -> ProposedIssue:
    data: dict[str, Any] = {
        "key": key,
        "title": f"Do {key}",
        "body": f"Why {key}.",
        "acceptance": [f"{key} works"],
        "blocked_by": list(deps),
        "agent": "jules",
        "playbook": "plan-build-review",
    }
    data.update(over)
    return ProposedIssue(**data)


def plan_file(tmp_path: Path, *issues: ProposedIssue) -> Path:
    plan = ProposedPlan(epic_title="The epic", epic_body="Body.", issues=list(issues))
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    return path


@pytest.fixture
def world(respx_mock) -> PlannerWorld:
    return PlannerWorld(respx_mock).install()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: PlannerWorld) -> Path:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    cfg = GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name=REPO)])
    save_global(cfg)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(cli_module, "_make_github", lambda: GitHub("t", write_spacing=0.0, sleep=no_sleep))
    store = Store(cfg.db_path)
    store.migrate()
    upsert_repo(store, REPO, "main", {"control_issue": CONTROL}, "2026-03-01T00:00:00Z")
    store.close()
    return cfg.db_path


def create(*args: str):
    return runner.invoke(app, ["plan", "create", *args])


def test_create_makes_the_epic_the_issues_the_links_and_the_control_rows(
    db: Path, world: PlannerWorld, tmp_path: Path
) -> None:
    path = plan_file(tmp_path, issue("a"), issue("b", "a"))
    result = create("--proposal", str(path))
    assert result.exit_code == 0, result.output

    out = result.stdout
    assert "epic:     #2" in out and "created:   a, b" in out and "links:    3 created" in out
    assert "  a -> #3" in out and "  b -> #4" in out
    assert [i["title"] for i in world.created()] == ["The epic", "Do a", "Do b"]
    assert len(world.sub_issues[2]) == 2 and world.blocked_by[4] == [world.by_title("Do a")["id"]]
    assert {r.issue for r in parse_table(world.issues[CONTROL]["body"])} == {3, 4}
    assert len(world.comments[CONTROL]) == 1


def test_repeating_it_creates_nothing_twice(db: Path, world: PlannerWorld, tmp_path: Path) -> None:
    path = plan_file(tmp_path, issue("a"), issue("b", "a"))
    assert create("--proposal", str(path)).exit_code == 0
    writes = len(world.writes)
    result = create("--proposal", str(path))
    assert result.exit_code == 0, result.output
    assert (
        len(world.writes) == writes and "created:   -" in result.stdout and "reused:   a, b" in result.stdout
    )


def test_the_default_run_key_comes_from_the_plan_and_run_key_overrides_it(
    db: Path, world: PlannerWorld, tmp_path: Path
) -> None:
    path = plan_file(tmp_path, issue("a"))
    plan = ProposedPlan.model_validate_json(path.read_text())
    assert f"run:      {plan_run_key(plan)}" in create("--proposal", str(path), "--dry-run").stdout
    assert "run:      mine" in create("--proposal", str(path), "--dry-run", "--run-key", "mine").stdout
    assert create("--proposal", str(path), "--run-key", "bad key").exit_code == 1


def test_dry_run_makes_no_write_call(db: Path, world: PlannerWorld, tmp_path: Path) -> None:
    path = plan_file(tmp_path, issue("a"), issue("b", "a"))
    result = create("--proposal", str(path), "--dry-run")
    assert result.exit_code == 0, result.output
    assert world.writes == [] and len(world.issues) == 1
    assert "dry run, nothing written" in result.stdout and "epic:     (not created yet)" in result.stdout
    assert "would create:   a, b" in result.stdout and "links:    3 to create" in result.stdout

    store = Store(db)
    assert store.query("SELECT COUNT(*) AS n FROM outbox")[0]["n"] == 0
    store.close()


def test_repo_and_control_issue_options(db: Path, world: PlannerWorld, tmp_path: Path) -> None:
    path = plan_file(tmp_path, issue("a"))
    result = create("--proposal", str(path), "--repo", REPO, "--control-issue", "1")
    assert result.exit_code == 0, result.output
    # the control issue is optional: without it only the issues are made
    other = plan_file(tmp_path, issue("z"))
    store = Store(db)
    upsert_repo(store, REPO, "main", {}, "2026-03-01T00:00:00Z")
    store.close()
    comments = len(world.comments.get(CONTROL, []))
    assert create("--proposal", str(other)).exit_code == 0
    assert len(world.comments.get(CONTROL, [])) == comments


def test_a_file_that_is_not_a_plan_is_refused(db: Path, world: PlannerWorld, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"epic_title": "x"}', encoding="utf-8")
    result = create("--proposal", str(bad))
    assert result.exit_code == 1 and "not a valid proposed plan" in result.stdout

    missing = create("--proposal", str(tmp_path / "nope.json"))
    assert missing.exit_code == 1 and "cannot read" in missing.stdout
    assert world.writes == []


def test_an_invalid_plan_is_refused_without_writes(db: Path, world: PlannerWorld, tmp_path: Path) -> None:
    path = plan_file(tmp_path, issue("a", "b"), issue("b", "a"))
    result = create("--proposal", str(path))
    assert result.exit_code == 1 and "cycle" in result.stdout and world.writes == []


def test_agents_come_from_the_repo_config(db: Path, world: PlannerWorld, tmp_path: Path) -> None:
    path = plan_file(tmp_path, issue("a", agent="codex"))  # only jules is enabled by default
    result = create("--proposal", str(path))
    assert result.exit_code == 1 and "unknown_agent" in result.stdout and world.writes == []


def test_a_playbook_of_the_repo_is_known_a_missing_one_is_not(
    db: Path, world: PlannerWorld, tmp_path: Path
) -> None:
    path = plan_file(tmp_path, issue("a", playbook="custom"))
    refused = create("--proposal", str(path))
    assert refused.exit_code == 1 and "unknown_playbook" in refused.stdout and world.writes == []

    world.files[".harness/playbooks/custom.yaml"] = "id: custom\n"
    accepted = create("--proposal", str(path))
    assert accepted.exit_code == 0, accepted.output
    assert "playbook:custom" in world.labels
