"""CLI: harness plan show / plan approve (M4.1). GitHub is the respx-backed World; config and database are temporary."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from harness import cli as cli_module
from harness import config as config_module
from harness.cli import app
from harness.config import GlobalConfig, RepoEntry, save_global
from harness.github.client import GitHub
from harness.planner.plan_gate import mirrored_plan_record
from harness.store.db import Store
from harness.store.repo import upsert_repo
from tests.planner.world import CONTROL, PLAN_PATH, PLAN_TEXT, REPO, T_LATEST, World

runner = CliRunner()


@pytest.fixture
def world(respx_mock) -> World:
    w = World(respx_mock).install()
    w.put_file()
    return w


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World) -> Path:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    cfg = GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name=REPO)])
    save_global(cfg)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(cli_module, "_make_github", lambda: GitHub("t", write_spacing=0.0, sleep=no_sleep))
    return cfg.db_path


def seed_repo(db: Path, **config: object) -> None:
    store = Store(db)
    store.migrate()
    upsert_repo(store, REPO, "main", {"control_issue": CONTROL, **config}, "2026-03-01T00:00:00Z")
    store.close()


def invoke(*args: str):
    return runner.invoke(app, ["plan", *args])


def label_posts(world: World) -> int:
    return world.count("POST", f"/issues/{CONTROL}/labels")


def test_show_prints_the_plan_and_that_it_is_pending(db: Path, world: World) -> None:
    seed_repo(db)
    result = invoke("show", "--path", PLAN_PATH)
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "title:    The plan" in out and world.files[PLAN_PATH].sha in out
    assert "  - Goals" in out and "approval: PENDING" in out


def test_show_without_a_control_issue_says_so(db: Path) -> None:
    seed_repo(db, control_issue=None)
    result = invoke("show", "--path", PLAN_PATH)
    assert result.exit_code == 0 and "no control issue" in result.stdout


def test_a_source_is_required(db: Path) -> None:
    seed_repo(db)
    for args in (["show"], ["approve"], ["show", "--path", PLAN_PATH, "--issue", "3"]):
        result = invoke(*args)
        assert result.exit_code == 1 and "exactly one of --path or --issue" in result.stdout


def test_approve_adds_the_label_through_the_outbox_and_records_the_approval(db: Path, world: World) -> None:
    seed_repo(db)
    result = invoke("approve", "--path", PLAN_PATH)
    assert result.exit_code == 0, result.output
    assert "approved docs/PLAN.md" in result.stdout and "@o" in result.stdout
    assert "harness:plan-approved" in world.labels and label_posts(world) == 1
    record = world.ledger()["plan"]  # type: ignore[index]
    assert (record["plan_sha"], record["approved_by"], record["via"]) == (
        world.files[PLAN_PATH].sha,
        "o",
        "cli",
    )
    store = Store(db)
    assert mirrored_plan_record(store, REPO, CONTROL) == record
    store.close()
    assert "approval: APPROVED by @o via cli" in invoke("show", "--path", PLAN_PATH).stdout


def test_approve_by_an_unauthorised_user_is_refused_with_a_clear_error(db: Path, world: World) -> None:
    seed_repo(db)
    world.user = "mallory"
    result = invoke("approve", "--path", PLAN_PATH)
    assert result.exit_code == 1
    assert "@mallory is not allowed to approve plans for o/r" in result.stdout
    assert label_posts(world) == 0 and world.ledger() is None


def test_an_allowlisted_user_may_approve(db: Path, world: World) -> None:
    seed_repo(db, command_users=["alice"])
    world.user = world.actor = "alice"
    result = invoke("approve", "--path", PLAN_PATH)
    assert result.exit_code == 0, result.output
    assert world.ledger()["plan"]["approved_by"] == "alice"  # type: ignore[index]


def test_approving_twice_writes_nothing_the_second_time(db: Path, world: World) -> None:
    seed_repo(db)
    assert invoke("approve", "--path", PLAN_PATH).exit_code == 0
    posts, calls = label_posts(world), len(world.calls)
    result = invoke("approve", "--path", PLAN_PATH)
    assert result.exit_code == 0 and "already approved" in result.stdout
    assert label_posts(world) == posts
    assert not [c for c in world.calls[calls:] if c[0] in ("POST", "PATCH", "DELETE")]


def test_after_the_plan_changes_the_cli_approval_is_stale_until_approved_again(
    db: Path, world: World
) -> None:
    seed_repo(db)
    assert invoke("approve", "--path", PLAN_PATH).exit_code == 0
    world.put_file(text=PLAN_TEXT + "\n## Changed\n", version=2, date=T_LATEST)
    assert "approval: STALE" in invoke("show", "--path", PLAN_PATH).stdout
    again = invoke("approve", "--path", PLAN_PATH)
    assert again.exit_code == 0, again.output
    assert world.ledger()["plan"]["plan_sha"] == world.files[PLAN_PATH].sha  # type: ignore[index]
    assert "approval: APPROVED" in invoke("show", "--path", PLAN_PATH).stdout


def test_an_issue_plan_can_be_approved(db: Path, world: World) -> None:
    seed_repo(db)
    world.put_issue(12, "Campaign", "# Plan\n")
    result = invoke("approve", "--issue", "12")
    assert result.exit_code == 0, result.output
    record = world.ledger()["plan"]  # type: ignore[index]
    assert record["kind"] == "issue" and record["issue"] == 12 and record["path"] is None


def test_a_missing_plan_is_a_clean_error(db: Path) -> None:
    seed_repo(db)
    result = invoke("approve", "--path", "docs/NOPE.md")
    assert result.exit_code == 1 and "not found" in result.stdout


def test_approve_needs_a_control_issue(db: Path) -> None:
    seed_repo(db, control_issue=None)
    result = invoke("approve", "--path", PLAN_PATH)
    assert result.exit_code == 1 and "no control issue" in result.stdout
    assert invoke("approve", "--path", PLAN_PATH, "--control-issue", str(CONTROL)).exit_code == 0
