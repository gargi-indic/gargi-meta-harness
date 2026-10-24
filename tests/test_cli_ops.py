"""``harness kill``, ``resume`` (no run), ``budget``: they only queue (a daemon drains the outbox)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harness import config as config_module
from harness.cli import app
from harness.config import BudgetConfig, GlobalConfig, RepoEntry, save_global
from harness.ops.halt import HaltState, control_issues_of
from harness.store.db import Store
from harness.store.repo import RunRow, get_run, insert_run, upsert_repo

cli = CliRunner()


def make_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, control: bool = True, daily: float | None = None
):
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    cfg = GlobalConfig(
        data_dir=tmp_path / "data",
        env_files=[],
        repos=[RepoEntry(name="o/r")],
        budget=BudgetConfig(daily_usd=daily),
    )
    save_global(cfg)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    store.migrate()
    upsert_repo(store, "o/r", "main", {"control_issue": 1} if control else {})
    for number, status in ((5, "running"), (6, "done")):
        insert_run(
            store,
            RunRow(
                id=f"o/r#{number}@1",
                repo="o/r",
                issue=number,
                playbook_id="p",
                playbook_sha=None,
                status=status,
                context={},
                ledger_comment_id=None,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            ),
        )
    return cfg, store


def halt_of(cfg: GlobalConfig):
    store = Store(cfg.db_path)
    try:
        return HaltState(store, None, lambda: control_issues_of(store, cfg)).halted()  # type: ignore[arg-type]
    finally:
        store.close()


def test_kill_asks_for_confirmation(tmp_path, monkeypatch) -> None:
    cfg, store = make_home(tmp_path, monkeypatch)
    result = cli.invoke(app, ["kill"], input="no\n")
    assert result.exit_code == 1 and "aborted" in result.output
    assert get_run(store, "o/r#5@1").status == "running"  # type: ignore[union-attr]
    assert halt_of(cfg) is None


def test_kill_yes_halts_cancels_and_queues_the_ledger_writes(tmp_path, monkeypatch) -> None:
    cfg, store = make_home(tmp_path, monkeypatch)
    result = cli.invoke(app, ["kill", "--yes"])
    assert result.exit_code == 0, result.output
    assert "halted (kill_switch)" in result.output
    store.close()
    store = Store(cfg.db_path)
    assert get_run(store, "o/r#5@1").status == "cancelled"  # type: ignore[union-attr]
    assert get_run(store, "o/r#6@1").status == "done"  # type: ignore[union-attr]
    ops = [
        json.loads(r["payload_json"]) for r in store.query("SELECT * FROM outbox WHERE op = 'merge_ledger'")
    ]
    assert [(o["repo"], o["issue"], o["key"]) for o in ops] == [("o/r", 1, "ops")]
    assert ops[0]["value"]["halt"]["by"] == "cli"
    assert len(store.query("SELECT * FROM events WHERE kind = 'ops_halted'")) == 1
    assert halt_of(cfg).reason == "kill_switch"  # type: ignore[union-attr]
    # idempotent: nothing is left to cancel and the halt is set, so nothing more is queued
    assert cli.invoke(app, ["kill", "--yes"]).exit_code == 0
    store2 = Store(cfg.db_path)
    assert len(store2.query("SELECT * FROM events WHERE kind = 'ops_halted'")) == 1
    store2.close()


def test_resume_without_a_run_lifts_the_halt(tmp_path, monkeypatch) -> None:
    cfg, _ = make_home(tmp_path, monkeypatch)
    cli.invoke(app, ["kill", "--yes"])
    result = cli.invoke(app, ["resume"])
    assert result.exit_code == 0, result.output
    assert "resumed (was halted: kill_switch)" in result.output
    assert halt_of(cfg) is None
    assert "not halted" in cli.invoke(app, ["resume"]).output


def test_resume_to_needs_a_run(tmp_path, monkeypatch) -> None:
    make_home(tmp_path, monkeypatch)
    result = cli.invoke(app, ["resume", "--to", "plan"])
    assert result.exit_code == 1 and "needs a run" in result.output


def test_budget_shows_sets_and_clears_the_override(tmp_path, monkeypatch) -> None:
    cfg, _ = make_home(tmp_path, monkeypatch, daily=25.0)
    shown = cli.invoke(app, ["budget"])
    assert shown.exit_code == 0 and "limit: $25.00" in shown.output and "halted: no" in shown.output
    assert "limit: $40.00" in cli.invoke(app, ["budget", "--set", "40"]).output
    store = Store(cfg.db_path)
    ops = [
        json.loads(r["payload_json"]) for r in store.query("SELECT * FROM outbox WHERE op = 'merge_ledger'")
    ]
    assert ops[-1]["value"]["daily_usd"] == 40.0
    store.close()
    assert "limit: $25.00" in cli.invoke(app, ["budget", "--clear"]).output  # the config applies again


def test_budget_rejects_bad_input(tmp_path, monkeypatch) -> None:
    make_home(tmp_path, monkeypatch)
    assert cli.invoke(app, ["budget", "--set", "-1"]).exit_code != 0
    both = cli.invoke(app, ["budget", "--set", "1", "--clear"])
    assert both.exit_code == 1 and "either" in both.output


def test_without_a_control_issue_nothing_is_written(tmp_path, monkeypatch) -> None:
    cfg, store = make_home(tmp_path, monkeypatch, control=False)
    result = cli.invoke(app, ["kill", "--yes"])
    assert result.exit_code == 1 and "control issue" in result.output
    assert get_run(store, "o/r#5@1").status == "running"  # type: ignore[union-attr]
    assert cli.invoke(app, ["budget", "--set", "3"]).exit_code == 1
